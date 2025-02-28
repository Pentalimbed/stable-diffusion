import argparse
import os
import re
import time
from contextlib import nullcontext
from itertools import islice
from random import randint

import numpy as np
import torch
from PIL import Image
from einops import rearrange
from omegaconf import OmegaConf
from pytorch_lightning import seed_everything
from torch import autocast
from torchvision.utils import make_grid
from tqdm.auto import tqdm, trange
from transformers import logging

from ldm.util import instantiate_from_config
from optimUtils import split_weighted_subprompts, logger

logging.set_verbosity_error()


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def load_model_from_config(ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    return sd


def get_image(opt, model, modelCS, modelFS, prompt=None):
    start_code = None
    if opt.fixed_code:
        start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=opt.device)

    speed_mp = opt.speed_mp
    batch_size = opt.n_samples
    if not opt.from_file and prompt is None:
        prompt = opt.prompt
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = batch_size * list(data)
            data = list(chunk(sorted(data), batch_size))

    if opt.precision == "autocast" and opt.device != "cpu":
        precision_scope = autocast
    else:
        precision_scope = nullcontext

    seeds = ""
    with torch.no_grad():
        all_samples = list()
        for _ in trange(opt.n_iter, desc="Sampling"):
            for prompts in tqdm(data, desc="data"):
                sample_path = os.path.join(outpath, "_".join(re.split(":| ", prompts[0])))[:150]
                os.makedirs(sample_path, exist_ok=True)
                base_count = len(os.listdir(sample_path))

                with precision_scope("cuda"):
                    modelCS.to(opt.device)
                    uc = None
                    if opt.scale != 1.0:
                        uc = modelCS.get_learned_conditioning(batch_size * [""])
                    if isinstance(prompts, tuple):
                        prompts = list(prompts)

                    subprompts, weights = split_weighted_subprompts(prompts[0])
                    if len(subprompts) > 1:
                        c = torch.zeros_like(uc)
                        totalWeight = sum(weights)
                        # normalize each "sub prompt" and add it
                        for i in range(len(subprompts)):
                            weight = weights[i]
                            # if not skip_normalize:
                            weight = weight / totalWeight
                            c = torch.add(c, modelCS.get_learned_conditioning(subprompts[i]), alpha=weight)
                    else:
                        c = modelCS.get_learned_conditioning(prompts)

                    shape = [opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f]

                    if opt.device != "cpu":
                        mem = torch.cuda.memory_allocated() / 1e6
                        modelCS.to("cpu")
                        while torch.cuda.memory_allocated() / 1e6 >= mem:
                            time.sleep(1)

                    samples_ddim = model.sample(
                        S=opt.ddim_steps,
                        conditioning=c,
                        seed=opt.seed,
                        shape=shape,
                        verbose=False,
                        unconditional_guidance_scale=opt.scale,
                        unconditional_conditioning=uc,
                        eta=opt.ddim_eta,
                        x_T=start_code,
                        sampler=opt.sampler,
                        speed_mp=speed_mp
                    )
                    modelFS.to(opt.device)

                    print(samples_ddim.shape)
                    print("saving images")
                    x_samples_ddim = modelFS.decode_first_stage(samples_ddim)
                    x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)

                    if not opt.skip_save:
                        folder_path = os.path.join(outpath + "/" + str(opt.prompt).replace("/", "")[:100] + "/")
                        
                        for x_sample in x_samples_ddim:
                            x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                            Image.fromarray(x_sample.astype(np.uint8)).save(
                                os.path.join(sample_path, f"{base_count:05}.png"))
                            opt.seed += 1
                            base_count += 1

                    if not opt.skip_grid:
                        all_samples.append(x_samples_ddim)
                        
                    if opt.device != "cpu":
                        mem = torch.cuda.memory_allocated() / 1e6
                        modelFS.to("cpu")
                        while torch.cuda.memory_allocated() / 1e6 >= mem:
                            time.sleep(1)
                    del samples_ddim
                    print("memory_final = ", torch.cuda.memory_allocated() / 1e6)

    toc = time.time()

    time_taken = (toc - tic) / 60.0

    print(
        (
                "Samples finished in {0:.2f} minutes"
        ).format(time_taken)
    )
    
    if not opt.skip_grid:
        # additionally, save as grid
        grid = torch.stack(all_samples, 0)
        grid = rearrange(grid, 'n b c h w -> (n b) c h w')
        grid = make_grid(grid, nrow=opt.n_iter)

        # to image
        grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
        Image.fromarray(grid.astype(np.uint8)).save(os.path.join(outpath + "/" + str(opt.prompt).replace("/", "")[:100] + f".{opt.format}"))

        return Image.fromarray(grid.astype(np.uint8))


