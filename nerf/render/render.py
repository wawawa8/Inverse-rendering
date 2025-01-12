# Copyright (c) 2020-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

import torch
import nvdiffrast.torch as dr

from . import util
from . import renderutils as ru
from . import light
from .mlptexture import MLPNeuralTex
from .mlptexture import positional_encoding

# ==============================================================================================
#  Helper functions
# ==============================================================================================
def interpolate(attr, rast, attr_idx, rast_db=None):
    return dr.interpolate(attr.contiguous(), rast, attr_idx, rast_db=rast_db, diff_attrs=None if rast_db is None else 'all')

def shade(
        attrs,
        view_pos,
        material,
        tex_type,
    ):

    ################################################################################
    # Texture lookups
    ################################################################################

    if 'vert' not in tex_type:
        gb_pos = attrs
        wo = util.safe_normalize(gb_pos - view_pos)
        all_tex = material['neural_tex'].sample(gb_pos, wo)
    else:
        gb_pos = attrs[:,:,:,:3]
        all_tex = attrs[:,:,:,3:]

    # print('max', gb_pos.max(), 'min', gb_pos.min())

    # albedo = gt_albedo.reshape((200, 200, 3))
    # print(albedo.max(), albedo.min())
    # albedo = albedo * 255
    # import imageio
    # imageio.imwrite('gt_albedo.png', albedo.detach().cpu().numpy().astype('uint8'))
    # print(all_tex['albedo'].shape)
    # exit(0)

    # test_albedo = all_tex['albedo'].reshape((200, 200, 3))
    # print(test_albedo.max(), test_albedo.min())
    # print(gt_albedo.max(), gt_albedo.min())
    # exit(0)

    # all_tex['albedo'] = gt_albedo.reshape((40000, 3))


    shaded_col, ret_dict = material['neural_tex'].neural_shade(all_tex, wo)

    # print('albedo', albedo.shape)
    for key, val in ret_dict.items():
        ret_dict[key] = val.reshape((200, 200, -1))

    # vis_albedo = albedo
    # print(vis_albedo.max(), vis_albedo.min())
    # vis_albedo = vis_albedo * 255
    # import imageio
    # imageio.imwrite('albedo11.png', vis_albedo.detach().cpu().numpy().astype('uint8'))
    # exit(0)

    alpha = torch.ones(list(shaded_col.shape[:3])+[1]).to(shaded_col.device)

    # Return multiple buffers
    buffers = {
        'shaded'    : torch.cat((shaded_col, alpha), dim=-1),
        # 'all_tex'   : all_tex[..., :3],
        'gb_pos'    : gb_pos,
        'wo'        : wo,
    }
    buffers.update(ret_dict)

    return buffers

def shade_uv(
        attrs,
        gb_texc,
        gb_texc_deriv,
        view_pos,
        material,
        tex_type,
    ):

    ################################################################################
    # Texture lookups
    ################################################################################
    gb_pos = attrs
    all_tex = material['uvmap'].sample(gb_texc, gb_texc_deriv)

    wo = util.safe_normalize(gb_pos - view_pos)

    shaded_col = material['neural_tex'].neural_shade(all_tex, wo)

    alpha = torch.ones(list(shaded_col.shape[:3])+[1]).to(shaded_col.device)


    # Return multiple buffers
    buffers = {
        'shaded'    : torch.cat((shaded_col, alpha), dim=-1),
        'all_tex'   : all_tex[..., :3],
        'gb_texc'   : gb_texc,
        'gb_pos'    : gb_pos,
        'wo'        : wo
    }
    return buffers

# ==============================================================================================
#  Render a depth slice of the mesh (scene), some limitations:
#  - Single mesh
#  - Single light
#  - Single material
# ==============================================================================================
def render_layer(
        rast,
        rast_deriv,
        mesh,
        view_pos,
        resolution,
        spp,
        msaa,
        bsdf,
        tex_type,
    ):

    full_res = [resolution[0]*spp, resolution[1]*spp]

    ################################################################################
    # Rasterize
    ################################################################################

    # Scale down to shading resolution when MSAA is enabled, otherwise shade at full resolution
    if spp > 1 and msaa:
        rast_out_s = util.scale_img_nhwc(rast, resolution, mag='nearest', min='nearest')
        rast_out_deriv_s = util.scale_img_nhwc(rast_deriv, resolution, mag='nearest', min='nearest') * spp
    else:
        rast_out_s = rast
        rast_out_deriv_s = rast_deriv

    ################################################################################
    # Interpolate attributes
    ################################################################################

    # Interpolate world space position
    if 'vert' not in tex_type:
        attrs, _ = interpolate(mesh.v_pos[None, ...], rast_out_s, mesh.t_pos_idx.int())
    else:
        attrs, _ = interpolate(torch.cat((mesh.v_pos, mesh.material['neural_tex'].feats), dim=1)[None, ...], rast_out_s, mesh.t_pos_idx.int())

    ################################################################################
    # Shade
    ################################################################################

    if tex_type != 'uvmap':
        buffers = shade(attrs, view_pos, mesh.material, tex_type)
    else:
        # Texture coordinate
        assert mesh.v_tex is not None
        gb_texc, gb_texc_deriv = interpolate(mesh.v_tex[None, ...], rast_out_s, mesh.t_tex_idx.int(), rast_db=rast_out_deriv_s)
        buffers = shade_uv(attrs, gb_texc, gb_texc_deriv, view_pos, mesh.material, tex_type)

    ################################################################################
    # Prepare output
    ################################################################################

    # Scale back up to visibility resolution if using MSAA
    if spp > 1 and msaa:
        for key in buffers.keys():
            buffers[key] = util.scale_img_nhwc(buffers[key], full_res, mag='nearest', min='nearest')

    # albedo = buffers['albedo']
    # import imageio
    # albedo = albedo.reshape((200, 200, 3))
    # albedo = albedo * 255
    # imageio.imwrite('albedo_ori.png', albedo.detach().cpu().numpy().astype('uint8'))

    # Return buffers
    return buffers

