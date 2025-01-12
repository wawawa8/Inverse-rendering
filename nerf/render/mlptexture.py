# Copyright (c) 2020-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

import torch
import tinycudann as tcnn
import numpy as np

from models.tensoRF import TensorVM, TensorCP
from utils import *
from geometry import utils
from models.tensoIR.relight_utils import compute_secondary_shading_effects, GGX_specular, linear2srgb_torch

#######################################################################################################################################################
# Small MLP using PyTorch primitives, internal helper class
#######################################################################################################################################################

class _MLP(torch.nn.Module):
    def __init__(self, cfg, loss_scale=1.0):
        super(_MLP, self).__init__()
        self.loss_scale = loss_scale
        net = (torch.nn.Linear(cfg['n_input_dims'], cfg['n_neurons'], bias=False), torch.nn.ReLU())
        for i in range(cfg['n_hidden_layers']-1):
            net = net + (torch.nn.Linear(cfg['n_neurons'], cfg['n_neurons'], bias=False), torch.nn.ReLU())
        net = net + (torch.nn.Linear(cfg['n_neurons'], cfg['n_output_dims'], bias=False),)
        self.net = torch.nn.Sequential(*net).cuda()

        self.net.apply(self._init_weights)

        if self.loss_scale != 1.0:
            self.net.register_full_backward_hook(lambda module, grad_i, grad_o: (grad_i[0] * self.loss_scale, ))

    def forward(self, x):
        return self.net(x.to(torch.float32))

    @staticmethod
    def _init_weights(m):
        if type(m) == torch.nn.Linear:
            torch.nn.init.kaiming_uniform_(m.weight, nonlinearity='relu')
            if hasattr(m.bias, 'data'):
                m.bias.data.fill_(0.0)

#######################################################################################################################################################
# Outward visible MLP class
#######################################################################################################################################################

class MLPTexture3D(torch.nn.Module):
    def __init__(self, AABB, channels = 3, internal_dims = 32, hidden = 2, min_max = None):
        super(MLPTexture3D, self).__init__()

        self.channels = channels
        self.internal_dims = internal_dims
        self.AABB = AABB
        self.min_max = min_max

        # Setup positional encoding, see https://github.com/NVlabs/tiny-cuda-nn for details
        desired_resolution = 4096
        base_grid_resolution = 16
        num_levels = 16
        per_level_scale = np.exp(np.log(desired_resolution / base_grid_resolution) / (num_levels-1))

        enc_cfg =  {
            "otype": "HashGrid",
            "n_levels": num_levels,
            "n_features_per_level": 2,
            "log2_hashmap_size": 19,
            "base_resolution": base_grid_resolution,
            "per_level_scale" : per_level_scale
	    }

        gradient_scaling = 128.0
        self.encoder = tcnn.Encoding(3, enc_cfg)
        self.encoder.register_full_backward_hook(lambda module, grad_i, grad_o: (grad_i[0] / gradient_scaling, ))

        # Setup MLP
        mlp_cfg = {
            "n_input_dims" : self.encoder.n_output_dims,
            "n_output_dims" : self.channels,
            "n_hidden_layers" : hidden,
            "n_neurons" : self.internal_dims
        }
        self.net = _MLP(mlp_cfg, gradient_scaling)
        print("Encoder output: %d dims" % (self.encoder.n_output_dims))

        # Setup Neural Shader
        shader_cfg = {
            "n_input_dims" : self.channels+3,
            "n_output_dims" : 3,
            "n_hidden_layers" : hidden,
            "n_neurons" : self.internal_dims*2
        }
        self.neural_shader = _MLP(shader_cfg)

    # Sample texture at a given location
    def sample(self, texc):
        _texc = (texc.view(-1, 3) - self.AABB[0][None, ...]) / (self.AABB[1][None, ...] - self.AABB[0][None, ...])
        _texc = torch.clamp(_texc, min=0, max=1)

        p_enc = self.encoder(_texc.contiguous())
        out = self.net.forward(p_enc)

        # Sigmoid limit and scale to the allowed range
        # out = torch.sigmoid(out) * (self.min_max[1][None, :] - self.min_max[0][None, :]) + self.min_max[0][None, :]

        return out.view(*texc.shape[:-1], self.channels) # Remap to [n, h, w, c]

    def neural_shade(self, features, viewdirs):
        return self.neural_shader(torch.cat([features, viewdirs], dim=-1))

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

    def cleanup(self):
        tcnn.free_temporary_memory()

