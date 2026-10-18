"""虫害目标检测推理封装 —— IP102 / YOLOv8n 导出成 ONNX 后用 onnxruntime 跑。

与 src/infer/predictor.py 是**并列关系而非继承**：两者的输出契约根本不同，
一个是「271 选 1 的 top-k 概率」，一个是「一张图上若干个框」。硬塞进同一个基类
只会让两边都变形，所以分开写。

## 为什么检测的后处理必须自己用 numpy 写

导出的 ONNX 只吐一个原始张量 `[1, 4+nc, 8400]`（8400 = 80²+40²+20² 三个尺度的锚点），
里面是**没做任何后处理的**东西：

    行 0..3       cx, cy, w, h —— 注意是中心点式，且单位是 letterbox 后的 640 像素
    行 4..105     102 个类别的得分（导出前已过 sigmoid，直接是概率）

要拿到能用的框，得自己做四件事：转置 → 按类取最大分 → 置信度过滤 → NMS →
再把坐标从 640 的 letterbox 空间反算回原图。这四步里任何一步写错，**都不会报错**，
只会返回空结果或者框整体偏移——所以它们被拆成不依赖 onnxruntime 的纯函数，
由 tests/test_units.py 直接断言。

## 三个最容易踩的坑（都在下面代码里有对应处理）

1. **预处理不能复用分类那套**。src/data/transforms.py 的 eval_transform() 是
   ImageNet 归一化 + CenterCrop(224)，而检测要的是 letterbox 等比例缩放 + 灰边填充 114
   + RGB / 255，不做减均值除方差。用错的话结果整体崩掉且不报错。

2. **letterbox 的缩放必须用 cv2.INTER_LINEAR，不能用 PIL 的 BILINEAR**。
   缩小时 PIL 会做抗锯齿重采样，cv2.INTER_LINEAR 不会——两者喂给模型的图**不是同一张**。
   模型是用 cv2 那条路训练和验证的，换 PIL 就是训练/线上不一致。
   同理填充色必须与 PEST_PAD_VALUE 一致。

3. **不要照抄 ultralytics 里的 `im[..., ::-1]`**。那句是在把 cv2 读出来的 BGR 转成 RGB；
   我们的上游是 PIL，本来就是 RGB，再反转一次就是红蓝对调。**它不报错**，
   只是精度悄悄掉下去。见 preprocess_image()。
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from src.common.constants import (
    PEST_INPUT_SIZE,
    PEST_MAX_DETECTIONS,
    PEST_NUM_CLASSES,
    PEST_PAD_VALUE,
    PEST_STRIDE,
)
from src.common.logging_utils import setup_logger
from src.common.paths import path_of, resolve

logger = setup_logger("agri.pest.detector")


# ===========================================================================
# 返回契约
# ===========================================================================

# 一次检测返回哪些顶层键、每个框有哪些键。**这不是文档，是被两边同时用着的常量**：
#   · OnnxPestDetector.detect() 造完结果后逐键核对（对不上直接抛，见 _assert_contract）
#   · tests/test_units.py 拿它去比 backend/app/schemas/models.py 的 PestResult /
#     PestDetection 声明了哪些字段
# 为什么要这么较真：pydantic 的 response_model 会**静默丢弃**未声明的字段 ——
# 不报错、不告警，前端只是拿到一个缺字段的对象。本项目已经因此踩过两次
# （整个 pest 字段被吃掉一次；confidence_threshold 又被吃掉一次，前端只能显示
# 「检测阈值 0%」）。所以契约必须有一处能被自动核对的单一来源。
DETECTION_RESULT_KEYS = (
    "detections", "count", "image_size",
    "confidence_threshold", "iou_threshold", "backend", "elapsed_ms",
)
DETECTION_BOX_KEYS = (
    "index", "label_cn", "label_en", "crop", "pest_type", "confidence", "bbox",
)


def _assert_contract(result: dict) -> dict:
    """核对 detect() 的返回值与上面两个常量一致，不一致就抛。

    代价是每次检测多两次集合比较，可以忽略；换来的是「改了返回结构却忘了同步
    schema/单测」这件事不可能悄悄发生。
    """
    got, want = set(result), set(DETECTION_RESULT_KEYS)
    if got != want:
        raise RuntimeError(
            f"detect() 的返回键与 DETECTION_RESULT_KEYS 不一致："
            f"多了 {sorted(got - want)}、少了 {sorted(want - got)}。"
            "改返回结构时请连同常量、backend 的 PestResult 声明、单测一起改。"
        )
    for d in result["detections"]:
        gk, wk = set(d), set(DETECTION_BOX_KEYS)
        if gk != wk:
            raise RuntimeError(
                f"单个检测框的键与 DETECTION_BOX_KEYS 不一致："
                f"多了 {sorted(gk - wk)}、少了 {sorted(wk - gk)}"
            )
    return result


# ===========================================================================
# 纯函数区：不依赖 onnxruntime，可单独单测
# ===========================================================================

def letterbox(
    img: np.ndarray,
    new_size: int = PEST_INPUT_SIZE,
    color: int = PEST_PAD_VALUE,
    scaleup: bool = True,
    auto: bool = False,
    stride: int = PEST_STRIDE,
) -> tuple[np.ndarray, float, tuple[float, float]]:
    """等比例缩放并填充到正方形 new_size × new_size，返回 (图, 缩放比, (左填充, 上填充))。

    对齐 ultralytics 的 LetterBox：auto=False 对应**验证/训练**那条路，
    auto=True 对应 **predict()** 那条路。两者不是同一个东西，本机实测：

        输入尺寸      auto=False      auto=True
        1920×1080     (640, 640)      (384, 640)   <- 非正方形
         800×600      (640, 640)      (480, 640)
         640×640      (640, 640)      (640, 640)   <- 仅正方形输入才碰巧一致

    `auto=True` 会把填充量再对 stride 取模，于是输入变成「宽或高恰为 stride 整数倍」
    的非正方形——这是 ultralytics 的矩形推理，省算力，但要求模型能吃动态形状。

    **服务端固定用 auto=False**，理由有两条：
      1. 导出的是 `dynamic=False` 的自包含静态 ONNX，喂不进 384×640 这种尺寸；
      2. 更重要的是，mAP 是**用 auto=False（val 那条路）量出来的**，
         服务时换成 auto=True 就等于「报告的指标」与「实际行为」不是同一件事。
    auto=True 保留下来是为了在 scripts/19 里精确复现 predict() 做对齐校验。
    """
    import cv2  # 见模块 docstring 第 2 条：这里必须用 cv2，不能换 PIL

    h, w = img.shape[:2]
    new_size = int(new_size)
    r = min(new_size / h, new_size / w)
    if not scaleup:
        r = min(r, 1.0)

    new_w, new_h = int(round(w * r)), int(round(h * r))
    dw, dh = new_size - new_w, new_size - new_h
    if auto:
        # 矩形推理：把填充量压到 stride 的最小整数倍，输出因此可能是非正方形
        dw, dh = int(dw % stride), int(dh % stride)

    # ±0.1 偏移让两边的取整之和恰好等于总填充量（否则奇数会丢 1 px）
    left, right = int(round(dw / 2 - 0.1)), int(round(dw / 2 + 0.1))
    top, bottom = int(round(dh / 2 - 0.1)), int(round(dh / 2 + 0.1))

    if (w, h) != (new_w, new_h):
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    if top or bottom or left or right:
        img = cv2.copyMakeBorder(
            img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(color, color, color)
        )
    return img, float(r), (float(left), float(top))


def xywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    """中心点式 (cx, cy, w, h) → 角点式 (x1, y1, x2, y2)。"""
    out = np.empty_like(boxes, dtype=np.float32)
    out[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    out[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    out[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    out[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    return out


def box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """两两 IoU，返回 (len(a), len(b))。用角点式坐标。"""
    a = np.asarray(a, dtype=np.float32).reshape(-1, 4)
    b = np.asarray(b, dtype=np.float32).reshape(-1, 4)
    if a.size == 0 or b.size == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)

    area_a = np.clip(a[:, 2] - a[:, 0], 0, None) * np.clip(a[:, 3] - a[:, 1], 0, None)
    area_b = np.clip(b[:, 2] - b[:, 0], 0, None) * np.clip(b[:, 3] - b[:, 1], 0, None)

    lt = np.maximum(a[:, None, :2], b[None, :, :2])
    rb = np.minimum(a[:, None, 2:], b[None, :, 2:])
    wh = np.clip(rb - lt, 0, None)
    inter = wh[..., 0] * wh[..., 1]

    union = area_a[:, None] + area_b[None, :] - inter
    return np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0).astype(np.float32)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float,
        max_det: int = PEST_MAX_DETECTIONS) -> np.ndarray:
    """贪心 NMS，返回保留下来的下标。

    与 torchvision.ops.nms 同语义：按分数降序，逐个保留并把与它 IoU 超阈的挤掉。
    排序用 stable：分数相同时按下标先后，保证结果可复现（换成默认的快速排序，
    同分框的取舍会随实现版本漂移，同样的图会给出不同的框）。
    """
    boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
    scores = np.asarray(scores, dtype=np.float32).reshape(-1)
    if boxes.size == 0:
        return np.zeros((0,), dtype=np.int64)

    order = np.argsort(-scores, kind="stable")
    keep: list[int] = []
    while order.size > 0 and len(keep) < max_det:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        ious = box_iou(boxes[i:i + 1], boxes[rest])[0]
        order = rest[ious <= iou_thres]
    return np.asarray(keep, dtype=np.int64)


def clip_boxes(boxes: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """把框裁到图像范围内。shape 是 (高, 宽)。"""
    h, w = shape[:2]
    out = np.asarray(boxes, dtype=np.float32).copy()
    out[:, [0, 2]] = np.clip(out[:, [0, 2]], 0, w)
    out[:, [1, 3]] = np.clip(out[:, [1, 3]], 0, h)
    return out


def scale_boxes(boxes: np.ndarray, ratio: float, pad: tuple[float, float],
                orig_shape: tuple[int, int]) -> np.ndarray:
    """把 letterbox 空间的框反算回原图坐标，并裁到图像边界。

    顺序不能反：先减填充、再除以缩放比。倒过来的话在缩放比不为 1 时框会整体漂移。
    """
    out = np.asarray(boxes, dtype=np.float32).copy()
    out[:, [0, 2]] -= pad[0]
    out[:, [1, 3]] -= pad[1]
    out[:, :4] /= max(float(ratio), 1e-9)
    return clip_boxes(out, orig_shape)


def decode_predictions(
    output: np.ndarray,
    conf_thres: float,
    num_classes: int = PEST_NUM_CLASSES,
    max_nms: int = 30000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把 YOLOv8 的原始输出解成若干候选框，返回 (xyxy, score, class_id)。

    只取每个锚点得分最高的那一类（单标签框），与 ultralytics 的
    non_max_suppression 一致——不是多标签，所以不要在这里改成「所有超过阈值的类都留」。
    """
    pred = np.asarray(output, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    if pred.ndim != 2:
        raise ValueError(f"ONNX 输出维度不对，期望 2 或 3 维，实际 {pred.shape}")

    n_attr = 4 + num_classes
    if pred.shape[0] == n_attr:
        pred = pred.T                                    # (4+nc, 8400) -> (8400, 4+nc)
    elif pred.shape[1] != n_attr:
        raise ValueError(
            f"ONNX 输出的属性维既不是 {n_attr} 也不是最后一维：{pred.shape}。"
            f"要么模型不是 {num_classes} 类的 YOLOv8 检测头，要么 label_map 配错了"
        )

    cls_scores = pred[:, 4:4 + num_classes]
    cls_ids = cls_scores.argmax(axis=1).astype(np.int64)
    scores = cls_scores[np.arange(len(cls_ids)), cls_ids]

    keep = scores > float(conf_thres)
    boxes = xywh_to_xyxy(pred[keep, :4])
    scores = scores[keep]
    cls_ids = cls_ids[keep]

    # 候选过多时先按分数截断，避免 NMS 退化成 O(n²) 拖死
    if boxes.shape[0] > max_nms:
        top = np.argsort(-scores, kind="stable")[:max_nms]
        boxes, scores, cls_ids = boxes[top], scores[top], cls_ids[top]

    return boxes, scores, cls_ids


def postprocess(
    output: np.ndarray,
    orig_shape: tuple[int, int],
    ratio: float,
    pad: tuple[float, float],
    conf_thres: float,
    iou_thres: float,
    num_classes: int = PEST_NUM_CLASSES,
    max_det: int = PEST_MAX_DETECTIONS,
) -> list[dict]:
    """完整后处理链：解码 → 逐类 NMS → 反算坐标。

    逐类而非跨类做 NMS：一张叶子上「稻飞虱」和「叶蝉」位置重合是正常的，
    跨类做会把其中一类整个吃掉。ultralytics 默认 agnostic_nms=False，也是逐类。
    """
    boxes, scores, cls_ids = decode_predictions(output, conf_thres, num_classes)
    if boxes.shape[0] == 0:
        return []

    kept: list[int] = []
    for c in np.unique(cls_ids):
        idx = np.where(cls_ids == c)[0]
        local = nms(boxes[idx], scores[idx], iou_thres, max_det)
        kept.extend(idx[local].tolist())

    kept_arr = np.asarray(sorted(kept), dtype=np.int64)
    boxes, scores, cls_ids = boxes[kept_arr], scores[kept_arr], cls_ids[kept_arr]

    # 全部类的框都齐了再统一裁到 max_det，避免「先到的类把名额占满」
    if boxes.shape[0] > max_det:
        top = np.argsort(-scores, kind="stable")[:max_det]
        boxes, scores, cls_ids = boxes[top], scores[top], cls_ids[top]

    boxes = scale_boxes(boxes, ratio, pad, orig_shape)

    return [
        {
            "index": int(cls_ids[i]),
            "confidence": float(scores[i]),
            "bbox": [round(float(v), 2) for v in boxes[i]],
        }
        for i in range(boxes.shape[0])
    ]


def preprocess_image(img: np.ndarray, input_size: int = PEST_INPUT_SIZE) -> tuple[np.ndarray, float, tuple[float, float]]:
    """原图 → ONNX 输入张量。返回 (NCHW float32, 缩放比, 填充量)。

    入参必须是**已经转成 RGB 的 uint8 数组**（PIL 的 `Image.convert("RGB")` 就是），
    这里**不做通道反转** —— YOLOv8 吃的是 RGB。

    别照抄 ultralytics 预测器里那句 `im[..., ::-1]`：它反转是因为上游用 cv2.imread
    读出来的是 BGR，而我们的上游是 PIL，本来就是 RGB。多反转一次就是红蓝对调，
    模型照样出框、不报任何错，只是精度悄悄掉下去。
    （tests/test_units.py 里那条「纯红图通道 0 应为 1.0」就是为了钉住这件事。）

    同样地，这里**没有** ImageNet 归一化：检测是 RGB / 255，仅此而已。
    """
    padded, ratio, pad = letterbox(img, input_size)
    tensor = np.ascontiguousarray(padded.transpose(2, 0, 1))     # HWC -> CHW
    tensor = tensor.astype(np.float32) / 255.0
    return tensor[None, ...], ratio, pad


# ===========================================================================
# 推理器
# ===========================================================================

class OnnxPestDetector:
    """IP102 虫害检测推理器（onnxruntime，CPU）。

    只挂 CPUExecutionProvider，理由与 src/infer/predictor.py 里那条相同：
    本机 torch 是 cu130，onnxruntime-gpu 通常滞后于 CUDA 13，版本地狱不值得踩；
    单图 CPU 推理是毫秒级，够用。
    """

    backend = "onnxruntime-cpu"

    def __init__(
        self,
        onnx_path: str | Path,
        label_map: dict,
        *,
        input_size: int = PEST_INPUT_SIZE,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.7,
        max_det: int = PEST_MAX_DETECTIONS,
    ) -> None:
        import onnxruntime as ort

        self.onnx_path = Path(onnx_path)
        if not self.onnx_path.exists():
            raise FileNotFoundError(f"虫害检测 ONNX 模型不存在：{self.onnx_path}")

        self.label_map = label_map
        self.index_to_label: dict[str, dict] = label_map["index_to_label"]
        self.num_classes = int(label_map["num_classes"])
        self.input_size = int(input_size)
        self.conf_threshold = float(conf_threshold)
        self.iou_threshold = float(iou_threshold)
        self.max_det = int(max_det)

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(self.onnx_path), sess_options=so, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

        if self.num_classes != PEST_NUM_CLASSES:
            logger.warning(
                "标签表类别数 %d 与约定的 %d 不一致，请确认 data/pest_label_map.json "
                "与训练用的 data.yaml 是同一套", self.num_classes, PEST_NUM_CLASSES,
            )
        logger.info(
            "已加载虫害检测模型：%s（输入 %s，输出 %s，%d 类，阈值 conf=%.2f/iou=%.2f）",
            self.onnx_path.name, self.input_name, self.output_name,
            self.num_classes, self.conf_threshold, self.iou_threshold,
        )

    # ---------------------------------------------------------------- 对外

    def detect(self, image: Image.Image) -> dict:
        """单图检测，返回与 /api/diagnose/multimodal 的 pest 字段一致的契约。"""
        t0 = time.perf_counter()
        rgb = np.asarray(image.convert("RGB"))
        orig_shape = rgb.shape[:2]                      # (高, 宽)

        tensor, ratio, pad = preprocess_image(rgb, self.input_size)
        raw = self.session.run([self.output_name], {self.input_name: tensor})[0]

        raw_dets = postprocess(
            raw, orig_shape, ratio, pad,
            self.conf_threshold, self.iou_threshold, self.num_classes, self.max_det,
        )

        detections = []
        for d in raw_dets:
            entry = self.index_to_label.get(str(d["index"]))
            if entry is None:
                # 标签表缺项：宁可丢掉这个框并明确告警，也不要用猜测的类名糊给前端
                logger.warning("标签表缺少下标 %d，已跳过该检测框", d["index"])
                continue
            detections.append({
                "index": d["index"],
                "label_cn": entry["label_cn"],
                "label_en": entry.get("label_en", ""),
                "crop": entry["crop"],
                "pest_type": entry.get("pest_type", ""),
                "confidence": round(d["confidence"], 4),
                "bbox": d["bbox"],
            })

        detections.sort(key=lambda x: -x["confidence"])
        elapsed = (time.perf_counter() - t0) * 1000

        return _assert_contract({
            "detections": detections,
            "count": len(detections),
            # 前端要把框画成百分比定位的覆盖层，必须知道原图尺寸才能换算
            "image_size": [int(orig_shape[1]), int(orig_shape[0])],   # [宽, 高]
            "confidence_threshold": self.conf_threshold,
            "iou_threshold": self.iou_threshold,
            "backend": self.backend,
            "elapsed_ms": round(elapsed, 2),
        })

    def detect_bytes(self, data: bytes) -> dict:
        with Image.open(io.BytesIO(data)) as im:
            return self.detect(im.convert("RGB"))

    def detect_path(self, path: str | Path) -> dict:
        with Image.open(path) as im:
            return self.detect(im.convert("RGB"))


# ===========================================================================
# 工厂
# ===========================================================================

def load_pest_label_map(path: str | Path | None = None) -> dict:
    p = Path(path) if path else path_of("pest_label_map")
    if not p.exists():
        raise FileNotFoundError(f"缺少虫害标签映射 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def get_pest_detector(*, conf_threshold: float | None = None,
                      iou_threshold: float | None = None) -> OnnxPestDetector:
    """构造虫害检测器。

    候选顺序：registry 里 pest_yolov8n.onnx 记的路径 → 约定路径 models/onnx/pest_yolo.onnx。
    找不到就直接抛错并给出该跑哪个脚本——这一步没有「降级到 torch」的兜底：
    检测链路的定位是「没有就明确报无」，而不是悄悄换一个后端改变行为。
    """
    from src.common.config import load_config

    label_map = load_pest_label_map()
    cfg = load_config("default")
    if conf_threshold is None:
        conf_threshold = float(cfg.get("pest_confidence_threshold", 0.25))
    if iou_threshold is None:
        iou_threshold = float(cfg.get("pest_iou_threshold", 0.7))

    reg = {}
    reg_path = path_of("model_registry")
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8")).get("pest_yolov8n", {})
        except json.JSONDecodeError:
            logger.warning("registry.json 解析失败，忽略：%s", reg_path)

    candidates: list[Path] = []
    if reg.get("onnx"):
        candidates.append(resolve(reg["onnx"]))
    candidates.append(path_of("pest_onnx"))

    errors: list[str] = []
    for path in candidates:
        if not path.exists():
            errors.append(f"{path} 不存在")
            continue
        try:
            return OnnxPestDetector(
                path, label_map,
                input_size=int(reg.get("input_size", PEST_INPUT_SIZE)),
                conf_threshold=conf_threshold, iou_threshold=iou_threshold,
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path} 加载失败（{e}）")
            logger.warning("加载失败，尝试下一个候选：%s（%s）", path, e)

    raise FileNotFoundError(
        "找不到可用的虫害检测模型。请先训练并导出：\n"
        "    python scripts/17_train_pest_yolo.py\n"
        "    python scripts/19_export_pest_onnx.py\n"
        "已尝试的候选：\n  " + "\n  ".join(errors)
    )


__all__ = [
    "OnnxPestDetector", "get_pest_detector", "load_pest_label_map",
    # 纯函数（单测直接断言这些）
    "letterbox", "preprocess_image", "xywh_to_xyxy", "box_iou", "nms",
    "clip_boxes", "scale_boxes", "decode_predictions", "postprocess",
]
