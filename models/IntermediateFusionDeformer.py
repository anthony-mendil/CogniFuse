import torch
from torch import nn
from einops import rearrange
from einops.layers.torch import Rearrange

from models.BaseBenchmarkModel import BaseBenchmarkModel


def pair(t):
    return t if isinstance(t, tuple) else (t, t)


class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, out_dim, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, out_dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.net(x)


class Attention(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim=-1)
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

    def forward(self, x):
        qkv = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.heads), qkv)

        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale

        attn = self.attend(dots)

        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class Transformer(nn.Module):
    def cnn_block(self, in_chan, kernel_size, dp):
        return nn.Sequential(
            nn.Dropout(p=dp),
            nn.Conv1d(in_channels=in_chan, out_channels=in_chan,
                      kernel_size=kernel_size, padding=self.get_padding_1D(kernel=kernel_size)),
            nn.BatchNorm1d(in_chan),
            nn.ELU(),
            nn.MaxPool1d(kernel_size=2, stride=2)
        )

    def __init__(self, dim, depth, heads, dim_head, mlp_dim, in_chan, fine_grained_kernel=11, dropout=0.):
        super().__init__()
        self.layers = nn.ModuleList([])
        for i in range(depth):
            dim = int(dim * 0.5)
            self.layers.append(nn.ModuleList([
                Attention(dim, heads=heads, dim_head=dim_head, dropout=dropout),
                FeedForward(dim, hidden_dim=mlp_dim, out_dim=dim, dropout=dropout),
                self.cnn_block(in_chan=in_chan, kernel_size=fine_grained_kernel, dp=dropout)
            ]))
        self.pool = nn.MaxPool1d(kernel_size=2, stride=2)

    def forward(self, x):
        dense_feature = []
        for attn, ff, cnn in self.layers:
            x_cg = self.pool(x)
            x_cg = attn(x_cg) + x_cg
            x_fg = cnn(x)
            x_info = self.get_info(x_fg)  # (b, in_chan)
            dense_feature.append(x_info)
            x = ff(x_cg) + x_fg

        x_dense = torch.cat(dense_feature, dim=-1)  # (b, in_chan * depth)
        x = x.view(x.size(0), -1)   # (b, in_chan * d_hidden_last_layer)
        emd = torch.cat((x, x_dense), dim=-1)  # (b, in_chan * (depth + d_hidden_last_layer))
        return emd

    def get_info(self, x):
        # x: (b, k, l)
        x = torch.log(torch.mean(x.pow(2), dim=-1))
        return x

    def get_padding_1D(self, kernel):
        return int(0.5 * (kernel - 1))


class Conv2dWithConstraint(nn.Conv2d):
    def __init__(self, *args, doWeightNorm=True, max_norm=1, **kwargs):
        self.max_norm = max_norm
        self.doWeightNorm = doWeightNorm
        super(Conv2dWithConstraint, self).__init__(*args, **kwargs)

    def forward(self, x):
        if self.doWeightNorm:
            self.weight.data = torch.renorm(
                self.weight.data, p=2, dim=0, maxnorm=self.max_norm
            )
        return super(Conv2dWithConstraint, self).forward(x)


