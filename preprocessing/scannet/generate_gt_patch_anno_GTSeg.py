import os
import os.path as osp
from re import I
import subprocess
import sys
from tracemalloc import start
from yacs.config import CfgNode as CN
from scipy import stats
from yaml import scan
vlsg_dir = osp.dirname(osp.dirname(osp.dirname(osp.abspath(__file__))))
sys.path.insert(0, vlsg_dir)
from utils import common, scannet_utils

import numpy as np
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torchvision import transforms as T
from torchvision import transforms as tvf
import torchvision.transforms as T
import matplotlib.pyplot as plt
from PIL import Image
from tqdm.auto import tqdm
import cv2

from plyfile import PlyData, PlyElement
import open3d as o3d
import open3d.core as o3c
from concurrent.futures import ThreadPoolExecutor

device = o3c.Device("CUDA", 0)
point_dtype = o3c.float32
dist_th = 0.05
mesh_file_name = "_vh_clean_2.labels.ply"
sg_pred_ply = "scene_graph_fusion/inseg_filtered.ply"

class ScannetPatchAnnoGen():
    def __init__(self, cfg, split):
        self.cfg = cfg
        
        # scannet scans info
        self.split = split
        scans_info_file = osp.join(cfg.data.root_dir, 'files', 'scans_{}.pkl'.format(split))
        self.rooms_info = common.load_pkl_data(scans_info_file)
        self.scan_ids = []
        self.scan2room = {}
        for room_id in self.rooms_info:
            self.scan_ids += self.rooms_info[room_id]
            for scan_id in self.rooms_info[room_id]:
                self.scan2room[scan_id] = room_id
        # out dir
        gt_patch_anno_name = cfg.data.gt_patch
        self.gt_patch = osp.join(cfg.data.root_dir, "files", gt_patch_anno_name)
        self.gt_anno_vis = osp.join(cfg.data.root_dir, "files", gt_patch_anno_name+"_vis")
        common.ensure_dir(self.gt_patch)
        common.ensure_dir(self.gt_anno_vis)
        
        # get image info
        self.data_split_dir = osp.join(cfg.data.root_dir, split)
        self.img_step = cfg.data.img_step
        ## img_cdg inference config
        self.img_w = self.cfg.data.img_w
        self.img_h = self.cfg.data.img_h
        self.image_resize_w = self.cfg.data.resize_w
        self.image_resize_h = self.cfg.data.resize_h
        self.resize_scale_w = self.image_resize_w / self.img_w
        self.resize_scale_h = self.image_resize_h / self.img_h
        self.patch_w = self.cfg.data.patch_w
        self.patch_h = self.cfg.data.patch_h
        ## img paths
        self.img_paths = {}
        ## img poses
        self.img_poses = {}
        ## color intrinsic
        self.color_intrinsics = {}
        for scan_id in self.scan_ids:
            img_paths = scannet_utils.load_frame_paths(self.data_split_dir, scan_id, self.img_step)
            self.img_paths[scan_id] = img_paths
            
            poses = scannet_utils.load_frame_poses(self.data_split_dir, scan_id, self.img_step)
            self.img_poses[scan_id] = poses
            
            intrinsic = scannet_utils.load_frame_intrinsics(self.data_split_dir, scan_id, 'color')
            intrinsic[0, :] *= self.resize_scale_w
            intrinsic[1, :] *= self.resize_scale_h
            self.color_intrinsics[scan_id] = intrinsic
        
        # 3D files
        self.mesh_files = {}
        self.segment_files = {}
        self.label_files = {}
        for scan_id in self.scan_ids:
            scene_folder = osp.join(self.data_split_dir, scan_id)
            segment_file = osp.join(scene_folder, '{}_vh_clean_2.0.010000.segs.json'.format(scan_id))
            label_file = osp.join(scene_folder, '{}_vh_clean.aggregation.json'.format(scan_id))
            mesh_file = osp.join(scene_folder, scan_id+mesh_file_name)
            self.segment_files[scan_id] = segment_file
            self.label_files[scan_id] = label_file
            self.mesh_files[scan_id] = mesh_file
            
        # label map
        label_map_file = osp.join(cfg.data.root_dir, 'files', 'scannetv2-labels.combined.tsv')
        self.label_map = scannet_utils.read_label_mapping(label_map_file)
        
    def generateGTPatchAnno(self):
        scan_ids = self.scan_ids
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(tqdm(executor.map(self.generateGTPatchAnnoEachScan, scan_ids), 
                                total=len(scan_ids), leave=False))


    def generateGTPatchAnnoEachScan(self, scan_id):
        gt_patch_anno = {}
        
        # load mesh
        mesh = o3d.io.read_triangle_mesh(self.mesh_files[scan_id])
        mesh_vertices = np.asarray(mesh.vertices)
        vertices_obj_ids = np.zeros(mesh_vertices.shape[0], dtype=np.int8)
        
        segment_file = self.segment_files[scan_id]
        label_file = self.label_files[scan_id]
        segsIndices = scannet_utils.read_segmentation(segment_file)
        objects_info = scannet_utils.read_obj_info_nyu40(label_file, self.label_map)
        for obj_id, obj_info in objects_info.items():
            segs = obj_info['segs']
            for seg_id in segs:
                seg_mask = segsIndices == seg_id
                vertices_obj_ids[seg_mask] = obj_id
        
        # get patch anno for each frame by raycast
        ## mesh info
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
        mesh_triangles_arr = np.asarray(mesh.triangles)
        mesh_colors = np.asarray(mesh.vertex_colors)*255.0
        mesh_colors = mesh_colors.round()
        ## raycast info 
        ray_w, ray_h = self.image_resize_w, self.image_resize_h
        intrinsic = self.color_intrinsics[scan_id]
        ## vis, each scan has one vis image
        vis = True
        for frame_idx in self.img_paths[scan_id]:
            if frame_idx not in self.img_poses[scan_id]:
                continue
            pose_W_C = self.img_poses[scan_id][frame_idx]
            pose_C_W = np.linalg.inv(pose_W_C)
            ray_idxs, hit_points_idx = scannet_utils.raycastImgFromMesh(
                scene, mesh_triangles_arr, ray_w, ray_h, intrinsic, pose_C_W)
            obj_id_map = np.zeros((self.image_resize_h, self.image_resize_w), dtype=np.int32)
            obj_id_map[ray_idxs] = vertices_obj_ids[hit_points_idx]
        
            patch_anno = scannet_utils.getPatchAnno(obj_id_map, self.patch_w, self.patch_h, 0.2)
            gt_patch_anno[frame_idx] = patch_anno
            
            if vis:
                vis = False
                vis_img = np.zeros((self.image_resize_h, self.image_resize_w, 3), dtype=np.uint8)
                
                obj_id2color = {}
                obj_labels_unique = np.unique(vertices_obj_ids)
                for obj_id in obj_labels_unique:
                    colors_per_obj = mesh_colors[vertices_obj_ids==obj_id]
                    ### get mode color
                    mode_color = stats.mode(colors_per_obj, axis=0)[0][0]
                    obj_id2color[obj_id] = mode_color
                    vis_img[obj_id_map==obj_id]= obj_id2color[obj_id]
                ### vis file
                vis_folder = osp.join(self.gt_anno_vis, scan_id)
                common.ensure_dir(vis_folder)
                vis_file = osp.join(vis_folder, "{}_label.jpg".format(frame_idx))
                vis_img_bgr = cv2.cvtColor(vis_img, cv2.COLOR_RGB2BGR)
                cv2.imwrite(vis_file, vis_img_bgr)
                ### obj_map
                obj_map_img_file = osp.join(vis_folder, "{}_obj_map.jpg".format(frame_idx))
                cv2.imwrite(obj_map_img_file, obj_id_map)
                ### rgb file for better vis
                target_rgb_file = osp.join(vis_folder, "{}_rgb.jpg".format(frame_idx))
                src_rgb_file = self.img_paths[scan_id][frame_idx]
                subprocess.run(["cp", src_rgb_file, target_rgb_file])
        out_file = osp.join(self.gt_patch, '{}.pkl'.format(scan_id))
        common.write_pkl_data(gt_patch_anno, out_file)
    
    
def main():
    cfg_file = "/home/yang/big_ssd/Scan3R/VLSG/preprocessing/scannet/scannet_patch_anno_GTSeg.yaml"
    cfg = CN()
    cfg.defrost()
    cfg.set_new_allowed(True)
    cfg.merge_from_file(cfg_file)
    
    scannet_dino_generator = ScannetPatchAnnoGen(cfg, split='test')
    scannet_dino_generator.generateGTPatchAnno()
    
if __name__ == '__main__':
    main()