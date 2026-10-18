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