#######################################################################################################################################################
# Outward visible neural texture MLP class
#######################################################################################################################################################

def positional_encoding(positions, freqs):
    freq_bands = (2**torch.arange(freqs).float()).to(positions.device)  # (F,)
    pts = (positions[..., None] * freq_bands).reshape(
        positions.shape[:-1] + (freqs * positions.shape[-1], ))  # (..., DF)
    pts = torch.cat([torch.sin(pts), torch.cos(pts)], dim=-1)
    # import ipdb; ipdb.set_trace()
    return pts

class MLPNeuralTex(torch.nn.Module):
    def __init__(self, AABB, channels = 3, internal_dims = 32, hidden = 2, pospe=4, feape = 4, viewpe = 0):
        super(MLPNeuralTex, self).__init__()

        self.channels = channels
        self.internal_dims = internal_dims
        self.AABB = AABB
        self.pospe = pospe
        self.feape = feape
        self.viewpe = viewpe

        gradient_scaling = 128.0

        # Setup MLP
        mlp_cfg = {
            "n_input_dims" : 2*pospe*3,
            "n_output_dims" : self.channels,
            "n_hidden_layers" : 6,
            "n_neurons" : 128
        }
        self.net = _MLP(mlp_cfg, gradient_scaling)
        # print("Encoder output: %d dims" % (self.encoder.n_output_dims))

        # Setup Neural Shader
        shader_cfg = {
            "n_input_dims" : self.channels+2*self.channels*feape+3+2*viewpe*3,
            "n_output_dims" : 3,
            "n_hidden_layers" : hidden,
            "n_neurons" : self.internal_dims*2
        }
        self.neural_shader = _MLP(shader_cfg)

        print("net", self.net)
        print("neural_shader", self.neural_shader)

    # Sample texture at a given location
    def sample(self, texc):
        _texc = (texc.view(-1, 3) - self.AABB[0][None, ...]) / (self.AABB[1][None, ...] - self.AABB[0][None, ...])
        _texc = torch.clamp(_texc, min=0, max=1)

        p_enc = positional_encoding(_texc.contiguous(), self.pospe)
        out = self.net.forward(p_enc)

        # out = self.net.forward(_texc.contiguous())

        return out.view(*texc.shape[:-1], self.channels) # Remap to [n, h, w, c]

    def neural_shade(self, features, viewdirs):
        if self.feape != 0:
            features_pe = positional_encoding(features, self.feape)
            features = torch.cat((features, features_pe), dim=3)
        if self.viewpe != 0:
            viewdirs_pe = positional_encoding(viewdirs, self.viewpe)
            viewdirs = torch.cat((viewdirs, viewdirs_pe), dim=3)
        texc = torch.cat((features, viewdirs), dim=-1)
        return torch.sigmoid(self.neural_shader(texc))

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

#######################################################################################################################################################
# Outward visible neural vertex texture class
#######################################################################################################################################################

class VertNeuralTex(torch.nn.Module):
    def __init__(self, AABB, n_verts, channels = 3, internal_dims = 32, hidden = 2, feape = 4, viewpe = 0):
        super(VertNeuralTex, self).__init__()

        self.n_verts = n_verts
        self.channels = channels
        self.internal_dims = internal_dims
        self.AABB = AABB
        self.feape = feape
        self.viewpe = viewpe

        self.feats = torch.nn.Parameter(torch.zeros((n_verts, channels), device='cuda'))

        # Setup Neural Shader
        shader_cfg = {
            "n_input_dims" : self.channels+2*self.channels*feape+3+2*viewpe*3,
            # "n_input_dims" : self.channels+2*self.channels*feape,

            "n_output_dims" : 3,
            "n_hidden_layers" : hidden,
            "n_neurons" : 128
        }
        self.neural_shader = _MLP(shader_cfg)

        print("neural_shader", self.neural_shader)

    def neural_shade(self, features, viewdirs):
        if self.feape != 0:
            features_pe = positional_encoding(features, self.feape)
            features = torch.cat((features, features_pe), dim=3)
        if self.viewpe != 0:
            viewdirs_pe = positional_encoding(viewdirs, self.viewpe)
            viewdirs = torch.cat((viewdirs, viewdirs_pe), dim=3)
        texc = torch.cat((features, viewdirs), dim=-1)
        # texc = features
        return torch.sigmoid(self.neural_shader(texc))

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

