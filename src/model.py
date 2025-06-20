# Add old pre-deepseek meta MTP and scale with that

import torch
import torch.nn as nn
from torch.nn import functional as F
import math
import inspect
from muon import Muon

config = {
    "n_embd": 256,
    "n_head": 16,
    "n_layer": 4,
    "n_experts": 32,
    "dropout": 0.2,
    "vocab_size": 65,
    "ctx_len": 2048,
    "init_moe_scaling": 1.25,
    "type": ['mlp', 'moe', 'mlp', 'moe'],
    "device": 'cuda' if torch.cuda.is_available() else 'cpu'
}

# RoPE

class RoPE(nn.Module):
    def __init__(self, d, base=100_000_000_000, device=config['device']):
        super().__init__()

        self.base = base
        self.d = d
        self.device = device
        self.cos_cached = None
        self.sin_cached = None

    def _build_cache(self, x):
        if self.cos_cached is not None:
            return

        head_dim = x.shape[-1]

        theta = 1 / (self.base ** (torch.arange(0, head_dim, 2, device=self.device).float() / self.d))
        seq_idx = torch.arange(x.shape[0], device=self.device).float()
        idx_theta = torch.einsum('n,d->nd', seq_idx, theta)

        cos_cache = torch.cos(idx_theta)
        sin_cache = torch.sin(idx_theta)

        self.cos_cached = torch.cat([cos_cache, cos_cache], dim=-1).unsqueeze(0).unsqueeze(0)
        self.sin_cached = torch.cat([sin_cache, sin_cache], dim=-1).unsqueeze(0).unsqueeze(0)

    def _neg_half(self, x):
        head_dim = x.shape[-1]
        d_2 = head_dim // 2
        return torch.cat([-x[:, :, :, d_2:], x[:, :, :, :d_2]], dim=-1)

    def forward(self, x):
        if self.cos_cached is None or self.cos_cached.shape[2] != x.shape[1]:
            self._build_cache(x)

        x_rope = x.clone()  # VERY IMPORTANT: Create a copy!
        neg_half_x = self._neg_half(x_rope)
        x_out = (x_rope * self.cos_cached[:, :, :x.shape[1], :]) + (neg_half_x * self.sin_cached[:, :, :x.shape[1], :])
        return x_out

def precompute_freqs_cis(dim, end, device, theta=10000.0):
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2, device=device).float() / dim))
    t = torch.arange(end, device=device)
    freqs = torch.outer(t, freqs)
    return torch.cos(freqs), torch.sin(freqs)

def apply_rope(x: torch.Tensor, y: torch.Tensor, freqs_cis) -> tuple[torch.Tensor,torch.Tensor]:
    cos_freqs, sin_freqs = freqs_cis
    seq_len = x.shape[-2]

    cos_seq = cos_freqs[:seq_len]
    sin_seq = sin_freqs[:seq_len]
    cos_seq = cos_seq.unsqueeze(0).unsqueeze(0)
    sin_seq = sin_seq.unsqueeze(0).unsqueeze(0)
    x_real, x_imag = x.chunk(2, dim=-1)
    y_real, y_imag = y.chunk(2, dim=-1)
    x_rotated_real = x_real * cos_seq - x_imag * sin_seq
    x_rotated_imag = x_real * sin_seq + x_imag * cos_seq
    y_rotated_real = y_real * cos_seq - y_imag * sin_seq
    y_rotated_imag = y_real * sin_seq + y_imag * cos_seq
    x_rotated = torch.cat([x_rotated_real, x_rotated_imag], dim=-1)
    y_rotated = torch.cat([y_rotated_real, y_rotated_imag], dim=-1)
    return x_rotated.type_as(x), y_rotated.type_as(y)

# MLA-NSA hybrid, not hardware optimized, just uses NSA sparsity for better training rn