class IntermediateFusionDeformer(BaseBenchmarkModel):
    @staticmethod
    def add_model_options(parser_group):
        parser_group.add_argument("--mlp_dim", default=[16, 16, 16, 16], type=int, nargs="+",
                           help="Dimensions of MLPs for the modalities")
        parser_group.add_argument("--num_kernel", default=[64, 4, 4, 4], type=int, nargs="+",
                           help="Numbers of kernels for the modalities")
        parser_group.add_argument("--temporal_kernel", default=[13, 13, 13, 13], type=int, nargs="+",
                           help="Lengths of temporal kernels for the modalities")
        parser_group.add_argument("--emb_dim", default=256, type=int, help="Embedding dimension")
        parser_group.add_argument("--depth", default=4, type=int, help="Depth of kernels")
        parser_group.add_argument("--heads", default=16, type=int, help="Number of heads")
        parser_group.add_argument("--dim_head", default=16, type=int, help="Dimension of heads")
        parser_group.add_argument("--dropout", default=0.2, type=float, help="Dropout rate")
        
    def cnn_block(self, out_chan, kernel_size, num_chan):
        return nn.Sequential(
            Conv2dWithConstraint(1, out_chan, kernel_size, padding=self.get_padding(kernel_size[-1]), max_norm=2),
            # Only do spatial convolution if there is more than one channel
            Conv2dWithConstraint(out_chan, out_chan, (num_chan, 1),
                                 padding=0, max_norm=2) if num_chan > 1 else nn.Identity(),
            nn.BatchNorm2d(out_chan),
            nn.ELU()
        )

    def __init__(self, *, num_time, num_chan, temporal_kernel, num_kernel,
                 emb_dim, out_dim, depth, heads,
                 mlp_dim, dim_head, dropout):
        super().__init__()

        self.cnn_encoders = nn.ModuleList([])
        for chan, temp_k, num_k in zip(num_chan, temporal_kernel, num_kernel):
            cnn_encoder = self.cnn_block(out_chan=num_k, kernel_size=(1, temp_k), num_chan=chan)
            self.cnn_encoders.append(cnn_encoder)

        self.to_patch_embedding = Rearrange('b k c f -> b k (c f)')

        self.pos_embeddings = nn.ParameterList([])
        for dim, num_k in zip(num_time, num_kernel):
            pos_embedding = nn.Parameter(torch.randn(1, num_k, dim))
            self.pos_embeddings.append(pos_embedding)

        self.transformers = nn.ModuleList([])
        out_size = 0
        for dim, mlp_d, temp_k, num_k in zip(num_time, mlp_dim, temporal_kernel, num_kernel):
            transformer_model = Transformer(
                dim=dim, depth=depth, heads=heads, dim_head=dim_head,
                mlp_dim=mlp_d, dropout=dropout,
                in_chan=num_k, fine_grained_kernel=temp_k,
            )
            
            self.transformers.append(transformer_model)

            L = self.get_hidden_size(input_size=dim, num_layer=depth)
            modality_out_size = int(num_k * L[-1]) + int(num_k * depth)
            out_size += modality_out_size

        self.mlp_head = FeedForward(
            out_size, hidden_dim=emb_dim, out_dim=out_dim, dropout=dropout
        )

    def forward(self, channels):
        # eeg: (b, chan, time)
        for i, chan in enumerate(channels):
            chan = torch.unsqueeze(chan, dim=1)  # (b, 1, channels, time)
            chan = self.cnn_encoders[i](chan)
            chan = self.to_patch_embedding(chan)
            b, n, _ = chan.shape
            chan += self.pos_embeddings[i]
            chan = self.transformers[i](chan)
            channels[i] = chan

        x = torch.cat(channels, dim=-1)

        return self.mlp_head(x)

    def get_padding(self, kernel):
        return (0, int(0.5 * (kernel - 1)))

    def get_hidden_size(self, input_size, num_layer):
        return [int(input_size * (0.5 ** i)) for i in range(num_layer + 1)]


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


if __name__ == "__main__":
    dummy_model = IntermediateFusionDeformer(
        num_time=[4 * 128, 6 * 128, 4 * 64, 10 * 32],
        num_chan=[16, 1, 1, 1],
        mlp_dim=[16, 16, 16, 16],
        num_kernel=[64, 4, 4, 4],
        temporal_kernel=[13, 13, 13, 13],
        emb_dim=256,
        depth=4,
        heads=16,
        dim_head=16,
        dropout=0.,
        out_dim=2
    )

    dummy_eeg = torch.randn(1, 16, 4 * 128)
    dummy_ppg = torch.randn(1, 1, 6 * 128)
    dummy_eda = torch.randn(1, 1, 4 * 64)
    dummy_resp = torch.randn(1, 1, 10 * 32)
    channels = [
        dummy_eeg,
        dummy_ppg, 
        dummy_eda,
        dummy_resp
    ]

    print(dummy_model)
    print(count_parameters(dummy_model))

    output = dummy_model(channels)

    print(output)
    print(output.shape)
