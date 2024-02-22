import argparse
import time
import os
from collections import OrderedDict
import sys
from IPython import embed
import numpy as np
import subprocess
src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ws_dir = os.path.dirname(src_dir)
sys.path.append(src_dir)
sys.path.append(ws_dir)
# config
from configs import update_config, config
# trainer
from engine import EpochBasedTrainer
# models
import torch
import torch.optim as optim
from mmdet.models import build_backbone
from mmcv import Config
# from models.GCVit.models import gc_vit
from models.patch_SGIE_aligner import PatchSGIEAligner
from models.lossE import get_loss, get_val_room_retr_loss
# dataset
from datasets.loaders import get_train_val_data_loader, get_val_dataloader
from src.datasets.scannet_objpair import ScannetPatchObjDataset
# utils
from utils import common


class Trainer(EpochBasedTrainer):
    def __init__(self, cfg, parser=None):
        super().__init__(cfg, parser)
        
        # cfg
        self.cfg = cfg  
        
        # get device 
        if not torch.cuda.is_available(): raise RuntimeError('No CUDA devices available.')
        self.device = torch.device("cuda")
        
        # get data loader
        start_time = time.time()
        self.train_loader, self.val_loader = self.getDataLoader(cfg)
        loading_time = time.time() - start_time
        message = 'Data loader created: {:.3f}s collapsed.'.format(loading_time)
        self.logger.info(message)
        
        # generate model
        self.registerPatchObjectAlignerFromCfg(cfg)  
        
        # optimizer
        self.registerOptim(cfg)
        
        # scheduler
        if cfg.train.optim.scheduler == 'step':
            scheduler = optim.lr_scheduler.StepLR(self.optimizer, cfg.train.optim.lr_decay_steps, gamma=cfg.train.optim.lr_decay)
        elif cfg.train.optim.scheduler == 'cosine':
            scheduler = optim.lr_scheduler.CosineAnnealingWarmRestarts(
                self.optimizer, T_0=cfg.train.optim.T_max, eta_min=cfg.train.optim.lr_min,
                T_mult=cfg.train.optim.T_mult, last_epoch=-1)
        else:
            raise NotImplementedError('Scheduler {} not implemented.'.format(cfg.train.optim.scheduler))
        self.register_scheduler(scheduler)
        
        # freeze backbone params if required
        self.freezeParams(cfg)
        
        # generate loss
        self.loss = get_loss(cfg)
        self.val_loss = get_val_room_retr_loss(cfg)
        
        # log step for training
        if self.cfg.train.log_steps:
            self.log_steps = self.cfg.train.log_steps
        self.snapshot_steps = self.cfg.train.snapshot_steps
        self.logger.info('Initialisation Complete')
        
        # register visualiser
        self.registerVisuliser(cfg)
        
    def registerPatchObjectAlignerFromCfg(self, cfg):
        if not self.cfg.data.img_encoding.use_feature:
            
            backbone_cfg_file = cfg.model.backbone.cfg_file
            # ugly hack to load pretrained model, maybe there is a better way
            backbone_cfg = Config.fromfile(backbone_cfg_file)
            backbone_pretrained_file = cfg.model.backbone.pretrained
            backbone_cfg.model['backbone']['pretrained'] = backbone_pretrained_file
            backbone = build_backbone(backbone_cfg.model['backbone'])
        else:
            backbone = None
        
        # get patch object aligner
        ## 2Dbackbone
        num_reduce = cfg.model.backbone.num_reduce
        backbone_dim = cfg.model.backbone.backbone_dim
        img_rotate = cfg.data.img_encoding.img_rotate
        ## scene graph encoder
        sg_modules = cfg.sgaligner.modules
        sg_rel_dim = cfg.sgaligner.model.rel_dim
        attr_dim = cfg.sgaligner.model.attr_dim
        img_patch_feat_dim = cfg.sgaligner.model.img_patch_feat_dim
        ## encoders
        patch_hidden_dims = cfg.model.patch.hidden_dims
        patch_encoder_dim = cfg.model.patch.encoder_dim
        obj_embedding_dim = cfg.model.obj.embedding_dim
        obj_embedding_hidden_dims = cfg.model.obj.embedding_hidden_dims
        obj_encoder_dim = cfg.model.obj.encoder_dim
        
        drop = cfg.model.other.drop
        
        self.model = PatchSGIEAligner(backbone,
                                num_reduce,
                                backbone_dim,
                                img_rotate, 
                                patch_hidden_dims,
                                patch_encoder_dim,
                                obj_embedding_dim,
                                obj_embedding_hidden_dims,
                                obj_encoder_dim,
                                sg_modules,
                                sg_rel_dim,
                                attr_dim,
                                img_patch_feat_dim,
                                drop,
                                cfg.train.loss.use_temporal)
        
        # load pretrained sgaligner if required
        if cfg.sgaligner.use_pretrained:
            assert os.path.isfile(cfg.sgaligner.pretrained), 'Pretrained sgaligner not found.'
            sgaligner_dict = torch.load(cfg.sgaligner.pretrained, map_location=torch.device('cpu'))
            sgaligner_model = sgaligner_dict['model']
            # remove weights of the last layer
            sgaligner_model.pop('fusion.weight')
            self.model.sg_encoder.load_state_dict(sgaligner_dict['model'], strict=False)
        
        # load snapshot if required
        if cfg.other.use_resume and os.path.isfile(cfg.other.resume):
            self.load_snapshot(cfg.other.resume)
        # model to cuda 
        self.model.to(self.device)
        
        # log
        message = 'Model description:\n' + str(self.model)
        self.logger.info(message)      

    def registerOptim(self, cfg):  
        # only optimise params that require grad
        self.params_register = list( filter(lambda p: p.requires_grad, self.model.parameters()) )
        self.params_register_ids = list(map(id, self.params_register))
        self.params = [{'params' :  self.params_register}]
        self.optimizer = optim.Adam(self.params, lr=cfg.train.optim.lr, 
                                    weight_decay=cfg.train.optim.weight_decay)

    def registerVisuliser(self, cfg):
        pass

    def freezeParams(self, cfg):
        # freeze backbone params if required
        self.free_backbone_epoch = cfg.train.optim.free_backbone_epoch
        if (self.free_backbone_epoch > 0) and not self.cfg.data.img_encoding.use_feature:
        # if (self.free_backbone_epoch > 0):
            for param in self.model.backbone.parameters():
                param.requires_grad = False
        self.free_sgaligner_epoch = cfg.train.optim.free_sgaligner_epoch
        if (self.free_sgaligner_epoch > 0):
            for param in self.model.sg_encoder.parameters():
                param.requires_grad = False
            
    def defreezeBackboneParams(self):
        if not self.cfg.data.img_encoding.use_feature:
            for param in self.model.backbone.parameters():
                param.requires_grad = True

    def defreezeSGAlignerParams(self):
        for param in self.model.sg_encoder.parameters():
            param.requires_grad = True


    def getDataLoader(self, cfg):
        dataset = ScannetPatchObjDataset
        train_dataloader, val_dataloader = get_train_val_data_loader(cfg, dataset)
        return train_dataloader, val_dataloader
    # def getDataLoader(self, cfg):
    #     dataset = ScannetPatchObjDataset
    #     val_dataset, val_dataloader = get_val_dataloader(cfg, dataset)
    #     return val_dataloader, val_dataloader

    def model_forward(self, data_dict):
        if self.cfg.data.img_encoding.use_feature:
            embeddings = self.model.forward_with_patch_features(data_dict)
        else:
            embeddings = self.model(data_dict)
            if self.cfg.data.img_encoding.record_feature:
                patch_raw_features = embeddings['patch_raw_features'].detach().cpu().numpy()
                for batch_i in range(data_dict['batch_size']):
                    file_path = data_dict['patch_features_paths'][batch_i]
                    file_parent_dir = os.path.dirname(file_path)
                    common.ensure_dir(file_parent_dir)
                    np.save(file_path, patch_raw_features[batch_i])   
        return embeddings

    def train_step(self, epoch, iteration, data_dict):
        embeddings = self.model_forward(data_dict)
        loss_dict = self.loss(embeddings, data_dict)
        return embeddings, loss_dict
    
    def after_train_step(self, epoch, iteration, data_dict, output_dict, result_dict):
        # visualize result and save
        if self.cfg.train.use_vis:
            self.result_visualizer.visualize(data_dict, output_dict, epoch)

    def val_step(self, epoch, iteration, data_dict):
        embeddings = self.model_forward(data_dict)
        loss_dict = self.val_loss(embeddings, data_dict)
        return embeddings, loss_dict

    def set_eval_mode(self):
        self.training = False
        self.model.eval()
        self.val_loss.eval()
        torch.set_grad_enabled(False)

    def set_train_mode(self):
        self.training = True
        self.model.train()
        self.loss.train()
        torch.set_grad_enabled(True)
        
    def after_train_epoch(self, epoch):
        if epoch > self.cfg.train.optim.free_backbone_epoch:
            self.defreezeBackboneParams()
        if epoch > self.cfg.train.optim.free_sgaligner_epoch:
            self.defreezeSGAlignerParams()
        
def parse_args(parser=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', dest='config', default='', type=str, help='configuration file name')
    parser.add_argument('--resume', action='store_true', help='resume training')
    parser.add_argument('--snapshot', default=None, help='load from snapshot')
    parser.add_argument('--epoch', type=int, default=None, help='load epoch')
    parser.add_argument('--log_steps', type=int, default=500, help='logging steps')
    parser.add_argument('--local_rank', type=int, default=-1, help='local rank for ddp')

    args = parser.parse_args()
    return parser, args

def main():
    parser, args = parse_args()
    
    config_file = args.config
    cfg = update_config(config, config_file)
    
    # copy config file to out dir
    out_dir = cfg.output_dir
    command = 'cp {} {}'.format(config_file, out_dir)
    subprocess.call(command, shell=True)
    
    # train
    trainer = Trainer(cfg, parser)
    trainer.run()

if __name__ == '__main__':
    main()