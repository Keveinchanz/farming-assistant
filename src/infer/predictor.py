"""统一推理封装 —— 被后端 /api/diagnose/image（任务 4.2）与评估脚本共用同一份实现。

两种后端：
    OnnxPredictor  : 生产路径。加载 models/onnx/inference_model.onnx 用 onnxruntime 推理。
                     只用 CPU 版 onnxruntime —— 本机 torch 是 cu130，onnxruntime-gpu 通常
                     滞后于 CUDA 13，强行上 GPU 版容易踩版本地狱；单图 CPU 推理是毫秒级，够用。
    TorchPredictor : 兜底路径。ONNX 还没导出时直接加载 .pth，保证接口先能跑通。

两者的预处理完全一致，都调用 src/data/transforms.py 的 eval_transform()，
避免"训练 90% / 线上乱答"。
"""

from __future__ import annotations

import io
import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from src.common.constants import DEFAULT_INPUT_SIZE, ONNX_INPUT_NAME, ONNX_OUTPUT_NAME
from src.common.logging_utils import setup_logger
from src.common.paths import path_of, resolve

logger = setup_logger("agri.predictor")


class BasePredictor:
    """推理器接口。返回结构与后端接口契约一一对应。"""

    backend: str = "base"

    def __init__(self, label_map: dict, *, input_size: int = DEFAULT_INPUT_SIZE,
                 topk: int = 5, confidence_threshold: float = 0.45) -> None:
        self.label_map = label_map
        self.index_to_label: dict[str, dict] = label_map["index_to_label"]
        self.num_classes = int(label_map["num_classes"])
        self.input_size = int(input_size)
        self.topk = int(topk)
        self.confidence_threshold = float(confidence_threshold)

        from src.data.transforms import eval_transform

        self.transform = eval_transform(self.input_size)

    # ---------------------------------------------------------------- 子类实现

    def _forward(self, batch: np.ndarray) -> np.ndarray:  # pragma: no cover - 抽象
        raise NotImplementedError

    def _softmax(self, logits: np.ndarray) -> np.ndarray:
        x = logits - logits.max(axis=1, keepdims=True)
        e = np.exp(x)
        return e / e.sum(axis=1, keepdims=True)

    # ---------------------------------------------------------------- 对外

    def preprocess(self, image: Image.Image) -> np.ndarray:
        tensor = self.transform(image.convert("RGB"))
        return tensor.unsqueeze(0).numpy().astype(np.float32)

    def predict_pil(self, image: Image.Image) -> dict:
        return self.predict(images=[image])

    def predict_bytes(self, data: bytes) -> dict:
        with Image.open(io.BytesIO(data)) as im:
            return self.predict(images=[im.convert("RGB")])

    def predict(self, images: list[Image.Image]) -> dict:
        """批量推理，返回与 /api/diagnose/image 一致的契约。"""
        t0 = time.perf_counter()
        if not images:
            raise ValueError("没有传入任何图像")

        batch = np.concatenate([self.preprocess(im) for im in images], axis=0)
        logits = np.asarray(self._forward(batch), dtype=np.float32)
        probs = self._softmax(logits)

        results = [self._one(probs[i]) for i in range(len(images))]
        elapsed = (time.perf_counter() - t0) * 1000

        if len(results) == 1:
            out = results[0]
        else:
            out = {"batch": results}

        if isinstance(out, dict) and "top1" in out:
            out["elapsed_ms"] = round(elapsed, 2)
            out["backend"] = self.backend
        return out

    def _one(self, prob: np.ndarray) -> dict:
        k = min(self.topk, prob.shape[0])
        idx = np.argsort(-prob)[:k]

        topk = []
        for i in idx:
            entry = self.index_to_label.get(str(int(i)))
            if entry is None:
                continue
            topk.append({
                "index": int(i),
                "label_cn": entry["label_cn"],
                "crop": entry["crop"],
                "condition": entry["condition"],
                "pathogen_type": entry["pathogen_type"],
                "is_healthy": entry["is_healthy"],
                "confidence": round(float(prob[i]), 4),
            })

        top1 = topk[0] if topk else None
        conf = top1["confidence"] if top1 else 0.0
        return {
            "top1": top1,
            "topk": topk,
            "low_confidence": bool(conf < self.confidence_threshold),
            "confidence_threshold": self.confidence_threshold,
        }


class OnnxPredictor(BasePredictor):
    """onnxruntime 推理（CPU）。"""

    backend = "onnxruntime-cpu"

    def __init__(self, onnx_path: str | Path, *args, **kwargs) -> None:
        import onnxruntime as ort

        super().__init__(*args, **kwargs)
        self.onnx_path = Path(onnx_path)
        if not self.onnx_path.exists():
            raise FileNotFoundError(f"ONNX 模型不存在：{self.onnx_path}")

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(self.onnx_path), sess_options=so, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        logger.info("已加载 ONNX 模型：%s（输入 %s，输出 %s）",
                    self.onnx_path.name, self.input_name, self.output_name)

    def _forward(self, batch: np.ndarray) -> np.ndarray:
        return self.session.run([self.output_name], {self.input_name: batch})[0]


