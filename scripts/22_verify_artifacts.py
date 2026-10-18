#!/usr/bin/env python
"""任务 6.2 功能测试 —— 逐条核对全部 33 项任务的产出物是否齐备且内容有效。

与 21_smoke_test_api.py 的分工：
    21 号脚本测的是「运行中的接口行为是否正确」（需要后端已启动）；
    本脚本测的是「磁盘上的交付物是否存在、结构是否完整、数据是否自洽」（不需要启动服务）。
两者合起来构成任务 6.2 的功能测试。

用法：
    python scripts/22_verify_artifacts.py
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402
from src.common.paths import PROJECT_ROOT, path_of  # noqa: E402

logger = setup_logger("agri.verify")

RESULTS: list[tuple[str, str, bool, str]] = []


def check(task: str, desc: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((task, desc, bool(ok), detail))


def exists(rel: str) -> bool:
    return (PROJECT_ROOT / rel).exists()


def load_json(rel: str):
    p = PROJECT_ROOT / rel
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def read_csv_rows(rel: str) -> list[dict]:
    p = PROJECT_ROOT / rel
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ==================================================================== 阶段 1

def stage1() -> None:
    from src.common.io_utils import list_images

    ds = path_of("processed_dataset")
    crops = sorted([d for d in ds.iterdir() if d.is_dir()]) if ds.exists() else []
    classes = [c for crop in crops for c in crop.iterdir() if c.is_dir()]
    # 用共享的 list_images：它按 suffix.lower() 过滤。
    # 不能写成 rglob("*.jpg") + rglob("*.JPG") —— Windows 文件系统大小写不敏感，
    # 两个模式会匹配到同一批文件，图片数会翻倍（本脚本初版就踩了这个坑）。
    images = list_images(ds) if ds.exists() else []

    check("1.1", "按「作物大类-病虫害名称」二级目录归档",
          len(crops) == 47 and len(classes) == 271 and len(images) == 10165,
          f"{len(crops)} 个作物 / {len(classes)} 个类别 / {len(images)} 张图片（期望 47 / 271 / 10165）")

    unparsed = path_of("reports_dir") / "unparsed_crops.txt"
    size = unparsed.stat().st_size if unparsed.exists() else -1
    check("1.1", "作物解析无未命中类别", size == 0,
          "unparsed_crops.txt 为空" if size == 0 else f"报告大小 {size} 字节")

    splits = {}
    for name in ("train", "val", "test"):
        rows = read_csv_rows(f"data/splits/{name}.csv")
        splits[name] = rows
        check("1.2", f"{name}.csv 路径标签对照表",
              len(rows) > 0 and all(k in (rows[0] if rows else {}) for k in ("path", "index", "label_cn")),
              f"{len(rows)} 条；字段 {list(rows[0]) if rows else '—'}")

    total = sum(len(v) for v in splits.values())
    check("1.2", "三份划分合计覆盖全部图片", total == 10165, f"合计 {total} 条")

    for name, rows in splits.items():
        covered = len({r["index"] for r in rows})
        check("1.2", f"{name} 集覆盖全部 271 个类别", covered == 271, f"覆盖 {covered} 个类别")

    # 1.3 增强类
    try:
        from src.data.transforms import AgriAugment, eval_transform

        aug = AgriAugment({})
        n_ops = len(list(aug.train_transform().transforms))
        n_eval = len(list(eval_transform().transforms))
        check("1.3", "可复用数据增强类 AgriAugment", n_ops >= 6 and n_eval == 4,
              f"训练管线 {n_ops} 步（含高斯噪声）、评估管线 {n_eval} 步（无增强）")
    except Exception as e:  # noqa: BLE001
        check("1.3", "可复用数据增强类 AgriAugment", False, str(e))

    lm = load_json("data/label_map.json")
    ok = bool(lm) and lm.get("num_classes") == 271 and len(lm.get("index_to_label", {})) == 271
    idx_ok = False
    if ok:
        idx = sorted(int(k) for k in lm["index_to_label"])
        idx_ok = idx == list(range(271))
    check("1.4", "label_map.json（中文↔索引↔基础信息）", ok and idx_ok,
          f"{lm.get('num_classes') if lm else 0} 类，索引 0..270 连续；"
          f"含 base_info {len(lm.get('base_info', {})) if lm else 0} 条" if lm else "文件缺失")

    pp = load_json("data/province_pest_top3.json")
    provs = (pp or {}).get("provinces", [])
    has_top3 = all(len(p.get("seasons", {}).get("spring", [])) == 3 for p in provs) if provs else False
    scopes = [p.get("threat_scope", 0) for p in provs]
    check("1.5", "province_pest_top3.json（省份×季节 TOP3）",
          len(provs) == 34 and has_top3 and (pp or {}).get("demo_data") is True,
          f"{len(provs)} 个省份 × 4 个季节，每季 3 条；已标注 demo_data；"
          f"病害种类基数 {min(scopes)}~{max(scopes)}" if provs else "文件缺失")

    po = load_json("data/policy_list.json")
    items = (po or {}).get("items", [])
    fetched = sum(1 for i in items if i.get("fetched"))
    check("1.6", "policy_list.json（2026 年政策条目）",
          len(items) >= 9 and fetched == len(items) and all(i.get("publish_unit") for i in items),
          f"{len(items)} 条，联网抓取成功 {fetched} 条；"
          f"发布日期 {[i.get('publish_date_iso') for i in items]}" if items else "文件缺失")

    # 这两篇是新闻稿/技术意见，页面本身就没有编号条目，摘要即原文导语。
    # 其余政策必须有编号条目 —— 条目数为 0 说明页面模板变了、解析器没跟上。
    NO_ITEM_PIDS = {"pol_009", "pol_010"}
    for i in items:
        need_items = i["id"] not in NO_ITEM_PIDS
        check("1.6", f"政策「{i['title'][:16]}…」含核心内容摘要",
              len(i.get("summary", "")) >= 40 and (not need_items or i.get("item_count", 0) > 0),
              f"{i.get('item_count')} 个条目，摘要 {len(i.get('summary', ''))} 字，"
              f"出处 {i.get('publish_unit')}")


# ==================================================================== 阶段 2

def stage2() -> None:
    ck = path_of("checkpoints_dir") / "best_resnet50.pth"
    check("2.1", "自定义 Dataset 类", _importable("src.data.dataset", "AgriImageDataset"),
          "src/data/dataset.py::AgriImageDataset")
    check("2.2", "ResNet50 / ViT-B/16 双骨干搭建代码",
          _importable("src.models.backbone", "build_model"),
          "src/models/backbone.py::build_model（resnet50 / vit_base_patch16_224）")

    trainer_ok = _importable("src.train.trainer", "Trainer")
    es_ok = _importable("src.train.early_stop", "EarlyStopping")
    check("2.3", "训练策略：交叉熵 + AdamW + 余弦退火 + 早停", trainer_ok and es_ok,
          "src/train/{trainer,early_stop,sampler}.py")

    log = path_of("logs_dir") / "train_resnet50.csv"
    rows = read_csv_rows(str(log.relative_to(PROJECT_ROOT))) if log.exists() else []
    check("2.4", "最佳模型权重 best_resnet50.pth",
          ck.exists() and ck.stat().st_size > 200 * 1024 * 1024,
          f"{ck.stat().st_size / 1024 / 1024:.1f} MB" if ck.exists() else "文件缺失")
    check("2.4", "训练日志（逐 epoch loss / 准确率）", len(rows) >= 2,
          f"{len(rows)} 个 epoch；最佳 val_acc="
          f"{max((float(r['val_acc']) for r in rows), default=0):.4f}" if rows else "日志缺失")

    report = load_json("outputs/reports/eval_resnet50.json")
    cm = path_of("figures_dir") / "confusion_matrix_resnet50.png"
    overall = (report or {}).get("overall", {})
    check("2.5", "评估报告（准确率/精确率/召回率）",
          bool(report) and all(k in (report.get("macro") or {}) for k in ("precision", "recall", "f1")),
          f"Top-1 {overall.get('accuracy', 0):.4f} / macro-F1 "
          f"{(report or {}).get('macro', {}).get('f1', 0):.4f}" if report else "报告缺失")
    check("2.5", "混淆矩阵图（271 类）",
          cm.exists() and cm.stat().st_size > 100 * 1024,
          f"{cm.stat().st_size / 1024:.0f} KB" if cm.exists() else "图片缺失")

    onnx = path_of("onnx_dir") / "inference_model.onnx"
    extra = list(onnx.parent.glob("inference_model.onnx.*"))
    check("2.6", "ONNX 模型（自包含单文件）",
          onnx.exists() and onnx.stat().st_size > 80 * 1024 * 1024 and not extra,
          f"{onnx.stat().st_size / 1024 / 1024:.1f} MB"
          + ("，⚠ 存在外部数据文件 " + str([e.name for e in extra]) if extra else "，无外部依赖")
          if onnx.exists() else "文件缺失")


# ============================================================== 阶段 2.7（虫害检测）

def stage2_7() -> None:
    """虫害目标检测（IP102 / YOLOv8n）的产物核对。

    刻意**不碰**上面 stage1 / stage2 里那些硬编码的 47 作物 / 271 类 / 10165 张 ——
    那说的是 PDD271 病害数据集，与 IP102 是两套互相独立的标签空间。
    这一节只核对虫害这条线的产物，两边的数字不该互相干扰。
    """
    from src.common.constants import PEST_INPUT_SIZE, PEST_NUM_CLASSES

    # ---- 数据集 ----
    yaml_path = path_of("pest_yaml")
    ds = {}
    if yaml_path.exists():
        try:
            import yaml as _yaml

            ds = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            check("2.7.1", "虫害数据集 data.yaml 可解析", False, str(e))
    nc = int(ds.get("nc") or 0)
    names_ok = isinstance(ds.get("names"), list) and len(ds.get("names")) == PEST_NUM_CLASSES
    check("2.7.1", f"虫害数据集 data.yaml（nc={PEST_NUM_CLASSES} + names 逐条）",
          yaml_path.exists() and nc == PEST_NUM_CLASSES and names_ok,
          f"nc={nc}，names={len(ds.get('names') or [])} 条" if yaml_path.exists()
          else f"{yaml_path} 缺失；请运行 python scripts/16_download_ip102.py")

    counts = {}
    for split in ("train", "val", "test"):
        img_dir = path_of("pest_dataset") / "images" / split
        lbl_dir = path_of("pest_dataset") / "labels" / split
        n_img = len(list(img_dir.glob("*.jpg"))) + len(list(img_dir.glob("*.png"))) if img_dir.exists() else 0
        n_lbl = len(list(lbl_dir.glob("*.txt"))) if lbl_dir.exists() else 0
        counts[split] = (n_img, n_lbl)
    # 图与标注必须一一对应：少一个 label 就是一张图训练时没有监督信号，
    # 而 ultralytics 默认只会静默跳过，不会报错
    paired = all(i > 0 and i == l for i, l in counts.values())
    check("2.7.1", "虫害数据集三档图/标注一一对应",
          paired,
          "；".join(f"{k}: {v[0]} 图 / {v[1]} 标注" for k, v in counts.items()))

    # ---- 标签映射 ----
    lm_path = path_of("pest_label_map")
    lm = {}
    if lm_path.exists():
        try:
            lm = json.loads(lm_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            lm = {}
    i2l = lm.get("index_to_label") or {}
    idx_ok = sorted(int(k) for k in i2l) == list(range(PEST_NUM_CLASSES))
    yaml_aligned = False
    if idx_ok and names_ok:
        yaml_aligned = all(ds["names"][i] == i2l[str(i)].get("label_en") for i in range(PEST_NUM_CLASSES))
    check("2.7.2", f"虫害标签映射（{PEST_NUM_CLASSES} 类，索引连续且与 data.yaml 逐条同序）",
          lm_path.exists() and idx_ok and yaml_aligned,
          f"{len(i2l)} 条，label_en 与 data.yaml 完全一致" if yaml_aligned
          else ("label_en 与 data.yaml 对不上" if idx_ok else f"索引不连续或文件缺失：{lm_path}"))

    # ---- 训练产物 ----
    ck = path_of("checkpoints_dir") / "pest_best.pt"
    check("2.7.3", "虫害检测权重 pest_best.pt",
          ck.exists() and ck.stat().st_size > 2 * 1024 * 1024,
          f"{ck.stat().st_size / 1024 / 1024:.1f} MB" if ck.exists()
          else f"{ck} 缺失；请运行 python scripts/17_train_pest_yolo.py")

    log = path_of("logs_dir") / "train_pest_yolo.csv"
    rows = read_csv_rows(str(log.relative_to(PROJECT_ROOT))) if log.exists() else []
    # 列名是 ultralytics 原样搬过来的 "metrics/mAP50(B)" —— 中间那个斜杠和括号
    # 是它 results.csv 的原始表头，不是笔误（PowerShell 的 Import-Csv 读不了这个列名）
    best_map = max((float(r.get("metrics/mAP50(B)") or 0) for r in rows), default=0.0)
    check("2.7.3", "虫害训练日志（逐轮 mAP50）", len(rows) >= 2,
          f"{len(rows)} 轮；最佳 mAP50={best_map:.4f}" if rows
          else f"日志缺失（训练脚本在跑完 30 轮后才会把它从 runs/ 拷过来）：{log}")

    report = load_json("outputs/reports/eval_pest_yolo.json")
    ours = (report or {}).get("ours") or {}
    check("2.7.4", "虫害评估报告（mAP50 / mAP50-95 / 逐类 AP）",
          bool(report) and "map50" in ours and bool(ours.get("per_class")),
          f"mAP50={ours.get('map50', 0):.4f} / mAP50-95={ours.get('map50_95', 0):.4f}"
          f"（{ours.get('num_classes_present', 0)} 类在 val 中出现）" if ours else
          "报告缺失；请运行 python scripts/18_evaluate_pest_yolo.py")

    # 两张图都要查。原先这一项只查了混淆矩阵，标题却写着「混淆矩阵与 PR 曲线图」——
    # 而 PR 曲线当时因为源文件名对不上（ultralytics 检测分支叫 BoxPR_curve.png）
    # 压根没被拷出来，这项却是绿的。标题承诺什么就得验什么。
    figs = [("虫害混淆矩阵", "pest_confusion_matrix.png"), ("虫害 PR 曲线", "pest_pr_curve.png")]
    missing_figs = [n for n, f in figs if not (path_of("figures_dir") / f).exists()
                    or (path_of("figures_dir") / f).stat().st_size <= 20 * 1024]
    check("2.7.4", "虫害混淆矩阵与 PR 曲线图", not missing_figs,
          "缺失或过小：" + "、".join(missing_figs) if missing_figs
          else "、".join(f"{n} {((path_of('figures_dir') / f).stat().st_size / 1024):.0f} KB"
                         for n, f in figs))

    # ---- 导出产物 ----
    onnx = path_of("pest_onnx")
    # 日志目录名字刻意与分类模型区分开：上面 stage2 用 glob("inference_model.onnx.*")
    # 检查自包含性，这里用 pest_yolo.onnx.*，两个模型各自干净、互不误伤
    extra = list(onnx.parent.glob("pest_yolo.onnx.*"))
    check("2.7.5", "虫害检测 ONNX（自包含单文件，无 .onnx.data 外部依赖）",
          onnx.exists() and onnx.stat().st_size > 5 * 1024 * 1024 and not extra,
          f"{onnx.stat().st_size / 1024 / 1024:.1f} MB"
          + ("，⚠ 存在外部数据文件 " + str([e.name for e in extra]) if extra else "，无外部依赖")
          if onnx.exists()
          else f"{onnx} 缺失；请运行 python scripts/19_export_pest_onnx.py")

    check("2.7.5", "虫害检测 ONNX 的输入尺寸与类别数符合约定",
          _pest_onnx_shape_ok(onnx), _pest_onnx_shape_note(onnx))

    # registry 里必须有 pest_yolov8n 键，且指到真实存在的文件
    reg = load_json("models/registry.json") or {}
    entry = reg.get("pest_yolov8n") or {}
    # registry 里的 onnx 存的是**项目相对路径**（与 resnet50 键同一约定，
    # 换台机器照抄项目目录也还能用），所以这里按 PROJECT_ROOT 解析
    reg_path = entry.get("onnx")
    resolved = (PROJECT_ROOT / reg_path) if reg_path else None
    check("2.7.5", "registry.json 的 pest_yolov8n 键指向存在的 ONNX",
          bool(resolved) and resolved.exists(),
          f"pest_yolov8n.onnx = {reg_path}" if reg_path
          else "缺少 pest_yolov8n.onnx 键")
    # 反向锁：不能把病害分类模型的键写坏了（registry_entry 是按名取的，
    # 只要 resnet50 这个键还在，既有链路的读取就不受影响）
    check("2.7.5", "registry.json 的 resnet50 键未被虫害改动影响",
          "resnet50" in reg, f"现有键：{sorted(reg)}")

    # ---- 2.7.6 对比实验脚本（34→39）----
    #
    # 这六个脚本产出的是**实验副产物**（models/pest_staging/、outputs/reports/pest_experiments.json …），
    # 不是交付物 —— 所以下面只核对「脚本在不在、有没有 main」。真正要守的红线在紧接着的两条断言里。
    for task, rel, desc in (
        ("2.7.6", "scripts/34_train_pest_experiments.py", "对比实验训练脚本（候选权重落 staging）"),
        ("2.7.6", "scripts/35_eval_pest_experiments.py", "对比实验评估脚本（不覆盖 eval_pest_yolo*.json）"),
        ("2.7.6", "scripts/36_download_ip102_cls.py", "IP102 分类集下载脚本（第二档）"),
        ("2.7.6", "scripts/37_build_pest_aug.py", "copy-paste 稀有类增广脚本（第二档）"),
        ("2.7.6", "scripts/38_download_pest24.py", "Pest24 负样本获取脚本（第三档）"),
        ("2.7.6", "scripts/39_build_pest_mixed.py", "负样本混训入口组装脚本（第三档）"),
    ):
        p = PROJECT_ROOT / rel
        src = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        check(task, desc, p.exists() and "def main(" in src and len(src) > 2000, rel)

    # 红线：34 号**绝不能**具备写交付物的代码路径。
    #
    # 这里刻意不看原始文本，而是把模块 parse 成 AST、只取**非 docstring** 的字符串常量：
    # 34 号的文档字符串里恰恰印着 `_copy_outputs` / `pest_best.pt` / `train_pest_yolo.csv`
    # 这几个名字 —— 它整段就是在解释「我为什么不做这些事」。拿 `in` 去扫原文必然误报，
    # 而误报的门禁等于没有门禁。AST 能精确区分「写在代码里」与「写在解释里」。
    p34 = PROJECT_ROOT / "scripts" / "34_train_pest_experiments.py"
    s34 = p34.read_text(encoding="utf-8", errors="replace") if p34.exists() else ""
    code_strs = _non_docstring_strings(s34)
    forbidden = sorted({t for t in ("_copy_outputs", "_write_registry", "pest_best.pt",
                                    "pest_last.pt", "train_pest_yolo.csv", "model_registry")
                        if any(t in cs for cs in code_strs)})
    guard_ok = "staging == ckpt_dir" in s34 and "拒绝运行" in s34
    check("2.7.6", "训练脚本不含写交付物的代码路径，且保留 staging/交付物 重叠断言",
          p34.exists() and not forbidden and guard_ok,
          "代码里未出现交付物写入名，且 _staging_dir 的重叠断言在" if (not forbidden and guard_ok)
          else f"⚠ 交付物写入痕迹：{forbidden or '无'}；重叠断言：{'在' if guard_ok else '**丢了**'}")

    # 红线：35 号必须自带交付物指纹开关（22 号门禁给不了这项：它只看大小与 ONNX 形状，
    # 不看内容），而且它**只**往 pest_experiments.json 里写结论。
    p35 = PROJECT_ROOT / "scripts" / "35_eval_pest_experiments.py"
    s35 = p35.read_text(encoding="utf-8", errors="replace") if p35.exists() else ""
    check("2.7.6", "评估脚本自带交付物指纹自证，结论只落 pest_experiments.json",
          "--verify-deliverables" in s35 and 'REPORT_NAME = "pest_experiments.json"' in s35,
          "含 --verify-deliverables；REPORT_NAME 是 pest_experiments.json")


def _non_docstring_strings(source: str) -> list[str]:
    """取出源码里**不是文档字符串**的字符串常量。

    用来做「代码里有没有出现某个名字」的断言：像 34 号那样整段文档在解释
    「我不写 pest_best.pt」，用 `"pest_best.pt" in source` 会误报。
    这里跳过 Module / FunctionDef / AsyncFunctionDef / ClassDef 体里的首条字符串表达式
    （即 docstring），其余字符串常量照收。parse 失败就退回全文 —— 宁可保守，不能漏。
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [source]

    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def _pest_onnx_shape_ok(onnx) -> bool:
    from src.common.constants import PEST_INPUT_SIZE, PEST_NUM_CLASSES

    if not onnx.exists():
        return False
    try:
        import onnx as _onnx

        m = _onnx.load(str(onnx), load_external_data=False)
        inp = m.graph.input[0]
        dims = [d.dim_value for d in inp.type.tensor_type.shape.dim]
        # 期望 [1, 3, 640, 640]，且必须是**静态**形状（dynamic=False）。
        # dim_value 为 0 就说明该维是动态的（导出时 dynamic=True），静态图才吃得下
        ok_in = (len(dims) == 4 and dims[0] == 1 and dims[1] == 3
                 and dims[2] == PEST_INPUT_SIZE and dims[3] == PEST_INPUT_SIZE)
        out = m.graph.output[0]
        odims = [d.dim_value for d in out.type.tensor_type.shape.dim]
        # 4 框 + 102 类 = 106；锚点 8400（=80²+40²+20²）。导出的是原始张量（nms=False），
        # 所以输出只有这一个，形状里没有 num_detections 那一档。
        #
        # 这里原来写的是 `max(odims[1], odims[2])` —— **是个恒不成立的判据**：
        # 8400 比 106 大，取 max 拿到的是锚点数，于是这一项稳定报 FAIL，
        # 而产物其实完全正确（[1, 106, 8400]）。正确的问法是「有一个维度等于 4+nc，
        # 另一个是远大于它的锚点数」，两种排布（[1,106,8400] / [1,8400,106]）都认。
        # 刻意不去写死 8400：锚点数取决于输入尺寸与 stride 划分，钉死它等于把
        # 判据锁在某一个 ultralytics 版本上，将来换个 stride 就会误报。
        nb = 4 + PEST_NUM_CLASSES
        dims2 = odims[1:3] if len(odims) == 3 else []
        ch_ok = len(dims2) == 2 and nb in dims2 and max(dims2) > nb
        return ok_in and ch_ok
    except Exception:  # noqa: BLE001
        return False