class Attn(nn.Module):
    """
    Native Sparse Attention with Multi-headed Latent Attention integration.
    Combines MLA's compression techniques with NSA's natural sparsity, also better loss
    """
    def __init__(self):
        super().__init__()
        self.device = config['device']
        self.n_embd = config['n_embd']
        self.n_head = config['n_head']
        self.dropout = config['dropout']
        self.ctx_len = config['ctx_len']
        self.rms_norm_eps = config.get('rms_norm_eps', 1e-6)

        # Original MLA parameters
        self.v_head_dim = 32
        self.kv_lora_rank = 32
        self.q_lora_rank = 3 * self.kv_lora_rank
        self.rope_head_dim = 64
        self.nope_head_dim = 32
        self.value_dim = self.n_head * self.v_head_dim
        self.nope_dim = self.n_head * self.nope_head_dim
        self.rope_dim = self.n_head * self.rope_head_dim

        # https://github.com/KellerJordan/modded-nanogpt/blob/ca964e982191830eebbd155e185937077511a8aa/records/110624_ShortcutsTweaks/43f60c4f-0448-4de7-83d9-643ca26f61e7.txt#L168C9-L168C68
        # A learnable scalar to mix the current block's value with the first block's value.
        # This parameter will be optimized by AdamW since its ndim < 2.
        self.lamb_v = nn.Parameter(torch.tensor(0.5))

        # NSA-specific parameters
        self.block_size = config.get('block_size', 16)  # Size of token blocks for compression
        self.num_blocks = self.ctx_len // self.block_size
        self.window_size = config.get('window_size', 128)  # Sliding window size
        self.num_tokens_to_keep = config.get('num_tokens_to_keep', self.ctx_len // 4)  # Number of fine-grained tokens to keep

        # === Branch 1: Coarse-grained compression branch (adapted from MLA) ===
        self.compress_q_linear = nn.Linear(self.n_embd, self.q_lora_rank, bias=False)
        self.q_norm = nn.RMSNorm(self.q_lora_rank, eps=self.rms_norm_eps)
        self.decompress_q_nope = nn.Linear(self.q_lora_rank, self.nope_dim, bias=False)
        self.decompress_q_rope = nn.Linear(self.q_lora_rank, self.rope_dim, bias=False)

        self.compress_kv_linear = nn.Linear(self.n_embd, self.kv_lora_rank, bias=False)
        self.kv_norm = nn.RMSNorm(self.kv_lora_rank, eps=self.rms_norm_eps)
        self.decompress_k_nope = nn.Linear(self.kv_lora_rank, self.nope_dim, bias=False)
        self.decompress_v_linear = nn.Linear(self.kv_lora_rank, self.value_dim, bias=False)
        self.k_rope_linear = nn.Linear(self.n_embd, self.rope_head_dim, bias=False)

        # === Branch 2: Token Selection Branch (NSA) ===
        self.importance_scorer = nn.Linear(self.n_embd, 1,bias=False)
        self.selection_k = nn.Linear(self.n_embd, self.n_head * (self.rope_head_dim + self.nope_head_dim), bias=False)
        self.selection_v = nn.Linear(self.n_embd, self.value_dim, bias=False)

        # === Branch 3: Sliding Window Branch (NSA) ===
        self.window_k = nn.Linear(self.n_embd, self.n_head * (self.rope_head_dim + self.nope_head_dim), bias=False)
        self.window_v = nn.Linear(self.n_embd, self.value_dim, bias=False)

        # Token Compression Mechanism (NSA)
        self.block_compressor = nn.Sequential(
            nn.Linear(self.block_size * self.n_embd, 4 * self.n_embd,bias=False),
            nn.GELU(),
            nn.Linear(4 * self.n_embd, self.n_embd,bias=False)
        )

        # Intra-block position encoding
        self.intra_block_pos_encoding = nn.Parameter(
            torch.randn(1, self.block_size, self.n_embd)
        )

        # Gated Multi-Branch Integration (NSA)
        self.branch_gate = nn.Linear(self.n_embd, 3,bias=False)

        # Output projection
        self.proj = nn.Linear(self.value_dim, self.n_embd, bias=False)
        self.res_dropout = nn.Dropout(p=self.dropout)

        # Caching for inference
        self.k_cache = None
        self.v_cache = None
        self.cache_filled = 0

        # RoPE
        self.rope = RoPE(self.rope_head_dim, device=self.device)
        self.freqs_cis = precompute_freqs_cis(self.rope_head_dim, self.ctx_len, self.device)

    def _compress_tokens(self, x):
        B, T, C = x.size()
        padded_len = ((T + self.block_size - 1) // self.block_size) * self.block_size
        if padded_len > T:
            padding = torch.zeros(B, padded_len - T, C, device=x.device, dtype=x.dtype)
            x_padded = torch.cat([x, padding], dim=1)
        else:
            x_padded = x
        blocks = x_padded.view(B, -1, self.block_size, C)
        pos_encoded_blocks = blocks + self.intra_block_pos_encoding
        blocks_flat = pos_encoded_blocks.view(B, -1, self.block_size * C)
        compressed_blocks = self.block_compressor(blocks_flat)
        return compressed_blocks

    def _select_important_tokens(self, x, importance_scores):
        B, T, _ = x.size()
        _, indices = torch.topk(importance_scores.squeeze(-1),
                                min(self.num_tokens_to_keep, T),
                                dim=1)
        indices, _ = torch.sort(indices, dim=1)
        batch_indices = torch.arange(B, device=x.device).unsqueeze(1).expand(-1, indices.size(1))
        selected_tokens = x[batch_indices, indices]
        return selected_tokens, indices

    def _get_sliding_window_tokens(self, x, current_pos=None):
        if self.training or current_pos is None:
            return x
        else:
            B, T, _ = x.size()
            window_start = max(0, current_pos - self.window_size // 2)
            window_end = min(T, window_start + self.window_size)
            return x[:, window_start:window_end]
            
    def forward(self, x, v1=None):
    # --- END: MODIFIED FORWARD SIGNATURE FOR VALUE RESIDUAL ---
        B, T, C = x.size()

        # === Prepare queries using MLA's approach ===
        compressed_q = self.compress_q_linear(x)
        norm_q = self.q_norm(compressed_q)
        query_nope = self.decompress_q_nope(norm_q)
        query_rope = self.decompress_q_rope(norm_q)

        query_nope = query_nope.view(B, T, self.n_head, self.nope_head_dim).transpose(1, 2)
        query_rope = query_rope.view(B, T, self.n_head, self.rope_head_dim).transpose(1, 2)
        q_rope, _ = apply_rope(query_rope, query_rope, self.freqs_cis)
        q_recombined = torch.empty((B, self.n_head, T, self.rope_head_dim + self.nope_head_dim),
                                  device=x.device, dtype=x.dtype)
        q_recombined[:, :, :, :self.nope_head_dim] = query_nope
        q_recombined[:, :, :, self.nope_head_dim:] = q_rope

        branch_weights = F.softmax(self.branch_gate(x).mean(dim=1), dim=-1)

        # === Branch 1: Coarse-grained compression branch (from MLA) ===
        compressed_kv = self.compress_kv_linear(x)
        norm_kv = self.kv_norm(compressed_kv)
        key_nope_1 = self.decompress_k_nope(norm_kv)
        value_1 = self.decompress_v_linear(norm_kv)
        key_rope_1 = self.k_rope_linear(x)

        key_nope_1 = key_nope_1.view(B, T, self.n_head, self.nope_head_dim).transpose(1, 2)
        key_rope_1 = key_rope_1.view(B, T, 1, self.rope_head_dim).transpose(1, 2)
        value_1 = value_1.view(B, T, self.n_head, self.v_head_dim).transpose(1, 2)

        # https://github.com/KellerJordan/modded-nanogpt/blob/ca964e982191830eebbd155e185937077511a8aa/records/110624_ShortcutsTweaks/43f60c4f-0448-4de7-83d9-643ca26f61e7.txt#L175
        # If v1 (value from the first block) is provided, mix it with the current value_1
        # using the learnable scalar `self.lamb_v`.
        if v1 is not None:
            value_1 = (1 - self.lamb_v) * value_1 + self.lamb_v * v1.view_as(value_1)

        key_rope_1 = key_rope_1 / self.n_head
        _, k_rope_1 = apply_rope(key_rope_1, key_rope_1, self.freqs_cis)
        k_recombined_1 = torch.empty((B, self.n_head, T, self.rope_head_dim + self.nope_head_dim),
                                   device=x.device, dtype=x.dtype)
        k_recombined_1[:, :, :, :self.nope_head_dim] = key_nope_1
        k_recombined_1[:, :, :, self.nope_head_dim:] = k_rope_1

        # === Branch 2: Token Selection Branch (NSA) ===
        importance_scores = self.importance_scorer(x)
        selected_tokens, selected_indices = self._select_important_tokens(x, importance_scores)
        B, S, _ = selected_tokens.size()
        k_selected = self.selection_k(selected_tokens)
        v_selected = self.selection_v(selected_tokens)
        k_selected = k_selected.view(B, S, self.n_head, self.rope_head_dim + self.nope_head_dim).transpose(1, 2)
        v_selected = v_selected.view(B, S, self.n_head, self.v_head_dim).transpose(1, 2)
        k_selected_rope = k_selected[:, :, :, self.nope_head_dim:]
        k_selected_nope = k_selected[:, :, :, :self.nope_head_dim]
        _, k_selected_rope = apply_rope(k_selected_rope, k_selected_rope, self.freqs_cis)
        k_selected[:, :, :, self.nope_head_dim:] = k_selected_rope
        k_selected[:, :, :, :self.nope_head_dim] = k_selected_nope

        # === Branch 3: Sliding Window Branch (NSA) ===
        window_tokens = self._get_sliding_window_tokens(x)
        B, W, _ = window_tokens.size()
        k_window = self.window_k(window_tokens)
        v_window = self.window_v(window_tokens)
        k_window = k_window.view(B, W, self.n_head, self.rope_head_dim + self.nope_head_dim).transpose(1, 2)
        v_window = v_window.view(B, W, self.n_head, self.v_head_dim).transpose(1, 2)
        k_window_rope = k_window[:, :, :, self.nope_head_dim:]
        k_window_nope = k_window[:, :, :, :self.nope_head_dim]
        _, k_window_rope = apply_rope(k_window_rope, k_window_rope, self.freqs_cis)
        k_window[:, :, :, self.nope_head_dim:] = k_window_rope
        k_window[:, :, :, :self.nope_head_dim] = k_window_nope

        # === Compute attention for each branch and blend results ===
        if self.training:
            self.cache_filled = 0
            output_1 = F.scaled_dot_product_attention(q_recombined, k_recombined_1, value_1, is_causal=True, dropout_p=self.dropout)
            output_2 = F.scaled_dot_product_attention(q_recombined, k_selected, v_selected, is_causal=False, dropout_p=self.dropout)
            output_3 = F.scaled_dot_product_attention(q_recombined, k_window, v_window, is_causal=True, dropout_p=self.dropout)
            blended_output = (output_1 * branch_weights[:, 0].view(B, 1, 1, 1) + output_2 * branch_weights[:, 1].view(B, 1, 1, 1) + output_3 * branch_weights[:, 2].view(B, 1, 1, 1))
        else: # Inference mode
            if self.k_cache is None or self.v_cache is None or self.k_cache.size(0) != B:
                self.k_cache = torch.zeros(B, self.n_head, self.ctx_len, self.rope_head_dim + self.nope_head_dim, device=self.device, dtype=x.dtype)
                self.v_cache = torch.zeros(B, self.n_head, self.ctx_len, self.v_head_dim, device=self.device, dtype=x.dtype)
                self.cache_filled = 0
            new_cache_filled = min(self.cache_filled + T, self.ctx_len)
            k_to_cache = k_recombined_1[:, :, :new_cache_filled - self.cache_filled]
            v_to_cache = value_1[:, :, :new_cache_filled - self.cache_filled]
            self.k_cache[:, :, self.cache_filled:new_cache_filled] = k_to_cache
            self.v_cache[:, :, self.cache_filled:new_cache_filled] = v_to_cache
            self.cache_filled = new_cache_filled
            k1 = self.k_cache[:, :, :self.cache_filled]
            v1_cache = self.v_cache[:, :, :self.cache_filled]
            output_1 = F.scaled_dot_product_attention(q_recombined, k1, v1_cache, is_causal=True, dropout_p=0)
            output_2 = F.scaled_dot_product_attention(q_recombined, k_selected, v_selected, is_causal=False, dropout_p=0)
            output_3 = F.scaled_dot_product_attention(q_recombined, k_window, v_window, is_causal=True, dropout_p=0)
            blended_output = (output_1 * branch_weights[:, 0].view(B, 1, 1, 1) + output_2 * branch_weights[:, 1].view(B, 1, 1, 1) + output_3 * branch_weights[:, 2].view(B, 1, 1, 1))

        output = blended_output.transpose(1, 2).contiguous().view(B, T, self.value_dim)
        output = self.proj(output)
        output = self.res_dropout(output)

        # --- START: MODIFIED RETURN VALUE ---
        # Return both the final output and the value tensor from the main branch.
        return output, value_1
        # --- END: MODIFIED RETURN VALUE ---

# Reg MLP 
class MLP(nn.Module):
    def __init__(self):
        super().__init__()
        n_embd = config['n_embd']
        self.c_fc    = nn.Linear(n_embd, 4 * n_embd,bias=False)
        self.c_proj  = nn.Linear(4 * n_embd, n_embd,bias=False)
        self.dropout = nn.Dropout(config['dropout'])

    def forward(self, x):
        x = self.c_fc(x)
        x = F.relu(x).square()
        x = self.c_proj(x)
        x = self.dropout(x)
        return x

# DS-MoE Layer
class UnitCenteredNoise(nn.Module):
    def __init__(self, scaling=0.02):
        super(UnitCenteredNoise, self).__init__()
        self.scaling = scaling
        self.base = 1 - (scaling * 0.5)

    def forward(self, x):
        if self.training:
            noise = torch.rand(x.size(), device=x.device, dtype=x.dtype)
            noise_centered = (noise * self.scaling) + self.base
            return x * noise_centered
        else:
            return x

class DSMoE(nn.Module):
    def __init__(self, index, num_exp=4):
        super().__init__()
        self.hidden_dim = config['n_embd'] * 2
        self.num_experts = config["n_experts"]
        self.num_exp = num_exp
        self.moe_scaling = config["init_moe_scaling"]
        self.experts = nn.ModuleList([MLP() for _ in range(self.num_experts)])
        self.gate = nn.Sequential(
            nn.Linear(config['n_embd'], self.num_experts - 1,bias=False),
            UnitCenteredNoise(scaling=0.02),
            nn.Softmax(dim=-1)
        )
        self.expert_bias = nn.Parameter(torch.zeros(self.num_experts - 1), requires_grad=False)

    def forward(self, x):
        b, t, c = x.shape
        x_flat = x.reshape(b * t, c)
        gate_val_continuous = self.gate(x_flat)
        biased_gate_vals = gate_val_continuous + self.expert_bias
        gate_vals, gate_val_indices = torch.topk(biased_gate_vals, self.num_exp - 1, dim=-1)
        gate_vals = gate_vals / gate_vals.sum(dim=-1, keepdim=True)
        shared_expert_weight = torch.ones_like(gate_vals[:, :1]) / self.num_exp
        gate_vals = torch.cat([shared_expert_weight, gate_vals * (self.num_exp - 1) / self.num_exp], dim=-1)
        gate_val_indices = torch.cat([torch.zeros_like(gate_val_indices[:, :1]), gate_val_indices + 1], dim=-1)
        expert_outputs = torch.stack([expert(x_flat) for expert in self.experts], dim=0)
        router_weights = torch.zeros(x_flat.size(0), self.num_experts, device=x.device)
        for i in range(self.num_exp):
            idx = gate_val_indices[:, i:i+1]
            val = gate_vals[:, i:i+1]
            router_weights.scatter_add_(1, idx, val)
        weighted_outputs = expert_outputs * router_weights.transpose(0, 1).unsqueeze(-1)
        output = weighted_outputs.sum(dim=0)
        return output.reshape(b, t, c), router_weights

class Block(nn.Module):
    def __init__(self, index):
        super().__init__()
        n_embd = config['n_embd']
        self.attn = Attn()
        self.ffn_type = config['type'][index]

        if self.ffn_type == "mlp":
            self.ffn = MLP()
        elif self.ffn_type == "moe":
            self.ffn = DSMoE(index)
        else:
            raise ValueError(f"Invalid layer type: {self.ffn_type}")

        self.rm1 = nn.RMSNorm(n_embd)
        self.rm2 = nn.RMSNorm(n_embd)
        

        self.lambdas = nn.Parameter(torch.tensor([1.0, 0.0]))

    def forward(self, x, x0, v1=None):
        # 1. Embed Shortcut: Mix current state `x` with the initial embedding `x0`.
        x_mixed = self.lambdas[0] * x + self.lambdas[1] * x0
        
        # 2. Attention (Pre-Norm): Normalize the mixed input *before* the attention layer.
        #    Pass `v1` to the attention layer and get both the output and the new value tensor.
        attn_out, v_out = self.attn(self.rm1(x_mixed), v1=v1)
        x = x + attn_out # First residual connection
        
        # 3. FFN (Pre-Norm): Normalize the state *before* the FFN.
        #    Handle both MoE (returns two values) and standard MLP cases.
        if self.ffn_type == "moe":
            ffn_out, router_weights = self.ffn(self.rm2(x))
            x = x + ffn_out # Second residual connection
            # Return a tuple structure: ((new_x, value_tensor), router_weights)
            return (x, v_out), router_weights
        else:
            ffn_out = self.ffn(self.rm2(x))
            x = x + ffn_out # Second residual connection
            # Return the same structure, but with router_weights as None.
            return (x, v_out), None

class Transformer(nn.Module):
    def __init__(self):
        super().__init__()
        self.config = config
        self.token_embedding_table = nn.Embedding(config['vocab_size'], config['n_embd'])
        self.position_embedding_table = nn.Embedding(config['ctx_len'], config['n_embd'])
        self.blocks = nn.Sequential(*[Block(i) for i in range(config['n_layer'])])
        self.rm_f = nn.RMSNorm(config['n_embd'])
        self.lm_head = nn.Linear(config['n_embd'], config['vocab_size'],bias=False)
        self.token_embedding_table.weight = self.lm_head.weight
        self.apply(self._init_weights)
        self.total_params = sum(p.numel() for p in self.parameters())

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        B, T = idx.shape
        tok_emb = self.token_embedding_table(idx)
        pos_emb = self.position_embedding_table(torch.arange(T, device=self.config['device']))
        x = tok_emb + pos_emb

        # 1. Clone the initial embedding to create `x0` for the Embed Shortcut.
        x0 = x.clone()
        
        # 2. Initialize `v1` to None. It will hold the value tensor from the first block.
        v1 = None

        all_router_weights = []

        # The loop is enumerated to detect the first block (i==0).
        for i, block in enumerate(self.blocks):
            # Pass x, x0, and v1 to each block and unpack the complex return tuple.
            (x, v_out), router_weights = block(x, x0, v1)
            
            if router_weights is not None:
                all_router_weights.append(router_weights)

            # After the first block (i==0), capture its value tensor (`v_out`).
            # .detach() is crucial to prevent gradients from flowing back through all subsequent blocks.
            if i == 0:
                v1 = v_out.detach()
        
        x = self.rm_f(x)
        logits = self.lm_head(x)
        # todo: check this (tanh)
        logits = 30 * torch.tanh(logits / 30)
        if targets is None:
            loss = None
        else:
            B, T, C = logits.shape
            logits = logits.view(B * T, C)
            targets = targets.view(B * T)
            loss = F.cross_entropy(logits, targets) 

        return logits, loss, all_router_weights

    @torch.no_grad()
    def generate(self, idx, max_new_tokens, temperature=1.0, top_k=None, tiktoken_vocab_size=None):
        # This function remains unchanged as it only concerns the forward pass and KV caching,
        # which are already handled by the modified layers.
        if temperature <= 0:
            print("Warning: Temperature <= 0. Using a very small value (1e-6) instead.")
            temperature = 1e-6

        model_vocab_size = config['vocab_size']
        use_vocab_mask = False
        if tiktoken_vocab_size is not None:
            if tiktoken_vocab_size < model_vocab_size:
                print(f"generate(): Masking logits for indices >= {tiktoken_vocab_size} (model vocab size: {model_vocab_size})")
                use_vocab_mask = True
            elif tiktoken_vocab_size > model_vocab_size:
                 print(f"generate(): Warning - tiktoken_vocab_size ({tiktoken_vocab_size}) > model_vocab_size ({model_vocab_size}). Masking ineffective.")

        for _ in range(max_new_tokens):
            start_pos = max(0, idx.size(1) - config['ctx_len'])
            idx_cond = idx[:, start_pos:]
            logits, _, _ = self(idx_cond)
            logits = logits[:, -1, :]
            logits = logits / temperature
            if use_vocab_mask:
                 logits[:, tiktoken_vocab_size:] = -float('Inf')
            if top_k is not None and top_k > 0:
                k = min(top_k, logits.size(-1))
                top_k_values, _ = torch.topk(logits, k=k, dim=-1)
                kth_logit_value = top_k_values[:, [-1]]
                logits[logits < kth_logit_value] = -float('Inf')
            probs = F.softmax(logits, dim=-1)
            idx_next = torch.multinomial(probs, num_samples=1)
            idx = torch.cat((idx, idx_next), dim=1)

        total_size_gb = 0
        if hasattr(self, 'blocks') and self.blocks is not None:
            for block in self.blocks:
                if hasattr(block, 'attn') and hasattr(block.attn, 'k_cache') and block.attn.k_cache is not None:
                    size_bytes = block.attn.k_cache.numel() * block.attn.k_cache.element_size()
                    total_size_gb += size_bytes / (1024**3)
                if hasattr(block, 'attn') and hasattr(block.attn, 'v_cache') and block.attn.v_cache is not None:
                    size_bytes = block.attn.v_cache.numel() * block.attn.v_cache.element_size()
                    total_size_gb += size_bytes / (1024**3)
        else:
            print("Warning: Cannot calculate KV cache size. `self.blocks` not found or is None.")
        return idx, total_size_gb

    def configure_optimizers(self, weight_decay, learning_rate, device):
        # No changes needed here. The new learnable scalars (`lamb_v`, `lambdas`) have
        # ndim < 2 and will automatically be assigned to the AdamW optimizer.
        muon_params = []
        adamw_params = []
        muon_exclude_patterns = [
            'attn.intra_block_pos_encoding',
            'attn.importance_scorer.weight',
            'attn.importance_scorer.bias',
            'attn.block_compressor',
        ]
        for name, param in self.named_parameters():
            if not param.requires_grad:
                continue
            is_excluded = False
            for pattern in muon_exclude_patterns:
                if pattern in name:
                    is_excluded = True
                    break
            if 'blocks' in name and param.ndim >= 2 and not is_excluded:
                muon_params.append(param)
            else:
                adamw_params.append(param)
        num_muon_params = sum(p.numel() for p in muon_params)
        num_adamw_params = sum(p.numel() for p in adamw_params)
        print(f"num Muon parameters: {num_muon_params:,}")
        print(f"num AdamW parameters: {num_adamw_params:,}")
        if not muon_params:
             print("\n\n*** WARNING: Muon parameter list is EMPTY after filtering! ***")
             optimizers = [
                 torch.optim.AdamW(adamw_params, lr=learning_rate, betas=(0.90, 0.95), weight_decay=weight_decay)
             ]
        else:
            optimizers = [
                Muon(muon_params, lr=0.02, momentum=0.95),
                torch.optim.AdamW(adamw_params, lr=learning_rate, betas=(0.90, 0.95), weight_decay=weight_decay)
            ]
        return optimizers

    def update_expert_biases(self, all_router_weights, update_rate):
        with torch.no_grad():
            j = 0 
            for block in self.blocks:
                if isinstance(block.ffn, DSMoE):
                    router_weights = all_router_weights[j]
                    j += 1
                    c_i = router_weights[:, 1:].sum(dim=0)
                    total_routed_tokens = c_i.sum()
                    c_i_bar = total_routed_tokens / (block.ffn.num_experts - 1)
                    e_i = c_i - c_i_bar
                    block.ffn.expert_bias.add_(update_rate * torch.sign(e_i))

    def estimate_mfu(self, params, fwdbwd_per_iter, dt):
        N = params
        L, H, Q, T = config['n_layer'], config['n_head'], config['n_embd']//config['n_head'], config['ctx_len']
        flops_per_token = 6*N + 12*L*H*Q*T
        flops_per_fwdbwd = flops_per_token * T
        flops_per_iter = flops_per_fwdbwd * fwdbwd_per_iter
        flops_achieved = flops_per_iter * (1.0/dt)
        flops_promised = 65e12
        mfu = flops_achieved / flops_promised
        return mfu
