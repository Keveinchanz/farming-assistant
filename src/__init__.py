"""乡村农技多方言智能服务平台 —— 可复用源码包。

依赖方向约定（硬约束）：
    scripts/  只做参数解析 + 调用 src/
    src/      不 import backend/
    backend/  只 import src/
这样训练评估与线上推理共用同一份实现（如 src/infer/predictor.py），避免逻辑分叉。
"""

__version__ = "1.0.0"