def _pest_onnx_shape_note(onnx) -> str:
    from src.common.constants import PEST_NUM_CLASSES

    if not onnx.exists():
        return "文件缺失"
    try:
        import onnx as _onnx

        m = _onnx.load(str(onnx), load_external_data=False)
        i = m.graph.input[0].type.tensor_type.shape.dim
        o = m.graph.output[0].type.tensor_type.shape.dim
        return (f"输入 [{'×'.join(str(d.dim_value or '?') for d in i)}]，"
                f"输出 [{'×'.join(str(d.dim_value or '?') for d in o)}]"
                f"（期望 输入 1×3×640×640，输出含 4+{PEST_NUM_CLASSES}=106 这一维）")
    except Exception as e:  # noqa: BLE001
        return f"无法读取 ONNX 形状：{e}"


# ==================================================================== 阶段 3

def stage3() -> None:
    check("3.1", "voice_utils 语音封装（ASR + TTS）",
          _importable("src.voice.baidu_provider", "BaiduVoice")
          and _importable("src.voice.iflytek_provider", "IflytekVoice")
          and _importable("src.voice.mock_provider", "MockVoice")
          and _importable("src.voice.factory", "get_voice"),
          "src/voice/{base,baidu_provider,iflytek_provider,mock_provider,factory}.py")

    lx = path_of("knowledge_dir") / "agri_lexicon.txt"
    terms = lx.read_text(encoding="utf-8").splitlines() if lx.exists() else []
    check("3.2", "农业场景自定义词库",
          len(terms) >= 300,
          f"{len(terms)} 个词 -> knowledge/agri_lexicon.txt（可直接上传百度控制台）")

    check("3.3", "llm_utils 大模型封装（多模态融合/报告/问答）",
          _importable("src.llm.deepseek_provider", "DeepSeekProvider")
          and _importable("src.llm.fusion", "DiagnosisFusion")
          and _importable("src.llm.factory", "get_llm"),
          "src/llm/{base,deepseek_provider,mock_provider,factory,prompts,fusion}.py")

    try:
        from src.dialect.config import list_dialects

        ds = list_dialects()                      # 百度口径
        ds_xf = list_dialects(provider="iflytek")  # 讯飞口径
        baidu_ok = [d for d in ds if d["asr_supported"]]
        tts_ok = [d for d in ds_xf if d["xfyun_tts_supported"]]
        # 八种方言全都得有方言发音人 —— 查不到发音人的方言（上海话/闽南语/北京话/
        # 天津话）按要求直接从配置里删掉了，所以 tts_ok 必须等于全部，一个都不能少。
        check("3.4", "方言配置管理（八大方言动态切换）",
              len(ds) == 8 and all(d["asr_supported"] for d in ds_xf) and len(tts_ok) == len(ds_xf),
              f"{len(ds)} 种方言；百度口径 {len(baidu_ok)} 种有独立 ASR 模型、其余如实标注为回退普通话；"
              f"讯飞口径八种全部可识别（accent=mulacc），且 {len(tts_ok)} 种都有各自的方言发音人"
              f"（2026-10-06 真实 Key 实测出声）")
    except Exception as e:  # noqa: BLE001
        check("3.4", "方言配置管理（八大方言动态切换）", False, str(e))


