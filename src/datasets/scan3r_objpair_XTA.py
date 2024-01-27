import os
import os.path as osp
import comm
import numpy as np
import random
import albumentations as A
import torch
import torch.utils.data as data
from torchvision.transforms import transforms
import cv2
import sys

from yaml import scan
sys.path.append('..')
sys.path.append('../..')
from utils import common, scan3r

def getPatchAnno(gt_anno_2D, patch_w, patch_h, th = 0.5):
    image_h, image_w = gt_anno_2D.shape
    patch_h_size = int(image_h / patch_h)
    patch_w_size = int(image_w / patch_w)
    
    patch_annos = np.zeros((patch_h, patch_w), dtype=np.uint8)
    for patch_h_i in range(patch_h):
        h_start = round(patch_h_i * patch_h_size)
        h_end = round((patch_h_i + 1) * patch_h_size)
        for patch_w_j in range(patch_w):
            w_start = round(patch_w_j * patch_w_size)
            w_end = round((patch_w_j + 1) * patch_w_size)
            patch_size = (w_end - w_start) * (h_end - h_start)
            
            anno = gt_anno_2D[h_start:h_end, w_start:w_end]
            obj_ids, counts = np.unique(anno.reshape(-1), return_counts=True)
            max_idx = np.argmax(counts)
            max_count = counts[max_idx]
            if(max_count > th*patch_size):
                patch_annos[patch_h_i,patch_w_j] = obj_ids[max_idx]
    return patch_annos

