"""虫害复核编排 —— 检测结果与分类结果的合并规则。

`src/infer/pest_detector.py` 只负责「图里有哪些框」，`src/infer/pest_classifier.py`
只负责「这块虫体是哪一类」。把两者拼成一个结论的策略放在这里，单独一个文件，原因是
它包含三个**可调且有后验依据**的判断，混进任何一边都会让那两个模块变得不好测：

## 一、什么时候请分类器改判（级联）

单阶段——直接取检测框的类别——在验证集上已经判对 75.64%。全量换成两阶段反而掉到
73.34%，因为分类器在自己不确定的样本上会拉低整体。所以不做全量替换，只做补丁：

    检测最高置信度 < gate            → 它自己也没底，交分类器
    最高与次高置信度之差 < margin    → 两个框分不开，也交分类器

gate 与 margin 由 `scripts/47_eval_two_stage.py` 在 val 上扫、在 test 上复核：
gate=0.60、margin=0.00 时验证集 76.23%、测试集 74.75%，介入率约三成。
**这两个数是后验参数，改它就得重跑 47 号**，不能凭手感调。

## 二、检测一个框都没给时怎么办（兜底）

IP102 的检测标注缺 5 个类（59/60/63/75/80，螨类与蚧壳虫），它们的检测输出头从未见过
正样本，正常路径下永远不出框。分类集里这 5 类有图，所以此时把整图交分类器。

**这条路径只能给线索，不能给结论。** 分类器只有 102 个虫名，没有「不是虫」这一类，
softmax 必然把任何输入映射到其中一类，因此对病害叶片这类域外图像也会给出看似自信的
答案。实测：一张玉米叶斑的病害照片，整图分类报「亚洲玉米螟 41.3%」，与次高名的间隔
0.36，比不少真虫图还高，概率门槛挡不住。所以 `mode=fallback` 时措辞一律是「仅作线索、
不作结论，建议补拍」，融合环节不得把它当作已检出的虫害写进问诊单结论。

5 个零框类在 IP102 测试集上的分类表现也不是全都能认：宏平均 Top-1 45.4%、Top-5 71.7%，
低于 102 类整体的 60.6%。个别类差距很大，茶跗线螨在 26 张测试图上只有 15.4%。

## 三、改判后置信度怎么写

只改类别，**不动检测置信度**。融合值 `检测置信度 × 分类概率` 虽然语义更完整，
但它是联合概率，通常只有 0.2 上下；拿它覆盖 confidence 会让前端和台账里所有
「置信度低于阈值就不显示」的判断跟着变，属于用一个不报错的方式改变既有行为。
融合值放在 `rewrites[].fused` 里给前端展示，原始值一个也不丢。
"""

from __future__ import annotations

import time

from src.common.constants import (
    PEST_CLS_FALLBACK_MIN_PROB,
    PEST_CLS_GATE,
    PEST_CLS_MARGIN,
    PEST_ZERO_BOX_CLASSES,
)
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.pest.review")

REVIEW_KEYS = (
    "mode", "applied", "gate", "margin", "trigger",
    "candidates", "rewrites", "note", "elapsed_ms",
)
REWRITE_KEYS = (
    "index", "from_index", "from_label", "to_index", "to_label",
    "to_label_en", "det_confidence", "cls_prob", "fused",
)