#######################################################################################################################################################
# Outward visible spherical harmonics class
#######################################################################################################################################################

class VertSHTex(torch.nn.Module):
    def __init__(self, n_verts, deg = 2, channels = 3, internal_dims = 32, hidden = 2, feape = 4, viewpe = 0, init_feats = None):
        super(VertSHTex, self).__init__()

        self.n_verts = n_verts
        self.channels = channels
        self.internal_dims = internal_dims
        self.feape = feape

        if init_feats is None:
            self.feats = torch.nn.Parameter(torch.randn((n_verts, channels), device='cuda'))
        else:
            self.feats = torch.nn.Parameter(init_feats)

        # Setup SH Shader
        self.neural_shader = SHRender(channels=self.channels, deg=deg)
        print("neural_shader", self.neural_shader)

    def neural_shade(self, features, viewdirs):
        return self.neural_shader(features, viewdirs)

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

class SHRender(torch.nn.Module):
    def __init__(self, channels, deg = 2):
        super(SHRender, self).__init__()

        chans = [1,3,5,7,9]
        self.deg = deg
        self.channels = channels

        output_channels = 0
        for i in range(deg+1):
            output_channels += chans[i]
        self.linear = torch.nn.Linear(channels, 3*output_channels, bias=False).cuda()

    def forward(self, features, viewdirs):
        sh_mult = eval_sh_bases(self.deg, viewdirs)[..., None, :]
        rgb_sh = features.view(-1, sh_mult.shape[1], sh_mult.shape[2], 3, sh_mult.shape[-1])
        rgb = torch.relu(torch.sum(sh_mult * rgb_sh, dim=-1) + 0.5)
        return rgb

C0 = 0.28209479177387814
C1 = 0.4886025119029199
C2 = [
    1.0925484305920792,
    -1.0925484305920792,
    0.31539156525252005,
    -1.0925484305920792,
    0.5462742152960396
]
C3 = [
    -0.5900435899266435,
    2.890611442640554,
    -0.4570457994644658,
    0.3731763325901154,
    -0.4570457994644658,
    1.445305721320277,
    -0.5900435899266435
]
C4 = [
    2.5033429417967046,
    -1.7701307697799304,
    0.9461746957575601,
    -0.6690465435572892,
    0.10578554691520431,
    -0.6690465435572892,
    0.47308734787878004,
    -1.7701307697799304,
    0.6258357354491761,
]

def eval_sh_bases(deg, dirs):
    """
    Evaluate spherical harmonics bases at unit directions,
    without taking linear combination.
    At each point, the final result may the be
    obtained through simple multiplication.
    :param deg: int SH max degree. Currently, 0-4 supported
    :param dirs: torch.Tensor (..., 3) unit directions
    :return: torch.Tensor (..., (deg+1) ** 2)
    """
    assert deg <= 4 and deg >= 0
    result = torch.empty((*dirs.shape[:-1], (deg + 1) ** 2), dtype=dirs.dtype, device=dirs.device)
    result[..., 0] = C0
    if deg > 0:
        x, y, z = dirs.unbind(-1)
        result[..., 1] = -C1 * y
        result[..., 2] = C1 * z
        result[..., 3] = -C1 * x
        if deg > 1:
            xx, yy, zz = x * x, y * y, z * z
            xy, yz, xz = x * y, y * z, x * z
            result[..., 4] = C2[0] * xy
            result[..., 5] = C2[1] * yz
            result[..., 6] = C2[2] * (2.0 * zz - xx - yy)
            result[..., 7] = C2[3] * xz
            result[..., 8] = C2[4] * (xx - yy)

            if deg > 2:
                result[..., 9] = C3[0] * y * (3 * xx - yy)
                result[..., 10] = C3[1] * xy * z
                result[..., 11] = C3[2] * y * (4 * zz - xx - yy)
                result[..., 12] = C3[3] * z * (2 * zz - 3 * xx - 3 * yy)
                result[..., 13] = C3[4] * x * (4 * zz - xx - yy)
                result[..., 14] = C3[5] * z * (xx - yy)
                result[..., 15] = C3[6] * x * (xx - 3 * yy)

                if deg > 3:
                    result[..., 16] = C4[0] * xy * (xx - yy)
                    result[..., 17] = C4[1] * yz * (3 * xx - yy)
                    result[..., 18] = C4[2] * xy * (7 * zz - 1)
                    result[..., 19] = C4[3] * yz * (7 * zz - 3)
                    result[..., 20] = C4[4] * (zz * (35 * zz - 30) + 3)
                    result[..., 21] = C4[5] * xz * (7 * zz - 3)
                    result[..., 22] = C4[6] * (xx - yy) * (7 * zz - 1)
                    result[..., 23] = C4[7] * xz * (xx - 3 * yy)
                    result[..., 24] = C4[8] * (xx * (xx - 3 * yy) - yy * (3 * xx - yy))
    return result


