#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# This file is a part of the vllm-ascend project.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from typing import Optional, Tuple

import torch
import torch.distributed as dist
from torch import nn
from torch.nn.parameter import Parameter
from vllm.distributed import divide, tensor_model_parallel_all_reduce
from vllm.distributed.parallel_state import get_tp_group
from vllm.model_executor.layers.logits_processor import LogitsProcessor
from vllm.model_executor.layers.quantization.base_config import (
    QuantizationConfig, QuantizeMethodBase, method_has_implemented_embedding)
from vllm.model_executor.layers.vocab_parallel_embedding import (
    DEFAULT_VOCAB_PADDING_SIZE, ParallelLMHead, UnquantizedEmbeddingMethod,
    VocabParallelEmbedding, pad_vocab_size)
from vllm.model_executor.utils import set_weight_attrs

from vllm_ascend.distributed.parallel_state import get_lmhead_tp_group
from vllm_ascend.utils import lmhead_tp_enable
from vllm.distributed.parallel_state import get_dp_group
from vllm.forward_context import get_forward_context

def get_masked_input_and_mask(
        input_: torch.Tensor, org_vocab_start_index: int,
        org_vocab_end_index: int, num_org_vocab_padding: int,
        added_vocab_start_index: int,
        added_vocab_end_index: int) -> Tuple[torch.Tensor, torch.Tensor]:
    # torch.compile will fuse all of the pointwise ops below
    # into a single kernel, making it very fast
    org_vocab_mask = (input_ >= org_vocab_start_index) & (
        input_ < org_vocab_end_index)
    # Adapt: avoid create added_vocab_mask when added_vocab_start_index == added_vocab_end_index.
    if added_vocab_start_index == added_vocab_end_index:
        valid_offset = (org_vocab_start_index * org_vocab_mask)
        vocab_mask = org_vocab_mask
    else:
        added_vocab_mask = (input_ >= added_vocab_start_index) & (
            input_ < added_vocab_end_index)
        added_offset = added_vocab_start_index - (
            org_vocab_end_index -
            org_vocab_start_index) - num_org_vocab_padding
        valid_offset = (org_vocab_start_index *
                        org_vocab_mask) + (added_offset * added_vocab_mask)
        vocab_mask = org_vocab_mask | added_vocab_mask
    # Adapt end.
    input_ = vocab_mask * (input_ - valid_offset)
    return input_, ~vocab_mask



