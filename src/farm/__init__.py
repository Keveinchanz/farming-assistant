"""阶段 7 —— 我的农田（用户账号、田块、积温生长引擎、农事建议、3D 参数）。

与项目其余部分的边界：
    · 本项目此前所有产物都是**只读文件**（谁读都一样）；这是第一个**按用户划分、
      会被改写**的模块，因此数据落在单独的 sqlite 里（data/farm/farm.db）。
    · src/farm 只依赖 src/common（配置、路径、日志、IO），**不 import backend**。
      FastAPI 的依赖注入、Cookie、路由都在 backend/app 里；反过来 backend 可以用这里。
      这条方向是项目硬规则（README 第二节）：scripts → src ← backend。
"""