#######################################################################################################################################################
# Outward visible neural texture TensorVMSplit class
#######################################################################################################################################################

class TensorVMSplitNeuralTex(torch.nn.Module):
    def __init__(self, AABB, channels = 3, N_voxel = 27000000, pospe=4, feape = 4, viewpe = 0, shader_internal_dims=64):
        super(TensorVMSplitNeuralTex, self).__init__()

        self.channels = channels
        self.aabb = torch.stack((AABB)).cuda()
        self.pospe = pospe
        self.feape = feape
        self.viewpe = viewpe
        self.aabbSize = self.aabb[1] - self.aabb[0]
        self.invgridSize = 1.0/self.aabbSize * 2

        # Setup TensorVMSplit
        reso_cur = N_to_reso(N_voxel, self.aabb)
        self.net = TensorVMSplit_App(self.aabb, reso_cur, 'cuda', app_dim=channels, featureC=shader_internal_dims)

        print("net", self.net)
        print("neural_shader", self.net.renderModule)

    def normalize_coord(self, xyz_sampled):
        return (xyz_sampled-self.aabb[0]) * self.invgridSize - 1

    # Sample texture at a given location
    def sample(self, texc):
        _texc = self.normalize_coord(texc.view(-1, 3))

        out = self.net.compute_appfeature(_texc)

        return out.view(*texc.shape[:-1], self.channels) # Remap to [n, h, w, c]

    def neural_shade(self, features, viewdirs):
        rgb = self.net.renderModule(None, viewdirs, features)   # TODO: some may neet pts

        return rgb

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

#######################################################################################################################################################
# Outward visible neural texture TensorVMSplit class
#######################################################################################################################################################

class TensorSHNeuralTex(torch.nn.Module):
    def __init__(self, AABB, channels = 27, N_voxel = 27000000, pospe=4, feape = 4, viewpe = 0, shader_internal_dims=64):
        super(TensorSHNeuralTex, self).__init__()

        self.channels = channels
        self.aabb = torch.stack((AABB)).cuda()
        self.pospe = pospe
        self.feape = feape
        self.viewpe = viewpe
        self.aabbSize = self.aabb[1] - self.aabb[0]
        self.invgridSize = 1.0/self.aabbSize * 2

        # Setup TensorVMSplit
        reso_cur = N_to_reso(N_voxel, self.aabb)
        self.net = TensorVMSplit_App(self.aabb, reso_cur, 'cuda', app_dim=channels, featureC=shader_internal_dims, shadingMode = 'SH')

        print("net", self.net)
        print("neural_shader", self.net.renderModule)

    def normalize_coord(self, xyz_sampled):
        return (xyz_sampled-self.aabb[0]) * self.invgridSize - 1

    # Sample texture at a given location
    def sample(self, texc):
        _texc = self.normalize_coord(texc.view(-1, 3))

        out = self.net.compute_appfeature(_texc)

        return out.view(*texc.shape[:-1], self.channels) # Remap to [n, h, w, c]

    def neural_shade(self, features, viewdirs):
        img_wh = features.shape[1:3]
        rgb = self.net.renderModule(None, viewdirs.view(-1,3), features.view(-1,self.channels))

        return rgb.view(-1, *img_wh, 3)

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

#######################################################################################################################################################
# Outward visible neural texture TensorVMSplit class
#######################################################################################################################################################

