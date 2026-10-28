"""一页纸问诊单的数据模型（分享页 / PDF / 二维码共用一份快照）。

**为什么单独一个文件**：这份结构同时被三处消费 —— `share.html` 的只读页、
`src/export/sheet_pdf.py` 的 PDF、以及分享接口的入参校验。放在 models.py 里
它会淹没在账号与农田模型里，而改字段时最需要一眼看到「谁在用它」。

**为什么每个字段都限制长度**：分享链接是公开的，快照是用户提交的内容。
不设上限，一条 10 MB 的处置建议就能把库和二维码一起撑坏。上限按「一张 A4
能写下多少字」给，不按数据库字段长度给。

注意：这里**没有图片字段**，也不该有 —— 二维码背后是村里的手机流量，
农户要看的是结论和「先干什么」。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SheetResult(BaseModel):
    name: str = Field("", max_length=80, description="结论名（病害/虫害中文名）")
    pest: bool = Field(False, description="是否虫害结论")
    healthy: bool = Field(False, description="是否「未见明显病斑」")
    confidence: int | None = Field(None, ge=0, le=100, description="置信度百分比，未知为 null")


class SheetSection(BaseModel):
    title: str = Field("", max_length=60)
    content: str = Field("", max_length=1200)


class SheetRegion(BaseModel):
    province: str = Field("", max_length=40)
    items: list[str] = Field(default_factory=list, max_length=6)
    demo: bool = Field(False, description="是否为演示/推导数据（分享页必须原样标注）")


class SheetSnapshot(BaseModel):
    """一张问诊单。字段名与 diagnose.js 的 `_sheetPayload()` 一一对应。"""

    v: int = Field(1, ge=1, le=9, description="快照版本，将来加字段时用来兼容旧链接")
    no: str = Field("", max_length=40, description="诊断编号，未归档为空")
    date: str = Field("", max_length=20)
    subject: str = Field("", max_length=60, description="服务对象（农户/合作社），不是账号")
    field: str = Field("", max_length=60)
    crop: str = Field("", max_length=20)
    sources: list[str] = Field(default_factory=list, max_length=6, description="送检方式")
    scene: str = Field("", max_length=1500, description="现场原始描述（文字优先，其次语音转写）")
    result: SheetResult = Field(default_factory=SheetResult)
    summary: str = Field("", max_length=1500)
    basis: str = Field("", max_length=300, description="模型候选，如「玉米大斑病 85% · …」")
    sections: list[SheetSection] = Field(default_factory=list, max_length=8)
    medication: str = Field("", max_length=1500, description="用药提醒（不含剂量，见 diagnose.js）")
    recheck: str = Field("", max_length=20, description="回访日期 YYYY-MM-DD，未排为空")
    region: SheetRegion = Field(default_factory=SheetRegion)


class SheetShareRequest(BaseModel):
    payload: SheetSnapshot


class ShareData(BaseModel):
    token: str
    path: str = Field(..., description="站内相对路径，前端复制用")
    url: str = Field(..., description="完整地址（按当前请求的 Host 拼，隧道下也是对的）")
    expires_at: str
    days: int


class SharedSheetData(BaseModel):
    payload: SheetSnapshot
    created_at: str
    expires_at: str