class PatchObjectPairXTemporalDataSet(data.Dataset):
    def __init__(self, cfg, split):
        self.cfg = cfg
        
        # undefined patch anno id
        self.undefined = 0
        
        # set random seed
        self.seed = cfg.seed
        random.seed(self.seed)
        
        # sgaliner related cfg
        self.split = split
        self.use_predicted = cfg.sgaligner.use_predicted
        self.sgaliner_model_name = cfg.sgaligner.model_name
        self.scan_type = cfg.sgaligner.scan_type
        
        # data dir
        self.data_root_dir = cfg.data.root_dir
        scan_dirname = '' if self.scan_type == 'scan' else 'out'
        scan_dirname = osp.join(scan_dirname, 'predicted') if self.use_predicted else scan_dirname
        self.scans_dir = osp.join(cfg.data.root_dir, scan_dirname)
        self.scans_files_dir = osp.join(self.scans_dir, 'files')
        self.mode = 'orig' if self.split == 'train' else cfg.sgaligner.val.data_mode
        self.scans_files_dir_mode = osp.join(self.scans_files_dir, self.mode)
        # 3D_obj_embedding_dir
        self.scans_3Dobj_embeddings_dir = osp.join(self.scans_files_dir_mode, "embeddings")
        # 2D images
        self.image_w = self.cfg.data.img.w
        self.image_h = self.cfg.data.img.h
        self.image_resize_w = self.cfg.data.img_encoding.resize_w
        self.image_resize_h = self.cfg.data.img_encoding.resize_h
        self.img_rotate = self.cfg.data.img_encoding.img_rotate
        # 2D_patch_anno_dir
        self.image_patch_w = self.cfg.data.img_encoding.patch_w
        self.image_patch_h = self.cfg.data.img_encoding.patch_h
        self.step = self.cfg.data.img.img_step
        # self.patch_w_size_int = int(self.image_w / self.image_patch_w)
        # self.patch_h_size_int = int(self.image_h / self.image_patch_h)
        self.num_patch = self.image_patch_w * self.image_patch_h
        self.patch_anno_folder_name = "patch_anno_{}_{}".format(self.image_patch_w, self.image_patch_h)
        self.scans_2Dpatch_anno_dir = osp.join(self.scans_files_dir, "patch_anno", self.patch_anno_folder_name)
        # scene_img_dir
        self.scans_scenes_dir = osp.join(self.scans_dir, 'scenes')
        
        # cross scenes cfg
        self.use_cross_scene = cfg.data.cross_scene.use_cross_scene
        self.num_scenes = cfg.data.cross_scene.num_scenes
        self.num_negative_samples = cfg.data.cross_scene.num_negative_samples
        
        # if split is val, then use all object from other scenes as negative samples
        # if room_retrieval, then use load additional data items
        self.room_retrieval = False
        if split == 'val' or split == 'test':
            self.num_negative_samples = -1
            self.room_retrieval = True
            self.room_retrieval_epsilon_th = cfg.val.room_retrieval.epsilon_th
        
        # scans info
        self.temporal = cfg.data.temporal
        self.rescan = cfg.data.rescan
        scan_info_file = osp.join(self.scans_files_dir, '3RScan.json')
        all_scan_data = common.load_json(scan_info_file)
        self.refscans2scans = {}
        self.scans2refscans = {}
        self.all_scans_split = []
        for scan_data in all_scan_data:
            ref_scan_id = scan_data['reference']
            self.refscans2scans[ref_scan_id] = [ref_scan_id]
            self.scans2refscans[ref_scan_id] = ref_scan_id
            for scan in scan_data['scans']:
                self.refscans2scans[ref_scan_id].append(scan['reference'])
                self.scans2refscans[scan['reference']] = ref_scan_id
        self.resplit = "resplit_" if cfg.data.resplit else ""
        ref_scans_split = np.genfromtxt(osp.join(self.scans_files_dir_mode, '{}_{}scans.txt'.format(split, self.resplit)), dtype=str)
        self.all_scans_split = []
        ## get all scans within the split(ref_scan + rescan)
        for ref_scan in ref_scans_split:
            self.all_scans_split += self.refscans2scans[ref_scan]
        if self.rescan:
            self.scan_ids = self.all_scans_split
        else:
            self.scan_ids = ref_scans_split
            
        # load 2D image paths
        self.image_paths = {}
        for scan_id in self.scan_ids:
            self.image_paths[scan_id] = scan3r.load_frame_paths(self.scans_dir, scan_id, self.step)
            
        # load 2D patch features if use pre-calculated feature
        self.patch_feature_folder = osp.join(self.scans_files_dir, self.cfg.data.img_encoding.feature_dir)
        self.patch_features = {}
        if self.cfg.data.img_encoding.use_feature:
            for scan_id in self.scan_ids:
                self.patch_features[scan_id] = scan3r.load_patch_features(
                    self.data_root_dir, self.patch_feature_folder, scan_id, self.step)
        self.patch_features_paths = {}
        if self.cfg.data.img_encoding.record_feature:
            common.ensure_dir(self.patch_feature_folder)
            for scan_id in self.scan_ids:
                self.patch_features_paths[scan_id] = scan3r.load_patch_feature_paths(
                    self.data_root_dir, self.patch_feature_folder, scan_id, self.step)
                
        # load 2D gt obj id annotation
        self.gt_2D_anno_folder = osp.join(self.scans_files_dir, 'gt_projection/obj_id_pkl')
        self.obj_2D_annos = {}
        for scan_id in self.scan_ids:
            anno_2D_file = osp.join(self.gt_2D_anno_folder, "{}.pkl".format(scan_id))
            self.obj_2D_annos[scan_id] = common.load_pkl_data(anno_2D_file)
                
        # data augmentation
        self.use_aug = cfg.train.data_aug.use_aug
        ## 2D image
        self.img_rot = cfg.train.data_aug.img.rotation
        self.img_Hor_flip = cfg.train.data_aug.img.horizontal_flip
        self.img_Ver_flip = cfg.train.data_aug.img.vertical_flip
        self.img_jitter = cfg.train.data_aug.img.color
        self.trans_2D = A.Compose(
            transforms=[
                A.VerticalFlip(p=self.img_Ver_flip),
                A.HorizontalFlip(p=self.img_Hor_flip),
                A.Rotate(limit=self.img_rot, p=0.8, 
                        interpolation=cv2.INTER_NEAREST,
                        border_mode=cv2.BORDER_CONSTANT, value=0)]
        )
        color_jitter = self.img_jitter
        self.brightness_2D = A.ColorJitter(
            brightness=color_jitter, contrast=color_jitter, saturation=color_jitter, hue=color_jitter)
        
        ## 3D obj TODO
        
        # load 3D obj embeddings
        self.obj_3D_embeddings = {}
        for scan_id in self.all_scans_split:
            embedding_file = osp.join(self.scans_3Dobj_embeddings_dir, "{}.pkl".format(scan_id))
            embeddings = common.load_pkl_data(embedding_file)
            obj_3D_embeddings_scan = embeddings['obj_embeddings']
            self.obj_3D_embeddings[scan_id] = obj_3D_embeddings_scan
            
        # load 3D obj semantic annotations
        self.obj_3D_anno = {}
        self.objs_config_file = osp.join(self.scans_files_dir, 'objects.json')
        objs_configs = common.load_json(self.objs_config_file)['scans']
        scans_objs_info = {}
        for scan_item in objs_configs:
            scan_id = scan_item['scan']
            objs_info = scan_item['objects']
            scans_objs_info[scan_id] = objs_info
        for scan_id in self.all_scans_split:
            self.obj_3D_anno[scan_id] = {}
            for obj_item in scans_objs_info[scan_id]:
                obj_id = int(obj_item['id'])
                obj_nyu_category = int(obj_item['nyu40'])
                self.obj_3D_anno[scan_id][obj_id] = (scan_id, obj_id, obj_nyu_category)
            
        # generate data items given multiple scans
        self.data_items = self.generateDataItems()
        
    # sample objects and other scenes for each data item 
    def sampleCrossScenes(self, scan_id, num_scenes, num_objects):
        
        candidate_scans = []
        scans_same_scene = self.refscans2scans[self.scans2refscans[scan_id]]
        # sample other scenes
        for scan in self.all_scans_split:
            if scan not in scans_same_scene:
                candidate_scans.append(scan)
        sampled_scans = random.sample(candidate_scans, num_scenes)
        
        # sample objects of sampled scenes
        candidate_objs = []
        for sampled_scan_id in sampled_scans:
            # record scan_id, obj_id, category for each candidate object
            # some obj may not have embeddings, thus only sample from objs with embeddings
            candidate_objs += [self.obj_3D_anno[sampled_scan_id][obj_id] 
                               for obj_id in self.obj_3D_embeddings[sampled_scan_id]]
        sampled_objs = []
        if num_objects >= 0:
            if num_objects < len(candidate_objs):
                sampled_objs = random.sample(candidate_objs, num_objects)
            else:
                # sample all objects if not enough objects
                sampled_objs = candidate_objs
        else:
            sampled_objs = candidate_objs
        return sampled_objs
    
    # sample cross time for each data item
    def sampleCrossTime(self, scan_id):
        candidate_scans = []
        ref_scan = self.scans2refscans[scan_id]
        for scan in self.refscans2scans[ref_scan]:
            if scan != scan_id:
                candidate_scans.append(scan)
        if len(candidate_scans) == 0:
            return None
        else:
            sampled_scan = random.sample(candidate_scans, 1)[0]
            return sampled_scan
            
    def generateDataItems(self):
        data_items = []
        # iterate over scans
        for scan_id in self.scan_ids:
            # obj_3D_embeddings_scan = self.obj_3D_embeddings[scan_id]
            # iterate over images
            obj_2D_anno = self.obj_2D_annos[scan_id]
            image_paths = self.image_paths[scan_id]
            for frame_idx in image_paths:
                data_item_dict = {}
                # 2D info
                if self.cfg.data.img_encoding.use_feature:
                    data_item_dict['patch_features'] = self.patch_features[scan_id][frame_idx]
                else:
                    data_item_dict['img_path'] = image_paths[frame_idx]
                data_item_dict['obj_2D_anno'] = obj_2D_anno[frame_idx]
                data_item_dict['frame_idx'] = frame_idx
                # 3D info
                data_item_dict['scan_id'] = scan_id
                data_items.append(data_item_dict)
                # sample cross scenes
                if self.use_cross_scene:
                    sampled_objs = self.sampleCrossScenes(scan_id, self.num_scenes, self.num_negative_samples)
                    data_item_dict['obj_across_scenes'] = sampled_objs
                else:
                    data_item_dict['obj_across_scenes'] = []

                # temporal data item, across scan time 
                if self.temporal:
                    data_item_dict['scan_id_across_time'] = self.sampleCrossTime(scan_id)
                    
        # if debug with single scan
        if self.cfg.mode == "debug_few_scan":
            return data_items[:1]
        return data_items
    
    def dataItem2DataDict(self, data_item, temporal=False):
        if temporal and data_item['scan_id_across_time'] is None:
            return None
        
        # 3D object embeddings
        img_scan_id = data_item['scan_id']
        scan_id = data_item['scan_id'] if not temporal else data_item['scan_id_across_time']
        obj_3D_embeddings = self.obj_3D_embeddings[scan_id]
        num_objs = len(obj_3D_embeddings)
        num_objs_across_scenes = len(data_item['obj_across_scenes'])
        obj_3D_id2idx = {} # only for objs in current scene
        obj_3D_idx2info = {} # for objs in current scene and other scenes
        candata_scan_obj_idxs = {}
        idx = 0
        obj_3D_embeddings_list = []
        ## objects within current scene
        for obj_id in  obj_3D_embeddings:
            obj_3D_id2idx[obj_id] = idx
            obj_3D_idx2info[idx] = self.obj_3D_anno[scan_id][obj_id]
            obj_3D_embeddings_list.append(obj_3D_embeddings[obj_id])
            if scan_id not in candata_scan_obj_idxs:
                candata_scan_obj_idxs[scan_id] = []
            candata_scan_obj_idxs[scan_id].append(idx)
            idx += 1 
        obj_3D_embeddings_arr = np.array(obj_3D_embeddings_list)
        ## objects across scenes
        obj_3D_across_scnes_embeddings = []
        for obj_info in data_item['obj_across_scenes']:
            obj_3D_idx2info[idx] = obj_info
            scan_id_across_scenes = obj_info[0]
            obj_id_across_scenes = obj_info[1]
            obj_3D_across_scnes_embeddings.append(
                self.obj_3D_embeddings[scan_id_across_scenes][obj_id_across_scenes])
            if scan_id_across_scenes not in candata_scan_obj_idxs:
                candata_scan_obj_idxs[scan_id_across_scenes] = []
            candata_scan_obj_idxs[scan_id_across_scenes].append(idx)
            idx += 1
            
        obj_3D_across_scnes_embeddings_arr = np.array(obj_3D_across_scnes_embeddings)
        if num_objs_across_scenes > 0:
            obj_3D_embeddings_arr = np.concatenate(
                [obj_3D_embeddings_arr, obj_3D_across_scnes_embeddings_arr], axis=0)
            
        # 2D data
        ## 2D path features
        frame_idx = data_item['frame_idx']
        if self.cfg.data.img_encoding.use_feature:
            patch_features = data_item['patch_features']
        else:
            # img data
            img_path = data_item['img_path']
            img = cv2.imread(img_path,cv2.IMREAD_UNCHANGED) # type: ignore
            img = cv2.resize(img, (self.image_resize_w, self.image_resize_h),  # type: ignore
                            interpolation=cv2.INTER_LINEAR) # type: ignore
            if self.img_rotate:
                img = img.transpose(1, 0, 2)
                img = np.flip(img, 1)
        ## 2D gt obj anno
        obj_2D_anno = data_item['obj_2D_anno']
        obj_2D_anno = cv2.resize(obj_2D_anno, (self.image_resize_w, self.image_resize_h),  # type: ignore
                        interpolation=cv2.INTER_NEAREST) # type: ignore
        if self.img_rotate:
            obj_2D_anno = obj_2D_anno.transpose(1, 0)
            obj_2D_anno = np.flip(obj_2D_anno, 1)
            
        ## data augmentation
        if self.use_aug and self.split == 'train':
            augments_2D = self.trans_2D(image=img, mask=obj_2D_anno)
            img = augments_2D['image']
            obj_2D_anno = augments_2D['mask']
            img = self.brightness_2D(image=img)['image']
            
        # 2D patch anno
        if self.img_rotate:
            patch_h = self.image_patch_w
            patch_w = self.image_patch_h
        else:
            patch_h = self.image_patch_h
            patch_w = self.image_patch_w
        obj_2D_patch_anno = getPatchAnno(obj_2D_anno, patch_w, patch_h, 0.3)

            
        obj_2D_patch_anno_flatten = obj_2D_patch_anno.reshape(-1)
        
        # generate relationship matrix for contrast learning 
        ## From 2D to 3D, denote as e1i_matrix, e1j_matrix, e2j_matrix      
        ## e1i_matrix,(num_patch, num_3D_obj), record 2D-3D patch-object pairs
        ## e2j_matrix,(num_patch, num_3D_obj), record 2D-3D patch-object unpairs
        e1i_matrix = np.zeros( (self.num_patch, num_objs+num_objs_across_scenes), dtype=np.uint8)
        e2j_matrix = np.ones( (self.num_patch, num_objs+num_objs_across_scenes), dtype=np.uint8)
        for patch_h_i in range(patch_h):
            patch_h_shift = patch_h_i*patch_w
            for patch_w_j in range(patch_w):
                obj_id = obj_2D_patch_anno[patch_h_i, patch_w_j]
                if obj_id != self.undefined and (obj_id in obj_3D_id2idx):
                    obj_idx = obj_3D_id2idx[obj_id]
                    e1i_matrix[patch_h_shift+patch_w_j, obj_idx] = 1 # mark 2D-3D patch-object pairs
                    e2j_matrix[patch_h_shift+patch_w_j, obj_idx] = 0 # mark 2D-3D patch-object unpairs
                
        ## e1j_matrix, (num_patch, num_patch), mark unpaired patch-patch pair for image patches
        e1j_matrix = np.zeros( (self.num_patch, self.num_patch), dtype=np.uint8)
        for patch_h_i in range(patch_h):
            patch_h_shift = patch_h_i*patch_w
            for patch_w_j in range(patch_w):
                obj_id = obj_2D_patch_anno[patch_h_i, patch_w_j]
                if obj_id != self.undefined and obj_id in obj_3D_id2idx:
                    e1j_matrix[patch_h_shift+patch_w_j, :] = np.logical_and(
                        obj_2D_patch_anno_flatten != self.undefined, obj_2D_patch_anno_flatten != obj_id
                    )
                else:
                     e1j_matrix[patch_h_shift+patch_w_j, :] = 1

        ## From 3D to 2D, denote as f1i_matrix, f1j_matrix, f2j_matrix
        ## f1i_matrix = e1i_matrix.T, thus skip
        ## f2j_matrix = e2j_matrix.T, thus skip
        ## f1j_matrix
        obj_cates = [obj_3D_idx2info[obj_idx][2] for obj_idx in range(len(obj_3D_idx2info))]
        obj_cates_arr = np.array(obj_cates)
        f1j_matrix = obj_cates_arr.reshape(1, -1) != obj_cates_arr.reshape(-1, 1)
            
        data_dict = {}
        # frame info
        data_dict['scan_id'] = scan_id
        data_dict['img_scan_id'] = data_item['scan_id']
        data_dict['frame_idx'] = frame_idx
        if self.cfg.data.img_encoding.use_feature:
            data_dict['patch_features'] = patch_features
        else:
            data_dict['image'] = img
            if self.cfg.data.img_encoding.record_feature:
                data_dict['patch_features_path'] = self.patch_features_paths[img_scan_id][frame_idx]
        # pano annotations
        # data_dict['patch_anno'] = obj_2D_patch_anno
        # obj info
        data_dict['num_objs'] = len(obj_3D_id2idx)
        data_dict['num_objs_across_scenes'] = num_objs_across_scenes
        data_dict['obj_3D_id2idx'] = obj_3D_id2idx
        data_dict['obj_3D_idx2info'] = obj_3D_idx2info
        data_dict['obj_3D_embeddings_arr'] = obj_3D_embeddings_arr
        if self.room_retrieval:
            data_dict['candata_scan_obj_idxs'] = candata_scan_obj_idxs
        # pairs info
        data_dict['e1i_matrix'] = e1i_matrix
        data_dict['e1j_matrix'] = e1j_matrix
        data_dict['e2j_matrix'] = e2j_matrix
        data_dict['f1j_matrix'] = f1j_matrix
        data_dict['obj_2D_patch_anno_flatten'] = obj_2D_patch_anno_flatten
        return data_dict
    
    def collateBatchDicts(self, batch_dicts, temporal=False):
        if temporal:
            batch = [batch_dict['data_dict_across_time'] for batch_dict in batch_dicts if batch_dict is not None]
        else:
            batch = [batch_dict for batch_dict in batch_dicts if batch_dict is not None]
        
        batch_size = len(batch)
        data_dict = {}
        data_dict['batch_size'] = batch_size
        # frame info 
        data_dict['scan_ids'] = np.stack([data['scan_id'] for data in batch])
        data_dict['img_scan_ids'] = np.stack([data['img_scan_id'] for data in batch])
        data_dict['frame_idxs'] = np.stack([data['frame_idx'] for data in batch])
        if self.cfg.data.img_encoding.use_feature:
            patch_features_batch = np.stack([data['patch_features'] for data in batch]) # (B, P_H, P_W, D)
            data_dict['patch_features'] = torch.from_numpy(patch_features_batch).float() # (B, H, W, C)
        else:
            images_batch = np.stack([data['image'] for data in batch])
            data_dict['images'] = torch.from_numpy(images_batch).float() # (B, H, W, C)
            if self.cfg.data.img_encoding.record_feature:
                data_dict['patch_features_paths'] = [data['patch_features_path'] for data in batch]
        
        # obj info; as obj number is different for each batch, we need to create a list
        data_dict['num_objs'] = [data['num_objs'] for data in batch]
        data_dict['num_objs_across_scenes'] = [data['num_objs_across_scenes'] for data in batch]
        data_dict['obj_3D_id2idx'] = [data['obj_3D_id2idx'] for data in batch]
        data_dict['obj_3D_idx2info_list'] = \
            [data['obj_3D_idx2info'] for data in batch]
        obj_3D_embeddings_list = [data['obj_3D_embeddings_arr'] for data in batch]
        data_dict['obj_3D_embeddings_list'] = [torch.from_numpy(obj_3D_embeddings).float()
                                               for obj_3D_embeddings in obj_3D_embeddings_list] # B - [N_O, N_Obj_Embed]
        if self.room_retrieval:
            data_dict['candate_scan_obj_idxs_list'] = []
            for data in batch:
                candate_scan_obj_idxs = {}
                for scan_id in data['candata_scan_obj_idxs']:
                   candate_scan_obj_idxs[scan_id] = \
                        torch.Tensor(data['candata_scan_obj_idxs'][scan_id]).long()
                data_dict['candate_scan_obj_idxs_list'].append(candate_scan_obj_idxs)
        # pairs info
        data_dict['e1i_matrix_list'] = [ torch.from_numpy(data['e1i_matrix']) for data in batch]  # B - [N_P, N_O]
        data_dict['e1j_matrix_list'] = [ torch.from_numpy(data['e1j_matrix']) for data in batch]  # B - [N_P, N_P]
        data_dict['e2j_matrix_list'] = [ torch.from_numpy(data['e2j_matrix']) for data in batch]  # B - [N_P, N_O]
        data_dict['f1j_matrix_list'] = [ torch.from_numpy(data['f1j_matrix']) for data in batch]  # B - [N_O, N_O]
        data_dict['obj_2D_patch_anno_flatten_list'] = \
            [ torch.from_numpy(data['obj_2D_patch_anno_flatten']) for data in batch] # B - [N_P]
        
        if len(batch) > 0:
            return data_dict
        else:
            return None
    
    def __getitem__(self, idx):
        data_item = self.data_items[idx]
        data_dict = self.dataItem2DataDict(data_item)
        if self.temporal:
            data_dict_across_time = self.dataItem2DataDict(data_item, temporal=True)
            data_dict['data_dict_across_time'] = data_dict_across_time
        return data_dict
    
    def collate_fn(self, batch):
        data_dict = {}
        data_dict['non_temporal'] = self.collateBatchDicts(batch)
        if self.temporal:
            data_dict_across_time = self.collateBatchDicts(batch, temporal=True)
            data_dict['temporal'] = data_dict_across_time
        return data_dict
        
    def __len__(self):
        return len(self.data_items)
    
if __name__ == '__main__':
    # TODO  check the correctness of dataset 
    from configs import config, update_config
    cfg_file = "/home/yang/big_ssd/Scan3R/VLSG/implementation/week9/tranval_Npair_aug/Npair_cfg.yaml"
    cfg = update_config(config, cfg_file)
    scan3r_ds = PatchObjectPairXTemporalDataSet(cfg, split='train')
    print(len(scan3r_ds))
    batch = [scan3r_ds[0], scan3r_ds[0]]
    data_batch = scan3r_ds.collate_fn(batch)
    breakpoint=None