class TensorVMSplitLoadNeuralTex(torch.nn.Module):
    def __init__(self, tensorf, channels = 3, unbounded=False):
        super(TensorVMSplitLoadNeuralTex, self).__init__()

        self.channels = channels
        self.aabb = tensorf.aabb.float().cuda()
        self.pospe = tensorf.pos_pe
        self.feape = tensorf.fea_pe
        self.viewpe = tensorf.view_pe
        self.aabbSize = self.aabb[1] - self.aabb[0]
        self.invgridSize = 1.0/self.aabbSize * 2
        self.unbounded = unbounded

        # Setup TensorVMSplit
        self.net = tensorf

        print("net", self.net)
        print("neural_shader", self.net.renderModule)

    def normalize_coord(self, xyz_sampled):
        return (xyz_sampled-self.aabb[0]) * self.invgridSize - 1

    # Sample texture at a given location
    def sample(self, texc):
        if self.unbounded:
            texc = utils.unbounded_warp(texc, radius=self.aabb[1][0])
            # pts_norm = torch.norm(texc, dim=-1)
            # scale = (self.aabb[1][0] - 1.0 / pts_norm[..., None]) / pts_norm[..., None]
            # mask_inside_inner_sphere = (pts_norm <= 1.0)[..., None]
            # texc = torch.where(mask_inside_inner_sphere, texc, scale * texc)

        _texc = self.normalize_coord(texc.view(-1, 3))

        out = self.net.compute_appfeature(_texc)

        # import ipdb; ipdb.set_trace()

        return out.view(*texc.shape[:-1], self.channels) # Remap to [n, h, w, c]
        # return out

    def neural_shade(self, features, viewdirs):
        img_wh = features.shape[1:3]
        rgb = self.net.renderModule(None, viewdirs.view(-1,3), features.view(-1,self.channels))

        return rgb.view(-1, *img_wh, 3)

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

#######################################################################################################################################################
# Outward visible neural texture TensoIR physical rendering class
#######################################################################################################################################################

