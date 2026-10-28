# -*- coding: utf-8 -*-
"""虫害检测推理侧扫描：输入尺寸与 TTA 对 mAP 的影响。

不重训，只改推理配置。目的：在现有权重上找可交付的最高指标。
"""
import json
import sys
import time
from pathlib import Path

from ultralytics import YOLO

CKPT = sys.argv[1] if len(sys.argv) > 1 else "models/checkpoints/pest_best.pt"
SPLIT = sys.argv[2] if len(sys.argv) > 2 else "val"
OUT = sys.argv[3] if len(sys.argv) > 3 else "outputs/reports/pest_imgsz_sweep.json"

m = YOLO(CKPT)
rows = []
for sz in (640, 768, 960):
    for tta in (False, True):
        t0 = time.perf_counter()
        r = m.val(data="data/pest/data.yaml", split=SPLIT, imgsz=sz, conf=0.001,
                  iou=0.7, augment=tta, verbose=False, plots=False, save=False,
                  project="tmp_chk/runs", name="v_%d_%d" % (sz, int(tta)),
                  exist_ok=True)
        sec = time.perf_counter() - t0
        rec = {
            "imgsz": sz, "tta": tta, "split": SPLIT,
            "map50": round(float(r.box.map50), 4),
            "map50_95": round(float(r.box.map), 4),
            "precision": round(float(r.box.mp), 4),
            "recall": round(float(r.box.mr), 4),
            "seconds": round(sec, 1),
        }
        rows.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)

Path(OUT).parent.mkdir(parents=True, exist_ok=True)
Path(OUT).write_text(json.dumps({"checkpoint": CKPT, "rows": rows}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
print("写出", OUT)