# ==============================================================================================
#  Render a depth peeled mesh (scene), some limitations:
#  - Single mesh
#  - Single light
#  - Single material
# ==============================================================================================
def render_mesh(
        ctx,
        mesh,
        mtx_in,
        view_pos,
        resolution,
        spp         = 1,
        num_layers  = 1,
        msaa        = False,
        background  = None,
        bsdf        = None,
        tex_type    = 'mlp',
        downsample  = 1,
        anti_aliasing   = False,
        anti_aliasing_mode = 'bilinear'
    ):

    def prepare_input_vector(x):
        x = torch.tensor(x, dtype=torch.float32, device='cuda') if not torch.is_tensor(x) else x
        return x[:, None, None, :] if len(x.shape) == 2 else x

    def composite_buffer(key, layers, background, antialias):
        accum = background
        for buffers, rast in reversed(layers):
            alpha = (rast[..., -1:] > 0).float() * buffers[key][..., -1:]
            accum = torch.lerp(accum, torch.cat((buffers[key][..., :-1], torch.ones_like(buffers[key][..., -1:])), dim=-1), alpha)
            if antialias:
                accum = dr.antialias(accum.contiguous(), rast, v_pos_clip, mesh.t_pos_idx.int())
        return accum

    assert mesh.t_pos_idx.shape[0] > 0, "Got empty training triangle mesh (unrecoverable discontinuity)"
    assert background is None or (background.shape[1] == resolution[0] and background.shape[2] == resolution[1])

    full_res = [resolution[0]*spp, resolution[1]*spp]

    # Convert numpy arrays to torch tensors
    mtx_in      = torch.tensor(mtx_in, dtype=torch.float32, device='cuda') if not torch.is_tensor(mtx_in) else mtx_in
    view_pos    = prepare_input_vector(view_pos)

    # clip space transform
    v_pos_clip = ru.xfm_points(mesh.v_pos[None, ...], mtx_in)
    # print(v_pos_clip)
    # exit(0)

    # Render all layers front-to-back
    layers = []
    with dr.DepthPeeler(ctx, v_pos_clip, mesh.t_pos_idx.int(), full_res) as peeler:
        for _ in range(num_layers):
            rast, db = peeler.rasterize_next_layer()
            layers += [(render_layer(rast, db, mesh, view_pos, resolution, spp, msaa, bsdf, tex_type), rast)]

    # Setup background
    if background is not None:
        if spp > 1:
            background = util.scale_img_nhwc(background, full_res, mag='nearest', min='nearest')
        background = torch.cat((background, torch.zeros_like(background[..., 0:1])), dim=-1)
    else:
        background = torch.zeros(1, full_res[0], full_res[1], 4, dtype=torch.float32, device='cuda')

    # Composite layers front-to-back
    out_buffers = {}
    for key in layers[0][0].keys():
        if key == 'shaded':
            accum = composite_buffer(key, layers, background, True)
        else:
            accum = layers[0][0][key]

        # Downscale to framebuffer resolution. Use avg pooling
        out_buffers[key] = util.avg_pool_nhwc(accum, spp) if spp > 1 else accum

    # albedo = out_buffers['albedo']
    # import imageio
    # albedo = albedo.reshape((200, 200, 3))
    # albedo = albedo * 255
    # imageio.imwrite('albedo_b4.png', albedo.detach().cpu().numpy().astype('uint8'))

    if downsample > 1 and anti_aliasing:
        for k in out_buffers.keys():
            out_buffers[k] = torch.nn.functional.interpolate(out_buffers[k].permute(0,3,1,2), scale_factor=[1/downsample, 1/downsample], mode=anti_aliasing_mode, align_corners=False, antialias=True).permute(0,2,3,1)

    return out_buffers

# ==============================================================================================
#  Render UVs
# ==============================================================================================
def render_uv(ctx, mesh, resolution, mlp_texture):

    # # clip space transform
    # uv_clip = mesh.v_tex[None, ...]*2.0 - 1.0

    # # pad to four component coordinate
    # uv_clip4 = torch.cat((uv_clip, torch.zeros_like(uv_clip[...,0:1]), torch.ones_like(uv_clip[...,0:1])), dim = -1)

    # # rasterize
    # rast, _ = dr.rasterize(ctx, uv_clip4, mesh.t_tex_idx.int(), resolution)

    # # Interpolate world space position
    # gb_pos, _ = interpolate(mesh.v_pos[None, ...], rast, mesh.t_pos_idx.int())

    # Sample out textures from MLP
    all_tex = mlp_texture.sample(mesh.v_pos)

    return all_tex