# Copyright 2023 Bingxin Ke, ETH Zurich. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# --------------------------------------------------------------------------
# If you find this code useful, we kindly ask you to cite our paper in your work.
# Please find bibtex at: https://github.com/prs-eth/Marigold#-citation
# More information about the method can be found at https://marigoldmonodepth.github.io
# --------------------------------------------------------------------------


import argparse
import logging
import os
from glob import glob
from noise_schedule import NoiseScheduleVP, StepOptim  # 假设 NoiseScheduleVP 和 StepOptim 定义在 noise_schedule.py 文件中

import numpy as np
import torch
from PIL import Image
from tqdm.auto import tqdm

from marigold import MarigoldPipeline
from config.config_test import hyperparameters as param
import time
import pandas as pd
EXTENSION_LIST = [".jpg", ".jpeg", ".png"]


if "__main__" == __name__:
    logging.basicConfig(level=logging.INFO)
    os.environ["CUDA_VISIBLE_DEVICES"] = param["CUDA"]
    # -------------------- Arguments --------------------
    parser = argparse.ArgumentParser(
        description="Run single-image depth estimation using Marigold."
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default="prs-eth/marigold-lcm-v1-0",
        help="Checkpoint path or hub name.",
    )

    parser.add_argument(
        "--input_rgb_dir",
        type=str,
        required=True,
        help="Path to the input image folder.",
    )

    parser.add_argument(
        "--output_dir", type=str, required=True, help="Output directory."
    )

    # inference setting
    parser.add_argument(
        "--denoise_steps",
        type=int,
        default=None,
        help="Diffusion denoising steps, more steps results in higher accuracy but slower inference speed. For the original (DDIM) version, it's recommended to use 10-50 steps, while for LCM 1-4 steps.",
    )
    parser.add_argument(
        "--ensemble_size",
        type=int,
        default=5,
        help="Number of predictions to be ensembled, more inference gives better results but runs slower.",
    )
    parser.add_argument(
        "--half_precision",
        "--fp16",
        action="store_true",
        help="Run with half-precision (16-bit float), might lead to suboptimal result.",
    )

    # resolution setting
    parser.add_argument(
        "--processing_res",
        type=int,
        default=None,
        help="Maximum resolution of processing. 0 for using input image resolution. Default: 768.",
    )
    parser.add_argument(
        "--output_processing_res",
        action="store_true",
        help="When input is resized, out put depth at resized operating resolution. Default: False.",
    )
    parser.add_argument(
        "--resample_method",
        choices=["bilinear", "bicubic", "nearest"],
        default="bilinear",
        help="Resampling method used to resize images and depth predictions. This can be one of `bilinear`, `bicubic` or `nearest`. Default: `bilinear`",
    )

    # depth map colormap
    parser.add_argument(
        "--color_map",
        type=str,
        default="Spectral",
        help="Colormap used to render depth predictions.",
    )

    # other settings
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Reproducibility seed. Set to `None` for unseeded inference.",
    )

    parser.add_argument(
        "--batch_size",
        type=int,
        default=0,
        help="Inference batch size. Default: 0 (will be set automatically).",
    )
    parser.add_argument(
        "--apple_silicon",
        action="store_true",
        help="Flag of running on Apple Silicon.",
    )

    args = parser.parse_args()

    checkpoint_path = args.checkpoint
    input_rgb_dir = args.input_rgb_dir
    output_dir = args.output_dir

    denoise_steps = args.denoise_steps
    ensemble_size = args.ensemble_size
    if ensemble_size > 15:
        logging.warning("Running with large ensemble size will be slow.")
    half_precision = args.half_precision

    processing_res = args.processing_res
    match_input_res = not args.output_processing_res
    if 0 == processing_res and match_input_res is False:
        logging.warning(
            "Processing at native resolution without resizing output might NOT lead to exactly the same resolution, due to the padding and pooling properties of conv layers."
        )
    resample_method = args.resample_method

    color_map = args.color_map
    seed = args.seed
    batch_size = args.batch_size
    apple_silicon = args.apple_silicon
    if apple_silicon and 0 == batch_size:
        batch_size = 1  # set default batchsize

    # -------------------- Preparation --------------------
    # Random seed
    if seed is None:
        import time
        seed = int(time.time())
    # Output directories
    output_dir_color = os.path.join(output_dir, "depth_colored")
    output_dir_tif = os.path.join(output_dir, "depth_bw")
    output_dir_npy = os.path.join(output_dir, "depth_npy")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(output_dir_color, exist_ok=True)
    os.makedirs(output_dir_tif, exist_ok=True)
    os.makedirs(output_dir_npy, exist_ok=True)
    logging.info(f"output dir = {output_dir}")

    # -------------------- Device --------------------
    if apple_silicon:
        if torch.backends.mps.is_available() and torch.backends.mps.is_built():
            device = torch.device("mps:0")
        else:
            device = torch.device("cpu")
            logging.warning("MPS is not available. Running on CPU will be slow.")
    else:
        if torch.cuda.is_available():
            device = torch.device("cuda")
        else:
            device = torch.device("cpu")
            logging.warning("CUDA is not available. Running on CPU will be slow.")
    logging.info(f"device = {device}")

    # -------------------- Data --------------------
    rgb_filename_list = glob(os.path.join(input_rgb_dir, "*"))
    rgb_filename_list = [
        f for f in rgb_filename_list if os.path.splitext(f)[1].lower() in EXTENSION_LIST
    ]
    start = param["start"]
    num_images_to_load = param["num_images_to_load"]  # Example number, change as needed
    rgb_filename_list = sorted(rgb_filename_list)[start:num_images_to_load]
    n_images = len(rgb_filename_list)
    if n_images > 0:
        logging.info(f"Found {n_images} images")
    else:
        logging.error(f"No image found in '{input_rgb_dir}'")
        exit(1)

    # -------------------- Model --------------------
    if half_precision:
        dtype = torch.float16
        variant = "fp16"
        logging.info(
            f"Running with half precision ({dtype}), might lead to suboptimal result."
        )
    else:
        dtype = torch.float32
        variant = None
    print(checkpoint_path)
    # 记录加载模型的开始时间
    model_load_start_time = time.time()
    pipe: MarigoldPipeline = MarigoldPipeline.from_pretrained(
        checkpoint_path, variant=variant, torch_dtype=dtype
    )

    try:
        pipe.enable_xformers_memory_efficient_attention()
    except ImportError:
        pass  # run without xformers
    # 记录加载模型的结束时间
    model_load_end_time = time.time()
    model_load_time = model_load_end_time - model_load_start_time  # 模型加载时间（秒）
    pipe = pipe.to(device)
    logging.info(
        f"scale_invariant: {pipe.scale_invariant}, shift_invariant: {pipe.shift_invariant}"
    )
    # 1. 定义噪声调度器 (NoiseScheduleVP) 和优化器 (StepOptim)
    ns = NoiseScheduleVP(schedule="discrete", betas=torch.linspace(0.0001, 0.02, denoise_steps))
    step_optim = StepOptim(ns)

    # 2. 使用优化器生成采样的时间步长序列
    N = denoise_steps // 5  # 将时间步数减少为三分之一
    eps = 1e-3  # 最小时间值，根据经验选择
    initType = 'quad'  # 可以尝试不同的初始时间步方案
    t_res, lambda_res = step_optim.get_ts_lambdas(N, eps, initType)
    pipe.scheduler.timesteps = t_res  # 直接设置调度器的时间步长序列

    # Print out config
    logging.info(
        f"Inference settings: checkpoint = `{checkpoint_path}`, "
        f"with denoise_steps = {denoise_steps or pipe.default_denoising_steps}, "
        f"ensemble_size = {ensemble_size}, "
        f"processing resolution = {processing_res or pipe.default_processing_resolution}, "
        f"seed = {seed}; "
        f"color_map = {color_map}."
    )

    # -------------------- Inference and saving --------------------
    # 在推理开始前记录时间
    # 创建一个列表，用来存储每张图片的推理时间
    inference_times = []
    with torch.no_grad():
        os.makedirs(output_dir, exist_ok=True)

        for rgb_path in tqdm(rgb_filename_list, desc="Estimating depth", leave=True):
            start_time = time.time()
            # Read input image
            input_image = Image.open(rgb_path)
            # Random number generator
            if seed is None:
                generator = None
            else:
                generator = torch.Generator(device=device)
                generator.manual_seed(seed)

            # Predict depth
            pipe_out = pipe(
                input_image,
                denoising_steps=denoise_steps,
                ensemble_size=ensemble_size,
                processing_res=processing_res,
                match_input_res=match_input_res,
                batch_size=batch_size,
                color_map=color_map,
                show_progress_bar=True,
                resample_method=resample_method,
                generator=generator,
            )

            # 在每张图片推理完成后，记录结束时间
            end_time = time.time()
            inference_time = end_time - start_time
            inference_times.append(inference_time)

            depth_pred: np.ndarray = pipe_out.depth_np
            depth_colored: Image.Image = pipe_out.depth_colored

            # Save as npy
            rgb_name_base = os.path.splitext(os.path.basename(rgb_path))[0]
            pred_name_base = rgb_name_base + "_pred"
            npy_save_path = os.path.join(output_dir_npy, f"{pred_name_base}.npy")
            if os.path.exists(npy_save_path):
                logging.warning(f"Existing file: '{npy_save_path}' will be overwritten")
            np.save(npy_save_path, depth_pred)

            # Save as 16-bit uint png
            depth_to_save = (depth_pred * 65535.0).astype(np.uint16)
            png_save_path = os.path.join(output_dir_tif, f"{pred_name_base}.png")
            if os.path.exists(png_save_path):
                logging.warning(f"Existing file: '{png_save_path}' will be overwritten")
            Image.fromarray(depth_to_save).save(png_save_path, mode="I;16")

            # Colorize
            colored_save_path = os.path.join(
                output_dir_color, f"{pred_name_base}_colored.png"
            )
            if os.path.exists(colored_save_path):
                logging.warning(
                    f"Existing file: '{colored_save_path}' will be overwritten"
                )
            depth_colored.save(colored_save_path)
        # 将推理时间和模型加载时间保存到 DataFrame 中
        df_inference_time = pd.DataFrame(inference_times)
        # 将模型加载时间也加到 DataFrame 中作为第一行数据
        df_model_load_time = pd.DataFrame([{
            "Image": "Model Loading",
            "Inference Time (seconds)": model_load_time
        }])
        # 将模型加载时间写入第一个位置，后面跟随推理时间数据
        df_final = pd.concat([df_model_load_time, df_inference_time], ignore_index=True)
        output_excel_path = param["metrics_path"] + param["model_name"] + "/" + param[
            "model_name"] + "-InferenceTimes-Acc.xlsx"
        # 合并两个 DataFrame，确保两者写入同一个 Excel 文件
        # 将合并后的数据写入 Excel 文件
        with pd.ExcelWriter(output_excel_path) as writer:
            df_final.to_excel(writer, sheet_name="Inference Times", index=False)
        logging.info(f"Inference times and model loading time have been saved to {output_excel_path}")