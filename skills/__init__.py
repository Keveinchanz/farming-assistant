"""按功能封装的模型技能。

两个技能：
    crop-diagnosis  病虫害检测 + 多模态融合诊断
    farm-growth     农田生长 + 收获预测 + 3D 植株参数

调用方式：
    Python   `from skills.registry import execute`
    CLI      `python -m skills list`
    HTTP     `GET /api/skills`、`POST /api/skills/{name}`

依赖方向固定为 `backend → skills → src`；本包不导入 backend，也不依赖 FastAPI。
"""

__all__ = ["registry"]
