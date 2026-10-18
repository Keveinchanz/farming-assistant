"""任务 2.6 —— 模型导出为 ONNX，用于后端推理部署。

几个刻意的选择：
    · opset_version=17：兼容性好且被 onnxruntime 1.30 完整支持，不用最新版以免踩坑
    · 显式声明 dynamic_axes：batch 维设为动态，后端可以按需单张或批量推理
    · 导出后**必须做数值对齐校验**：用同一输入分别跑 torch 和 onnxruntime，
      比较输出是否一致（atol=1e-3）。只导出不校验是常见的坑 ——
      图结构转换出问题时不会报错，只会在线上给出错误结果。
    · 输入输出名统一为 constants.ONNX_INPUT_NAME / ONNX_OUTPUT_NAME，
      后端 src/infer/predictor.py 直接按这个约定取名，不靠位置猜。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch

from src.common.constants import ONNX_INPUT_NAME, ONNX_OUTPUT_NAME
from src.common.io_utils import ensure_dir
from src.common.logging_utils import setup_logger
from src.common.paths import path_of, resolve
from src.models.backbone import build_model, resolve_device

logger = setup_logger("agri.export")


def export_onnx(
    *,
    run_name: str = "resnet50",
    checkpoint: str | Path | None = None,
    output: str | Path | None = None,
    opset: int = 17,
    verify: bool = True,
) -> Path:
    from src.common.config import load_training_config

    cfg_name = "train_vit" if run_name.startswith("vit") else "train_resnet50"
    cfg = load_training_config(cfg_name)
    default_ckpt = cfg["output"].get("checkpoint", f"models/checkpoints/best_{run_name}.pth")
    ckpt_path = resolve(checkpoint) if checkpoint else resolve(default_ckpt)

    if not ckpt_path.exists():
        raise FileNotFoundError(
            f"权重不存在：{ckpt_path}\n请先训练：python scripts/10_train_resnet50.py"
        )

    out_path = resolve(output) if output else (path_of("onnx_dir") / "inference_model.onnx")
    ensure_dir(out_path.parent)

    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    arch = ckpt.get("arch", "resnet50")
    num_classes = int(ckpt["num_classes"])
    input_size = int(ckpt.get("input_size", cfg["model"].get("input_size", 224)))

    logger.info("导出 ONNX：%s（arch=%s，类别数=%d，输入 %dx%d，epoch=%s）",
                ckpt_path.name, arch, num_classes, input_size, input_size, ckpt.get("epoch"))

    device = resolve_device("cpu")
    model = build_model(arch=arch, num_classes=num_classes, pretrained=False).to(device).eval()
    model.load_state_dict(ckpt["model_state"])

    dummy = torch.randn(1, 3, input_size, input_size, device=device)

    with torch.no_grad():
        _export_torch_onnx(model, dummy, out_path, opset)

    size_mb, extras = _artifact_size(out_path)
    logger.info("已导出：%s（主文件 %.1f MB，opset=%d）", out_path, size_mb, opset)
    if extras:
        logger.warning(
            "⚠ 权重被写进了外部数据文件 %s —— 部署时必须与 .onnx 一起拷贝，"
            "只拷主文件会导致模型无法加载。如需单文件，请确保 torch 导出时不使用 external_data。",
            "、".join(extras),
        )

    if verify:
        _verify(model, out_path, input_size, num_classes, device)

    _update_registry(run_name, out_path, num_classes, input_size, arch, opset, size_mb)
    return out_path


def _export_torch_onnx(model, dummy, out_path: Path, opset: int) -> None:
    """调用 torch.onnx.export，并在两种导出器之间自动回退。

    两个坑这里都处理了：

    1. **导出器选择**：torch 2.x 之后的默认导出器基于 dynamo，需要额外装 onnxscript；
       目标环境没装时自动退回旧的 TorchScript 导出器（dynamo=False）。
    2. **权重落盘方式**：dynamo 导出器默认把权重写到同名的 `.onnx.data` 外部文件里，
       于是 `inference_model.onnx` 只有 0.2 MB —— 单看文件大小会以为导出失败，
       而且部署时只拷贝 .onnx 会直接失效。这里强制 `external_data=False` 输出自包含单文件
       （ResNet50 fp32 约 92 MB，完全在可接受范围）。
    """
    kwargs = dict(
        input_names=[ONNX_INPUT_NAME],
        output_names=[ONNX_OUTPUT_NAME],
        dynamic_axes={
            ONNX_INPUT_NAME: {0: "batch"},
            ONNX_OUTPUT_NAME: {0: "batch"},
        },
        opset_version=opset,
        do_constant_folding=True,
        external_data=False,
    )

    for label, extra in (("默认（dynamo）", {}), ("旧版 TorchScript（dynamo=False）", {"dynamo": False})):
        try:
            torch.onnx.export(model, dummy, str(out_path), **kwargs, **extra)
            logger.info("ONNX 导出器：%s", label)
            return
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning("%s 导出器不可用（%s），尝试下一个", label, e)
        except TypeError as e:
            # 参数不被接受：老版本 torch 不认识 external_data / dynamo
            kwargs.pop("external_data", None)
            logger.warning("%s 导出器不接受 external_data 参数（%s），去掉后重试", label, e)

    raise RuntimeError(
        "ONNX 导出失败：请安装 onnxscript（python -m pip install onnxscript）后重试"
    )


def _artifact_size(out_path: Path) -> tuple[float, list[str]]:
    """统计导出产物的总大小，并列出所有附属文件。

    权重若被写进外部 .data 文件，单独看主文件会严重低估体积（曾出现主文件 0.2 MB +
    外部文件 91.75 MB 的情况），因此这里把同名的附属文件一并统计并显式提示。
    """
    total = 0
    extras: list[str] = []
    for p in out_path.parent.glob(out_path.name + "*"):
        total += p.stat().st_size
        if p.name != out_path.name:
            extras.append(p.name)
    return total / 1024 ** 2, extras


@torch.no_grad()
def _verify(model, onnx_path: Path, input_size: int, num_classes: int, device) -> None:
    """用同一输入比对 torch 与 onnxruntime 的输出，防止导出静默出错。"""
    import onnxruntime as ort

    x = torch.randn(2, 3, input_size, input_size, device=device)
    torch_out = model(x).float().cpu().numpy()

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    ort_out = sess.run([ONNX_OUTPUT_NAME], {ONNX_INPUT_NAME: x.cpu().numpy()})[0]

    if ort_out.shape != torch_out.shape:
        raise AssertionError(f"输出形状不一致：torch {torch_out.shape} vs onnx {ort_out.shape}")
    if ort_out.shape != (2, num_classes):
        raise AssertionError(f"输出形状异常：期望 (2, {num_classes})，实际 {ort_out.shape}")

    max_diff = float(np.max(np.abs(torch_out - ort_out)))
    ok = np.allclose(torch_out, ort_out, atol=1e-3)
    logger.info("数值对齐校验：最大绝对误差 %.3e，allclose(atol=1e-3) = %s", max_diff, ok)

    # 顺便验证动态 batch 维确实生效
    y1 = sess.run([ONNX_OUTPUT_NAME], {ONNX_INPUT_NAME: x[:1].cpu().numpy()})[0]
    if y1.shape[0] != 1:
        raise AssertionError(f"动态 batch 维未生效，单张输入返回 {y1.shape}")
    logger.info("动态 batch 维校验通过：输入 1 张 -> 输出 %s；输入 2 张 -> 输出 %s",
                y1.shape, ort_out.shape)

    if not ok:
        raise AssertionError(
            f"ONNX 与 PyTorch 输出差异过大（{max_diff:.3e}），不要用于部署，请检查导出配置"
        )


def _update_registry(run_name: str, onnx_path: Path, num_classes: int,
                     input_size: int, arch: str, opset: int, size_mb: float) -> None:
    reg_path = path_of("model_registry")
    data = {}
    if reg_path.exists():
        try:
            data = json.loads(reg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("registry.json 解析失败，将重建：%s", reg_path)

    entry = data.setdefault(run_name, {})
    entry.update({
        "arch": arch,
        "num_classes": num_classes,
        "input_size": input_size,
        "onnx": str(onnx_path.relative_to(path_of("models_dir").parent)).replace("\\", "/"),
        "onnx_opset": opset,
        "onnx_size_mb": round(size_mb, 1),
    })
    ensure_dir(reg_path.parent)
    reg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("registry 已更新 ONNX 记录：%s -> %s", run_name, entry["onnx"])