if __name__ == '__main__':

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--prompt", type=str, nargs="?", default="a painting of a virus monster playing guitar",
        help="the prompt to render"
    )
    parser.add_argument("--outdir", type=str, nargs="?", help="dir to write results to",
                        default="outputs/txt2img-samples")
    parser.add_argument(
        "--skip_grid",
        action="store_true",
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )
    parser.add_argument(
        "--skip_save",
        action="store_true",
        help="do not save individual samples. For speed measurements.",
    )
    parser.add_argument(
        "--config_path", type=str, default="optimizedSD/v1-inference.yaml",
        help="config path"
    )
    parser.add_argument(
        "--ckpt_path", type=str, default="models/ldm/stable-diffusion-v1/model.ckpt",
        help="checkpoint path"
    )
    parser.add_argument(
        "--ddim_steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
    )

    parser.add_argument(
        "--fixed_code",
        action="store_true",
        help="if enabled, uses the same starting code across samples ",
    )
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=1,
        help="sample this often",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor",
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=1,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
    )
    parser.add_argument(
        "--n_rows",
        type=int,
        default=0,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=7.5,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda",
        help="specify GPU (cuda/cuda:0/cuda:1/...)",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        help="if specified, load prompts from this file",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--unet_bs",
        type=int,
        default=1,
        help="Slightly reduces inference time at the expense of high VRAM (value > 1 not recommended )",
    )
    parser.add_argument(
        "--speed_mp",
        type=int,
        default=3,
        help="More vram, more image res (better not touch this )",
    )
    parser.add_argument(
        "--turbo",
        action="store_true",
        help="Reduces inference time on the expense of 1GB VRAM",
    )
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast"
    )
    parser.add_argument(
        "--format",
        type=str,
        help="output image format",
        choices=["jpg", "png"],
        default="png",
    )
    parser.add_argument(
        "--sampler",
        type=str,
        help="sampler",
        choices=["ddim", "plms"],
        default="plms",
    )
    opt = parser.parse_args()

    tic = time.time()
    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir
    grid_count = len(os.listdir(outpath)) - 1

    if opt.seed is None:
        opt.seed = randint(0, 1000000)
    seed_everything(opt.seed)

    # Logging
    logger(vars(opt), log_csv="logs/txt2img_logs.csv")

    sd = load_model_from_config(f"{opt.ckpt_path}")
    li, lo = [], []
    for key, value in sd.items():
        sp = key.split(".")
        if (sp[0]) == "model":
            if "input_blocks" in sp:
                li.append(key)
            elif "middle_block" in sp:
                li.append(key)
            elif "time_embed" in sp:
                li.append(key)
            else:
                lo.append(key)
    for key in li:
        sd["model1." + key[6:]] = sd.pop(key)
    for key in lo:
        sd["model2." + key[6:]] = sd.pop(key)

    config = OmegaConf.load(f"{opt.config_path}")

    _model = instantiate_from_config(config.modelUNet)
    _, _ = _model.load_state_dict(sd, strict=False)
    _model.eval()
    _model.unet_bs = opt.unet_bs
    _model.cdevice = opt.device
    _model.turbo = opt.turbo

    _modelCS = instantiate_from_config(config.modelCondStage)
    _, _ = _modelCS.load_state_dict(sd, strict=False)
    _modelCS.eval()
    _modelCS.cond_stage_model.device = opt.device

    _modelFS = instantiate_from_config(config.modelFirstStage)
    _, _ = _modelFS.load_state_dict(sd, strict=False)
    _modelFS.eval()
    del sd

    if opt.device != "cpu" and opt.precision == "autocast":
        _model.half()
        _modelCS.half()

    get_image(
        opt,
        _model,
        _modelCS,
        _modelFS
    )
    print("exported to", outpath + "/" + str(opt.prompt).replace("/", "")[:100] + f".{opt.format}")
