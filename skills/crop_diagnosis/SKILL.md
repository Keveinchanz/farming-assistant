---
name: crop-diagnosis
description: 病虫害检测与多模态融合诊断。接收作物照片、方言语音或文字症状，输出病害候选、虫害检测框、融合诊断报告与防治方案。适用于田间问诊、病虫害排查与知识库检索增强诊断。
---

# 病虫害检测与多模态融合诊断

## 能力范围

- 图片病害分类（PDD271，271 类，ONNX）
- 图片虫害检测（IP102，102 类，YOLOv8s ONNX）
- 语音转写（可选）与文字症状匹配
- 知识库检索与大模型生成诊断报告
- 支持纯图片、纯文字、纯语音及任意组合调用

## 输入

| 字段 | 说明 |
|---|---|
| `image_base64` / `image_path` | 图片，二选一。HTTP 仅接受 base64，CLI 可用本地路径 |
| `audio_base64` / `audio_path` | 音频，二选一，可选 |
| `audio_format` | `auto` / `wav` / `pcm`；其余容器需先转为 16 kHz 单声道 WAV |
| `text` | 文字症状描述，最长 4000 字 |
| `dialect` | 方言 key，留空使用默认方言 |
| `topk` | 病害候选数，1–10，默认 5 |

至少提供图片、语音、文字中的一项。单文件解码后不超过 20 MB。

## 输出

- `recognition`：病害 Top-K；无图时为 `null`
- `pest`：虫害检测结果。`null` 表示本次未运行检测，`count = 0` 表示已运行但未检出
- `report`：诊断报告，含 `source_notes`
- `candidates`：病害闭集候选
- `sources`：知识库引用
- `degraded`：`llm / rag / voice / pest` 降级状态
- `input_summary`：本次实际使用的输入路径

## HTTP

```bash
curl -X POST http://127.0.0.1:8000/api/skills/crop-diagnosis \
  -H "Content-Type: application/json" \
  -d '{"text":"水稻叶片有褐色斑点，最近多雨"}'
```

图片调用需先将文件转为 base64：

```bash
python - <<'PY'
import base64, json, pathlib
p = pathlib.Path("leaf.jpg")
print(json.dumps({"image_base64": base64.b64encode(p.read_bytes()).decode(),
                  "text": "看看这是什么病"}, ensure_ascii=False))
PY
```

## CLI

```bash
python -m skills run crop-diagnosis --input payload.json --pretty
python -m skills run crop-diagnosis --input - --pretty   # 从 stdin 读
```

## 降级语义

- 图像模型缺失：返回 503，并明确提示模型不可用。
- 虫害模型缺失：诊断照常进行，`pest = null`，`degraded.pest = false`。
- 语音未配置：`asr.mock = true`、`degraded.voice = true`，不阻断图片与文字诊断。
- 大模型未配置：报告走模板，`degraded.llm = true`，字段结构不变。
- RAG 未启用：`degraded.rag = true`，仍返回诊断结果。

## 限制

- HTTP 不接收本地文件路径，以避免任意文件读取。
- 诊断结果与防治建议仅供参考，不构成用药指令。
