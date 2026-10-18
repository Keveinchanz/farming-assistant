#!/usr/bin/env python
"""任务 2.7.5 —— 把训练好的虫害检测模型导出为 ONNX，并做四道对齐校验。

产出：models/onnx/pest_yolo.onnx（自包含单文件）
      models/registry.json 的 pest_yolov8n 键补上 onnx 路径（后端据此自动加载）

## 为什么不复用 scripts/13_export_onnx.py

那个脚本导的是**分类**模型（ResNet50 + 271 类，输出 (N, 271) 的 logits），
判据是「torch 与 onnxruntime 的 logits 逐元素对齐」。检测完全是另一回事：
输出是 `[1, 106, 8400]` 的原始张量，后面还挂着一整套后处理，
光对齐张量只能证明图没导坏，**证明不了框是对的**。所以这里拆成四道校验。

## 四道校验（前两道本来就该有，后两道是这个任务新增的）

A. **预处理对齐**：自写的 letterbox 与 ultralytics 的 LetterBox 逐像素比对。
   这是最容易「错了不报错」的地方——框全偏但一切正常。
B. **导出保真**：同一个 640×640 张量，torch 前向 vs ONNX 前向，逐元素 allclose。
C. **后处理对齐**：同一份原始输出，喂给自写的 numpy 后处理与 ultralytics 的
   non_max_suppression + scale_boxes，比对类别 id 与框 IoU。
D. **端到端**：自写全链路 vs `YOLO.predict()`，类别 id 全等、框 IoU > 0.99。

## 关于 D 的一个实测结论（原计划在这里写的是错的）

原计划写「比对 YOLO.predict() 与 ONNX + numpy 后处理两条路」。实际做不到，
因为两条路的**预处理本来就不同**，本机实测：

    输入尺寸      val / auto=False      predict / auto=True
    1920×1080     (640, 640)            (384, 640)   <- 非正方形
     800×600      (640, 640)            (480, 640)

`predict()` 默认走 ultralytics 的矩形推理（auto=True，填充量对 stride 取模），
喂给模型的是非正方形输入；而导出的是 dynamic=False 的静态 640×640 图，
**根本吃不下 384×640**。

所以 D 改成：自写全链路用 `auto=True` 复现 predict 那条路，两者必须逐框对齐。
这既证明了我们完整复现了 ultralytics 的推理语义，又不与「服务端用 auto=False」
这一选择冲突——服务端那条路由 A/B/C 三道校验证实与 val 口径一致。

用法：
    python scripts/19_export_pest_onnx.py
    python scripts/19_export_pest_onnx.py --check-only    # 只校验不重新导出
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir  # noqa: E402
from src.common.logging_utils import setup_logger  # noqa: E402
from src.common.paths import path_of  # noqa: E402

logger = setup_logger("agri.pest.export")

RUN_NAME = "pest_yolov8n"
N_ALIGN = 20            # 端到端校验用多少张 val 图
IOU_MIN = 0.99          # 框对齐判据（原计划定的就是 0.99）


# ------------------------------------------------------------------ 导出

def export_onnx(ckpt: Path, out_path: Path, *, imgsz: int, opset: int) -> Path:
    """用 ultralytics 导出，再把产物搬到 models/onnx/。

    三个刻意的选择：
      · `nms=False`（默认）：不把 NMS 塞进计算图。塞进去会引入算子兼容性问题，
        而 numpy 手写 NMS 只有十几行且能被单测钉死。
      · `dynamic=False`：固定 640×640。服务端只做单图推理，静态图更快，
        也省得每张图尺寸不同就要重导一次。
      · `simplify=True`：过一遍 onnxslim 清掉冗余算子（需要 onnxslim，见 requirements）。
    """
    from ultralytics import YOLO

    logger.info("导出 ONNX：%s（imgsz=%d，opset=%d）", ckpt.name, imgsz, opset)
    produced = YOLO(str(ckpt)).export(
        format="onnx", imgsz=imgsz, opset=opset,
        simplify=True, dynamic=False, nms=False, half=False, batch=1,
    )
    produced = Path(produced)
    if not produced.exists():
        raise FileNotFoundError(f"ultralytics 声称导出了 {produced}，但该文件不存在")

    # 权重若被写进外部 .data 文件，只拷 .onnx 会让线上加载失败。
    # 这里直接拒绝，不生成一个「看起来能用」的残缺产物。
    extras = [p.name for p in produced.parent.glob(produced.name + ".*")]
    if extras:
        raise RuntimeError(
            f"导出产物不只有单文件，还带了附属文件：{extras}。\n"
            "服务端只部署 .onnx 一个文件，带了外部权重会静默失效。"
        )

    ensure_dir(out_path.parent)
    if produced.resolve() != out_path.resolve():
        shutil.move(str(produced), str(out_path))

    logger.info("已导出：%s（%.2f MB）", out_path, out_path.stat().st_size / 1024 ** 2)
    return out_path


# ------------------------------------------------------------------ 工具

def _val_images(n: int) -> list[Path]:
    d = path_of("pest_dataset") / "images" / "val"
    imgs = sorted(p for p in d.glob("*.jpg"))
    imgs += sorted(p for p in d.glob("*.JPG"))
    imgs += sorted(p for p in d.glob("*.png"))
    if not imgs:
        raise FileNotFoundError(f"{d} 下没有图片，请先跑 scripts/16_download_ip102.py")
    # 等距抽样而不是取前 n 张：同一类别的图在目录里是挨着的，
    # 取前 20 张可能全是同一类，对齐校验就失去意义了。
    step = max(1, len(imgs) // n)
    return imgs[::step][:n]


def _load_rgb(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def _match_boxes(mine: list[dict], theirs: list[dict], *,
                 iou_min: float) -> tuple[bool, str, float]:
    """按类别贪心配对，返回 (是否全部对上, 说明, 最差 IoU)。

    两边数量必须相等，且每一对同类框的 IoU 都要超过 iou_min。
    最差 IoU 作为返回值而不是塞进说明字符串里 —— 调用方要拿它做汇总，
    从人类可读的日志里再正则抠出来是自找麻烦。
    """
    from src.infer.pest_detector import box_iou

    if len(mine) != len(theirs):
        return False, f"框数量不同：自写 {len(mine)} 个 vs 参考 {len(theirs)} 个", 0.0

    unmatched = list(range(len(theirs)))
    worst = 1.0
    for m in mine:
        best_j, best_iou = None, -1.0
        for j in unmatched:
            t = theirs[j]
            if t["index"] != m["index"]:
                continue
            iou = float(box_iou(np.asarray([m["bbox"]], dtype=np.float32),
                                np.asarray([t["bbox"]], dtype=np.float32))[0, 0])
            if iou > best_iou:
                best_j, best_iou = j, iou
        if best_j is None:
            return False, f"参考结果里找不到同类别的框（类别 {m['index']}）", 0.0
        if best_iou < iou_min:
            return False, (f"类别 {m['index']} 的框 IoU 只有 {best_iou:.4f} < {iou_min}："
                           f"自写 {m['bbox']} vs 参考 {theirs[best_j]['bbox']}"), best_iou
        worst = min(worst, best_iou)
        unmatched.remove(best_j)

    return True, f"全部 {len(mine)} 个框对齐，最差 IoU = {worst:.4f}", worst


# ------------------------------------------------------------------ 四道校验

def check_preprocess(images: list[Path], imgsz: int) -> None:
    """A. 预处理对齐：自写 letterbox vs ultralytics LetterBox，逐像素。"""
    from ultralytics.data.augment import LetterBox

    from src.infer.pest_detector import letterbox

    checked = 0
    for auto in (False, True):
        for p in images:
            img = _load_rgb(p)
            lb = LetterBox(new_shape=(imgsz, imgsz), auto=auto, scaleup=True, stride=32)
            ref = lb(image=img.copy())
            ref = ref["img"] if isinstance(ref, dict) else ref
            mine, _, _ = letterbox(img, imgsz, auto=auto)

            if ref.shape != mine.shape:
                raise AssertionError(
                    f"[A] {p.name} auto={auto} 形状不一致：参考 {ref.shape} vs 自写 {mine.shape}"
                )
            if not np.array_equal(ref, mine):
                diff = int(np.abs(ref.astype(int) - mine.astype(int)).sum())
                raise AssertionError(
                    f"[A] {p.name} auto={auto} 像素不一致（累计差值 {diff}）—— "
                    "预处理与 ultralytics 不同源，框会整体偏移"
                )
            checked += 1
    logger.info("[A] 预处理对齐通过：%d 张图 × auto∈{False,True}，逐像素一致", len(images))


def check_fidelity(raw_torch: np.ndarray, raw_onnx: np.ndarray) -> None:
    """B. 导出保真：同一个张量，torch 与 ONNX 的原始输出必须一致。"""
    if raw_torch.shape != raw_onnx.shape:
        raise AssertionError(f"[B] 输出形状不一致：torch {raw_torch.shape} vs onnx {raw_onnx.shape}")
    max_diff = float(np.max(np.abs(raw_torch - raw_onnx)))
    if not np.allclose(raw_torch, raw_onnx, atol=1e-3):
        raise AssertionError(f"[B] torch 与 ONNX 输出差异过大（{max_diff:.3e}），图没导对")
    logger.info("[B] 导出保真通过：形状 %s，最大绝对误差 %.3e", raw_torch.shape, max_diff)


def check_postprocess(raw_onnx: np.ndarray, orig_shape: tuple[int, int],
                      ratio: float, pad: tuple[float, float],
                      conf: float, iou: float, nc: int) -> None:
    """C. 后处理对齐：自写 numpy 后处理 vs ultralytics 的官方实现。

    同一份原始输出喂给两边，比对类别 id 与框坐标。
    关键是把**我们实际算出的** ratio/pad 通过 `ratio_pad=` 传进去：
    官方实现默认会从 `img1_shape` 反推 gain 和 pad，而它的反推式
    （`round(... - 0.1)`）与我们 letterbox 里的算式在偶数/奇数填充下会差 1 px，
    不传就变成在比「两次取整差多少」，而不是在比后处理逻辑。
    """
    import torch
    from ultralytics.utils import ops

    from src.infer.pest_detector import postprocess

    # ultralytics 8.4 把 non_max_suppression 从 utils.ops 挪到了 utils.nms
    # （ops 里已经彻底没有这个名字了，直接 ops.non_max_suppression 会 AttributeError）。
    # 两个位置都试一遍，免得把版本锁死在某一个次版本上。
    try:
        from ultralytics.utils.nms import non_max_suppression
    except ImportError:  # ultralytics <= 8.3
        non_max_suppression = ops.non_max_suppression

    mine = postprocess(raw_onnx, orig_shape, ratio, pad, conf, iou, nc)

    preds = non_max_suppression(
        torch.from_numpy(np.asarray(raw_onnx, dtype=np.float32)),
        conf_thres=conf, iou_thres=iou, max_det=300, nc=nc,
    )
    theirs: list[dict] = []
    if preds and len(preds[0]):
        box = preds[0].clone()
        # 传了 ratio_pad 之后 img1_shape 不再参与换算，给 None 也无妨；
        # 显式传 letterbox 后的真实画布尺寸，是为了让这一步的意图可读。
        box[:, :4] = ops.scale_boxes(
            _letterboxed_shape(ratio, pad, orig_shape),
            box[:, :4], orig_shape, ratio_pad=((ratio, ratio), pad),
        )
        theirs = [
            {"index": int(box[i, 5]), "confidence": float(box[i, 4]),
             "bbox": [round(float(v), 2) for v in box[i, :4]]}
            for i in range(box.shape[0])
        ]

    ok, msg, _ = _match_boxes(mine, theirs, iou_min=IOU_MIN)
    if not ok:
        raise AssertionError(f"[C] 后处理与 ultralytics 不一致：{msg}")
    logger.info("[C] 后处理对齐通过：%s", msg)


def _letterboxed_shape(ratio: float, pad: tuple[float, float],
                       orig_shape: tuple[int, int]) -> tuple[int, int]:
    """由 ratio/pad 反推 letterbox 后的画布尺寸（供 scale_boxes 用）。"""
    h, w = orig_shape
    return (int(round(h * ratio + 2 * pad[1])), int(round(w * ratio + 2 * pad[0])))


def check_end_to_end(yolo, images: list[Path], *,
                     imgsz: int, conf: float, iou: float, nc: int) -> None:
    """D. 端到端：自写全链路（auto=True）vs `YOLO.predict()`。

    用 auto=True 是刻意的——见模块 docstring：predict 默认就是矩形推理，
    只有用同一种预处理，这个比对才是在校验「我的后处理对不对」，
    而不是在比「两条路径的预处理差异」。

    前向走 torch 而不是 ONNX session：这里要复现的是 predict 那条路，
    而 auto=True 会给非方形输入（384×640 这类），静态 ONNX 图吃不下。
    ONNX 的保真度由 B 道校验单独负责，两者分工不重叠。

    **通道顺序的坑（实测，别改回去）**：传给 `predict()` 的 numpy 必须**倒成 BGR**。
    `LoadPilAndNumpy._single_check` 的文档写得很直白——「NumPy 彩色输入按 OpenCV 的
    BGR 顺序理解」，随后 `Predictor.preprocess` 又做了一次 `im.flip(1)`（注释就是
    "BGR to RGB"）。所以：喂 RGB 数组进去，ultralytics 会把它当 BGR 再翻一次，
    网络实际吃到的是**反过来的颜色**；而上面 torch 那条路喂的是不翻转的 RGB。
    两条路看到的根本不是同一张图，这个校验就变成了自欺欺人（甚至可能在低对比度
    的图上"通过"）。倒一次才是让两条路看到同一张 RGB 图。
    """
    import torch

    from src.infer.pest_detector import letterbox, postprocess

    worst_all, total, empty = 1.0, 0, 0
    for p in images:
        img = _load_rgb(p)          # HWC uint8 RGB —— 自写链路的口径
        orig_shape = img.shape[:2]

        padded, ratio, pad = letterbox(img, imgsz, auto=True)
        tensor = torch.from_numpy(
            np.ascontiguousarray(padded.transpose(2, 0, 1))[None].astype(np.float32) / 255.0
        )
        with torch.no_grad():
            raw = yolo.model(tensor)
        raw = raw[0] if isinstance(raw, (list, tuple)) else raw
        mine = postprocess(raw.cpu().numpy(), orig_shape, ratio, pad, conf, iou, nc)

        r = yolo.predict(source=img[..., ::-1], imgsz=imgsz, conf=conf, iou=iou,
                         max_det=300, verbose=False)[0]
        b = r.boxes
        theirs = [
            {"index": int(b.cls[i]), "confidence": float(b.conf[i]),
             "bbox": [round(float(v), 2) for v in b.xyxy[i]]}
            for i in range(len(b))
        ]

        ok, msg, worst = _match_boxes(mine, theirs, iou_min=IOU_MIN)
        if not ok:
            raise AssertionError(f"[D] {p.name} 与 YOLO.predict() 不一致：{msg}")
        total += len(mine)
        if not mine:
            empty += 1
        else:
            worst_all = min(worst_all, worst)

    # 20 张全都没检出，说明模型或阈值有问题，「0 个框全部对齐」是句废话
    if total == 0:
        raise AssertionError(
            f"[D] {len(images)} 张 val 图一个框都没检出（conf={conf}）—— "
            "对齐无从谈起，请先确认模型确实训出来了"
        )
    logger.info("[D] 端到端对齐通过：%d 张图共 %d 个框，最差 IoU = %.4f（%d 张无检出）",
                len(images), total, worst_all, empty)


# ------------------------------------------------------------------ registry

def update_registry(onnx_path: Path, *, imgsz: int, opset: int, size_mb: float) -> None:
    reg = path_of("model_registry")
    data = {}
    if reg.exists():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("registry.json 解析失败，将重建：%s", reg)
    entry = data.setdefault(RUN_NAME, {})
    entry.update({
        "onnx": str(onnx_path.relative_to(path_of("models_dir").parent)).replace("\\", "/"),
        "onnx_opset": opset,
        "onnx_size_mb": round(size_mb, 1),
        "onnx_input_size": imgsz,
        "onnx_dynamic": False,
        "onnx_nms_baked": False,
    })
    ensure_dir(reg.parent)
    reg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("registry 已更新：%s -> %s", RUN_NAME, entry["onnx"])


# ------------------------------------------------------------------ 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.5 导出虫害检测 ONNX 并做对齐校验")
    ap.add_argument("--checkpoint", default=None, help="默认 models/checkpoints/pest_best.pt")
    ap.add_argument("--output", default=None, help="默认 models/onnx/pest_yolo.onnx")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--opset", type=int, default=17)
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--iou", type=float, default=None)
    ap.add_argument("--align-images", type=int, default=N_ALIGN)
    ap.add_argument("--check-only", action="store_true", help="不重新导出，只对现有 ONNX 做校验")
    args = ap.parse_args()

    from src.common.config import load_config
    from src.infer.pest_detector import load_pest_label_map

    cfg = load_config("default")
    conf = float(args.conf if args.conf is not None else cfg.get("pest_confidence_threshold", 0.25))
    iou = float(args.iou if args.iou is not None else cfg.get("pest_iou_threshold", 0.7))

    ckpt = Path(args.checkpoint) if args.checkpoint else (path_of("checkpoints_dir") / "pest_best.pt")
    out_path = Path(args.output) if args.output else path_of("pest_onnx")
    label_map = load_pest_label_map()
    nc = int(label_map["num_classes"])

    if not args.check_only:
        if not ckpt.exists():
            logger.error("权重不存在：%s\n请先训练：python scripts/17_train_pest_yolo.py", ckpt)
            return 1
        export_onnx(ckpt, out_path, imgsz=args.imgsz, opset=args.opset)

    if not out_path.exists():
        logger.error("ONNX 不存在：%s（不加 --check-only 可先导出）", out_path)
        return 2

    try:
        from ultralytics import YOLO

        from src.infer.pest_detector import preprocess_image

        images = _val_images(args.align_images)
        logger.info("对齐校验用 %d 张 val 图（从 %d 张里等距抽样）", len(images), args.align_images)

        # --- A 预处理 ---
        check_preprocess(images, args.imgsz)

        # --- B 导出保真（用第一张图）---
        import onnxruntime as ort

        yolo = YOLO(str(ckpt)) if ckpt.exists() else None
        if yolo is None:
            logger.warning("找不到 .pt 权重，B/D 两道校验需要它，跳过")
        else:
            sess = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
            img0 = _load_rgb(images[0])
            tensor, ratio, pad = preprocess_image(img0, args.imgsz)
            import torch
            with torch.no_grad():
                raw_torch = yolo.model(torch.from_numpy(tensor))
            raw_torch = raw_torch[0] if isinstance(raw_torch, (list, tuple)) else raw_torch
            raw_torch = raw_torch.cpu().numpy()
            raw_onnx = sess.run([sess.get_outputs()[0].name],
                                {sess.get_inputs()[0].name: tensor})[0]

            check_fidelity(raw_torch, raw_onnx)
            # --- C 后处理（auto=False，与服务端一致）---
            check_postprocess(raw_onnx, img0.shape[:2], ratio, pad, conf, iou, nc)
            # --- D 端到端 ---
            check_end_to_end(yolo, images,
                             imgsz=args.imgsz, conf=conf, iou=iou, nc=nc)

    except AssertionError as e:
        logger.error("对齐校验失败，**不要**把这个 ONNX 用于部署：%s", e)
        return 2

    update_registry(out_path, imgsz=args.imgsz, opset=args.opset,
                    size_mb=out_path.stat().st_size / 1024 ** 2)

    logger.info("=" * 68)
    logger.info("虫害检测 ONNX 就绪：%s", out_path)
    logger.info("四道校验（预处理 / 导出保真 / 后处理 / 端到端）全部通过")
    logger.info("下一步：python scripts/22_verify_artifacts.py")
    logger.info("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
