import os, sys
parent_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(parent_dir)
src_dir = os.path.dirname(parent_dir)
sys.path.append(src_dir)

import torch
import torch.nn as nn
import torch.nn.functional as F
# 2D image feature extractor
from GCVit.models import gc_vit
# 3D scene graph feature extractor
from src.models.sg_encoder import MultiModalEncoder


def _to_channel_last(x):
    """
    Args:
        x: (B, C, H, W)

    Returns:
        x: (B, H, W, C)
    """
    return x.permute(0, 2, 3, 1)


def _to_channel_first(x):
    """
    Args:
        x: (B, H, W, C)

    Returns:
        x: (B, C, H, W)
    """
    return x.permute(0, 3, 1, 2)

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

class PatchSGIEAligner(nn.Module):
    def __init__(self, 
                 backbone,
                 num_reduce,
                 backbone_dim,
                 img_transpose, 
                 patch_hidden_dims,
                 patch_encoder_dim,
                 obj_embedding_dim,
                 obj_embedding_hidden_dims,
                 obj_encoder_dim,
                 sg_modules,
                 sg_rel_dim,
                 attr_dim,
                 img_feat_dim, 
                 drop,
                 use_temporal
                 ):
        super().__init__()
        
        # backbone 
        self.backbone = backbone
        reduce_list = [gc_vit.ReduceSize(dim=backbone_dim, keep_dim=True)
                            for i in range(num_reduce)]
        self.reduce_layers = nn.Sequential(*reduce_list)
        
        # patch feature encoder
        self.img_transpose = img_transpose
        self.patch_encoder = Mlps(backbone_dim, hidden_features = patch_hidden_dims, 
                                 out_features= patch_encoder_dim, drop = drop)
        
        # 3D scene graph encoder
        self.sg_encoder = MultiModalEncoder(
            modules = sg_modules, rel_dim = sg_rel_dim, attr_dim=attr_dim, 
            img_feat_dim = img_feat_dim, dropout = drop)
        self.obj_embedding_encoder = Mlps(obj_embedding_dim, hidden_features = obj_embedding_hidden_dims, 
                                        out_features = obj_encoder_dim, drop = drop)
        
        # use temporal information
        self.use_temporal = use_temporal
        
    def forward(self, data_dict):
        # get data
        images = data_dict['images'] # (B, H, W, C)
        channel_last = True
        
        # patch encoding 
        images = _to_channel_first(images)
        channel_last = False
        features = self.backbone(images)[-1] # (B, C', H/32, W/32); input channel first,output channel first 
        features = _to_channel_last(features)
        channel_last = True
        patch_features = self.reduce_layers(features) # (B, H/64, W/64, C'); input channel last,output channel last 
        patch_features = self.patch_encoder(patch_features) # (B, P_H, P_W, C*); input channel last,output channel last 
        patch_features = patch_features.flatten(1, 2) # (B, P_H*P_W, C*)
        
        # sg encoding
        obj_3D_embeddings = self.forward_scene_graph(data_dict)  # (O, C*)
        obj_3D_embeddings = self.obj_embedding_encoder(obj_3D_embeddings) # (O, C*)
        obj_3D_embeddings_norm = F.normalize(obj_3D_embeddings, dim=-1)
        ## as long as batch_size^2 < batch_size*num_candidates^2, it is faster to calculate similarity between objs and patches
        obj_3D_embeddings_sim = torch.mm(obj_3D_embeddings_norm, obj_3D_embeddings_norm.permute(1, 0)) # (O, O)
         
        # calculate similarity between patches and objs
        batch_size = data_dict['batch_size']
        
        patch_obj_sim_list = []
        patch_patch_sim_list = []
        obj_obj_sim_list = []
        patch_obj_sim_temp_list = []
        patch_patch_sim_temp_list = []
        obj_obj_sim_temp_list = []
        
        for batch_i in range(batch_size):
            # calculate similarity between patches and objs
            # patch features per batch
            patch_features_pb = patch_features[batch_i] # (P_H*P_W, C*)
            patch_features_pb_norm = F.normalize(patch_features_pb, dim=-1)
            assoc_data_dict = data_dict['assoc_data_dict'][batch_i]
            patch_obj_sim, patch_patch_sim, obj_obj_sim = self.calculate_similarity(
                obj_3D_embeddings_norm, patch_features_pb_norm, assoc_data_dict, obj_3D_embeddings_sim)
            patch_obj_sim_list.append(patch_obj_sim)
            patch_patch_sim_list.append(patch_patch_sim)
            obj_obj_sim_list.append(obj_obj_sim)
            # temporal information
            if self.use_temporal:
                assoc_data_dict = data_dict['assoc_data_dict_temp'][batch_i]
                patch_obj_sim_temp, patch_patch_sim_temp, obj_obj_sim_temp = self.calculate_similarity(
                    obj_3D_embeddings_norm, patch_features_pb_norm, assoc_data_dict, obj_3D_embeddings_sim)
                patch_obj_sim_temp_list.append(patch_obj_sim_temp)
                patch_patch_sim_temp_list.append(patch_patch_sim_temp)
                obj_obj_sim_temp_list.append(obj_obj_sim_temp)

        embs = {}
        embs['patch_raw_features'] = features # (B, H/32, W/32, C');
        embs['patch_features'] = patch_features # (B, P_H*P_W, C*)
        embs['obj_features'] = obj_3D_embeddings # [O, C*]
        embs['patch_obj_sim'] = patch_obj_sim_list # B - [P_H*P_W, N]
        embs['patch_patch_sim'] = patch_patch_sim_list # B - [P_H*P_W, P_H*P_W]
        embs['obj_obj_sim'] = obj_obj_sim_list # B - [N, N]
        embs['patch_obj_sim_temp'] = patch_obj_sim_temp_list # B - [P_H*P_W, N]
        embs['patch_patch_sim_temp'] = patch_patch_sim_temp_list # B - [P_H*P_W, P_H*P_W]
        embs['obj_obj_sim_temp'] = obj_obj_sim_temp_list
        return embs
    
    def calculate_similarity(self, obj_3D_embeddings_norm, patch_features_norm, 
                             assoc_data_dict, obj_3D_embeddings_sim):
        scan_objs_idx = assoc_data_dict['scans_sg_obj_idxs']
        scans_obj_embeddings_norm = obj_3D_embeddings_norm[scan_objs_idx, :] # (N, C*)
        patch_obj_sim = torch.mm(patch_features_norm, scans_obj_embeddings_norm.permute(1, 0)) # (P_H*P_W, N)
        patch_patch_sim = torch.mm(patch_features_norm, patch_features_norm.permute(1, 0)) # (P_H*P_W, P_H*P_W)
        obj_obj_sim = obj_3D_embeddings_sim[scan_objs_idx, :][:, scan_objs_idx] # (N, N)
        return patch_obj_sim, patch_patch_sim, obj_obj_sim
    
    def forward_with_patch_features(self, data_dict):
        # get data
        patch_features = data_dict['patch_features'] # (B, P_H*2, P_W*2, C*)
        
        # encoding patch features
        patch_features = self.reduce_layers(patch_features) 
        patch_features = self.patch_encoder(patch_features) # (B, P_H, P_W, C*)
        patch_features = patch_features.flatten(1, 2) # (B, P_H*P_W, C*)
        
        # sg encoding
        obj_3D_embeddings = self.forward_scene_graph(data_dict)  # (O, C*)
        obj_3D_embeddings = self.obj_embedding_encoder(obj_3D_embeddings) # (O, C*)
        obj_3D_embeddings_norm = F.normalize(obj_3D_embeddings, dim=-1)
        ## as long as batch_size^2 < batch_size*num_candidates^2, it is faster to calculate similarity between objs and patches
        obj_3D_embeddings_sim = torch.mm(obj_3D_embeddings_norm, obj_3D_embeddings_norm.permute(1, 0)) # (O, O)
         
        # calculate similarity between patches and objs
        batch_size = data_dict['batch_size']
        
        patch_obj_sim_list = []
        patch_patch_sim_list = []
        obj_obj_sim_list = []
        patch_obj_sim_temp_list = []
        patch_patch_sim_temp_list = []
        obj_obj_sim_temp_list = []
        
        for batch_i in range(batch_size):
            # calculate similarity between patches and objs
            # patch features per batch
            patch_features_pb = patch_features[batch_i] # (P_H*P_W, C*)
            patch_features_pb_norm = F.normalize(patch_features_pb, dim=-1)
            assoc_data_dict = data_dict['assoc_data_dict'][batch_i]
            patch_obj_sim, patch_patch_sim, obj_obj_sim = self.calculate_similarity(
                obj_3D_embeddings_norm, patch_features_pb_norm, assoc_data_dict, obj_3D_embeddings_sim)
            patch_obj_sim_list.append(patch_obj_sim)
            patch_patch_sim_list.append(patch_patch_sim)
            obj_obj_sim_list.append(obj_obj_sim)
            # temporal information
            if self.use_temporal:
                assoc_data_dict = data_dict['assoc_data_dict_temp'][batch_i]
                patch_obj_sim_temp, patch_patch_sim_temp, obj_obj_sim_temp = self.calculate_similarity(
                    obj_3D_embeddings_norm, patch_features_pb_norm, assoc_data_dict, obj_3D_embeddings_sim)
                patch_obj_sim_temp_list.append(patch_obj_sim_temp)
                patch_patch_sim_temp_list.append(patch_patch_sim_temp)
                obj_obj_sim_temp_list.append(obj_obj_sim_temp)

        embs = {}
        embs['patch_features'] = patch_features # (B, P_H*P_W, C*)
        embs['obj_features'] = obj_3D_embeddings # [O, C*]
        embs['patch_obj_sim'] = patch_obj_sim_list # B - [P_H*P_W, N]
        embs['patch_patch_sim'] = patch_patch_sim_list # B - [P_H*P_W, P_H*P_W]
        embs['obj_obj_sim'] = obj_obj_sim_list # B - [N, N]
        embs['patch_obj_sim_temp'] = patch_obj_sim_temp_list # B - [P_H*P_W, N]
        embs['patch_patch_sim_temp'] = patch_patch_sim_temp_list # B - [P_H*P_W, P_H*P_W]
        embs['obj_obj_sim_temp'] = obj_obj_sim_temp_list
        return embs
    
    def forward2DImage(self, data_dict):
        # get data
        images = data_dict['images']
        
        # patch encoding 
        images = _to_channel_first(images)
        channel_last = False
        features = self.backbone(images)[-1] # (B, C', H/32, W/32); input channel first,output channel first 
        features = _to_channel_last(features)
        channel_last = True
        patch_features = self.reduce_layers(features) # (B, H/64, W/64, C'); input channel last,output channel last 
        patch_features = self.patch_encoder(patch_features) # (B, P_H, P_W, C*); input channel last,output channel last 
        patch_features = patch_features.flatten(1, 2) # (B, P_H*P_W, C*)
        
        return patch_features
    
    def forward_scene_graph(self, data_dict):

        scene_graph_dict = data_dict['scene_graphs']
        object_embeddings = self.sg_encoder(scene_graph_dict)
            
        return object_embeddings['joint']