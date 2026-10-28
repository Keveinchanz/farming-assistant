"""全局常量 —— 训练、导出、推理三处必须共用同一份，避免"训练 90% / 线上乱答"。

最重要的一条：**归一化参数必须唯一**。训练时的 ToTensor + Normalize 与后端推理
前的预处理如果 mean/std 不一致，模型会稳定输出错误结果，而且不报任何错。
因此把 IMAGENET_MEAN / IMAGENET_STD 放在这里，transforms.py、predictor.py 都从这里取。
"""

from __future__ import annotations

# ImageNet 预训练权重对应的归一化参数（torchvision 官方值）
IMAGENET_MEAN: tuple[float, float, float] = (0.485, 0.456, 0.406)
IMAGENET_STD: tuple[float, float, float] = (0.229, 0.224, 0.225)

# 默认输入分辨率。ResNet50 / ViT-B/16 都用 224；256 的原始图先 Resize 到 256 再 CenterCrop。
DEFAULT_INPUT_SIZE: int = 224
DEFAULT_RESIZE_SIZE: int = 256

# 模型导出与推理共用的输入张量名
ONNX_INPUT_NAME: str = "input"
ONNX_OUTPUT_NAME: str = "logits"

# 支持的主干网络名称 -> 实现位置标识
SUPPORTED_ARCHS: tuple[str, ...] = ("resnet50", "vit_base_patch16_224")


# ---------------------------------------------------------------------------
# 虫害目标检测（IP102 / YOLOv8n）
#
# 这一节与上面的分类常量是**两套独立的东西**，同名概念含义不同，不要互相套用：
#   - DEFAULT_INPUT_SIZE = 224 是分类（短边缩放 + CenterCrop）的输入；
#     PEST_INPUT_SIZE = 640 是检测（letterbox 等比例缩放 + 灰边填充）的输入。
#   - IMAGENET_MEAN/STD 归一化**只适用于分类**；检测是 RGB / 255，不做减均值除方差，
#     误用 IMAGENET_MEAN 会让检测结果整体崩掉且不报错。
# ---------------------------------------------------------------------------

PEST_INPUT_SIZE: int = 640
PEST_NUM_CLASSES: int = 102
# letterbox 的填充色。YOLO 系列的约定值 114，训练与推理必须一致
PEST_PAD_VALUE: int = 114
# YOLOv8 的最大下采样倍数，letterbox 的边对齐用它
PEST_STRIDE: int = 32
# 单图最多保留多少个框（与 ultralytics 的 max_det 默认值一致）
PEST_MAX_DETECTIONS: int = 300


# ---------------------------------------------------------------------------
# 虫害分类复核（IP102 分类集 / ResNet50，102 类）
#
# 与上面的检测是**同一套 102 类标签空间**，类别索引可以直接互换，区别只在输入形态：
# 检测吃 640 的 letterbox 整图，分类吃 224 的虫体小图。所以分类器不能自己再建一份
# 标签表，必须复用 pest_label_map.json，否则两个模型的「第 48 类」会指向不同的虫。
#
# 存在的理由：IP102 的检测标注只有 18976 张带框图，5 个类在 train/val/test 里一个框
# 都没有，检测路径永远给不出它们。分类集 102 类齐全，于是补一条「检测不到框就让分类器
# 看整图」的兜底路径。
# ---------------------------------------------------------------------------

# 与 scripts/45、47 的训练/评测口径保持一致，改一个就得三处一起改
PEST_CLS_INPUT_SIZE: int = 224
PEST_CLS_CROP_SIZE: int = 256        # 先在 256 上裁，再缩到 255 中心裁 224
PEST_CLS_CROP_PAD: float = 0.25      # 框外扩比例
# 级联门控：检测最高置信度低于 gate 时才请分类器改判。gate 在 val 上选出、test 上复核，
# 是本仓库 script/47 扫出来的后验参数，不是拍脑袋定的
PEST_CLS_GATE: float = 0.6
PEST_CLS_MARGIN: float = 0.0
# 兜底时分类 Top-1 低于这个概率就只回候选、不下结论
PEST_CLS_FALLBACK_MIN_PROB: float = 0.35
# 检测标注里零框的 5 个类。业务上它们本来就难以画框（螨类 0.1–0.2 mm、蚧壳虫固定不动），
# 只能走分类兜底
PEST_ZERO_BOX_CLASSES: tuple[int, ...] = (59, 60, 63, 75, 80)