class PestReviewer:
    """在检测结果上叠加分类复核。分类器为 None 时不构造本对象。"""

    def __init__(self, classifier, *, gate: float = PEST_CLS_GATE,
                 margin: float = PEST_CLS_MARGIN,
                 fallback_min_prob: float = PEST_CLS_FALLBACK_MIN_PROB,
                 topk: int = 5):
        self.classifier = classifier
        self.gate = float(gate)
        self.margin = float(margin)
        self.fallback_min_prob = float(fallback_min_prob)
        self.topk = int(topk)

    # ---------------------------------------------------------------- 内部

    def _base(self) -> dict:
        return {
            "mode": "single",
            "applied": False,
            "gate": self.gate,
            "margin": self.margin,
            "trigger": "",
            "candidates": [],
            "rewrites": [],
            "note": "",
            "elapsed_ms": 0.0,
        }

    def _fallback(self, image, base: dict) -> dict:
        """无框路径：整图分类，结论只作线索。

        这一条路径的局限必须写清楚：分类器只有 102 个虫名，没有「不是虫」这一类，
        softmax 必然把任何输入映射到其中一类。实测过一张玉米叶斑的病害照片，
        整图分类给出「亚洲玉米螟 41.3%」，间隔 0.36，比不少真虫图还自信 —— 概率门槛
        挡不住这种误报。所以这里只产出线索，措辞上不说「检出」，并明确要求补拍。
        是否采信由融合环节决定，本模块不替它下判断。
        """
        cands = self.classifier.classify_image(image, topk=self.topk)
        base["candidates"] = cands
        base["trigger"] = "no_detection"
        top1 = cands[0]["prob"] if cands else 0.0
        if cands and top1 >= self.fallback_min_prob:
            base["mode"] = "fallback"
            base["applied"] = True
            base["note"] = (
                "未检出虫体框。整图外观接近「%s」，可信度 %d%%，仅作线索、不作结论；"
                "建议对准虫体补拍一张特写，或改用文字描述症状。"
                % (cands[0]["label_cn"] or "未知", round(top1 * 100))
            )
        else:
            base["mode"] = "none"
            base["applied"] = False
            base["note"] = (
                "未检出虫体框，整图外观也没有明确指向，不下结论；"
                "建议靠近虫体、对准叶片正反面补拍一张。"
            )
        return base

    def _cascade(self, image, detections: list[dict], trigger: str, base: dict) -> dict:
        """有框路径：逐框裁切分类，按「检测置信度 × 分类概率」取联合最高。"""
        best = None
        for i, det in enumerate(detections):
            try:
                cands = self.classifier.classify_crop(image, det["bbox"], topk=1)
            except Exception as exc:                      # 单框失败不能带崩整次诊断
                logger.warning("第 %d 个框裁切分类失败：%s", i, exc)
                continue
            if not cands:
                continue
            cls = cands[0]
            fused = float(det.get("confidence", 0.0)) * float(cls["prob"])
            if best is None or fused > best["fused"]:
                best = {"i": i, "cls": cls, "fused": fused}

        if best is None:
            base["trigger"] = trigger
            base["note"] = "分类复核未产出可用候选，沿用检测结果。"
            return base

        i, cls = best["i"], best["cls"]
        det = detections[i]
        base["candidates"] = [cls]
        base["trigger"] = trigger
        if int(cls["index"]) == int(det["index"]):
            # 复核走过了但结论没变。mode 报 cascade（表示分类器介入过），applied 报 False
            # （表示结论未被改写）——两个字段分别回答「走没走这条路」和「结果变没变」。
            base["mode"] = "cascade"
            base["note"] = "分类复核与检测结论一致，维持原判。"
            return base

        base["mode"] = "cascade"
        base["applied"] = True
        base["rewrites"] = [{
            "index": i,
            "from_index": int(det["index"]),
            "from_label": det.get("label_cn", ""),
            "to_index": int(cls["index"]),
            "to_label": cls.get("label_cn", ""),
            "to_label_en": cls.get("label_en", ""),
            "det_confidence": round(float(det.get("confidence", 0.0)), 4),
            "cls_prob": float(cls["prob"]),
            "fused": round(best["fused"], 4),
        }]
        base["note"] = (
            "检测置信度 %d%% 未过门控 %d%%，已交分类器复核，结论由「%s」改为「%s」。"
            % (round(float(det.get("confidence", 0.0)) * 100), round(self.gate * 100),
               det.get("label_cn", ""), cls.get("label_cn", ""))
        )
        return base

    # ---------------------------------------------------------------- 对外

    def review(self, image, detections: list[dict]) -> dict:
        """在已排序的检测框上做复核，返回 review 块。

        `detections` 必须按 confidence 降序，本方法依赖「第 0 个是最高分」这个前提。
        """
        t0 = time.perf_counter()
        base = self._base()

        if not detections:
            base = self._fallback(image, base)
        else:
            top_c = float(detections[0].get("confidence", 0.0))
            second_c = float(detections[1].get("confidence", 0.0)) if len(detections) > 1 else 0.0
            trigger = ""
            if top_c < self.gate:
                trigger = "low_confidence"
            elif len(detections) > 1 and (top_c - second_c) < self.margin:
                trigger = "close_margin"
            if trigger:
                base = self._cascade(image, detections, trigger, base)

        base["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return base


def apply_rewrites(detections: list[dict], review: dict | None) -> list[dict]:
    """把 review 里判定的改判落到检测框上。只改类别，不动置信度（理由见模块头）。"""
    if not review or not review.get("rewrites"):
        return detections
    for rw in review["rewrites"]:
        i = rw.get("index")
        if i is None or not (0 <= i < len(detections)):
            continue
        d = detections[i]
        d["index"] = rw["to_index"]
        d["label_cn"] = rw["to_label"]
        d["label_en"] = rw.get("to_label_en", "")
    return detections