class TensoIRPhysicalRendering(torch.nn.Module):
    def __init__(self, tensorf, channels = 3, unbounded=False):
        super(TensoIRPhysicalRendering, self).__init__()

        self.channels = channels
        self.aabb = tensorf.aabb.float().cuda()
        self.pospe = tensorf.pos_pe
        self.feape = tensorf.fea_pe
        self.viewpe = tensorf.view_pe
        self.aabbSize = self.aabb[1] - self.aabb[0]
        self.invgridSize = 1.0 / self.aabbSize * 2
        self.unbounded = unbounded

        # Setup TensorVMSplit
        self.net = tensorf

        self.num = 0

        print("net", self.net)
        print("neural_shader", self.net.renderModule_brdf)

    def normalize_coord(self, xyz_sampled):
        return (xyz_sampled-self.aabb[0]) * self.invgridSize - 1

    # Sample texture at a given location
    def sample(self, texc, viewdirs):
        # print("------------------------------------")
        # print("texc", texc.shape)
        # print(texc[0][100][95:105, :])
        # print(texc.max(), texc.min())
        if self.unbounded:
            texc = utils.unbounded_warp(texc, radius=self.aabb[1][0])
            # pts_norm = torch.norm(texc, dim=-1)
            # scale = (self.aabb[1][0] - 1.0 / pts_norm[..., None]) / pts_norm[..., None]
            # mask_inside_inner_sphere = (pts_norm <= 1.0)[..., None]
            # texc = torch.where(mask_inside_inner_sphere, texc, scale * texc)

        _texc = self.normalize_coord(texc.view(-1, 3))  # (1, 800, 800, 3)
        positions = _texc

        intrinsic_feat = self.net.compute_intrinfeature_with_grad(positions)
        brdf = self.net.renderModule_brdf(positions, intrinsic_feat)
        albedo, roughness = brdf[..., :3], (brdf[..., 3:4] * 0.9 + 0.09)
        derived_normals = self.net.compute_derived_normals(positions)
        predicted_normals = self.net.renderModule_normal(positions, intrinsic_feat)

        viewdirs = viewdirs.reshape(predicted_normals.shape)
        normals_diff = torch.sum(torch.pow(derived_normals - predicted_normals, 2), dim=-1, keepdim=True)
        normals_orientation_loss = torch.sum(viewdirs * predicted_normals, dim=-1, keepdim=True).clamp(min=0)

        if self.net.normals_kind == 'purely_predicted':
            normal = predicted_normals
        elif self.net.normals_kind == 'purely_derived':
            normal = derived_normals
        elif self.net.normals_kind == 'derived_plus_predicted':
            normal = predicted_normals
        else:
            raise NotImplementedError('Unknown normals_kind: {}'.format(self.net.normals_kind))

        positions_jitter = positions + torch.rand_like(positions) * 0.01
        intrinsic_feat_jitter = self.net.compute_intrinfeature(positions_jitter)
        brdf = self.net.renderModule_brdf(positions_jitter, intrinsic_feat_jitter)
        albedo_jitter, roughness_jitter = brdf[..., :3], (brdf[..., 3:4] * 0.9 + 0.09)

        # import imageio
        # vis_positions = positions.reshape((200, 200, 3))
        # print("vis_positions", vis_positions.shape, vis_positions.max(), vis_positions.min())
        # vis_positions = (vis_positions + 1) / 2 * 255
        # vis_positions = vis_positions.clamp(0, 255)
        # imageio.imwrite('vis_positions.png', vis_positions.detach().cpu().numpy().astype(np.uint8))

        # vis_albedo = albedo.reshape((200, 200, 3))
        # vis_albedo = vis_albedo * 255
        # vis_albedo = vis_albedo.clamp(0, 255)
        # imageio.imwrite('vis_albedo.png', vis_albedo.detach().cpu().numpy().astype(np.uint8))

        albedo = albedo.clamp(0, 1)
        fresnel = torch.zeros_like(albedo).fill_(self.net.fixed_fresnel)
        roughness = roughness.clamp(0, 1)
        normal = F.normalize(normal, p=2, dim=-1, eps=1e-6)

        # vis_roughness = roughness.reshape((200, 200, 1))
        # vis_roughness = vis_roughness.repeat(1, 1, 3).detach().cpu().numpy()
        # vis_roughness = (vis_roughness * 255).astype(np.uint8)
        # imageio.imwrite('vis_roughness.png', vis_roughness)

        return {
            'positions': texc.view(-1, 3),
            'albedo': albedo,
            'fresnel': fresnel,
            'roughness': roughness,
            'normal': normal,
            'albedo_jitter': albedo_jitter,
            'roughness_jitter': roughness_jitter,
            'normals_diff': normals_diff,
            'normals_orientation_loss': normals_orientation_loss,
        }

    def neural_shade(self, ret, viewdirs):
        albedo = ret['albedo']
        fresnel = ret['fresnel']
        roughness = ret['roughness']
        normal = ret['normal']
        positions = ret['positions']

        num_ray = positions.shape[0]
        shape = viewdirs.shape
        dirs = viewdirs.view(-1, 3)  # [bs, 3]

        light_area_weight = self.net.light_area_weight.cuda()

        incident_light_dirs = self.net.gen_light_incident_dirs(method="stratified_sampling").cuda()  # [envW * envH, 3]

        envir_map_light_rgbs = self.net.get_light_rgbs(incident_light_dirs).cuda()  # [light_num, envW * envH, 3]

        # save incident light dirs to image
        # import imageio
        # vis_light_dirs = incident_light_dirs.reshape((16, 32, 3))
        # vis_light_dirs = (vis_light_dirs + 1) / 2 * 255
        # print('vis_light_dirs', vis_light_dirs.shape, vis_light_dirs.max(), vis_light_dirs.min())
        # vis_light_dirs = vis_light_dirs.clamp(0, 255)
        # imageio.imwrite('vis_light_dirs.png', vis_light_dirs.detach().cpu().numpy().astype(np.uint8))
        # exit(0)

        ret_rgb = []
        ret_wo_indir_rgb = []
        ret_wo_visibilty_direct_rgb = []
        ret_indir_rgb = []

        chunk_size = 8192
        num_chunk = num_ray // chunk_size + int(num_ray % chunk_size != 0)
        for ind in range(num_chunk):
            nw_pos = positions[ind * chunk_size:(ind + 1) * chunk_size]
            nw_albedo = albedo[ind * chunk_size:(ind + 1) * chunk_size]
            nw_fresnel = fresnel[ind * chunk_size:(ind + 1) * chunk_size]
            nw_roughness = roughness[ind * chunk_size:(ind + 1) * chunk_size]
            nw_normal = normal[ind * chunk_size:(ind + 1) * chunk_size]
            nw_dirs = dirs[ind * chunk_size:(ind + 1) * chunk_size]
            nw_num = nw_pos.shape[0]

            light_idx = torch.zeros((nw_num, 1), dtype=torch.int32).cuda()

            surf2l = incident_light_dirs.reshape(1, -1, 3).repeat(nw_num, 1, 1)  # [bs, envW * envH, 3]  148
            surf2c = -nw_dirs  # [bs, 3]
            surf2c = F.normalize(surf2c, p=2, dim=-1, eps=1e-6)  # [bs, 3]

            ## get visibilty map from visibility network or compute it using density
            cosine = torch.einsum("ijk,ik->ij", surf2l, nw_normal)  # surf2l:[bs, envW * envH, 3]   16
            cosine = torch.clamp(cosine, min=0.0)   #
            cosine_mask = (cosine > 1e-6)

            visibility_compute = torch.zeros((*cosine_mask.shape, 1)).cuda()   # [bs, envW * envH, 1]   16
            indirect_light = torch.zeros((*cosine_mask.shape, 3)).cuda()   # [bs, envW * envH, 3]   48

            visibility_compute[cosine_mask], \
                indirect_light[cosine_mask] = compute_secondary_shading_effects(
                    tensoIR=self.net,
                    surface_pts=nw_pos.unsqueeze(1).expand(-1, surf2l.shape[1], -1)[cosine_mask],
                    surf2light=surf2l[cosine_mask],
                    light_idx=light_idx.view(-1, 1, 1).expand((*cosine_mask.shape, 1))[cosine_mask],
                    nSample=96,
                    vis_near=0.05,
                    vis_far=1.5,
                    chunk_size=160000,
                )

            visibility_to_use = visibility_compute
            ## Get BRDF specs
            nlights = surf2l.shape[1]
            specular = GGX_specular(nw_normal, surf2c, surf2l, nw_roughness, nw_fresnel)  # [bs, envW * envH, 3]  384
            surface_brdf = nw_albedo.unsqueeze(1).expand(-1, nlights, -1) / np.pi + specular # [bs, envW * envH, 3] 48

            ## Compute rendering equation
            direct_light_rgbs = torch.index_select(envir_map_light_rgbs, dim=0, index=light_idx.squeeze(-1)).cuda()  # [bs, envW * envH, 3]

            # print(visibility_to_use.shape)
            # visualize_vis = visibility_to_use[:, 31, 0]
            # # normalize to [0, 1]
            # visualize_vis = visualize_vis.clamp(min=0.0, max=1.0)
            # # if visualize_vis.shape[0] != 0:
            # #     visualize_vis = (visualize_vis - visualize_vis.min()) / (visualize_vis.max() - visualize_vis.min())
            # visualize_vis = visualize_vis.reshape(-1, 1).expand(-1, 3)

            light_rgbs = visibility_to_use * direct_light_rgbs + indirect_light # [bs, envW * envH, 3]  48

            wo_indirect_light_rgbs = visibility_to_use * direct_light_rgbs
            wo_visibilty_direct_light_rgbs = direct_light_rgbs.clone()

            # no visibility and indirect light
            # light_rgbs = direct_light_rgbs

            # no indirect light
            # light_rgbs = visibility_to_use * direct_light_rgbs  # [bs, envW * envH, 3]

            light_pix_contrib = surface_brdf * light_rgbs * cosine[:, :, None] * light_area_weight[None,:, None]   # [bs, envW * envH, 3]  48
            rgb_with_brdf = torch.sum(light_pix_contrib, dim=1)  # [bs, 3]
            ### Tonemapping
            rgb_with_brdf = torch.clamp(rgb_with_brdf, min=0.0, max=1.0)
            ### Colorspace transform
            if rgb_with_brdf.shape[0] > 0:
                rgb_with_brdf = linear2srgb_torch(rgb_with_brdf)
            ret_rgb.append(rgb_with_brdf)

            wo_indir_light_pix_contrib = surface_brdf * wo_indirect_light_rgbs * cosine[:, :, None] * light_area_weight[None,:, None]   # [bs, envW * envH, 3]  48
            wo_indir_rgb_with_brdf = torch.sum(wo_indir_light_pix_contrib, dim=1)
            if wo_indir_rgb_with_brdf.shape[0] > 0:
                wo_indir_rgb_with_brdf = linear2srgb_torch(wo_indir_rgb_with_brdf)
            ret_wo_indir_rgb.append(wo_indir_rgb_with_brdf)

            wo_visibilty_direct_light_pix_contrib = surface_brdf * wo_visibilty_direct_light_rgbs * cosine[:, :, None] * light_area_weight[None,:, None]
            wo_visibilty_direct_rgb_with_brdf = torch.sum(wo_visibilty_direct_light_pix_contrib, dim=1)
            if wo_visibilty_direct_rgb_with_brdf.shape[0] > 0:
                wo_visibilty_direct_rgb_with_brdf = linear2srgb_torch(wo_visibilty_direct_rgb_with_brdf)
            ret_wo_visibilty_direct_rgb.append(wo_visibilty_direct_rgb_with_brdf)

            indirect_pix_contrib = surface_brdf * indirect_light * cosine[:, :, None] * light_area_weight[None,:, None]   # [bs, envW * envH, 3]  48
            indirect_rgb_with_brdf = torch.sum(indirect_pix_contrib, dim=1)
            if indirect_rgb_with_brdf.shape[0] > 0:
                indirect_rgb_with_brdf = linear2srgb_torch(indirect_rgb_with_brdf)
            ret_indir_rgb.append(indirect_rgb_with_brdf)

        ret_rgb = torch.cat(ret_rgb, dim=0)

        # return to shape
        ret_rgb = ret_rgb.reshape(shape)

        ret['wo_indir_rgb'] = torch.cat(ret_wo_indir_rgb, dim=0).reshape(shape)
        ret['wo_visibility_direct_rgb'] = torch.cat(ret_wo_visibilty_direct_rgb, dim=0).reshape(shape)
        ret['indirect_light_rgb'] = torch.cat(ret_indir_rgb, dim=0).reshape(shape)

        return ret_rgb, ret


    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