# ==================================================================== 阶段 4 / 5 / 6

def stage45() -> None:
    check("4.1", "FastAPI 服务入口", exists("backend/app/main.py") and exists("backend/app/core/errors.py"),
          "backend/app/main.py（统一响应包 + CORS + 静态挂载）")
    check("4.2", "图像识别接口 /api/diagnose/image",
          exists("backend/app/routers/diagnose.py") and _importable("src.infer.predictor", "OnnxPredictor"),
          "routers/diagnose.py + src/infer/predictor.py（ONNX CPU 推理）")
    check("4.3", "多模态融合接口 /api/diagnose/multimodal",
          _importable("src.llm.fusion", "DiagnosisFusion"), "src/llm/fusion.py")
    check("4.4", "农业 RAG 知识库",
          exists("src/rag/builder.py") and len(list(path_of("documents_dir").glob("*.txt"))) > 200,
          f"切分源 {len(list(path_of('documents_dir').glob('*.txt')))} 个文档"
          + _rag_index_note())
    check("4.5", "知识图书库接口 + 数据",
          exists("backend/app/routers/library.py")
          and exists("knowledge/library/library_items.json")
          and exists("frontend/assets/data/library_preload.js"),
          "routers/library.py + knowledge/library/library_items.json"
          " + frontend/assets/data/library_preload.js（页面首屏用的本地快照）")
    check("4.6", "地图数据接口 /api/map/pest_top3", exists("backend/app/routers/map.py"), "routers/map.py")
    check("4.7", "惠农政策接口 /api/policy/list", exists("backend/app/routers/policy.py"), "routers/policy.py")

    for task, rel, desc in [
        ("5.1", "frontend/assets/css/layout.css", "全局布局（顶部固定导航 + 主体 + 底部信息栏）"),
        ("5.2", "frontend/index.html", "首页（交互式中国地图 + 季节切换）"),
        ("5.3", "frontend/diagnose.html", "智能诊断页（三输入区 -> 结果区）"),
        ("5.4", "frontend/library.html", "知识图书库页（卡片网格 + 全屏模态详情）"),
        ("5.5", "frontend/policy.html", "惠农政策页（条式列表）"),
        ("5.6", "frontend/assets/js/dialect.js", "全局方言切换组件（localStorage 持久化）"),
    ]:
        check(task, desc, exists(rel), rel)

    geo = PROJECT_ROOT / "frontend/assets/geo/china.json"
    relief = PROJECT_ROOT / "frontend/assets/geo/china_relief.json"
    water = PROJECT_ROOT / "frontend/assets/geo/china_water.json"
    three = PROJECT_ROOT / "frontend/assets/vendor/three/three.min.js"
    ok3d = all(f.exists() for f in (geo, relief, water, three))
    check("5.2", "地图 GeoJSON 与 3D 沙盘本地打包（运行时不依赖 CDN）",
          ok3d,
          (f"china.json {geo.stat().st_size // 1024} KB / "
           f"china_relief.json {relief.stat().st_size // 1024} KB / "
           f"china_water.json {water.stat().st_size // 1024} KB / "
           f"three.min.js {three.stat().st_size // 1024} KB") if ok3d else "资源缺失")

    _check_home_preload()
    _check_library_preload()
    _check_library_voice()

    check("6.1", "前后端联调脚本", exists("scripts/21_smoke_test_api.py"),
          # 这里**刻意不写死断言条数**：21 号脚本的虫害那几条是看分支走的
          # （检测器就位与否 → 断言条数不同），写死一个数字下一轮必然过期
          "scripts/21_smoke_test_api.py（接口行为 + 静态资源可达性；条数随虫害模型是否就位浮动）")
    check("6.2", "功能测试脚本",
          exists("scripts/22_verify_artifacts.py") and exists("tests/test_units.py"),
          # 条数**从源码里数出来**而不是写死：这个数字已经过期过两轮了
          # （32 → 45 → 62 → …），每次加用例都得手改一遍，纯属自找麻烦。
          # 用例没有条件跳过，@case 出现几次就会跑几项，所以直接数装饰器是准的。
          f"scripts/22_verify_artifacts.py（本脚本：交付物核对）+ "
          f"tests/test_units.py（{_count_unit_cases()} 项单元测试）")
    check("6.3", "启动脚本与部署说明",
          exists("start.ps1") and exists("scripts/20_run_backend.py") and exists("README.md"),
          "start.ps1 / scripts/20_run_backend.py / README.md")


