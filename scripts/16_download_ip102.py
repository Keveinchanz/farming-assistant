#!/usr/bin/env python
"""任务 2.7.1 —— 下载并整理 IP102 虫害检测数据集（YOLO 格式）。

## 为什么不用 PestYOLO 自带的 download_IP102.PY

那份脚本靠 gdown 拉 Google Drive（`drive.google.com`），本机实测不可达
（「无法连接到远程服务器」）。因此改用一个同格式、可直连的镜像仓库：

    phtrungagu321o/Yolov5_IP102    YOLO 检测格式，nc=102
      train/images 15178 + train/labels 15178
      test/images   3798 + test/labels   3798

整包 zip 778 MB。**必须经 gh-proxy 中转**：直连 codeload 实测只有 ~42 KB/s
（要 5 小时），经 https://gh-proxy.com 实测 15.5 MB/s，约 1 分钟。

## 两处必须自己动手的修正

1. **val 要自己切**。上游只有 train/test 两档，没有 val；而 PestYOLO 的
   data.yaml 把 val 也指向了 train（等于拿训练集当验证集，mAP 是假的）。
   这里从 train 按类别分层抽 10% 出来作 val —— 必须分层，102 类里长尾类
   样本很少，整体随机抽会让某些类在 val 里挂零，那一类的 AP 就永远是 0。
2. **data.yaml 必须写绝对路径**。ultralytics 对 yaml 里的相对 `path` 是按
   `DATASETS_DIR`（settings 里的 datasets_dir）解析的，不是按 yaml 所在目录，
   写相对路径会在别的机器/别的 cwd 下静默指向空目录。沿用本项目 README 里
   对 registry.json 绝对路径的既有说明口径。

## 用法

    python scripts/16_download_ip102.py                 # 下载 + 整理 + 切分
    python scripts/16_download_ip102.py --rewrite-yaml  # 只重写 data.yaml 的路径
    python scripts/16_download_ip102.py --force         # 推倒重来
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, list_images, write_json, write_text  # noqa: E402
from src.common.logging_utils import setup_logger  # noqa: E402
from src.common.paths import path_of, project_path  # noqa: E402

logger = setup_logger("agri.pest.download")

# 按优先级排列的下载地址。gh-proxy 是实测可用的那一个（15.5 MB/s）；
# 直连排最后只是「总比没有强」，真走到那一步大概要跑几小时。
SOURCES = [
    ("gh-proxy", "https://gh-proxy.com/https://codeload.github.com/phtrungagu321o/Yolov5_IP102/zip/refs/heads/main"),
    ("gh-proxy-raw", "https://gh-proxy.com/https://github.com/phtrungagu321o/Yolov5_IP102/archive/refs/heads/main.zip"),
    ("direct", "https://codeload.github.com/phtrungagu321o/Yolov5_IP102/zip/refs/heads/main"),
]

# PestYOLO 仓库 IP102_YOLOv8/classes.txt 的 102 个类名，逐条抄录。
# 用途是**交叉校验**：只有下载数据集的 names 与这份逐条同序，
# 仓库自带的 runs/IP102_detection/weights/best.pt 才和本地类别索引互通，
# 才能拿它跟自训模型做同口径对照。这条断言比「看起来好像是 IP102」可靠得多。
PESTYOLO_CLASSES = [
    "rice leaf roller", "rice leaf caterpillar", "paddy stem maggot", "asiatic rice borer",
    "yellow rice borer", "rice gall midge", "Rice Stemfly", "brown plant hopper",
    "white backed plant hopper", "small brown plant hopper", "rice water weevil",
    "rice leafhopper", "grain spreader thrips", "rice shell pest", "grub", "mole cricket",
    "wireworm", "white margined moth", "black cutworm", "large cutworm", "yellow cutworm",
    "red spider", "corn borer", "army worm", "aphids", "Potosiabre vitarsis", "peach borer",
    "english grain aphid", "green bug", "bird cherry-oataphid", "wheat blossom midge",
    "penthaleus major", "longlegged spider mite", "wheat phloeothrips", "wheat sawfly",
    "cerodonta denticornis", "beet fly", "flea beetle", "cabbage army worm", "beet army worm",
    "Beet spot flies", "meadow moth", "beet weevil", "sericaorient alismots chulsky",
    "alfalfa weevil", "flax budworm", "alfalfa plant bug", "tarnished plant bug", "Locustoidea",
    "lytta polita", "legume blister beetle", "blister beetle",
    "therioaphis maculata Buckton", "odontothrips loti", "Thrips", "alfalfa seed chalcid",
    "Pieris canidia", "Apolygus lucorum", "Limacodidae", "Viteus vitifoliae", "Colomerus vitis",
    "Brevipoalpus lewisi McGregor", "oides decempunctata", "Polyphagotars onemus latus",
    "Pseudococcus comstocki Kuwana", "parathrene regalis", "Ampelophaga", "Lycorma delicatula",
    "Xylotrechus", "Cicadella viridis", "Miridae", "Trialeurodes vaporariorum",
    "Erythroneura apicalis", "Papilio xuthus", "Panonchus citri McGregor",
    "Phyllocoptes oleiverus ashmead", "Icerya purchasi Maskell", "Unaspis yanonensis",
    "Ceroplastes rubens", "Chrysomphalus aonidum", "Parlatoria zizyphus Lucus",
    "Nipaecoccus vastalor", "Aleurocanthus spiniferus", "Tetradacus c Bactrocera minax",
    "Dacus dorsalis(Hendel)", "Bactrocera tsuneonis", "Prodenia litura", "Adristyrannus",
    "Phyllocnistis citrella Stainton", "Toxoptera citricidus", "Toxoptera aurantii",
    "Aphis citricola Vander Goot", "Scirtothrips dorsalis Hood", "Dasineura sp",
    "Lawana imitata Melichar", "Salurnis marginella Guerr", "Deporaus marginatus Pascoe",
    "Chlumetia transversa", "Mango flat beak leafhopper", "Rhytidodera bowrinii white",
    "Sternochetus frigidus", "Cicadellidae",
]

SPLITS = ("train", "val", "test")


# ------------------------------------------------------------------ 下载

def download(url: str, dest: Path) -> Path:
    """流式下载并显示进度。

    注意 gh-proxy 是分块流式响应，不带 Content-Length / Accept-Ranges，
    也就是说**断流不能续传**（curl -C - 无效）。恢复策略只能是重下 ——
    好在全程约一分钟，重试成本可以接受。这里记录已收字节数只为显示进度。
    """
    import requests
    from tqdm import tqdm

    ensure_dir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=(15, 300)) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with tmp.open("wb") as f, tqdm(
            total=total or None, unit="B", unit_scale=True, unit_divisor=1024,
            desc=f"下载 {dest.name}", leave=False,
        ) as bar:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                bar.update(len(chunk))
    tmp.replace(dest)
    logger.info("下载完成：%s（%.1f MB）", dest.name, dest.stat().st_size / 1024 / 1024)
    return dest


def fetch_zip(dest: Path, *, force: bool) -> Path:
    if dest.exists() and not force:
        logger.info("已存在原始 zip，跳过下载：%s", dest)
        return dest
    errors = []
    for name, url in SOURCES:
        logger.info("尝试下载源 %s …", name)
        try:
            return download(url, dest)
        except Exception as e:  # noqa: BLE001
            logger.warning("下载源 %s 失败：%s", name, e)
            errors.append(f"{name}: {e}")
    raise RuntimeError("所有下载源均失败：\n  " + "\n  ".join(errors))


# ------------------------------------------------------------------ 解压

def extract(zip_path: Path, out_dir: Path, *, force: bool) -> Path:
    """解压并返回 zip 里的顶层目录。

    先 testzip() 全量 CRC 校验再解压：gh-proxy 不能续传，半个 zip 在
    zipfile 眼里往往仍然「能打开」，不校验就会静默解出残缺数据集，
    等到训练时才发现某些类别是空的 —— 那时已经浪费了几小时。
    """
    if out_dir.exists() and not force:
        tops = [d for d in out_dir.iterdir() if d.is_dir()]
        if len(tops) == 1:
            logger.info("已存在解压目录，跳过解压：%s", tops[0])
            return tops[0]

    ensure_dir(out_dir)
    logger.info("校验 zip CRC（778 MB，需要一会儿）…")
    with zipfile.ZipFile(zip_path) as z:
        broken = z.testzip()
        if broken:
            raise RuntimeError(f"zip 损坏，第一个出错的成员：{broken}（请删掉重下）")
        logger.info("CRC 校验通过，开始解压 %d 个成员…", len(z.namelist()))
        z.extractall(out_dir)

    tops = [d for d in out_dir.iterdir() if d.is_dir()]
    if len(tops) != 1:
        raise RuntimeError(f"预期 zip 里只有一个顶层目录，实际 {len(tops)} 个：{[t.name for t in tops]}")
    logger.info("解压完成：%s", tops[0].name)
    return tops[0]


# ------------------------------------------------------------------ 校验

def read_names_from_yaml(root: Path) -> list[str]:
    import yaml

    yaml_path = root / "data.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"上游 data.yaml 缺失：{yaml_path}")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    names = data.get("names")
    if isinstance(names, dict):  # 少数版本写成 {0: 'a', 1: 'b'}
        names = [names[k] for k in sorted(names, key=int)]
    return [str(n) for n in (names or [])]


def _norm(name: str) -> str:
    """归一化类名以便跨仓库比较。

    两个来源的**下标顺序完全一致**，但命名习惯不同：
        上游 data.yaml   rice_leaf_roller / Dacus_dorsalis(hendel)
        PestYOLO        rice leaf roller  / Dacus dorsalis(Hendel)
    差别只在分隔符（下划线 vs 空格）与大小写。用下标对齐才是这里真正要保证的事，
    直接比字符串会把「只是写法不同」误报成「类序不同」——这恰恰是本脚本初版犯的错。
    """
    return " ".join(name.replace("_", " ").replace("-", " ").lower().split())


def verify_classes(names: list[str]) -> None:
    """校验下载数据集的类序与 PestYOLO 逐条对应。

    下标对齐才意味着官方 best.pt 的类别索引在本地成立、能拿来做同口径对照。
    要是真的错位，即使类别集合一样，官方权重也会把「稻纵卷叶螟」认成别的虫，
    而且这种错误**不会报错**，只会让对照表整张作废。
    """
    if len(names) != len(PESTYOLO_CLASSES):
        raise RuntimeError(f"类别数不符：上游 {len(names)}，PestYOLO {len(PESTYOLO_CLASSES)}")

    diff = [(i, a, b) for i, (a, b) in enumerate(zip(names, PESTYOLO_CLASSES))
            if _norm(a) != _norm(b)]
    if diff:
        head = "\n  ".join(f"[{i}] 上游={a!r}  PestYOLO={b!r}" for i, a, b in diff[:10])
        raise RuntimeError(
            f"{len(diff)} 个类别下标对不上，官方 best.pt 不可用：\n  {head}"
        )

    # 下标全对齐，再看纯写法差异 —— 只报告，不算失败
    spelling = sum(1 for a, b in zip(names, PESTYOLO_CLASSES) if a != b)
    logger.info("类序校验通过：%d 类，下标与 PestYOLO classes.txt 逐条对齐", len(names))
    if spelling:
        logger.info("其中 %d 类仅写法不同（上游下划线 / PestYOLO 空格），已归一化后比对", spelling)


def scan_split(root: Path, split: str) -> tuple[list[Path], dict[str, Path]]:
    """返回 (图片列表, {stem: 标注路径})，并校验图片与标注一一对应。"""
    img_dir, lbl_dir = root / split / "images", root / split / "labels"
    if not img_dir.is_dir():
        raise FileNotFoundError(f"缺少目录：{img_dir}")
    images = list_images(img_dir)
    labels = {p.stem: p for p in lbl_dir.glob("*.txt")} if lbl_dir.is_dir() else {}

    missing = [p.stem for p in images if p.stem not in labels]
    orphan = sorted(set(labels) - {p.stem for p in images})
    if missing:
        logger.warning("%s 集有 %d 张图没有对应标注（前 5 个：%s）", split, len(missing), missing[:5])
    if orphan:
        logger.warning("%s 集有 %d 个标注没有对应图片", split, len(orphan))
    logger.info("%s：%d 张图 / %d 个标注", split, len(images), len(labels))
    return images, labels


def primary_class(label_path: Path) -> int:
    """取一张图的主类别 = 标注里出现次数最多的类 id（并列取小）。

    分层抽 val 要有「这张图属于哪一类」这个标签，而检测标注一图可能多个框；
    用主类别分层即可，长尾类不会在 val 里挂零才是这里真正要保证的事。
    空标注（纯背景负样本）返回 -1，单独成组。
    """
    counts: Counter[int] = Counter()
    try:
        for line in label_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 5:
                counts[int(float(parts[0]))] += 1
    except (OSError, ValueError):
        return -1
    if not counts:
        return -1
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


# ------------------------------------------------------------------ 切分与落盘

def stratified_val(images: list[Path], labels: dict[str, Path], ratio: float,
                   seed: int) -> tuple[list[Path], list[Path]]:
    """按主类别分层从 train 里抽 val。"""
    groups: dict[int, list[Path]] = defaultdict(list)
    for p in images:
        lp = labels.get(p.stem)
        groups[primary_class(lp) if lp else -1].append(p)

    val: list[Path] = []
    for cls in sorted(groups):
        items = sorted(groups[cls])
        rng = random.Random(seed + cls)
        rng.shuffle(items)
        n_val = max(1, round(len(items) * ratio)) if len(items) > 1 else 0
        val.extend(items[:n_val])
    val_set = set(val)
    train = [p for p in images if p not in val_set]
    logger.info("分层切分：train %d / val %d（覆盖 %d 个主类别组）", len(train), len(val), len(groups))
    return train, val


def move_into(root: Path, dest: Path, split: str, images: list[Path],
              labels: dict[str, Path]) -> None:
    """把图与标注搬进 data/pest/<images|labels>/<split>/。

    用 move 而不是 copy：解压后已占约 800 MB，再复制一份就是白白多占一倍。
    """
    img_out = ensure_dir(dest / "images" / split)
    lbl_out = ensure_dir(dest / "labels" / split)
    for p in images:
        target = img_out / p.name
        if p.resolve() != target.resolve():
            shutil.move(str(p), str(target))
        lp = labels.get(p.stem)
        if lp is not None and lp.exists():
            shutil.move(str(lp), str(lbl_out / f"{p.stem}.txt"))


def write_yaml(dest: Path, names: list[str]) -> Path:
    """写 data.yaml。

    `path` 必须是绝对路径：ultralytics 对相对的 `path` 是拿 settings 里的
    DATASETS_DIR 去拼的，不是拿 yaml 所在目录，写相对路径换个 cwd 就会静默
    指向空目录。这里的路径只对本机有意义，换机器重跑本脚本即可（--rewrite-yaml）。
    """
    import yaml

    payload = {
        "path": dest.resolve().as_posix(),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "nc": len(names),
        "names": names,
    }
    p = dest / "data.yaml"
    write_text(p, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False))
    logger.info("已写 data.yaml：path=%s，nc=%d", payload["path"], len(names))
    return p


def build(root: Path, dest: Path, *, val_ratio: float, seed: int) -> dict:
    # 归档是「搬」不是「拷」（省一倍磁盘），所以中途失败会留下半搬状态：
    # 图已经进了 data/pest，而 _extract 里那部分已经没了，直接重跑会找不到文件。
    # 与其静默搬出一份残缺数据集，不如在这里挡住，让调用方明确 --force 重来。
    leftover = list(dest.glob("images/*/*.jpg")) + list(dest.glob("images/*/*.JPG"))
    if leftover:
        raise RuntimeError(
            f"{dest / 'images'} 下已有 {len(leftover)} 张图，可能是上次中途失败留下的。\n"
            "请删掉 data/pest 下的 images/ 与 labels/ 后重跑（或加 --force 从头再来）"
        )

    names = read_names_from_yaml(root)
    verify_classes(names)

    train_images, labels = scan_split(root, "train")
    test_images, test_labels = scan_split(root, "test")

    train, val = stratified_val(train_images, labels, val_ratio, seed)

    for split, imgs, lbls in (("train", train, labels), ("val", val, labels),
                              ("test", test_images, test_labels)):
        move_into(root, dest, split, imgs, lbls)
        logger.info("已归档 %s：%d 张", split, len(imgs))

    write_yaml(dest, names)

    manifest = {
        "source": "phtrungagu321o/Yolov5_IP102（YOLO 检测格式镜像）",
        "ip102_classes_verified_against": "jianxi-Erin/PestYOLO · IP102_YOLOv8/classes.txt（下标逐条对齐）",
        "class_name_spelling": "上游用下划线（rice_leaf_roller），PestYOLO 用空格（rice leaf roller）；"
                               "两者下标与语义完全一致，仅写法不同，故官方 best.pt 的类别索引在本数据集上成立",
        "num_classes": len(names),
        "num_images": {"train": len(train), "val": len(val), "test": len(test_images)},
        "num_images_total": len(train) + len(val) + len(test_images),
        "val_ratio": val_ratio,
        "seed": seed,
        "val_note": "上游只有 train/test，无 val；val 由 train 按主类别分层抽出",
        "names": names,
        "names_pestyolo": PESTYOLO_CLASSES,
    }
    write_json(dest / "manifest.json", manifest)
    return manifest


# ------------------------------------------------------------------ 主流程

def rewrite_yaml_only(dest: Path) -> int:
    """只按当前机器重写 data.yaml 的绝对路径（数据集本身不动）。"""
    man = dest / "manifest.json"
    if not man.exists():
        logger.error("缺少 %s，说明数据集还没整理过；请直接不带参数运行本脚本", man)
        return 1
    names = json.loads(man.read_text(encoding="utf-8"))["names"]
    write_yaml(dest, names)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="下载并整理 IP102 虫害检测数据集")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载/解压/切分")
    ap.add_argument("--rewrite-yaml", action="store_true", help="只重写 data.yaml 的绝对路径")
    ap.add_argument("--val-ratio", type=float, default=0.1, help="从 train 分层抽 val 的比例")
    ap.add_argument("--seed", type=int, default=42, help="切分随机种子")
    ap.add_argument("--keep-raw", action="store_true", help="保留原始 zip 与解压目录（默认清理）")
    args = ap.parse_args()

    dest = path_of("pest_dataset")
    if args.rewrite_yaml:
        return rewrite_yaml_only(dest)

    zip_path = path_of("pest_raw") / "ip102_yolo.zip"
    extract_dir = path_of("pest_raw") / "_extract"

    fetch_zip(zip_path, force=args.force)
    root = extract(zip_path, extract_dir, force=args.force)

    manifest = build(root, dest, val_ratio=args.val_ratio, seed=args.seed)

    if not args.keep_raw:
        for junk in (extract_dir, zip_path):
            if junk.is_dir():
                shutil.rmtree(junk, ignore_errors=True)
            elif junk.exists():
                junk.unlink()
        logger.info("已清理原始 zip 与解压目录（--keep-raw 可保留）")

    logger.info("=" * 70)
    logger.info("数据集就绪：%s", dest)
    for k, v in manifest["num_images"].items():
        logger.info("  %-6s %6d 张", k, v)
    logger.info("  合计   %6d 张 / %d 类", manifest["num_images_total"], manifest["num_classes"])
    logger.info("=" * 70)
    logger.info("下一步：python scripts/17_train_pest_yolo.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