#######################################################################################################################################################
# Outward visible neural texture TensorVMSplit class
#######################################################################################################################################################

class UVMapNeuralTex(torch.nn.Module):
    def __init__(self, channels = 3, internal_dims = 128, pospe=4, feape = 4, viewpe = 0):
        super(UVMapNeuralTex, self).__init__()

        self.channels = channels
        self.internal_dims = internal_dims
        self.pospe = pospe
        self.feape = feape
        self.viewpe = viewpe

        layer1 = torch.nn.Linear(self.channels+2*self.channels*feape+3+2*viewpe*3, internal_dims)
        layer2 = torch.nn.Linear(internal_dims, internal_dims)
        layer3 = torch.nn.Linear(internal_dims,3)

        self.neural_shader = torch.nn.Sequential(layer1, torch.nn.ReLU(inplace=True), layer2, torch.nn.ReLU(inplace=True), layer3).cuda()
        torch.nn.init.constant_(self.neural_shader[-1].bias, 0)

        print("neural_shader", self.neural_shader)

    def neural_shade(self, features, viewdirs):
        indata = [features, viewdirs]
        if self.feape > 0:
            indata += [positional_encoding(features, self.feape)]
        if self.viewpe > 0:
            indata += [positional_encoding(viewdirs, self.viewpe)]
        mlp_in = torch.cat(indata, dim=-1)
        rgb = self.neural_shader(mlp_in)
        rgb = torch.sigmoid(rgb)

        return rgb

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

#######################################################################################################################################################
# Outward visible neural texture TensorVMSplit class
#######################################################################################################################################################

class NeuSLoadNeuralTex(torch.nn.Module):
    def __init__(self, neus, channels = 3):
        super(NeuSLoadNeuralTex, self).__init__()

        self.channels = channels

        # Setup TensorVMSplit
        self.net = neus

        print("net", self.net)
        print("neural_shader", self.net.texture)

    def normalize_coord(self, xyz_sampled):
        return (xyz_sampled-self.aabb[0]) * self.invgridSize - 1

    # Sample texture at a given location
    def sample(self, texc):
        # TODO normalize
        sdf, sdf_grad, feature = self.net.geometry(texc, with_grad=True, with_feature=True)
        normal = F.normalize(sdf_grad, p=2, dim=-1)

        return torch.cat([feature, normal], dim=-1) # Remap to [n, h, w, c]

    def neural_shade(self, features, viewdirs):
        feature, normal = torch.split(features, [self.channels, 3], dim=-1)
        rgb = self.net.texture(feature, viewdirs, normal)

        return rgb

    # In-place clamp with no derivative to make sure values are in valid range after training
    def clamp_(self):
        pass