class TorchPredictor(BasePredictor):
    """PyTorch 检查点推理（ONNX 尚未导出时的兜底）。"""

    backend = "pytorch"

    def __init__(self, ckpt_path: str | Path, *args, **kwargs) -> None:
        import torch

        from src.models.backbone import build_model, resolve_device

        super().__init__(*args, **kwargs)
        self.ckpt_path = Path(ckpt_path)
        if not self.ckpt_path.exists():
            raise FileNotFoundError(f"权重文件不存在：{self.ckpt_path}")

        ckpt = torch.load(self.ckpt_path, map_location="cpu", weights_only=False)
        num_classes = int(ckpt.get("num_classes", self.num_classes))
        if num_classes != self.num_classes:
            raise ValueError(
                f"权重类别数 {num_classes} 与 label_map 的 {self.num_classes} 不一致，"
                "请确认用的是同一个数据集训练出来的模型"
            )
        self.device = resolve_device("auto")
        self.model = build_model(
            arch=ckpt.get("arch", "resnet50"), num_classes=num_classes, pretrained=False
        ).to(self.device).eval()
        self.model.load_state_dict(ckpt["model_state"])
        self.torch = torch
        logger.info("已加载 PyTorch 权重：%s（arch=%s，epoch=%s）",
                    self.ckpt_path.name, ckpt.get("arch"), ckpt.get("epoch"))

    def _forward(self, batch: np.ndarray) -> np.ndarray:
        with self.torch.no_grad():
            x = self.torch.from_numpy(batch).to(self.device)
            with self.torch.amp.autocast("cuda", enabled=self.device.type == "cuda"):
                out = self.model(x)
        return out.float().cpu().numpy()


# -------------------------------------------------------------------- 工厂

def load_label_map(path: str | Path | None = None) -> dict:
    p = Path(path) if path else path_of("label_map")
    if not p.exists():
        raise FileNotFoundError(f"缺少标签映射 {p}，请先运行：python scripts/03_build_label_map.py")
    return json.loads(p.read_text(encoding="utf-8"))


def registry_entry(run_name: str = "resnet50") -> dict:
    """读 models/registry.json 里该模型的元数据（由训练脚本写入）。"""
    p = path_of("model_registry")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(run_name, {})
    except json.JSONDecodeError:
        logger.warning("registry.json 解析失败，忽略：%s", p)
        return {}


def get_predictor(
    *,
    prefer: str = "auto",
    run_name: str = "resnet50",
    topk: int = 5,
    confidence_threshold: float | None = None,
) -> BasePredictor:
    """构造推理器。

    prefer="auto" 时的优先顺序：registry 里记录的 ONNX -> models/onnx/inference_model.onnx
    -> registry 里的 .pth -> 约定路径的 .pth。任一环节缺文件就继续往下降级，不直接抛错，
    这样后端在不完整的 checkout 上也能起来（只是会明确告诉调用方当前用的是哪种后端）。
    """
    label_map = load_label_map()
    reg = registry_entry(run_name)

    if confidence_threshold is None:
        from src.common.config import load_config

        confidence_threshold = float(load_config("default").get("confidence_threshold", 0.45))

    input_size = int(reg.get("input_size", DEFAULT_INPUT_SIZE))
    if int(reg.get("num_classes", label_map["num_classes"])) != int(label_map["num_classes"]):
        logger.warning("registry 记录的类别数(%s)与 label_map(%d)不一致，以 label_map 为准并重载权重",
                       reg.get("num_classes"), label_map["num_classes"])

    common = dict(label_map=label_map, input_size=input_size, topk=topk,
                  confidence_threshold=confidence_threshold)

    onnx_candidates: list[Path] = []
    if reg.get("onnx"):
        onnx_candidates.append(resolve(reg["onnx"]))
    onnx_candidates.append(path_of("onnx_dir") / "inference_model.onnx")

    ckpt_candidates: list[Path] = []
    if reg.get("checkpoint"):
        ckpt_candidates.append(resolve(reg["checkpoint"]))
    ckpt_candidates.append(path_of("checkpoints_dir") / f"best_{run_name}.pth")

    order: list[tuple[str, Path]] = []
    for p in onnx_candidates:
        order.append(("onnx", p))
    for p in ckpt_candidates:
        order.append(("torch", p))

    if prefer == "onnx":
        order = [x for x in order if x[0] == "onnx"]
    elif prefer == "torch":
        order = [x for x in order if x[0] == "torch"]

    errors: list[str] = []
    for kind, path in order:
        if not path.exists():
            errors.append(f"{kind}: {path} 不存在")
            continue
        try:
            if kind == "onnx":
                return OnnxPredictor(path, **common)
            return TorchPredictor(path, **common)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{kind}: {path} 加载失败（{e}）")
            logger.warning("加载失败，尝试下一个候选：%s（%s）", path, e)

    raise FileNotFoundError(
        "找不到可用的模型文件。请先训练并导出：\n"
        "    python scripts/10_train_resnet50.py\n"
        "    python scripts/13_export_onnx.py\n"
        "已尝试的候选：\n  " + "\n  ".join(errors)
    )


@lru_cache(maxsize=1)
def get_shared_predictor() -> BasePredictor:
    """进程内共享的图像分类器。

    HTTP 接口与 skills 技能都会用到分类模型。若各自调用 `get_predictor()`，
    同一份 ONNX 会在进程里被加载两次，白占几百 MB；技能层与后端必须共用这一份。
    首次调用才加载，import 本模块不会读权重。
    """
    return get_predictor()


__all__ = [
    "BasePredictor", "OnnxPredictor", "TorchPredictor",
    "get_predictor", "get_shared_predictor", "load_label_map", "registry_entry",
    "ONNX_INPUT_NAME", "ONNX_OUTPUT_NAME",
]
