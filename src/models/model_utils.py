
import torch
import torch.nn.functional as F
from torch import nn, Tensor
class Attention(nn.Module):
    def __init__(self, d_model, heads=8, dim_head=64):
        super().__init__()
        inner_dim = dim_head * heads
        self.heads = heads
        self.logit_scale = nn.Parameter(torch.log(10 * torch.ones((heads, 1, 1))), requires_grad=True)

        self.softmax = nn.Softmax(dim=-1)

        self.to_qkv = nn.Linear(d_model, inner_dim * 3, bias=True)
        self.to_out = nn.Linear(inner_dim, d_model, bias=False)

    def forward(self, x):
        B_, N, C = x.shape

        qkv = self.to_qkv(x)
        qkv = qkv.reshape(B_, N, 3, self.heads, -1).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # make torchscript happy (cannot use tensor as tuple)

        # scaled cosine attention
        attn = (F.normalize(q, dim=-1) @ F.normalize(k, dim=-1).transpose(-2, -1))
        logit_scale = torch.clamp(self.logit_scale,
                                  max=torch.log(torch.tensor(1. / 0.01, device=self.logit_scale.device))).exp()
        attn = attn * logit_scale
        attn = self.softmax(attn)

        out = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        return self.to_out(out)


class TransformerEncoderLayer(nn.Module):
    def __init__(self, 
                 d_model: int = 256, 
                 nhead: int = 4,
                 acti_layer = nn.GELU()
                 ) -> None:
        super().__init__()

        self.self_attn = Attention(d_model, nhead, 64)
        self.norm = nn.LayerNorm(d_model)
        self.acti_layer = acti_layer
        return None

    def forward(self, x):
        # x: (B, N, d_model)
        x_attn = x + self.norm(self.self_attn(x))
        x_out = self.acti_layer(x_attn)
        return x_out
    
class TransformerEncoder(nn.Module):
    def __init__(self, 
                 num_layers: int = 6,
                 d_model_in: int = 256,
                 d_model_inner: int = 256,
                 d_model_out: int = 256,
                 num_heads: int = 4,
                 acti_layer = nn.GELU()
                 ) -> None:
        super().__init__()
        self.cls_token = nn.Parameter( F.normalize(torch.ones(1, 1, d_model_inner), dim=-1), requires_grad=True)
        
        self.map_in = nn.Linear(d_model_in, d_model_inner)
        self.layers = nn.ModuleList([TransformerEncoderLayer(
            d_model_inner, num_heads, acti_layer) for _ in range(num_layers)])
        # self.encoder_layer = nn.TransformerEncoderLayer(d_model=d_model_in, nhead=num_heads)
        # self.transformer_encoder = nn.TransformerEncoder(self.encoder_layer, num_layers=num_layers)
        self.map_out = nn.Linear(d_model_inner, d_model_out)

    def forward(self, x):
        x = self.map_in(x)
        for l_i, layer in enumerate(self.layers):
            x = layer(x)
        # x = self.transformer_encoder(x)
        x = self.map_out(x)
        return x
    
    def forward_cls(self, x):
        x_in = self.map_in(x)
        cls_tokens = self.cls_token.expand(x_in.size(0), -1, -1)
        x_cls = torch.cat((cls_tokens, x_in), dim=1)
        for l_i, layer in enumerate(self.layers):
            x_cls = layer(x_cls)
        cls_embed = x_cls[:, 0, :]
        return cls_embed
    
class PatchAggregator(nn.Module):
    
    def __init__(self, d_model, nhead, num_layers, dropout):
        super().__init__()
        # transformer encoders
        # self.encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=nhead, dropout=dropout)
        # self.transformer_encoder = nn.TransformerEncoder(self.encoder_layer, num_layers=num_layers)
        # Initialize the [CLS] token
        # self.cls_token = nn.Parameter(torch.zeros(1, 1, d_model))
        
        self.transformer_encoder = TransformerEncoderLayer(d_model=d_model, nhead=nhead)

    def forward(self, x):
        # # x: (batch_size, num_patches, d_model)
        # cls_tokens = self.cls_token.expand(x.size(0), -1, -1)
        # x = torch.cat((cls_tokens, x), dim=1)
        # output = self.transformer_encoder(x)
        # cls_token_output = output[:, 0, :]
        # return cls_token_output
        
        # x: (batch_size, num_patches, d_model)
        output = self.transformer_encoder(x)
        return output
    
class Mlps(nn.Module):
    def __init__(self, in_features, hidden_features=[], out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        self.direct_output = False

        layers_list = []
        if(len(hidden_features) <= 0):
            assert out_features == in_features
            self.direct_output  = True
            # layers_list.append(nn.Linear(in_features, out_features))
            # layers_list.append(nn.Dropout(drop))
            
        else:
            hidden_features = [in_features] + hidden_features
            for i in range(len(hidden_features)-1):
                layers_list.append(nn.Linear(hidden_features[i], hidden_features[i+1]))
                layers_list.append(act_layer())
                layers_list.append(nn.Dropout(drop))
            layers_list.append(nn.Linear(hidden_features[-1], out_features))
            layers_list.append(nn.Dropout(drop))
            
        self.mlp_layers =  nn.Sequential(*layers_list)

    def forward(self, x):
        if self.direct_output:
            return x
        else:
            return self.mlp_layers(x)
    
class Residual(nn.Module):
    """The Residual block of ResNet."""
    def __init__(self, input_channels, num_channels, use_1x1conv=False,
                 strides=1):
        super().__init__()
        self.conv1 = nn.Conv2d(input_channels, num_channels, kernel_size=3,
                               padding=1, stride=strides)
        self.conv2 = nn.Conv2d(num_channels, num_channels, kernel_size=3,
                               padding=1)
        if use_1x1conv:
            self.conv3 = nn.Conv2d(input_channels, num_channels,
                                   kernel_size=1, stride=strides)
        else:
            self.conv3 = None
        self.bn1 = nn.BatchNorm2d(num_channels)
        self.bn2 = nn.BatchNorm2d(num_channels)

    def forward(self, X):
        Y = F.relu(self.bn1(self.conv1(X)))
        Y = self.bn2(self.conv2(Y))
        if self.conv3:
            X = self.conv3(X)
        Y += X
        return F.relu(Y)
    
class PatchCNN(nn.Module):
    """ CNN for context information between patches
    d_model: dim of patch embeds
    num_layers: must be even
    """
    def __init__(self, 
                 d_model: int = 256, 
                 num_layers: int = 6
                 ) -> None:
        super().__init__()

        self.num_block = num_layers // 2
        modules = []
        for i in range(self.num_block):
            modules.append(Residual(d_model, d_model))
        self.blocks = nn.Sequential(*modules)
        
    def forward(self, x):
        return self.blocks(x)