class AscendVocabParallelEmbedding(VocabParallelEmbedding):
    """
    Register VocabParallelEmbedding as a custom op for Ascend.
    AscendVocabParallelEmbedding support different communication parallel groups
    Added the feature of lmheadTP in pure dp scenario
    """

    def __init__(self,
                 num_embeddings: int,
                 embedding_dim: int,
                 params_dtype: Optional[torch.dtype] = None,
                 org_num_embeddings: Optional[int] = None,
                 padding_size: int = DEFAULT_VOCAB_PADDING_SIZE,
                 quant_config: Optional[QuantizationConfig] = None,
                 prefix: str = ""):
        nn.Module.__init__(self)

        if lmhead_tp_enable() and prefix.find("lm_head") != -1:
            self.comm_group = get_lmhead_tp_group()
        elif lmhead_tp_enable() and prefix.find("embed_tokens") != -1:
            self.comm_group = get_lmhead_tp_group()
        else:
            self.comm_group = get_tp_group()

        self.tp_size = self.comm_group.world_size
        self.tp_rank = self.comm_group.rank_in_group

        self.num_embeddings = num_embeddings
        self.padding_size = padding_size
        self.org_vocab_size = org_num_embeddings or num_embeddings
        num_added_embeddings = num_embeddings - self.org_vocab_size
        self.org_vocab_size_padded = pad_vocab_size(self.org_vocab_size,
                                                    self.padding_size)
        self.num_embeddings_padded = pad_vocab_size(
            self.org_vocab_size_padded + num_added_embeddings,
            self.padding_size)
        assert self.org_vocab_size_padded <= self.num_embeddings_padded

        self.shard_indices = self._get_indices(self.num_embeddings_padded,
                                               self.org_vocab_size_padded,
                                               self.num_embeddings,
                                               self.org_vocab_size,
                                               self.tp_rank, self.tp_size)
        self.embedding_dim = embedding_dim
        quant_method = None
        if quant_config is not None:
            quant_method = quant_config.get_quant_method(self, prefix=prefix)
        if quant_method is None:
            quant_method = UnquantizedEmbeddingMethod()

        # If we are making an embedding layer, then our quantization linear
        # method must implement the embedding operation. If we are another
        # layer type like ParallelLMHead, this is not important.
        is_embedding_layer = type(self) is VocabParallelEmbedding
        quant_method_implements_embedding = method_has_implemented_embedding(
            type(quant_method))
        if is_embedding_layer and not quant_method_implements_embedding:
            raise NotImplementedError(
                f"The class {type(quant_method).__name__} must implement "
                "the 'embedding' method, see UnquantizedEmbeddingMethod.")

        self.quant_method: QuantizeMethodBase = quant_method

        if params_dtype is None:
            params_dtype = torch.get_default_dtype()
        # Divide the weight matrix along the vocaburaly dimension.
        self.num_added_embeddings = self.num_embeddings - self.org_vocab_size
        self.num_embeddings_per_partition = divide(self.num_embeddings_padded,
                                                   self.tp_size)
        assert (self.shard_indices.num_elements_padded ==
                self.num_embeddings_per_partition)
        self.num_org_embeddings_per_partition = (
            self.shard_indices.org_vocab_end_index -
            self.shard_indices.org_vocab_start_index)
        self.num_added_embeddings_per_partition = (
            self.shard_indices.added_vocab_end_index -
            self.shard_indices.added_vocab_start_index)

        self.quant_method.create_weights(self,
                                         self.embedding_dim,
                                         [self.num_embeddings_per_partition],
                                         self.embedding_dim,
                                         self.num_embeddings_padded,
                                         params_dtype=params_dtype,
                                         weight_loader=self.weight_loader)
    
    def forward(self, input_):
        if self.tp_size > 1:
            return self._forward_embed_tp(input_)
        else:
            return self._forward_normal(input_)
        
    def _forward_embed_tp(self, input_):
        cu_tokens_across_dp_cpu = get_forward_context().dp_metadata.cu_tokens_across_dp_cpu
        
        global_dp_batch_size = torch.diff(cu_tokens_across_dp_cpu, prepend=cu_tokens_across_dp_cpu.new_zeros(1))
        print(f"zzh-debug input_: {input_.shape} \n global_dp_batch_size: {global_dp_batch_size}\n ")
        lmhead_group_batch_size = [global_dp_batch_size[x] for x in get_lmhead_tp_group().ranks]
        local_batch_size = input_.size(0)
        gathered_input = [torch.empty(batch_size, dtype=input_.dtype, device='npu') for batch_size in lmhead_group_batch_size]
        torch.distributed.all_gather(
            gathered_input, input_, group=get_lmhead_tp_group().device_group)
        complete_input = torch.cat(gathered_input, dim=0)
        print(f"all_gather_down complete_input: {complete_input.shape}")

        output = self.quant_method.embedding(self, complete_input.long())
        input_splits = lmhead_group_batch_size
        output_splits = [local_batch_size *
                         self.num_embeddings_per_partition for _ in lmhead_group_batch_size]
        all_to_all_result = torch.empty(
            local_batch_size * (self.num_embeddings_per_partition * self.tp_size),
            dtype=output.dtype, device='npu')
        print(f"befor all_to_all_single output: {output.shape}")
        dist.all_to_all_single(
            all_to_all_result,
            output,
            output_splits,
            input_splits,
            group=get_lmhead_tp_group().device_group)
        print(f"after all_to_all_single all_to_all_result: {all_to_all_result.shape}")
        reshaped = all_to_all_result.view(self.tp_size, local_batch_size, self.num_embeddings_per_partition)
        output = reshaped.permute(1, 0, 2).reshape(local_batch_size, -1)
        return output

    def _forward_normal(self, input_):
        
        if self.tp_size > 1:
            # Build the mask.
            masked_input, input_mask = get_masked_input_and_mask(
                input_, self.shard_indices.org_vocab_start_index,
                self.shard_indices.org_vocab_end_index,
                self.shard_indices.num_org_vocab_padding,
                self.shard_indices.added_vocab_start_index,
                self.shard_indices.added_vocab_end_index)
        else:
            masked_input = input_
        # Get the embeddings.
        output_parallel = self.quant_method.embedding(self, masked_input.long())
        # Mask the output embedding.
        if self.tp_size > 1:
            output_parallel.masked_fill_(input_mask.unsqueeze(-1), 0)
        # Reduce across all the model parallel GPUs.
        output = tensor_model_parallel_all_reduce(output_parallel)

        print(f"rank:{get_dp_group().rank_in_group}  forward_normal output:{output.shape}, input:{input_.shape}")
        return output