def _count_unit_cases() -> int:
    """数出 tests/test_units.py 里的用例条数（数 @case 装饰器）。

    放进核对脚本来数，是为了让这个数字**自己跟着涨**，不用每次加用例都回来手改文案。
    前提是用例没有条件跳过 —— 确实没有，装饰器在导入时逐个执行，写几个就跑几个。
    """
    p = PROJECT_ROOT / "tests" / "test_units.py"
    if not p.exists():
        return 0
    return p.read_text(encoding="utf-8").count("@case(")


def _check_home_preload() -> None:
    """首页本地预加载快照（改造项 1）。

    首页首屏能否不依赖任何网络请求，全靠这个文件 —— 它要是缺了或过期了，
    「一直转圈」的老问题就会换个形式回来。
    """
    import re

    rel = "frontend/assets/data/home_preload.js"
    path = PROJECT_ROOT / rel
    if not path.exists():
        check("5.2", "首页本地预加载快照（零网络首屏）", False,
              f"{rel} 不存在；请运行 python scripts/15_bundle_frontend_data.py")
        return

    text = path.read_text(encoding="utf-8")
    try:
        geo = json.loads(re.search(r"window\.__CHINA_GEO__\s*=\s*(\{.*?\});\s*\n",
                                   text, re.S).group(1))
        snap = json.loads(re.search(r"window\.__PROVINCE_PEST__\s*=\s*(\{.*\});\s*$",
                                    text.strip(), re.S).group(1))
    except (AttributeError, json.JSONDecodeError) as e:
        check("5.2", "首页本地预加载快照（零网络首屏）", False,
              f"{rel} 内容无法解析出两个全局变量：{type(e).__name__}: {e}")
        return

    provs = snap.get("provinces", [])
    seasons = snap.get("seasons", [])
    # 与 data/province_pest_top3.json 对齐：快照是它的前端副本，两边省份数必须一致
    src = load_json("data/province_pest_top3.json") or {}
    same = len(provs) == len((src.get("provinces") or []))
    check("5.2", "首页本地预加载快照（零网络首屏）",
          len(provs) == 34 and len(seasons) == 4 and same
          and geo.get("type") == "FeatureCollection",
          f"{path.stat().st_size // 1024} KB；{len(provs)} 个省份 × {len(seasons)} 个季节；"
          f"底图 FeatureCollection；与 province_pest_top3.json "
          f"{'一致' if same else '**不一致，需重跑 15 号脚本**'}")

    html = (PROJECT_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    i_three = html.find("/assets/vendor/three/three.min.js")
    i_3d = html.find("/assets/js/map3d.js")
    i_pre = html.find("/assets/data/home_preload.js")
    i_map = html.find("/assets/js/map.js")
    # 四段都得同步：three.js 要先把全局 THREE 挂上（map3d.js 一执行就用它），
    # 预加载要在 map.js 之前，这样首屏一个接口都不发。
    # 只锁三对先后关系，不写死全序 —— 中间还夹着几个公共 js。
    tags = [html[html.rfind("<script", 0, i):i] for i in (i_three, i_3d, i_pre, i_map) if i > 0]
    plain = len(tags) == 4 and all("defer" not in t and "async" not in t for t in tags)
    ordered = -1 < i_three < i_3d < i_map and -1 < i_pre < i_map
    check("5.2", "index.html 在 map.js 之前同步加载 three.js 与快照",
          ordered and plain,
          "three.js → map3d.js，且 three/map3d/home_preload 都早于 map.js，四段均无 defer/async"
          if ordered and plain
          else "顺序不对、未引用或带了 defer/async，首屏会退回依赖接口")


def _check_library_preload() -> None:
    """知识图书库的本地快照（改造项 2）—— 打开图书库页立刻就有卡片。

    与首页那份是同一套约定、同一个生成器，但**只放列表字段**：
    带正文的 library_items.json 是 1.3 MB，同步脚本装不起，所以这里顺带核对
    快照里确实没有 sections —— 哪天有人"顺手"把正文塞进去，这一条就是刹车。
    """
    import re

    rel = "frontend/assets/data/library_preload.js"
    path = PROJECT_ROOT / rel
    if not path.exists():
        check("5.4", "图书库本地预加载快照（首屏零网络）", False,
              f"{rel} 不存在；请运行 python scripts/15_bundle_frontend_data.py")
        return

    text = path.read_text(encoding="utf-8")
    try:
        snap = json.loads(re.search(r"window\.__LIBRARY_PRELOAD__\s*=\s*(\{.*\});\s*$",
                                    text.strip(), re.S).group(1))
    except (AttributeError, json.JSONDecodeError) as e:
        check("5.4", "图书库本地预加载快照（首屏零网络）", False,
              f"{rel} 内容无法解析出 window.__LIBRARY_PRELOAD__：{type(e).__name__}: {e}")
        return

    src = load_json("knowledge/library/library_items.json") or {}
    kinds = {c["key"]: c["count"] for c in (snap.get("categories") or [])}
    same = snap.get("total") == (src.get("total") or 0) and len(snap.get("items") or []) == snap.get("total")
    check("5.4", "图书库本地预加载快照（首屏零网络）",
          same and kinds.get("insect") == 102 and "sections" not in text,
          f"{path.stat().st_size // 1024} KB；{snap.get('total')} 张卡片；"
          f"病害 {kinds.get('pest')} / 虫害 {kinds.get('insect')} / 养护 {kinds.get('care')}；"
          f"与 library_items.json {'一致' if same else '**不一致，需重跑 15 号脚本**'}"
          + ("；**含正文 sections，会拖垮首屏**" if "sections" in text else "；不含正文"))

    html = (PROJECT_ROOT / "frontend/library.html").read_text(encoding="utf-8")
    i_pre = html.find("/assets/data/library_preload.js")
    i_js = html.find("/assets/js/library.js")
    tag = html[html.rfind("<script", 0, i_pre):i_pre] if i_pre > 0 else ""
    check("5.4", "library.html 在 library.js 之前同步加载快照",
          -1 < i_pre < i_js and "defer" not in tag and "async" not in tag
          and 'id="library-notice"' in html,
          "顺序 library_preload.js → library.js，未带 defer/async，"
          "且备有 library-notice（后端未启动时的快照提示位）"
          if -1 < i_pre < i_js else "顺序不对或未引用，首屏会退回依赖接口")


def _check_library_voice() -> None:
    """知识图书库的方言语音检索 —— 录音归公共模块 recorder.js，两个页面共用一份。

    最容易出事的不是录音本身，而是 <script> 顺序：diagnose.js 与 library.js 都直接
    引用 window.Recorder，漏引或排错位置，用户点下的那一下就是 undefined 报错；
    诊断页漏了更是整条录音链路一起废。所以两个页面都钉住。
    """
    rel = "frontend/assets/js/recorder.js"
    path = PROJECT_ROOT / rel
    if not path.exists():
        check("5.4", "方言语音检索：公共录音模块", False,
              f"{rel} 不存在；录音采集必须由诊断页与图书库共用同一份，不许各留一份")
        return

    rec = path.read_text(encoding="utf-8")
    ok = ("createScriptProcessor" in rec and "gain.value = 0" in rec
          and '"RIFF"' in rec and 'type: "audio/wav"' in rec)
    check("5.4", "方言语音检索：公共录音模块", ok,
          "16kHz 单声道 PCM 自封 WAV 头，且经 gain=0 的静音节点（否则扬声器啸叫）"
          if ok else "采集链或 WAV 封装被改动了")

    for page, own, marker in (("library.html", "library.js", 'id="library-mic"'),
                              ("diagnose.html", "diagnose.js", 'id="btn-record"')):
        html = (PROJECT_ROOT / "frontend" / page).read_text(encoding="utf-8")
        i_rec = html.find("/assets/js/recorder.js")
        i_own = html.find(f"/assets/js/{own}")
        tag = html[html.rfind("<script", 0, i_rec):i_rec] if i_rec > 0 else ""
        ordered = -1 < i_rec < i_own and "defer" not in tag and "async" not in tag
        check("5.4", f"{page} 在 {own} 之前同步引入 recorder.js", ordered and marker in html,
              f"顺序 recorder.js → {own}，未带 defer/async" if ordered
              else f"recorder.js 缺失或顺序不对，{own} 里的 window.Recorder 会是 undefined")


def _importable(module: str, attr: str) -> bool:
    try:
        mod = __import__(module, fromlist=[attr])
        return hasattr(mod, attr)
    except Exception:  # noqa: BLE001
        return False


# ==================================================================== 阶段 7

def stage7() -> None:
    """阶段 7（我的农田）的交付物核对。

    迁移链**用临时库真跑一遍**而不是只看文件在不在：光有 .sql 文件不代表能跑通，
    而「建表语句写错」正是那种只在别人机器上才炸的错误。
    """
    # ---- 后端模块 ----
    modules = (
        ("7.0.1", "src/farm/db.py", "连接与迁移器"),
        ("7.0.2", "src/farm/auth.py", "密码哈希 / 会话 / 限流"),
        ("7.0.3", "src/farm/users.py", "用户与会话"),
        ("7.1", "src/farm/repo.py", "数据访问（全部带 user_id 过滤）"),
        ("7.1", "src/farm/seed.py", "注册时预置示例田"),
        ("7.2", "src/farm/weather.py", "Open-Meteo 取数"),
        ("7.3.1", "src/farm/stages.py", "生育期配置读取与校验"),
        ("7.3.2", "src/farm/gdd.py", "积温四段式与阶段匹配"),
        ("7.3", "src/farm/growth.py", "生长引擎与 3D 参数"),
        ("7.3.4", "src/farm/harvest_model.py", "收获日预测（XGBoost / 积温法）"),
        ("7.4", "src/farm/advisor.py", "农事建议引擎"),
        ("7.5", "src/farm/narrator.py", "大模型叙述层"),
        ("7.8", "src/farm/voice_parse.py", "语音文字解析"),
    )
    for task, rel, desc in modules:
        check(task, desc, exists(rel), rel)

    # ---- 迁移脚本 ----
    mig_dir = PROJECT_ROOT / "src" / "farm" / "migrations"
    sqls = sorted(p.name for p in mig_dir.glob("*.sql")) if mig_dir.exists() else []
    check("7.0.10", "迁移脚本齐备", len(sqls) >= 3, "、".join(sqls))

    try:
        from src.farm.db import MIGRATIONS

        versions = [v for v, _ in MIGRATIONS]
        contiguous = versions == list(range(1, len(versions) + 1))
        check("7.0.10", "迁移版本号从 1 连续递增", contiguous, f"v{versions}")
        files_ok = all((mig_dir / name).exists() for _, name in MIGRATIONS)
        check("7.0.10", "MIGRATIONS 里每条都有对应脚本", files_ok)
    except Exception as e:  # noqa: BLE001
        check("7.0.10", "读取 MIGRATIONS 失败", False, str(e))

    # ---- 迁移链在临时库上真跑一遍 ----
    try:
        import tempfile

        from src.farm.db import MIGRATIONS, connect, migrate

        with connect(Path(tempfile.mkdtemp(prefix="agri_verify_")) / "farm.db") as conn:
            migrate(conn, verbose=False)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        expected = {"users", "sessions", "user_settings", "login_attempts", "fields", "logs",
                    "daily_temp", "climate_normals", "diagnoses", "growth_snapshots",
                    "harvest_records", "reminder_snoozes"}
        missing = expected - tables
        check("7.0.10", "全新库能从 v0 迁到最新且 12 张表就位",
              version == max(v for v, _ in MIGRATIONS) and not missing,
              f"v{version}，缺：{'、'.join(sorted(missing)) if missing else '无'}")
    except Exception as e:  # noqa: BLE001
        check("7.0.10", "迁移链可执行", False, str(e))

    # ---- 后端接线 ----
    for task, module, attr, desc in (
        ("7.0.3", "backend.app.routers.auth", "router", "账号路由"),
        ("7.10", "backend.app.routers.farm", "router", "农田路由"),
        ("7.0.4", "backend.app.core.auth_deps", "get_current_user", "认证依赖"),
        ("7.10", "backend.app.schemas.farm", "ModelData", "3D 参数模型"),
        ("7.0.1", "src.farm.db", "init_db", "建库入口"),
    ):
        check(task, desc, _importable(module, attr), f"{module}.{attr}")

    # ---- 配置 ----
    stages_yaml = PROJECT_ROOT / "configs" / "crop_stages.yaml"
    check("7.3.1", "生育期配置", stages_yaml.exists(), "configs/crop_stages.yaml")
    try:
        from src.farm.stages import all_stages

        data = all_stages()
        crops_ok = set(data) == {"水稻", "玉米", "油菜", "小麦", "大豆", "棉花", "马铃薯"}
        detail = "、".join(f"{k}({len(v['stages'])}阶段/成熟{v['maturity_gdd']})"
                          for k, v in data.items())
        check("7.3.1", "七个作物的生育期配置齐全且通过校验", crops_ok, detail)
    except Exception as e:  # noqa: BLE001
        check("7.3.1", "生育期配置可加载", False, str(e))

    # ---- 省 / 市 / 县 离线数据集（7.11 新建田块的选址与经纬度推导）----
    admin_json = PROJECT_ROOT / "data" / "admin_divisions.json"
    admin_js = PROJECT_ROOT / "frontend" / "assets" / "data" / "admin_divisions.js"
    if admin_json.exists() and admin_js.exists():
        try:
            payload = json.loads(admin_json.read_text(encoding="utf-8"))
            n_p = payload.get("province_count", 0)
            n_c = payload.get("city_count", 0)
            n_d = payload.get("county_count", 0)
            check("7.11", "省市区离线数据集齐全（三级 + 中心经纬度）",
                  n_p >= 30 and n_c >= 300 and n_d >= 2000,
                  f"{n_p} 省 / {n_c} 市 / {n_d} 县区")
            head = admin_js.read_text(encoding="utf-8")[:800]
            check("7.11", "前端打包版与 JSON 同源（挂 window.__ADMIN_DIVISIONS__）",
                  "window.__ADMIN_DIVISIONS__" in head,
                  f"{admin_js.stat().st_size // 1024} KB")
        except Exception as e:  # noqa: BLE001
            check("7.11", "省市区数据集可解析", False, str(e))
    else:
        check("7.11", "省市区离线数据集", False,
              "缺 data/admin_divisions.json 或 frontend/assets/data/admin_divisions.js；"
              "运行 scripts/27_build_admin_divisions.py 生成")

    # ---- 前端 ----
    pages = (
        ("7.0.7", "frontend/login.html", "登录页"),
        ("7.0.7", "frontend/register.html", "注册页"),
        ("7.0.7", "frontend/profile.html", "个人中心"),
        ("7.0.7", "frontend/admin.html", "管理员页"),
        ("7.11", "frontend/farm.html", "我的农田页"),
        ("7.6", "frontend/assets/js/farm3d.js", "3D 田块视图"),
        ("7.11", "frontend/assets/js/farm.js", "农田页逻辑"),
        ("7.0.7", "frontend/assets/js/auth.js", "账号页逻辑"),
        ("7.6.1", "frontend/assets/vendor/three/three.min.js", "Three.js（本地打包）"),
    )
    for task, rel, desc in pages:
        check(task, desc, exists(rel), rel)

    three = PROJECT_ROOT / "frontend" / "assets" / "vendor" / "three" / "three.min.js"
    if three.exists():
        head = three.read_text(encoding="utf-8", errors="ignore")[:2000]
        check("7.6.1", "Three.js 是 UMD 构建（挂全局 THREE，无需构建步骤）",
              "THREE" in head, "与 echarts.min.js 同一套引入方式")
        check("7.6.1", "Three.js 体积正常（应约 600~800 KB）",
              300_000 < three.stat().st_size < 1_500_000,
              f"{three.stat().st_size // 1024} KB")

    # ---- 脚本 ----
    for task, rel, desc in (
        ("7.0.10", "scripts/23_init_farm_db.py", "建库脚本"),
        ("7.3.4", "scripts/24_train_harvest_model.py", "收获模型训练脚本"),
        ("7.2.4", "scripts/25_warm_farm_cache.py", "天气缓存预热脚本"),
        ("7.11", "scripts/27_build_admin_divisions.py", "省市区数据集构建脚本"),
        ("7.3.4", "scripts/29_download_phenology_datasets.py", "物候数据集下载脚本"),
        ("7.3.4", "scripts/30_fetch_phenology_weather.py", "逐县天气拉取脚本"),
        ("7.3.4", "scripts/31_build_harvest_dataset.py", "训练样本表构建脚本"),
        ("7.3.4", "scripts/32_train_harvest_external.py", "外部数据集训练与上线闸门脚本"),
        ("7.0.2", "scripts/33_make_dev_cert.py", "局域网调试用自签证书生成脚本"),
    ):
        check(task, desc, exists(rel), rel)

    # ---- 文档 ----
    for task, rel, desc in (
        ("7.13", "docs/papers.md", "论文引用"),
        ("7.14", "docs/datasets.md", "训练数据集"),
    ):
        check(task, desc, exists(rel), rel)

    try:
        papers = (PROJECT_ROOT / "docs" / "papers.md").read_text(encoding="utf-8")
        # 清单 7.13 点名的五篇，按可辨认的作者/标题关键词检查
        wanted = ("栾青", "牛博文", "Liu", "scalable machine learning", "Physics-guided")
        missing = [w for w in wanted if w not in papers]
        check("7.13", "论文文档覆盖清单里的 5 篇", not missing,
              "、".join(wanted) if not missing else f"缺：{'、'.join(missing)}")
        # 每篇都要说清「用在哪」，否则引用只是摆设
        check("7.13", "每篇论文都对应到具体实现", papers.count("`src/farm/") >= 4,
              f"{papers.count('`src/farm/')} 处指向实现")
    except Exception as e:  # noqa: BLE001
        check("7.13", "论文文档可读", False, str(e))

    # ---- 隐私：训练样本不含身份字段 ----
    try:
        from src.farm import repo as farm_repo

        source = (PROJECT_ROOT / "src" / "farm" / "repo.py").read_text(encoding="utf-8")
        i = source.find("def list_harvest_features")
        body = source[i:i + 900]
        check("7.14", "训练特征导出不选 user_id / 位置（7.14 隐私）",
              "SELECT crop, sow_date, harvest_date" in body and "user_id" not in body.split("SELECT")[1][:120],
              "SQL 里就没选那些列")
    except Exception as e:  # noqa: BLE001
        check("7.14", "训练特征导出可检查", False, str(e))

    # ---- 忽略规则（数据库与图片是运行期产物，不该进版本库）----
    try:
        ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        check("7.0", ".gitignore 覆盖农田运行期数据",
              "data/farm/" in ignore and "farm_images" in ignore,
              "data/farm/ 与 data/farm_images/")
    except Exception as e:  # noqa: BLE001
        check("7.0", ".gitignore 可读", False, str(e))

    # ---- 真实库（若已建）状态作为说明，不作为失败条件 ----
    try:
        from src.farm.db import applied_version_of, farm_db_path

        path = farm_db_path()
        if path.exists():
            check("7.0.10", "现有农田库版本", True,
                  f"v{applied_version_of(path)} · {path.name}")
        else:
            check("7.0.10", "农田库尚未建立", True,
                  "启动服务或运行 scripts/23_init_farm_db.py 会自动创建")
    except Exception as e:  # noqa: BLE001
        check("7.0.10", "读取库版本失败", False, str(e))


def _rag_index_note() -> str:
    """向量库建了没有 —— 没建也能跑（RAG 自动降级），但值得在核对结果里说清楚。"""
    try:
        import chromadb

        from src.common.paths import path_of as _p

        persist = _p("vectorstore_dir")
        if not persist.exists():
            return "；向量索引未建（RAG 会自动降级，运行 scripts/08_build_rag_index.py 可启用）"
        client = chromadb.PersistentClient(path=str(persist.resolve()))
        col = client.get_collection("agri_knowledge")
        return f"；向量索引已建：{col.count()} 个片段"
    except Exception as e:  # noqa: BLE001
        return f"；向量索引不可用（{type(e).__name__}），RAG 会自动降级"


# ==================================================================== 主流程

def main() -> int:
    logger.info("=" * 76)
    logger.info("交付物核对（任务 6.2 功能测试 · 静态部分）")
    logger.info("=" * 76)

    for fn in (stage1, stage2, stage2_7, stage3, stage45, stage7):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logger.exception("检查函数 %s 抛出异常：%s", fn.__name__, e)
            check("—", f"{fn.__name__} 执行异常", False, str(e))

    passed = sum(1 for *_, ok, _ in RESULTS if ok)
    failed = [(t, d, det) for t, d, ok, det in RESULTS if not ok]

    logger.info("")
    for task, desc, ok, detail in RESULTS:
        mark = "[OK]  " if ok else "[FAIL]"
        logger.info("  %s %-5s %s", mark, task, desc)
        if detail:
            logger.info("           %s", detail)

    logger.info("=" * 76)
    logger.info("结果：%d 项通过 / %d 项未通过（共 %d 项）", passed, len(failed), len(RESULTS))
    if failed:
        logger.error("未通过项：")
        for task, desc, det in failed:
            logger.error("  %s %s —— %s", task, desc, det)
    else:
        logger.info("全部交付物核对通过")
    logger.info("=" * 76)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
