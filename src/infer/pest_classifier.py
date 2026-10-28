"""虫害分类复核 —— IP102 / ResNet50，102 类，onnxruntime 推理。

## 为什么需要第二个模型

检测器 `src/infer/pest_detector.py` 的输出头设了 102 类，但 IP102 的**检测**标注只有
18976 张带框图，以下 5 个类在 train / val / test 三个划分里一个框都没有：

    59 viteus_vitifoliae          葡萄根瘤蚜
    60 colomerus_vitis            葡萄缺节瘿螨
    63 polyphagotars_onemus_latus 侧多食跗线螨
    75 phyllocoptes_oleiverus     橄榄瘿螨
    80 parlatoria_zizyphus_lucus  黑片盾蚧

这 5 类的检测输出头从未见过正样本，走「检测出框 → 裁切 → 分类」永远走不到第一步。
但它们在 IP102 **分类集**里有图，102 类齐全。于是补一条分类路径，承担两件事：

1. **级联改判**：检测最高置信度低于 `PEST_CLS_GATE` 时，把框裁出来交分类器重新判决。
   gate 与 margin 是 `scripts/47_eval_two_stage.py` 在 val 上扫出来、在 test 上复核过的
   后验参数，不是拍脑袋定的：val 提升 0.59 个百分点、test 提升 0.26 个百分点，
   介入率三成。
2. **无框兜底**：检测一个框都没给时，整图直接送分类器。这是 5 个零框类唯一的出口。
   实测口径见 `scripts/48_eval_pest_cls_coverage.py`：这 5 类在 IP102 测试集上
   宏平均 Top-1 45.4%、Top-5 71.7%，明显低于 102 类整体，所以兜底只给候选、
   并且带上「请补拍」的提示，不硬下结论。

## 为什么不建第二份标签表

分类与检测共用 `data/pest_label_map.json`。两个模型各持一份标签表是这类项目最容易
出的错：改了一边忘了另一边，第 48 类在检测里是蝗虫、在分类里是盲蝽，而推理不报错。

## 为什么预处理手写 numpy 而不是 torchvision

线上只装 onnxruntime，没有 torch/torchvision。同理，检测那边的 letterbox 与 NMS 也是
手写 numpy 的。这里的缩放假装成 torchvision 的 `Resize(int)` 语义：短边缩到目标值、
长宽比不变，再中心裁切。裁切复核走 `CROP_SIZE(256) → Resize(255) → CenterCrop(224)`，
与 `scripts/45`、`scripts/47` 的训练/评测口径逐字对齐，否则线上和报出的指标不是一回事。
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from src.common.constants import (
    PEST_CLS_CROP_PAD,
    PEST_CLS_CROP_SIZE,
    PEST_CLS_INPUT_SIZE,
    PEST_NUM_CLASSES,
)
from src.common.logging_utils import setup_logger
from src.common.paths import path_of, resolve
from src.infer.pest_detector import load_pest_label_map

logger = setup_logger("agri.pest.classifier")

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# 一条候选的键。与 backend 的 PestCandidate 声明对齐，多一个少一个都要同步改
CANDIDATE_KEYS = ("index", "label_cn", "label_en", "prob")


# ===========================================================================
# 纯函数区：不依赖 onnxruntime，可单独单测
# ===========================================================================

def square_crop_box(bbox: list[float], pad: float, width: int, height: int) -> tuple[int, int, int, int]:
    """像素框 → 外扩 pad 的正方形像素框，越界平移回图内。

    与 `scripts/45_build_pest_crops.py`、`scripts/47_eval_two_stage.py` 同一口径。
    为什么必须正方形：分类器训练时吃的就是正方形裁切，喂长条图等于换了一种输入分布。
    """
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * (1.0 + 2.0 * pad)
    side = min(max(side, 16.0), float(min(width, height)))
    bx1 = min(max(cx - side / 2.0, 0.0), width - side)
    by1 = min(max(cy - side / 2.0, 0.0), height - side)
    return int(round(bx1)), int(round(by1)), int(round(bx1 + side)), int(round(by1 + side))


def resize_shorter(rgb: np.ndarray, size: int) -> np.ndarray:
    """短边缩到 size，长宽比不变。对齐 torchvision `Resize(int)` 的语义。"""
    import cv2

    h, w = rgb.shape[:2]
    if min(h, w) == size:
        return rgb
    scale = size / float(min(h, w))
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    return cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)


def center_crop(rgb: np.ndarray, size: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    if h < size or w < size:
        import cv2

        rgb = cv2.resize(rgb, (max(w, size), max(h, size)), interpolation=cv2.INTER_LINEAR)
        h, w = rgb.shape[:2]
    top, left = (h - size) // 2, (w - size) // 2
    return rgb[top: top + size, left: left + size]


def to_tensor(rgb: np.ndarray, size: int = PEST_CLS_INPUT_SIZE) -> np.ndarray:
    """RGB uint8 HWC → NCHW float32（ImageNet 归一化），batch 固定 1。"""
    x = rgb.astype(np.float32) / 255.0
    x = (x - IMAGENET_MEAN) / IMAGENET_STD
    return np.ascontiguousarray(x.transpose(2, 0, 1))[None, ...]


def preprocess_full(rgb: np.ndarray, size: int = PEST_CLS_INPUT_SIZE) -> np.ndarray:
    """整图路径：短边缩到 1.14×size，中心裁 size。用于无框兜底。

    与 48 号评测脚本的 `Resize(int(size*1.14)) + CenterCrop(size)` 一致，
    这样报出的 45.4% 和线上真正在跑的是同一件事。
    """
    return to_tensor(center_crop(resize_shorter(rgb, int(size * 1.14)), size), size)


def preprocess_crop(image: Image.Image, bbox: list[float],
                    size: int = PEST_CLS_INPUT_SIZE) -> np.ndarray:
    """框裁切路径：正方形外扩 → 256 → 缩 255 → 中心裁 224。用于级联改判。"""
    import cv2

    rgb = np.asarray(image.convert("RGB"))
    h, w = rgb.shape[:2]
    x1, y1, x2, y2 = square_crop_box(bbox, PEST_CLS_CROP_PAD, w, h)
    patch = rgb[y1:y2, x1:x2]
    if patch.size == 0:
        patch = np.zeros((PEST_CLS_CROP_SIZE, PEST_CLS_CROP_SIZE, 3), np.uint8)
    else:
        patch = cv2.resize(patch, (PEST_CLS_CROP_SIZE, PEST_CLS_CROP_SIZE),
                           interpolation=cv2.INTER_AREA)
    return to_tensor(center_crop(resize_shorter(patch, int(size * 1.14)), size), size)


# ===========================================================================
# 推理器
# ===========================================================================

class OnnxPestClassifier:
    """102 类虫害分类推理器（onnxruntime，CPU，静态 batch=1）。"""

    backend = "onnxruntime-cpu"

    def __init__(self, onnx_path: str | Path, label_map: dict, input_size: int = PEST_CLS_INPUT_SIZE):
        import onnxruntime as ort

        self.onnx_path = str(onnx_path)
        self.input_size = int(input_size)
        # 与检测器同一份标签表，结构是 {"num_classes": 102, "index_to_label": {...}}
        self.index_to_label: dict[str, dict] = label_map["index_to_label"]
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            self.onnx_path, sess_options=opts, providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

    def _softmax(self, logits: np.ndarray) -> np.ndarray:
        z = logits.astype(np.float64)
        z = z - z.max()
        e = np.exp(z)
        return (e / e.sum()).astype(np.float32)

    def _label(self, index: int) -> dict:
        entry = self.index_to_label.get(str(index)) or {}
        return {
            "label_cn": entry.get("label_cn", ""),
            "label_en": entry.get("label_en", ""),
            "crop": entry.get("crop", ""),
            "pest_type": entry.get("pest_type", ""),
        }

    def classify_tensor(self, tensor: np.ndarray, topk: int = 5) -> list[dict]:
        logits = self.session.run([self.output_name], {self.input_name: tensor})[0][0]
        prob = self._softmax(logits)
        k = min(topk, prob.shape[0])
        order = np.argsort(-prob)[:k]
        out = []
        for i in order:
            meta = self._label(int(i))
            out.append({
                "index": int(i),
                "label_cn": meta["label_cn"],
                "label_en": meta["label_en"],
                "crop": meta["crop"],
                "pest_type": meta["pest_type"],
                "prob": round(float(prob[i]), 4),
            })
        return out

    def classify_image(self, image: Image.Image, topk: int = 5) -> list[dict]:
        """整图兜底：不依赖检测框。"""
        rgb = np.asarray(image.convert("RGB"))
        return self.classify_tensor(preprocess_full(rgb, self.input_size), topk)

    def classify_crop(self, image: Image.Image, bbox: list[float], topk: int = 5) -> list[dict]:
        """按框裁切后复核。"""
        return self.classify_tensor(preprocess_crop(image, bbox, self.input_size), topk)


# ===========================================================================
# 工厂
# ===========================================================================

@lru_cache(maxsize=1)
def get_pest_classifier() -> OnnxPestClassifier | None:
    """构造分类器。找不到权重时明确返回 None，让上层退化为「不做复核」。

    这里与 `get_pest_detector()` 的策略不同：检测是主链路，没有就抛错；分类是复核，
    没有时整条诊断仍然可用，只是少了 5 个零框类的出口与级联改判。所以记一条 warning
    后返回 None，调用方拿到 None 就不挂复核，且 `review` 字段报 null —— 不静默假装跑过。
    """
    reg = {}
    reg_path = path_of("model_registry")
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8")).get("pest_cls", {})
        except json.JSONDecodeError:
            logger.warning("registry.json 解析失败，忽略：%s", reg_path)

    candidates: list[Path] = []
    if reg.get("onnx"):
        candidates.append(resolve(reg["onnx"]))
    candidates.append(path_of("pest_cls_onnx"))

    for c in candidates:
        if c and Path(c).exists():
            label_map = load_pest_label_map()
            # load_pest_label_map() 返回的是整个 json（顶层有 version/source/index_to_label…），
            # 类别表在 index_to_label 里，长度要对它数，不是对顶层键数
            n_cls = len(label_map.get("index_to_label", {}))
            if n_cls != PEST_NUM_CLASSES:
                logger.warning("标签表有 %d 个类别，与 %d 类不符，仍按索引取用",
                               n_cls, PEST_NUM_CLASSES)
            logger.info("虫害分类复核加载 %s（%d 类）", c, n_cls)
            return OnnxPestClassifier(c, label_map, int(reg.get("input_size", PEST_CLS_INPUT_SIZE)))

    logger.warning(
        "未找到虫害分类权重（候选：%s）。级联改判与无框兜底将不生效，"
        "先跑 scripts/49_export_pest_cls_onnx.py 导出。",
        ", ".join(str(c) for c in candidates),
    )
    return None


def classifier_elapsed(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)