class AscendParallelLMHead(ParallelLMHead):
    """
    Register ParallelLMHead as a custom op for Ascend."""

    def __init__(self,
                 num_embeddings: int,
                 embedding_dim: int,
                 bias: bool = False,
                 params_dtype: Optional[torch.dtype] = None,
                 org_num_embeddings: Optional[int] = None,
                 padding_size: int = DEFAULT_VOCAB_PADDING_SIZE,
                 quant_config: Optional[QuantizationConfig] = None,
                 prefix: str = ""):
        AscendVocabParallelEmbedding.__init__(self, num_embeddings,
                                              embedding_dim, params_dtype,
                                              org_num_embeddings, padding_size,
                                              quant_config, prefix)

        self.quant_config = quant_config
        if bias:
            self.bias = Parameter(
                torch.empty(self.num_embeddings_per_partition,
                            dtype=params_dtype))
            set_weight_attrs(self.bias, {
                "output_dim": 0,
                "weight_loader": self.weight_loader,
            })
        else:
            self.register_parameter("bias", None)


class AscendLogitsProcessor(LogitsProcessor):
    """
    Register LogitsProcessor as a custom op for Ascend.
    Added the feature of lmheadTP in pure dp scenario
    """

    def _get_logits(
        self,
        hidden_states: torch.Tensor,
        lm_head: AscendParallelLMHead,
        embedding_bias: Optional[torch.Tensor],
    ) -> Optional[torch.Tensor]:
        if lmhead_tp_enable():
            return self._get_logits_lmheadtp(hidden_states, lm_head,
                                             embedding_bias)
        else:
            return self._get_logits_normal(hidden_states, lm_head,
                                           embedding_bias)

    def _get_logits_lmheadtp(
        self,
        hidden_states: torch.Tensor,
        lm_head: AscendParallelLMHead,
        embedding_bias: Optional[torch.Tensor],
    ) -> Optional[torch.Tensor]:
        # Gather hidden states from all devices in tensor parallel group
        gathered_hidden_states = get_lmhead_tp_group().all_gather(
            hidden_states, dim=0)
        local_logits = lm_head.quant_method.apply(lm_head,
                                                  gathered_hidden_states,
                                                  bias=embedding_bias)
        # Gather logits for tensor parallel
        logits = get_lmhead_tp_group().all_to_all(local_logits)
        # Remove paddings in vocab (if any)
        if logits is not None:
            logits = logits[..., :self.org_vocab_size]
        return logits

    def _get_logits_normal(
        self,
        hidden_states: torch.Tensor,
        lm_head: AscendParallelLMHead,
        embedding_bias: Optional[torch.Tensor],
    ) -> Optional[torch.Tensor]:
        local_logits = lm_head.quant_method.apply(lm_head,
                                                  hidden_states,
                                                  bias=embedding_bias)
        # Gather logits for tensor parallel
        logits = self._gather_logits(local_logits)

        # Remove paddings in vocab (if any)
        if logits is not None:
            logits = logits[..., :self.org_vocab_size]

        return logits
