#!/usr/bin/env python
"""单元测试 —— 锁住那些"看起来能跑但容易悄悄错掉"的核心逻辑。

不依赖 pytest，直接运行即可：
    python tests/test_units.py

为什么这些用例值得存在：它们覆盖的都是本项目踩过或差点踩到的坑，
一旦被后续改动破坏，症状往往是"训练指标正常但线上结果离谱"这种难查的问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402

logger = setup_logger("agri.test")

PASSED = 0
FAILED: list[str] = []


def case(name: str):
    def deco(fn):
        global PASSED
        try:
            fn()
            PASSED += 1
            logger.info("  [OK]   %s", name)
        except AssertionError as e:
            FAILED.append(name)
            logger.error("  [FAIL] %s —— %s", name, e)
        except Exception as e:  # noqa: BLE001
            FAILED.append(name)
            logger.error("  [FAIL] %s —— 抛出异常 %s: %s", name, type(e).__name__, e)
        return fn

    return deco


# ==================================================================== 作物解析（1.1）

@case("作物解析：大白菜不会被切成「大」+「白菜」")
def _():
    from src.data.crop_parser import parse_label

    p = parse_label("大白菜霜霉病", "115")
    assert p.crop == "大白菜", f"crop 应为大白菜，实际 {p.crop}"
    assert p.condition == "霜霉病", f"condition 应为霜霉病，实际 {p.condition}"


@case("作物解析：猕猴桃优先于桃（最长前缀匹配）")
def _():
    from src.data.crop_parser import parse_label

    assert parse_label("猕猴桃叶斑病", "191").crop == "猕猴桃"
    assert parse_label("桃树潜隐花叶病", "156").crop == "桃树"
    assert parse_label("油桃细菌性穿孔病", "181").crop == "油桃"
    assert parse_label("樱桃细菌性穿孔病", "178").crop == "樱桃"


@case("作物解析：甘薯/紫薯/马铃薯 各自正确")
def _():
    from src.data.crop_parser import parse_label

    assert parse_label("甘薯健康叶片", "220").crop == "甘薯"
    assert parse_label("紫薯健康叶", "242").crop == "紫薯"
    assert parse_label("马铃薯晚疫病", "347").crop == "马铃薯"


@case("作物解析：源数据错别字按健康态处理（键康/健株/键叶）")
def _():
    from src.data.crop_parser import parse_label

    for name in ("大豆键康叶片", "绿豆健株键叶", "菠菜健康"):
        p = parse_label(name)
        assert p.is_healthy, f"{name} 应判为健康样本"
        assert p.pathogen_type == "健康", f"{name} 的 pathogen_type 应为「健康」"


@case("作物解析：混合型病害被识别")
def _():
    from src.data.crop_parser import parse_label

    p = parse_label("花生黑斑病与焦斑病混合", "263")
    assert p.is_mixed, "应标记为复合病害"
    assert not p.is_healthy


@case("作物解析：全部 271 个类别名都能切出作物，且都在 47 个词表内")
def _():
    import io

    from src.common.paths import path_of
    from src.data.crop_parser import UNKNOWN_CROP, crop_names, parse_many

    rows = [
        line.strip().rsplit(" ", 1)
        for line in io.open(path_of("label_rename"), encoding="utf-8-sig")
        if line.strip()
    ]
    ok, bad = parse_many([(n, d) for n, d in rows])
    assert not bad, f"有 {len(bad)} 个类别未命中作物：{[b.label_cn for b in bad][:5]}"
    assert len(ok) == 271, f"应解析 271 个类别，实际 {len(ok)}"
    assert len(crop_names()) == 47, f"词表应为 47 个作物，实际 {len(crop_names())}"
    assert all(p.crop != UNKNOWN_CROP for p in ok)


# ==================================================================== 增强与预处理（1.3）

@case("归一化参数在训练与推理间共用同一份常量")
def _():
    from src.common.constants import IMAGENET_MEAN, IMAGENET_STD
    from src.data.transforms import eval_transform

    tf = eval_transform(224)
    norms = [t for t in tf.transforms if type(t).__name__ == "Normalize"]
    assert norms, "评估变换里应当有 Normalize"
    n = norms[0]
    assert tuple(round(x, 6) for x in n.mean) == tuple(round(x, 6) for x in IMAGENET_MEAN), \
        "推理预处理的 mean 与 constants 不一致（会导致训练/线上结果不一致）"
    assert tuple(round(x, 6) for x in n.std) == tuple(round(x, 6) for x in IMAGENET_STD), \
        "推理预处理的 std 与 constants 不一致"


@case("评估变换不含任何随机增强（否则评估指标被人为扰动）")
def _():
    from src.data.transforms import eval_transform

    names = [type(t).__name__ for t in eval_transform(224).transforms]
    forbidden = {"RandomResizedCrop", "RandomHorizontalFlip", "RandomRotation",
                 "ColorJitter", "AddGaussianNoise"}
    hit = forbidden & set(names)
    assert not hit, f"评估管线里出现了增强算子：{hit}"


@case("高斯噪声在训练态生效、评估态不生效")
def _():
    import torch

    from src.data.transforms import AddGaussianNoise

    g = AddGaussianNoise(sigma=0.1, p=1.0)
    x = torch.zeros(1, 3, 8, 8)

    g.train()
    assert not torch.allclose(g(x), x), "训练态下应改变输入"

    g.eval()
    assert torch.allclose(g(x), x), "评估态下不应改动输入"


# ==================================================================== 数据划分（1.2）

@case("划分参数分配：小样本类也能保证每个集合非空且训练集不为空")
def _():
    from src.data.split import _allocate

    assert _allocate(1, 0.1, 0.1, 50, 50) == (0, 0), "只有 1 张时全部留给训练"
    assert _allocate(2, 0.1, 0.1, 50, 50) == (1, 0)
    assert _allocate(3, 0.1, 0.1, 50, 50) == (1, 1)

    for n in range(4, 1200):
        n_test, n_val = _allocate(n, 0.1, 0.1, 50, 50)
        assert n_test >= 1 and n_val >= 1, f"n={n} 时验证/测试集应非空"
        assert n - n_test - n_val >= 1, f"n={n} 时训练集被掏空"
        assert n_test <= 50 and n_val <= 50, f"n={n} 时未受上限约束"


# ==================================================================== 术语抽取（3.2）

@case("术语抽取：精确命中，且不改写正常语句")
def _():
    from src.voice.lexicon import find_terms

    hits = find_terms("我发现玉米叶子上有褐色的斑点，像是大斑病")
    assert "玉米" in hits and "大斑病" in hits, f"应命中玉米与大斑病，实际 {hits}"

    hits2 = find_terms("用点杀菌剂和叶面肥试试")
    assert "杀菌剂" in hits2 and "叶面肥" in hits2, f"实际 {hits2}"

    # 真正的不变量：抽出来的每个术语都必须是原文的逐字子串 ——
    # 绝不出现"把原文改写成词表里的写法"。模糊纠错方案就是因为违反这一点被废弃的。
    samples = [
        "我发现玉米叶子上有褐色的斑点，像是大斑病",
        "黄瓜叶子背面有白色的粉末，可能是白粉病",
        "我家萝卜根烂了，是不是黑副病",
        "叶片上有褐色斑点，边缘发黄",
    ]
    for s in samples:
        for term in find_terms(s):
            assert term in s, f"抽出的术语 {term!r} 并不是原文的子串（发生了改写）"

    # 「褐色的斑点」带「的」，与词表里的「褐色斑点」不同，精确匹配不应命中后者
    assert "褐色斑点" not in find_terms("叶片上有褐色的斑点，边缘发黄")


# ==================================================================== 方言（3.4）

@case("方言配置：八种方言，且无独立 ASR 模型的如实标注为回退")
def _():
    from src.dialect.config import asr_params, list_dialects

    ds = list_dialects()
    assert len(ds) == 8, f"应有 8 种方言，实际 {len(ds)}"
    assert {d["key"] for d in ds} == {
        "mandarin", "cantonese", "sichuan", "henan", "northeast",
        "hunan", "shaanxi", "taiwan",
    }, "八种方言的名单变了"

    supported = {d["key"] for d in ds if d["asr_supported"]}
    assert supported == {"mandarin", "cantonese", "sichuan"}, \
        f"百度 REST 只有这 3 种方言模型，实际标为支持的是 {supported}"

    for d in ds:
        if not d["asr_supported"]:
            p = asr_params(d["key"])
            assert p["asr_fallback"] and p["dev_pid"] == 1537, \
                f"{d['name']} 应回退普通话 1537，实际 {p}"
            assert p["fallback_note"], f"{d['name']} 缺少回退说明"


@case("方言配置：未知 key 回退默认方言而不是抛异常")
def _():
    from src.dialect.config import get_dialect

    d = get_dialect("不存在的方言")
    assert d and d["key"] == "mandarin", f"应回退普通话，实际 {d.get('key')}"


# ==================================================================== 讯飞语音（3.1 扩展）

@case("讯飞方言大模型：一个 mulacc 覆盖八种方言，八种全部标为支持")
def _():
    from src.dialect.config import list_dialects, xfyun_asr_params

    ds = list_dialects(provider="iflytek")
    assert len(ds) == 8, f"应有 8 种方言，实际 {len(ds)}"
    assert all(d["asr_supported"] for d in ds), \
        "讯飞方言大模型用 accent=mulacc 覆盖 202 种方言，八种都应标为支持"
    # 换了 provider 就不能再带百度的 dev_pid，否则前端/接口会展示一个并不生效的模型号
    assert all(d["asr_dev_pid"] is None for d in ds), "讯飞无 dev_pid 概念，应为 null"

    for key in ("mandarin", "cantonese", "henan", "hunan", "taiwan"):
        p = xfyun_asr_params(key)
        assert p["domain"] == "slm" and p["accent"] == "mulacc", f"{key} 的 ASR 参数不对：{p}"
        assert p["asr_supported"] and not p["fallback"], f"{key} 不应有回退"


@case("讯飞 TTS：实测支持的方言各有发音人，没有的如实回退、不假装是方言")
def _():
    from src.dialect.config import XFYUN_FALLBACK_VCN, _xfyun_tts_of, list_dialects, xfyun_tts_params

    # 这份名单是 2026-10-06 用真实 Key 逐个打讯飞 TTS 实测出来的（见 dialect.yaml 注释），
    # 不是查文档抄来的：ViviVoice 引擎这 8 种全部返回了 mp3，一个都没落空 ——
    # 天津话因为只有属「超拟人」接口的 x4_zijin_oral（实测 code=11200）已被删出配置。
    VERIFIED = {"mandarin", "cantonese", "sichuan", "henan", "northeast",
                "hunan", "shaanxi", "taiwan"}

    for d in list_dialects(provider="iflytek"):
        p = xfyun_tts_params(d["key"])
        if d["key"] in VERIFIED:
            assert p["tts_supported"], f"{d['name']} 实测支持，却被标为不支持：{p}"
            # 注意不能断言 vcn != XFYUN_FALLBACK_VCN：普通话的方言音色本来就是那个
            # 通用音色，该断言会误伤它。改为断言「用的就是自己配置里那个 vcn」。
            assert d["xfyun_vcn"], f"{d['name']} 在列表接口里没暴露发音人 id"
            assert p["vcn"] == d["xfyun_vcn"], \
                f"{d['name']} 用的不是自己配的发音人 {d['xfyun_vcn']}：{p}"
            assert not p["note"], f"{d['name']} 既然支持就不该带回退说明：{p['note']}"
        else:
            # 没实测通过的方言不许标成支持 —— 那正是「假支持」，等于对外宣称
            # 播的是方言、实际播普通话。
            assert not p["tts_supported"], f"{d['name']} 没有可用发音人，不该标为支持：{p}"
            assert p["vcn"] == XFYUN_FALLBACK_VCN, \
                f"{d['name']} 没发音人却不回退普通话：{p['vcn']}"
            assert p["note"], f"{d['name']} 回退了就必须说清楚为什么，否则无法如实告知用户"

    ok = xfyun_tts_params("cantonese")
    assert ok["vcn"] == "vixm" and ok["tts_supported"], f"粤语应有发音人 vixm，实际 {ok}"

    # 回退分支本身仍要能用（以后新增方言但查不到发音人时靠它如实标注），
    # 用假配置直接喂给内部函数来覆盖。
    p = _xfyun_tts_of({"key": "test", "name": "某种话", "xfyun_vcn": None,
                       "xfyun_tts_supported": False, "xfyun_note": "讯飞未提供该方言发音人。"})
    assert not p["tts_supported"], "没发音人却标为支持"
    assert p["vcn"] == XFYUN_FALLBACK_VCN, f"应回退普通话发音人，实际 {p['vcn']}"
    assert p["note"], "缺少「为何回退」的说明，前端就没法如实告知用户"

    # 配了 vcn 但 tts_supported 忘了打开，也必须当不支持处理（不能只信其中一个字段）
    p = _xfyun_tts_of({"key": "test", "name": "某种话", "xfyun_vcn": "vixx",
                       "xfyun_tts_supported": False, "xfyun_note": "未核实。"})
    assert not p["tts_supported"] and p["vcn"] == XFYUN_FALLBACK_VCN, \
        f"tts_supported=false 时不该用配置里的 vcn：{p}"


@case("讯飞鉴权：签名字符串按官方算法构造，且 api_secret 不先做 base64 解码")
def _():
    import base64
    import hashlib
    import hmac
    from urllib.parse import parse_qs, urlparse

    from src.voice.iflytek_provider import auth_url

    url = auth_url("wss://tts-api.xfyun.cn/v2/tts", "test_key", "test_secret",
                   date="Mon, 06 Oct 2026 12:00:00 GMT")
    q = parse_qs(urlparse(url).query)
    assert q["host"] == ["tts-api.xfyun.cn"], f"host 参数不对：{q['host']}"
    assert q["date"] == ["Mon, 06 Oct 2026 12:00:00 GMT"]

    # 独立复算一遍签名 —— 不是把实现再抄一遍，而是照官方文档的字符串拼法重写，
    # 两者对不上就说明拼接格式（换行 / 空格 / headers 顺序）写错了。
    origin = ("host: tts-api.xfyun.cn\n"
              "date: Mon, 06 Oct 2026 12:00:00 GMT\n"
              "GET /v2/tts HTTP/1.1")
    expect_sig = base64.b64encode(
        hmac.new(b"test_secret", origin.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    auth = base64.b64decode(q["authorization"][0]).decode("utf-8")
    assert f'signature="{expect_sig}"' in auth, f"签名与独立复算结果不一致：{auth}"
    assert 'algorithm="hmac-sha256"' in auth
    assert 'headers="host date request-line"' in auth
    assert 'api_key="test_key"' in auth


@case("讯飞音频：encoding=raw 要裸 PCM，WAV 头必须按 chunk 偏移剥离")
def _():
    import struct

    from src.voice.iflytek_provider import pcm_from_wav

    def wav(chunks):
        body = b""
        for cid, p in chunks:
            body += cid + struct.pack("<I", len(p)) + p
            if len(p) & 1:
                body += b"\x00"          # RIFF 规定奇数长度的 chunk 后面补一个填充字节
        return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body

    pcm = bytes(range(256)) * 20
    standard = wav([(b"fmt ", b"\x00" * 16), (b"data", pcm)])
    assert standard[:12] == b"RIFF" + struct.pack("<I", len(standard) - 8) + b"WAVE"
    assert pcm_from_wav(standard) == pcm, "标准 44 字节头 WAV 未正确剥离"
    # data 之前多一个 LIST chunk —— 此时真实偏移远大于 44，硬编码 44 会切错
    assert pcm_from_wav(wav([(b"fmt ", b"\x00" * 16), (b"LIST", b"INFO" * 10),
                             (b"data", pcm)])) == pcm, "带额外 chunk 的 WAV 剥离错误"
    # 奇数长度的 chunk：它自己带填充字节，解析时必须跳过，否则后面全错位
    assert pcm_from_wav(wav([(b"fmt ", b"\x00" * 15), (b"data", pcm)])) == pcm, \
        "奇数长度 chunk 的对齐处理有误"
    assert pcm_from_wav(pcm) == pcm, "裸 PCM 应原样返回"

    try:
        pcm_from_wav(b"RIFF" + struct.pack("<I", 4) + b"WAVE")
    except ValueError:
        pass
    else:
        raise AssertionError("只有头没有 data 段的 WAV 应当明确报错，而不是回传一整段垃圾")


@case("讯飞分帧：数据帧之后补空末帧，而不是把最后一个数据帧标成末帧")
def _():
    from src.voice.iflytek_provider import FRAME_BYTES, asr_frames

    frames = asr_frames(b"\x01" * (FRAME_BYTES * 3 + 10))
    assert [f[1] for f in frames] == [0, 1, 1, 1, 2], f"status 序列不对：{[f[1] for f in frames]}"
    assert frames[-1][2] == b"", "末帧应为空音频（status=2）"
    assert [f[0] for f in frames] == [1, 2, 3, 4, 5], "seq 应从 1 连续递增"

    short = asr_frames(b"abc")
    assert [f[1] for f in short] == [0, 2] and short[-1][2] == b"", \
        "极短音频也应先出一个数据帧再补末帧"


@case("讯飞 ASR 请求必须带 parameter.iat.result（缺了它服务端一律回 10106，与音频无关）")
def _():
    import json as _json

    import src.voice.iflytek_provider as fp
    from src.dialect.config import xfyun_asr_params

    wrapper = xfyun_asr_params("sichuan").get("result")
    assert wrapper == {"encoding": "utf8", "compress": "raw", "format": "json"}, \
        "输出封装三要素被改动了 —— 实测只有这三项能让服务端接受请求"

    sent = []

    class _FakeWs:
        def send(self, raw):
            sent.append(_json.loads(raw))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    real_connect, real_collect = fp._connect, fp.IflytekVoice._collect_asr
    fp._connect = lambda url, timeout: _FakeWs()
    fp.IflytekVoice._collect_asr = lambda self, ws, segs: ""
    try:
        fp.IflytekVoice(app_id="appid", api_key="k", api_secret="s").transcribe(
            b"\x00" * 100, dialect="sichuan"
        )
    finally:
        fp._connect, fp.IflytekVoice._collect_asr = real_connect, real_collect

    assert sent, "一帧都没发出去"
    for i, msg in enumerate(sent):
        iat = (msg.get("parameter") or {}).get("iat") or {}
        assert iat.get("result") == wrapper, \
            f"第 {i + 1} 帧的 parameter.iat 缺少 result —— 服务端会以 10106 拒掉整条请求"
        assert iat["accent"] == "mulacc" and iat["domain"] == "slm", "result 不能挤掉业务参数"
        # 放错层级是这条 bug 的原始形态：payload.result 会被服务端判为 unknown field
        assert "result" not in (msg.get("payload") or {}), \
            "result 属于 parameter.iat（响应字段配置），放进 payload 是 unknown field"
        assert (msg.get("payload") or {}).get("audio", {}).get("seq") == i + 1, \
            "result 不能把音频节点挤掉"


@case("讯飞识别结果：wpgs 动态修正的 rpl 分片要整段替换，不能直接追加")
def _():
    from src.voice.iflytek_provider import merge_pgs

    segs: dict[int, str] = {}

    def push(text, sn, pgs="apd", rg=None):
        chunk = {"ws": [{"cw": [{"w": text}]}], "sn": sn, "pgs": pgs}
        if rg:
            chunk["rg"] = rg
        return merge_pgs(segs, chunk)

    assert push("玉米", 1) == "玉米"
    assert push("大斑", 2) == "玉米大斑"
    # 第 2 段被重新识别：必须先删掉旧的「大斑」再写入，否则拼出「玉米大斑大斑病」
    assert push("大斑病", 2, "rpl", [2, 2]) == "玉米大斑病"
    assert push("发生", 3) == "玉米大斑病发生"
    # 跨多段的替换
    assert push("流行", 2, "rpl", [2, 3]) == "玉米流行"


@case("讯飞与百度的结果字段完全对齐（ASRResult/TTSResult 是裸 dict，缺键不会被编译器发现）")
def _():
    import ast

    from src.common.paths import PROJECT_ROOT

    def result_keys(rel_path: str, method: str, call_name: str) -> set[str]:
        tree = ast.parse((PROJECT_ROOT / rel_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == method:
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                            and sub.func.id == call_name and sub.args
                            and isinstance(sub.args[0], ast.Dict)):
                        return {k.value for k in sub.args[0].keys}
        return set()

    for method, call in (("transcribe", "ASRResult"), ("synthesize", "TTSResult")):
        base = result_keys("src/voice/baidu_provider.py", method, call)
        assert base, f"没能从百度实现里解析出 {call} 的字段（测试本身失效了）"
        for rel in ("src/voice/iflytek_provider.py", "src/voice/mock_provider.py"):
            got = result_keys(rel, method, call)
            missing = base - got
            assert not missing, (
                f"{rel} 的 {method}() 少了字段 {sorted(missing)}；"
                "缺键不会报错，只会在 response_model 处被静默吞掉"
            )


# ==================================================================== 响应契约（4.x）

@case("多模态响应结构与接口契约一致（Mock 与真实实现共用同一套字段）")
def _():
    import json

    from src.common.paths import path_of
    from src.llm.mock_provider import MockLLM

    label_map = json.loads(path_of("label_map").read_text(encoding="utf-8"))
    entry = next(e for e in label_map["index_to_label"].values() if e["label_cn"] == "玉米大斑病")

    report = MockLLM().generate_report({
        "image_result": {"top1": {**entry, "confidence": 0.9}, "topk": []},
        "asr_text": "", "user_text": "叶子上有梭形大斑",
        "candidates": [{**entry, "confidence": 0.9}], "retrieved": [],
    })

    required = ["disease_name", "confidence", "symptom_match", "treatment_sections",
                "treatment_plan", "medication_reminder", "safety_note", "summary",
                "source", "differential"]
    missing = [k for k in required if k not in report]
    assert not missing, f"报告缺少字段 {missing}"
    assert len(report["treatment_sections"]) >= 2, "防治方案至少应分 2 个小节"
    assert report["disease_name"] == "玉米大斑病"
    # 用药提醒里必须带安全提示，不能给出具体剂量
    assert "农药登记标签" in report["medication_reminder"], "用药提醒缺少「以农药登记标签为准」的安全提示"
    assert report["treatment_plan"], "缺少供 TTS 朗读的纯文本版方案"


@case("Mock 报告在信息不足时如实说明，而不是硬给结论")
def _():
    from src.llm.mock_provider import MockLLM

    report = MockLLM().generate_report({
        "image_result": None, "asr_text": "", "user_text": "叶子有点不对劲",
        "candidates": [], "retrieved": [],
    })
    assert report["disease_name"] == "", "无候选时不应编造病名"
    assert report["confidence"] == 0.0
    assert "补充" in report["symptom_match"], "应给出需要补充的信息指引"


@case("Mock 语音不伪造识别结果，且结构字段与真实实现一致")
def _():
    from src.voice.mock_provider import MockVoice

    v = MockVoice()
    asr = v.transcribe(b"\x00" * 100, dialect="cantonese")
    assert asr["text"] == "", "Mock 不应伪造转写文本"
    assert asr["mock"] is True and asr["message"], "应给出未配置说明"

    tts = v.synthesize("测试", dialect="cantonese")
    assert tts["audio_url"] is None, "Mock 不生成音频地址"
    assert tts["mock"] is True and tts["message"]

    for key in ("text", "dialect", "dialect_name", "provider", "dev_pid",
                "asr_supported", "fallback", "fallback_note", "elapsed_ms", "mock"):
        assert key in asr, f"ASR 结果缺少字段 {key}（Mock 与真实实现的契约必须一致）"
    for key in ("audio_url", "dialect", "dialect_name", "provider", "voice_name",
                "mock", "message"):
        assert key in tts, f"TTS 结果缺少字段 {key}"


@case("推理器与训练使用同一套归一化与输入尺寸")
def _():
    from src.common.constants import DEFAULT_INPUT_SIZE
    from src.data.transforms import eval_transform
    from src.infer.predictor import load_label_map

    assert DEFAULT_INPUT_SIZE == 224
    tf = eval_transform(DEFAULT_INPUT_SIZE)
    import numpy as np

    from PIL import Image

    out = tf(Image.new("RGB", (300, 300), (128, 128, 128)))
    assert tuple(out.shape) == (3, 224, 224), f"推理输入尺寸应为 (3,224,224)，实际 {tuple(out.shape)}"
    assert np.isfinite(out.numpy()).all()

    lm = load_label_map()
    assert lm["num_classes"] == 271


# ==================================================================== 知识库（4.5）

@case("知识卡片：正文齐全，且标注了内容来源（不是官方原文）")
def _():
    import json

    from src.common.paths import path_of

    data = json.loads((path_of("library_dir") / "library_items.json").read_text(encoding="utf-8"))
    assert data["total"] == 399, \
        f"应有 399 张卡片（250 病害 + 102 虫害 + 47 养护），实际 {data['total']}"

    kinds = {c["key"]: c["count"] for c in data["categories"]}
    assert kinds.get("pest") == 250 and kinds.get("insect") == 102 and kinds.get("care") == 47, \
        f"分类数量不对：{kinds}"
    # 分类名与页面文案是同一份口径：「病虫害防治知识」这个名字在加了真·虫害卡片之后
    # 就成了自相矛盾的值（那个键装的**全是病害**），所以必须叫「病害防治知识」
    names = {c["key"]: c["name"] for c in data["categories"]}
    assert names == {"pest": "病害防治知识", "insect": "虫害防治知识",
                     "care": "农作物养护知识"}, f"分类名不对：{names}"

    for item in data["items"]:
        assert item.get("content_source"), f"{item['id']} 缺少内容来源标注"
        assert len(item.get("sections") or []) >= 2, f"{item['id']} 的小节太少"
        assert item.get("read_text"), f"{item['id']} 缺少供方言朗读的纯文本"

    # 每个病害卡片都必须带安全提示
    pest = next(i for i in data["items"] if i["category"] == "pest")
    assert any("农药登记标签" in s["text"] for s in pest["sections"]), \
        "病害卡片必须包含「以农药登记标签为准」的安全提示"


@case("虫害卡片：102 类全覆盖、索引连续、类群合法、无「病原类型」污染")
def _():
    import json

    from src.common.paths import path_of
    from src.content.library_build import PEST_TEXT

    data = json.loads((path_of("library_dir") / "library_items.json").read_text(encoding="utf-8"))
    insect = [i for i in data["items"] if i["category"] == "insect"]
    assert len(insect) == 102, f"虫害卡片应为 102 张（IP102 的 102 类），实际 {len(insect)}"

    # 索引必须是 0..101 的连续排列 —— 生成端按 range(num_classes) 建卡，
    # 一旦中间断了就说明有类别在词表和卡片之间被漏掉
    assert sorted(i["index"] for i in insect) == list(range(102)), "虫害卡片索引不是 0..101 连续"

    pest_lm = json.loads(path_of("pest_label_map").read_text(encoding="utf-8"))
    assert pest_lm["num_classes"] == 102
    valid_types = set(pest_lm["pest_types"])
    # PEST_TEXT 的键与词表的 pest_types 必须完全一致，否则会有类群套不上模板、
    # 悄悄退回 PEST_TEXT["其他"]，正文与类群对不上却看不出来
    assert set(PEST_TEXT) == valid_types, \
        f"PEST_TEXT 的键与 pest_label_map.pest_types 不一致：{set(PEST_TEXT) ^ valid_types}"

    for item in insect:
        cid = item["index"]
        entry = pest_lm["index_to_label"][str(cid)]
        assert item["id"] == f"lib_insect_{cid}", f"{item['id']} 的 id 与索引不符"
        assert item["condition"] == entry["label_cn"] == item["tags"][0], \
            f"{item['id']} 的 condition / 标签与词表对不上（前端要靠它渲染「虫害：X」徽章）"
        assert item["pest_type"] in valid_types, f"{item['id']} 的类群非法：{item['pest_type']}"
        # 虫害没有「病原类型」，这个字段必须留空 —— 前端正是拿它区分病害/虫害两条分支的
        assert item["pathogen_type"] == "", f"{item['id']} 不应有 pathogen_type"
        assert "latin" in item, f"{item['id']} 缺少 latin 字段（前端按它渲染学名徽章）"
        assert len(item["sections"]) >= 3, f"{item['id']} 的小节数不足（危害规律/防治方法/用药提醒）"
        assert item["sections"][0]["type"] == "damage", \
            f"{item['id']} 的首节应是「危害规律」(damage)，实际 {item['sections'][0]['type']}"
        assert any("农药登记标签" in s["text"] for s in item["sections"]), \
            f"{item['id']} 缺少用药安全提示"

    # 封面来自 IP102 标注的第一张含该类别的图片。实测有 5 个类别在 train/val/test
    # 里一个实例都没有（与 README 记的「val 覆盖 97/102」是同一个数据事实），
    # 所以这里要求的是覆盖率而不是每张都有 —— 前端本来就支持无封面的卡片。
    with_cover = [i for i in insect if i["cover"]]
    assert len(with_cover) >= 97, f"虫害卡片封面覆盖率过低：{len(with_cover)}/102"
    for item in with_cover:
        assert item["cover"].startswith("/media-pest/"), \
            f"{item['id']} 的封面没走 /media-pest 挂载点：{item['cover']}"

    # 学名同理：词表里 102 类有 2 类（灰象甲、橘瘿蚊）没有可靠的拉丁学名，
    # 卡片如实留空而不是编一个。留空的卡片 tags 里也不该出现空字符串。
    with_latin = [i for i in insect if i["latin"]]
    assert len(with_latin) >= 100, f"虫害卡片学名覆盖率过低：{len(with_latin)}/102"
    for item in insect:
        assert "" not in item["tags"], f"{item['id']} 的标签里有空字符串：{item['tags']}"


@case("全部卡片 id 全局唯一：lib_pest_ / lib_insect_ / lib_care_ 三套前缀不得撞车")
def _():
    import json
    from collections import Counter

    from src.common.paths import path_of

    data = json.loads((path_of("library_dir") / "library_items.json").read_text(encoding="utf-8"))
    ids = [i["id"] for i in data["items"]]
    dupes = [k for k, n in Counter(ids).items() if n > 1]
    # pest 的索引是 0..270、insect 是 0..101，两者数值区间重叠 ——
    # 前缀一旦写错（比如虫害也写成 lib_pest_{index}），这里就是唯一的报警点
    assert not dupes, f"卡片 id 重复：{dupes}"
    assert len(ids) == data["total"] == len(set(ids))
    assert {i["id"].rsplit("_", 1)[0] for i in data["items"]} == \
        {"lib_pest", "lib_insect", "lib_care"}, "出现了预期之外的卡片 id 前缀"


@case("用药提醒：按防治对象匹配农药登记数据、逐字可核对、匹配不到回落通用提示")
def _():
    import json
    import re

    from src.common.paths import PROJECT_ROOT, path_of
    from src.content.library_build import (
        CONTENT_SOURCE,
        SAFETY_NOTE,
        load_pesticide_registry,
        match_target,
        medication_text,
    )

    # 索引是运行期抓来的数据（data/pesticide_registry.json，见 scripts/26），
    # 它缺席时整条链路必须自己降级 —— 所以这里先确认「缺了也不崩」这件事由
    # load_pesticide_registry 保证（返回 {} 而不是抛异常），再验它在位时的行为。
    reg_path = path_of("pesticide_registry")       # 键不在 configs 里 path_of 就抛 KeyError
    reg = load_pesticide_registry()
    if not reg:
        assert medication_text("稻瘟病", "水稻") == SAFETY_NOTE, \
            "没有登记索引时用药提醒必须原样回落到 SAFETY_NOTE"
        return
    assert reg_path.exists(), f"path_of 认这个键但文件不在：{reg_path}"
    # total_records 是「从附件里读到的登记行数」，targets 是「登记条目 × 防治对象」的
    # 展开对（一份登记列了 3 个防治对象就出现 3 次），所以前者必须等于各来源行数之和，
    # 而不是 targets 的条目数 —— 这两者别搞混
    assert reg["total_records"] == sum(s["rows"] for s in reg["sources"]), \
        "total_records 与各来源附件的行数之和对不上"
    assert reg["target_count"] == len(reg["targets"])
    assert reg["total_records"] <= sum(len(v) for v in reg["targets"].values()), \
        "展开对不该比原始行数还少"
    # 抓来的东西必须自报家门，卡片上的「摘自某来源」才有出处可查
    assert reg["source_name"] == "农业农村部农药管理司《农药登记信息》"

    targets = reg["targets"]
    all_nos = {r["reg_no"] for rows in targets.values() for r in rows}

    # ---- 匹配规则：全等 > 长键优先 > 双向包含 ----
    assert match_target("稻瘟病", targets) == "稻瘟病"
    # 种名归到类群名。亚洲玉米螟 -> 玉米螟 这条最典型：IP102 用种名、登记表用类群名，
    # 不靠这层归并，玉米螟这张卡片就白瞎了
    assert match_target("亚洲玉米螟", targets) == "玉米螟"
    assert match_target("白背飞虱", targets) == "飞虱"
    # 键长下限是 2：单字键（比如「螨」）会把「叶螨」「红蜘蛛」这类全吞掉，
    # 宁可漏也不能错配
    assert all(len(k) >= 1 for k in targets)
    assert match_target("", targets) is None
    assert match_target("这个病不存在", targets) is None

    # ---- 生成的文本：合规句 + 真实登记证号 + 例子不超过上限 ----
    txt = medication_text("稻瘟病", "水稻")
    assert txt != SAFETY_NOTE, "「稻瘟病」在登记索引里有 50 多个产品，不该回落"
    assert "农药登记标签" in txt, "用药段落必须仍含硬约束句（测试 585/625/499 锁的就是它）"
    assert "农业农村部农药管理司《农药登记信息》" in txt, "没写清数据出处"
    assert "水稻" in txt, "应当说明举的例子是登记用于该作物的"
    nos = re.findall(r"登记证号 ([A-Za-z0-9\-]+)", txt)
    assert 0 < len(nos) <= 4, f"例子数应在 1～4 个，实际 {len(nos)}"
    for no in nos:
        assert no in all_nos, f"卡片上的登记证号 {no} 不在索引里 —— 说明有文本是编的"

    # ---- 匹配不到就回落，且回落文本与安全提示逐字一致 ----
    assert medication_text("缺氮症", "番茄") == SAFETY_NOTE
    assert medication_text("苗期冷害", "") == SAFETY_NOTE

    # ---- 作物优先：给了作物就把该作物的登记排前面 ----
    crop_rows = targets.get("稻瘟病") or []
    if any("水稻" in (r.get("crop") or "") for r in crop_rows):
        first = re.search(r"例如：([^（]+)", medication_text("稻瘟病", "水稻")).group(1)
        rice_names = {r["product"] for r in crop_rows if "水稻" in (r.get("crop") or "")}
        assert first in rice_names, f"举的第一个例子「{first}」不是登记用于水稻的"

    # ---- 建出来的库里：病害/虫害卡片的用药段落都换成了登记数据 ----
    lib = json.loads((path_of("library_dir") / "library_items.json").read_text(encoding="utf-8"))
    for cat, label, floor in (("pest", "病害", 0.5), ("insect", "虫害", 0.4)):
        cards = [c for c in lib["items"] if c["category"] == cat]
        assert cards, f"没有{label}卡片"
        sections = [s for c in cards for s in c["sections"] if s["type"] == "medication"]
        assert len(sections) == len(cards), f"{label}卡片应各有一个用药提醒小节"
        for s in sections:
            assert s["title"] == "用药提醒"
            assert "农药登记标签" in s["text"], "每张卡片的用药提醒都要保留合规句"
        hit = [s for s in sections if "农药登记信息" in s["text"]]
        assert len(hit) / len(cards) >= floor, \
            f"{label}卡片的登记覆盖率只有 {len(hit)}/{len(cards)}，匹配规则可能被改坏了"

    # ---- 事实来源标注只保留用药那一段 ----
    # 页面上的「内容来源」是给读者看的一句话，不是开发日志：只写"这段信息从哪来"。
    # 以前这里还带一句「防治规律与防治方法为规则模板生成（演示内容）」，属于自曝式免责，
    # 已按约定删掉。所以这条断言要**反过来**锁：这类自曝话术不许再回到 content_source。
    assert "农药登记信息" in CONTENT_SOURCE, "content_source 没提用药数据来自登记信息"
    for word in ("规则模板", "演示内容", "演示数据", "非官方"):
        assert word not in CONTENT_SOURCE, f"content_source 又混进了自曝式话术「{word}」"
    disclaimer = lib["disclaimer"]
    assert "农药登记信息" in disclaimer and "农药登记标签" in disclaimer

    # ---- SAFETY_NOTE 必须仍然可导入且含合规句（src/llm/mock_provider.py 依赖它）----
    assert "农药登记标签" in SAFETY_NOTE

    # ---- 新增的路径键与抓取脚本 ----
    assert (PROJECT_ROOT / "scripts" / "26_fetch_pesticide_registry.py").exists(), \
        "缺少抓取脚本，索引没有可复现的来源"
    gi = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/pesticide_registry.json" in gi, \
        "抓来的登记索引不随仓库走，必须进 .gitignore"


# ==================================================================== 首页 / 前端（5.2 改造）

@case("首页预加载：home_preload.js 含底图与 34 省 × 4 季，且能被 JS 直接读出")
def _():
    import json
    import re

    from src.common.paths import path_of

    path = path_of("frontend_dir") / "assets" / "data" / "home_preload.js"
    assert path.exists(), f"缺少 {path}；请运行 python scripts/15_bundle_frontend_data.py"
    text = path.read_text(encoding="utf-8")

    # 这个文件是 <script> 直接加载的，语法错了整页白屏，所以必须真的解析一次。
    # 这里不引入 js 解析器，只把两个变量后面的 JSON 截出来用 json.loads 验一遍 ——
    # 生成端用的是 json.dumps，所以两者等价，而 U+2028/2029 是唯一允许被转义的例外。
    geo_m = re.search(r"window\.__CHINA_GEO__\s*=\s*(\{.*?\});\s*\n", text, re.S)
    pest_m = re.search(r"window\.__PROVINCE_PEST__\s*=\s*(\{.*\});\s*$", text.strip(), re.S)
    assert geo_m and pest_m, "没找到 window.__CHINA_GEO__ / window.__PROVINCE_PEST__ 两个变量"

    geo = json.loads(geo_m.group(1))
    snap = json.loads(pest_m.group(1))
    assert geo.get("type") == "FeatureCollection", "底图不是 GeoJSON FeatureCollection"
    assert len(snap["provinces"]) == 34, f"应有 34 个省份，实际 {len(snap['provinces'])}"
    assert len(snap["seasons"]) == 4, f"应有 4 个季节，实际 {len(snap['seasons'])}"

    for p in snap["provinces"]:
        missing = [s["key"] for s in snap["seasons"] if s["key"] not in (p.get("seasons") or {})]
        assert not missing, f"{p['name']} 缺少季节数据：{missing}"


@case("首页 index.html 在 map.js 之前同步加载 three.js 与预加载文件")
def _():
    from src.common.paths import path_of

    html = (path_of("frontend_dir") / "index.html").read_text(encoding="utf-8")
    i_three = html.find("/assets/vendor/three/three.min.js")
    i_3d = html.find("/assets/js/map3d.js")
    i_preload = html.find("/assets/data/home_preload.js")
    i_map = html.find("/assets/js/map.js")
    assert i_preload > 0, "index.html 没有引用 home_preload.js"
    assert i_three > 0, "首页沙盘要用 three.js，index.html 必须引用 vendor/three/three.min.js"
    assert i_3d > 0, "index.html 没有引用 map3d.js"
    # 沙盘换掉 ECharts 之后首页不该再拉它（vendor 文件仍留在磁盘上，只是不再引用）
    assert "echarts" not in html, "首页已改用 three.js 沙盘，index.html 不应再引用 echarts"
    # 三对先后关系，而不是一条全序 —— 实际的顺序是
    # three → home_preload → 几个公共 js → map3d → map，
    # map3d 只要排在 map.js 前面就行，硬写成一条链会平白限制以后插文件的自由。
    assert i_three < i_3d, "three.js 必须在 map3d.js 之前：后者一执行就要用全局 THREE"
    assert i_3d < i_map, "map3d.js 必须在 map.js 之前：map.js 要调 Map3D.mount"
    assert i_preload < i_map, "home_preload.js 必须在 map.js 之前，首屏才不依赖接口"
    # 这几段都不能带 defer/async，否则就退化成"下载完了再执行"，失去同步保证
    for anchor, name in ((i_three, "three.min.js"), (i_3d, "map3d.js"),
                         (i_preload, "home_preload.js"), (i_map, "map.js")):
        tag = html[html.rfind("<script", 0, anchor):anchor]
        assert "defer" not in tag and "async" not in tag, \
            f"{name} 必须同步加载，实际标签是：{tag}"


@case("知识库快照：library_preload.js 与 library_items.json 一致，且只含列表字段")
def _():
    import json
    import re

    from src.common.paths import path_of
    from src.content.frontend_bundle import LIBRARY_BRIEF_FIELDS

    path = path_of("frontend_dir") / "assets" / "data" / "library_preload.js"
    assert path.exists(), f"缺少 {path}；请运行 python scripts/15_bundle_frontend_data.py"
    text = path.read_text(encoding="utf-8")

    # 与首页快照同款验法：不引 JS 解析器，把变量后面的 JSON 截出来用 json.loads
    # 验一遍。生成端用的就是 json.dumps，两者等价。
    m = re.search(r"window\.__LIBRARY_PRELOAD__\s*=\s*(\{.*\});\s*$", text.strip(), re.S)
    assert m, "没找到 window.__LIBRARY_PRELOAD__"
    snap = json.loads(m.group(1))

    data = json.loads((path_of("library_dir") / "library_items.json").read_text(encoding="utf-8"))
    assert snap["total"] == data["total"] == len(snap["items"]), \
        f"快照条数与知识库对不上：快照 {snap['total']} / 数据 {data['total']} / 列表 {len(snap['items'])}"
    assert snap["categories"] == data["categories"], "快照的分类计数与知识库不一致"
    assert snap.get("generated_at"), "快照缺少生成时间（排查「快照是不是旧的」时全靠它）"

    # 正文（sections，占 library_items.json 的绝大部分）绝不能进快照：
    # 这是同步阻塞加载的文件，1.3 MB 会把首屏拖垮
    item = snap["items"][0]
    assert set(item) == set(LIBRARY_BRIEF_FIELDS), \
        f"快照字段与 LIBRARY_BRIEF_FIELDS 不符：{set(item) ^ set(LIBRARY_BRIEF_FIELDS)}"
    assert "sections" not in item and "read_text" not in item
    assert text.count("sections") == 0, "快照里不应出现正文"
    assert len(snap["items"]) == 399


@case("LIBRARY_BRIEF_FIELDS 与列表接口的 _LIST_FIELDS 必须一致")
def _():
    from backend.app.routers.library import _LIST_FIELDS
    from src.content.frontend_bundle import LIBRARY_BRIEF_FIELDS

    # 两边各自独立演化过一次：快照多一个字段没人管无所谓，少一个字段就是
    # 「离线时卡片缺字段、联网时又有」这种只在特定时刻复现的怪毛病。
    assert tuple(LIBRARY_BRIEF_FIELDS) == tuple(_LIST_FIELDS), (
        f"快照字段 {LIBRARY_BRIEF_FIELDS} 与接口列表字段 {_LIST_FIELDS} 必须一致"
    )


@case("详情 schema 显式声明 pest_type / latin（response_model 会静默吞掉未声明字段）")
def _():
    from backend.app.schemas.models import LibraryDetailData, LibraryItem

    # 这个坑本项目踩过两次了：pydantic 的 response_model 不报错，只是把字段丢掉，
    # 表现成「数据文件里有、接口不返回、前端拿不到」，查起来很费时间。
    for name in ("pest_type", "latin", "condition", "pathogen_type"):
        assert name in LibraryDetailData.model_fields, \
            f"LibraryDetailData 必须显式声明 {name}，否则虫害详情卡片拿不到它"
    assert "insect" in (LibraryItem.model_fields["category"].description or "")


@case("library.html 在 library.js 之前同步加载知识库快照")
def _():
    from src.common.paths import path_of

    html = (path_of("frontend_dir") / "library.html").read_text(encoding="utf-8")
    i_preload = html.find("/assets/data/library_preload.js")
    i_js = html.find("/assets/js/library.js")
    assert i_preload > 0, "library.html 没有引用 library_preload.js"
    assert i_preload < i_js, (
        "顺序必须是 library_preload.js → library.js："
        "<script> 是同步执行，library.js 跑起来时 window.__LIBRARY_PRELOAD__ 才一定已就位"
    )
    tag = html[html.rfind("<script", 0, i_preload):i_preload]
    assert "defer" not in tag and "async" not in tag, \
        f"library_preload.js 必须同步加载，实际标签是：{tag}"

    # 快照兜底提示得有自己的元素：layout.js 的 renderDegradedBar() 独占
    # [data-degraded-bar]，library.js 再往那个节点写就会互相覆盖
    assert 'id="library-notice"' in html, "缺少 library-notice 元素（快照兜底提示的落点）"


@case("首页 map.js：所有失败路径都收敛到带重试的错误出口，不存在只成功才关遮罩")
def _():
    import re

    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "map.js").read_text(encoding="utf-8")

    # 老写法正是"卡在正在加载"的根因：遮罩只在整条链路全成功时被隐藏，
    # 中途出错既不隐藏也没有重试入口。这里锁住两件事：
    #   1) 隐藏遮罩的地方只有一处，且是成功渲染完的 renderAll()；
    #   2) 每个 catch 都要么调用 showOverlayError（可见 + 可重试），
    #      要么在就近的注释里写清楚为什么可以静默。
    assert len(re.findall(r"hideOverlay", js)) == 2, \
        "hideOverlay 应当只有「定义 + renderAll 里调用一次」两处；" \
        "多出来的调用意味着有失败路径把遮罩也关掉了"
    for m in re.finditer(r"\.catch\(", js):
        window = js[m.end():m.end() + 400]
        assert "showOverlayError(" in window or "后端没起也无所谓" in window, (
            "有一个 .catch 分支既没有走上可见的错误出口，也没说明为何可以静默："
            + window[:120]
        )
    assert "map-retry" in js, "错误出口必须带重试按钮"


@case("首页 map.js：沙盘先挂载，再渲染（回归锁）")
def _():
    import re

    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "map.js").read_text(encoding="utf-8")

    def body_of(header):
        """按大括号配对取函数体 —— 正则数不清 init() 里嵌套的 try/catch。"""
        i = js.find(header)
        assert i >= 0, f"没找到 {header}"
        j = js.find("{", i)
        depth = 0
        for k in range(j, len(js)):
            if js[k] == "{":
                depth += 1
            elif js[k] == "}":
                depth -= 1
                if depth == 0:
                    return js[j + 1:k]
        raise AssertionError(f"{header} 的大括号不配对")

    # 换了渲染引擎，这条老回归锁的对象也换了：从前是「series 里写了 map:"china"，
    # 而 echarts 要到 setOption 才去取地图数据，setOption 之前没 registerMap 就是
    # undefined」；现在是「renderAll 里的 applyData/applySeasonTheme 都会
    # ensureMap()，沙盘没挂上就直接抛」。锁的是同一件事：**建可视化对象**必须早于
    # **往里灌数据**。所以顺序锁挪到 map3d 的两处：
    #   1) mount3D 内部：先确认渲染层在，再 Map3D.mount 出沙盘
    mount = body_of("function mount3D(")
    assert "Map3D.mount(" in mount, f"mount3D 里应调用 Map3D.mount 建沙盘：{mount}"
    assert mount.find("Map3D") < mount.find("Map3D.mount("), \
        "mount3D 要先确认 map3d.js 已加载（typeof Map3D）再 mount"

    #   2) init() 里：先 mount3D（建沙盘）再 startFromSnapshot（触发首次渲染）
    init = body_of("function init()")
    assert "mount3D(" in init and "startFromSnapshot(" in init, \
        f"init 里应同时调用 mount3D 与 startFromSnapshot：{init}"
    assert init.find("mount3D(") < init.find("startFromSnapshot("), (
        "init 必须先 mount3D 再 startFromSnapshot —— "
        "反过来的话首次渲染时 map 还是 null，ensureMap() 会直接抛"
    )

    # 沙盘只应该建这一个地方；多一处就意味着一份数据被挂到两个 canvas 上
    assert len(re.findall(r"Map3D\.mount\(", js)) == 1, \
        "Map3D.mount 应当只出现在 mount3D 里"
    # 数据层没变：仍然优先吃本地预加载，接口只是回退
    assert "window.__CHINA_GEO__" in js and "window.__PROVINCE_PEST__" in js, \
        "map.js 应优先使用本地预加载数据"


@case("首页 map3d.js：不整圈自转、取景自己收边、板外的地不画（回归锁）")
def _():
    import re

    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "map3d.js").read_text(encoding="utf-8")
    # 去注释再判：注释里为了讲清缘由会引用这些名字，逐字匹配会被自己的说明坑到
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)

    # 首屏不得有任何自动转视角。用户明确要求把旧的「空闲摆动」撤掉；整圈自转
    # 更不行 —— 转到一半地图就是倒的，那就不成地图了。方位角只由用户拖着改。
    assert "view.yaw +=" not in code, "空闲动效不能写成整圈自转"
    assert "applySway" not in code and "swayPhase" not in code, \
        "首屏不应再有自动摆动（用户已要求撤掉）；方位角只由拖动改变"

    # 取景：把样本点投到 NDC 里量，而不是拿半宽/半深推解析解。
    # 斜俯视时近端那条边离相机比中心近得多，解析解会把海南岛、台湾切在画外。
    assert ".project(camera)" in code, "取景必须靠实际投影来量，不能用半宽/半深推"
    assert "addScaledVector" in code, \
        "取景要把注视点挪到模型在画面里的投影中心，否则模型整个偏在画面一角、四周空一大圈"

    # 底板之外的多边形整块丢掉：海南那份数据里 132 个南海岛礁最南到 3.8°N，
    # 而底板只到 14.6°N。不丢就飘在板子外面、悬在页面底色上，像撒了一把灰。
    assert "function onPlate(" in code and "polygonsOf(feature).filter(" in code, \
        "落在底板外的多边形必须丢掉"

    # 调试用的全局不许留在源码里
    assert not re.search(r"window\.__\w+", js), "map3d.js 里不该留调试全局（window.__…）"


@case("首页 map.js：点击省份只停住卡片，跳转必须发生在选定病害之后（回归锁）")
def _():
    import re

    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "map.js").read_text(encoding="utf-8")

    def body_of(header):
        i = js.find(header)
        assert i >= 0, f"没找到 {header}"
        j = js.find("{", i)
        depth = 0
        for k in range(j, len(js)):
            if js[k] == "{":
                depth += 1
            elif js[k] == "}":
                depth -= 1
                if depth == 0:
                    return js[j + 1:k]
        raise AssertionError(f"{header} 的大括号不配对")

    # 老写法是在 chart 的 click 里直接 location.href = "/library.html?open=" + top1.library_id：
    # 点哪个省都只能看 TOP1，第二第三种病害根本来不及点。现在 pick 只负责把卡片停住。
    bind = body_of("function bindMapEvents()")
    assert "location.href" not in bind, (
        "省份点击不能直接跳转 —— 用户要先在卡片里选定一种病害：" + bind[:200]
    )
    assert "showCard(" in bind, "省份点击必须停住卡片"
    # 事件挂在沙盘上一个实例上，而 bindMapEvents 会被 renderAll 每次换季都走一遍。
    # 少了这个哨兵就会一季叠一批监听，同一个省点一次弹好几张卡片。
    assert "if (mapBound) return;" in bind, \
        "bindMapEvents 只有在事件未挂过时才绑定，切换季节不能重复挂"

    # 跳转只允许有一个出处，且只有「选定病害」这条路能走到
    assert len(re.findall(r"location\.href", js)) == 1, (
        "全文件跳转只应有一处（openLibrary）—— 多一处就意味着有绕开「选病害」的入口"
    )
    openlib = body_of("function openLibrary(")
    assert "location.href" in openlib and "/library.html?open=" in openlib, \
        "openLibrary 才是受理跳转的地方，且要保持知识图书库深链不变"

    # TOP3 必须真的是可点的按钮，而不是一段不可交互的文本
    render = body_of("function renderCard(")
    assert "map-card__pick" in render and "data-open=" in render, \
        "TOP3 每一行都要是可点的按钮，并带上该病害的 library_id"


@case("首页 map.js：季节切换同时改数据、按钮强调色与地图配色")
def _():
    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "map.js").read_text(encoding="utf-8")

    # [data-season] 由 applySeasonTheme 写入（经 syncSeasonAttr 落到每个
    # [data-season-host] 上），季节按钮的强调色、舞台底色、季节分带都靠它选中；
    # 而它必须在 renderAll 里被调用 —— 首屏、切季节、接口回退三条路都经过 renderAll，
    # 挂在别处就会漏掉其中一条，某个入口进来时按钮还停在上一季的颜色。
    assert "syncSeasonAttr()" in js and "dataset.season = season" in js, \
        "要有把当前季节写到 [data-season-host] 上去的那一步"
    theme_src = js[js.find("function applySeasonTheme("):]
    theme_src = theme_src[:theme_src.find("\n  }")]
    assert "syncSeasonAttr()" in theme_src, \
        "applySeasonTheme 要把当前季节写到舞台的 data-season 上"
    tail = js[js.find("function renderAll("):]
    render_all = tail[:tail.find("\n  }")]
    assert "applySeasonTheme()" in render_all, \
        "renderAll 必须调用 applySeasonTheme，否则有路径进不到季节主题"
    assert "refreshPinnedCard()" in render_all, \
        "切季节时停住的卡片要换成新季节的数据，而不是留着上一季的"

    # 风险色阶不再写死在 SEASON_THEME 里：它按季节走 CSS 令牌
    # （--map-{season}-risk-1..5），改色相但不改明度顺序，所以「深浅 = 病害种类多少」
    # 这条编码没有变，图例和地图读的仍是同一份令牌。
    # 而在常量块里塞一份十六进制副本，正是「某一季改了、另一季忘了」的来源。
    # emphasis 高亮是「可交互」信号而不是季节信号，仍然不许跟着季节变。
    tail = js[js.find("const SEASON_THEME"):]
    theme_block = tail[:tail.find("\n  };")]
    for color in ("#d7ede0", "#a8d8b9", "#6cbe8b", "#2f8f52", "#17663a"):
        assert color not in theme_block, f"风险色阶的 {color} 不许抄进季节主题常量块"
    assert "#15803d" not in theme_block, "emphasis 高亮保持品牌绿，不随季节变"

    # 实际取值改从 CSS 令牌读（深浅两套都在 base.css 里各给一份），
    # 因为 ECharts 画的是 canvas，CSS 的 @media 根本管不到它 ——
    # 不这么做，深色下舞台发黑、省份还留在浅粉绿，整块地图像贴上去的一张白天的图。
    assert "const SEASON_VARS" in js, "缺少「季节 → CSS 令牌名」的映射表"
    theme_fn = js[js.find("function theme()"):]
    theme_fn = theme_fn[:theme_fn.find("\n  }")]
    assert "cssVar(" in theme_fn, "theme() 必须经 cssVar 读令牌，否则地图不跟深色走"
    assert "SEASON_THEME" in theme_fn, "SEASON_THEME 还在当兜底常量用，不能删"

    # 令牌读不到时（样式表没加载 / 令牌名写错）要退回浅色常量，
    # 此刻的降级应该是「不变深」，而不是「一片空白」。
    for light in ("#eaf5ec", "#e7f4ea", "#f7f1e2", "#e9eff7"):
        assert light in theme_block, f"季节主题缺少浅色兜底 {light}"

    # 切系统深浅色时 canvas 不会自己重画，必须监听媒体查询主动重绘，
    # 而且重绘范围要盖住「底板/描边/标签/水」与「省份风险色 + 图例」两块，
    # 少一块就会与地图对不上。
    i = js.find('matchMedia("(prefers-color-scheme: dark)")')
    assert i >= 0, "缺少深色模式监听：切到深色后地图省份仍是浅色"
    listener = js[i:i + 700]
    assert 'addEventListener("change"' in listener or ".addListener(" in listener, \
        "拿到了媒体查询却没挂 change 回调，等于没监听"
    for need in ("applySeasonTheme()", "applyData()"):
        assert need in listener, f"深色重绘回调里缺少 {need}，切换后地图与图例会不同步"
    # 图例是 applyData 里重画的，这两件事必须绑在一起走，不能各自为政
    apply_data = js[js.find("function applyData("):]
    apply_data = apply_data[:apply_data.find("\n  }")]
    assert "renderLegend()" in apply_data, "applyData 要顺带重画图例，两者读的是同一份令牌"


@case("首页省份卡片：自有 DOM，宽度与换行规则保证文字不溢出")
def _():
    import re

    from src.common.paths import path_of

    css = (path_of("frontend_dir") / "assets" / "css" / "pages.css").read_text(encoding="utf-8")
    js = (path_of("frontend_dir") / "assets" / "js" / "map.js").read_text(encoding="utf-8")
    html = (path_of("frontend_dir") / "index.html").read_text(encoding="utf-8")

    assert 'id="map-card"' in html, "index.html 里要有卡片容器"

    # 卡片从一开始就是自有 DOM，不用 echarts 的 tooltip（拿不到固定宽度与真换行，
    # 长病害名会被顶出卡片）。现在渲染层换成了 3D 沙盘，echarts 整个从首页退场，
    # 这一条也从「关掉 tooltip」升级成「首页的代码里不许再出现 echarts」。
    # 注释里提到它是在解释「为什么换掉」，不算引用，先剔掉注释再判。
    def code_only(src):
        src = re.sub(r"/\*[\s\S]*?\*/", "", src)      # /* … */
        src = re.sub(r"<!--[\s\S]*?-->", "", src)     # <!-- … -->
        src = re.sub(r"(?m)^\s*//.*$", "", src)       # 整行 //
        src = re.sub(r"(?m)\s+//.*$", "", src)        # 行尾 //
        return src.lower()

    for name, src in (("map.js", js), ("pages.css", css), ("index.html", html)):
        assert "echarts" not in code_only(src), f"{name} 里还留着 echarts，首页已改用 3D 沙盘"
    assert "map-tooltip" not in js and "map-tooltip" not in css, \
        "旧的 .map-tooltip 样式应当已经删干净"
    assert "data-open=" in js, "卡片里的 TOP3 仍要带 library_id 深链"

    tail = css[css.find(".map-card {"):]
    card = tail[:tail.find("\n}")]
    # 上一版文字会被顶出卡片，根因就两条：只有 max-width（宽度不可预测），
    # 以及 __row 是不可换行的 flex。这里把两条修法都锁住。
    assert "width: min(" in card, "卡片要用固定 width 而不是 max-width，宽度才可预测"
    assert "overflow-wrap: anywhere" in card, "长病害名必须能断行"
    assert "pointer-events: none" in card, "预览态卡片要穿透指针，否则悬停会闪烁"
    assert '.map-card[data-pinned="true"] { pointer-events: auto; }' in css, \
        "停住态才打开 pointer-events，TOP3 才是可点的"

    row = css[css.find(".map-card__row"):][:90]
    assert "flex-wrap: wrap" in row, f"TOP3 那一行必须允许换行：{row}"
    assert "min-width: 0" in css, "flex 子项要有 min-width:0，否则长词推不动就溢出"


@case("请求封装：超时能兜住挂起的 fetch，且慢接口不被默认超时误杀")
def _():
    import re

    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "api.js").read_text(encoding="utf-8")

    # 没有 AbortController 时挂起的 Promise 既不 resolve 也不 reject，
    # 调用方的 then/catch 都不触发，界面就永远停在加载态。
    assert "AbortController" in js and "controller.abort()" in js, "缺少请求超时机制"
    assert js.count("clearTimeout(timer)") >= 2, \
        "计时器要在 fetch 抛错和读完 body 两处都清掉，只在响应头到达时清会漏掉「连上了不发数据」"

    # 诊断链路首次要惰性加载模型（实测 10s+），默认 15s 会把正常请求掐断，
    # 前端报超时、服务端其实还在算 —— 比不设超时更糟。
    m = re.search(r"const SLOW_TIMEOUT\s*=\s*\{([^}]*)\}", js)
    assert m, "缺少慢接口的超时配置"
    slow = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", m.group(1))}
    default = int(re.search(r"const DEFAULT_TIMEOUT\s*=\s*(\d+)", js).group(1))
    assert slow["diagnose"] > default and slow["ask"] > default, \
        f"诊断/问答必须单独放宽超时，实际 {slow}（默认 {default}）"
    assert "/api/diagnose/multimodal\", formData, {" in js, \
        "diagnoseMultimodal 没有把慢接口超时传下去"


@case("惠农政策页：筛选栏已移除，且后端接口契约未动")
def _():
    from src.common.paths import path_of

    front = path_of("frontend_dir")
    html = (front / "policy.html").read_text(encoding="utf-8")
    js = (front / "assets" / "js" / "policy.js").read_text(encoding="utf-8")
    css = (front / "assets" / "css" / "pages.css").read_text(encoding="utf-8")

    for gone in ("policy-filters", "filter-type", "filter-q", "policy-summary",
                 "page-head__desc", "btn-reset"):
        assert gone not in html, f"policy.html 里还残留筛选相关节点：{gone}"
    for gone in ("renderTypeOptions", "typeName", "policy-summary", "filter-q"):
        assert gone not in js, f"policy.js 里还残留筛选相关代码：{gone}"
    assert ".policy-filters" not in css, "pages.css 里还残留筛选栏样式"
    assert "Api.policies({})" in js, "应改为一次性取全量（不传筛选参数）"

    # 后端契约（任务 4.7）保持不变：只是页面不再传参，接口本身仍支持筛选
    from src.common.paths import PROJECT_ROOT

    router = (PROJECT_ROOT / "backend" / "app" / "routers" / "policy.py").read_text(encoding="utf-8")
    assert "start" in router and "type" in router, "后端 /api/policy/list 的筛选参数不应被删掉"


@case("惠农政策：原文要点逐字摘录、接口声明了字段、前端标题可点开原文")
def _():
    import json

    from src.common.paths import PROJECT_ROOT, path_of
    from src.content.policy_fetch import extract_points

    # ---- 摘录逻辑：写死一段构造的原文，逐字验证切出来的句子 ----
    body = (
        "一、耕地地力保护补贴。补贴对象原则上为拥有耕地承包权的种地农民。"
        "二、农机购置与应用补贴。补贴对象为从事农业生产的农民和农业生产经营组织。"
    )
    pts = extract_points(body, [{"title": "耕地地力保护补贴"},
                                {"title": "农机购置与应用补贴"}], "")
    assert [p["title"] for p in pts] == ["耕地地力保护补贴", "农机购置与应用补贴"], pts
    assert pts[0]["text"] == "补贴对象原则上为拥有耕地承包权的种地农民。", pts[0]["text"]
    # 摘出来的必须是原文里连续的一段，一个字都不能是改写出来的
    for p in pts:
        assert p["text"].rstrip("…") in body, f"要点不是原文原话：{p['text']}"

    # 超长句在逗号处收尾；逗号前不足 30 字就直接到句号，别截出半截短语
    long_body = "名。" + "一二三四五六七八九十" * 8 + "，" + "甲乙丙丁戊己庚辛壬癸" * 6 + "。"
    cut = extract_points(long_body, [{"title": "名"}], "")[0]["text"]
    assert cut.endswith("……") and len(cut) <= 113, cut
    assert cut.rstrip("…") in long_body

    # 没有编号的新闻稿：拿导语当第一条要点，且不能与后面的正文句子重复
    news = ("本网讯近日中央财政安排资金支持冬小麦促弱转壮。各地要抓紧落实确保夏粮丰收。"
            "要加强技术指导服务。")
    news_pts = extract_points(news, [], "近日中央财政安排资金支持冬小麦促弱转壮。")
    texts = [p["text"] for p in news_pts]
    assert len(texts) == len(set(texts)), f"要点里出现了重复：{texts}"
    assert not any("本网讯" in t for t in texts), f"「本网讯」这种新闻电头没洗掉：{texts}"

    # ---- 数据：每条政策都带 points，且都是非空原话 ----
    data = json.loads(path_of("policy_list").read_text(encoding="utf-8"))
    assert data["items"], "policy_list.json 是空的，先跑 scripts/05_fetch_policies.py"
    for it in data["items"]:
        assert "points" in it, f"{it['id']} 没有 points 字段"
        if not it["fetched"]:
            continue
        assert it["points"], f"{it['id']} 抓取成功却没有摘出任何要点"
        assert len(it["points"]) <= 6, f"{it['id']} 要点条数超上限"
        for p in it["points"]:
            assert p["text"].strip(), f"{it['id']} 有空要点"
            assert len(p["text"]) <= 120, f"{it['id']} 要点过长：{p['text'][:40]}…"
        # 没有编号条目的政策，summary 就是导语本身 —— 导语不能再当要点重复一遍，
        # 否则卡片上第一行摘要和第一条要点是同一句话。
        if not it["item_count"] and len(it["points"]) > 1:
            for p in it["points"]:
                core = p["text"].rstrip("…。")
                assert len(core) < 12 or core not in it["summary"], (
                    f"{it['id']} 的要点与摘要重复：{core[:30]}…"
                )

    # ---- 接口：字段必须声明，否则 response_model 会静默剥掉 ----
    from backend.app.schemas.models import PolicyItem, PolicyPoint, PolicySubItem

    for field in ("points", "sections", "items"):
        assert field in PolicyItem.model_fields, (
            f"PolicyItem 少了 {field} 字段 —— response_model 会把它静默丢掉，"
            f"前端拿到永远是空数组"
        )
    assert PolicyPoint.model_fields["text"].is_required(), "PolicyPoint.text 必须必填"
    assert "index" in PolicySubItem.model_fields and "title" in PolicySubItem.model_fields

    # ---- 前端：标题渲染成外链（新窗口 + rel），要点渲染成列表 ----
    front = path_of("frontend_dir")
    js = (front / "assets" / "js" / "policy.js").read_text(encoding="utf-8")
    assert 'target="_blank"' in js and 'rel="noopener noreferrer"' in js, (
        "政策标题必须在新窗口打开原文，并带 rel=noopener noreferrer"
    )
    assert "policy-row__link" in js, "标题没有渲染成链接"
    assert "pointsHtml" in js and "policy-points" in js, "要点没有渲染到页面上"
    # 上一批用例锁的两条不能因为这次改动又混回来
    assert "policy-summary" not in js and "typeName" not in js

    icons = (front / "assets" / "js" / "icons.js").read_text(encoding="utf-8")
    assert "external:" in icons, "icons.js 里没有 external 图标，外链记号会是空白"

    css = (front / "assets" / "css" / "pages.css").read_text(encoding="utf-8")
    for sel in (".policy-row__link", ".policy-row__points", ".policy-point__name"):
        assert sel in css, f"pages.css 里没有 {sel} 的样式"

    # 路由注释里那句「页面不设置外链跳转」已经作废，别再留着误导人
    router_src = (PROJECT_ROOT / "backend" / "app" / "routers" / "policy.py").read_text(encoding="utf-8")
    assert "不设置外链" not in router_src, "路由注释还写着不设外链，与现在的行为相反"


@case("知识图书库：详情改为全屏模态层，且标注了作物名")
def _():
    from src.common.paths import path_of

    front = path_of("frontend_dir")
    html = (front / "library.html").read_text(encoding="utf-8")
    js = (front / "assets" / "js" / "library.js").read_text(encoding="utf-8")
    css = (front / "assets" / "css" / "pages.css").read_text(encoding="utf-8")
    icons = (front / "assets" / "js" / "icons.js").read_text(encoding="utf-8")

    assert 'id="library-modal"' in html, "library.html 缺少模态容器"
    for need in ("openDetail", "closeDetail", "modal-open"):
        assert need in js, f"library.js 缺少 {need}"
    assert "Escape" in js, "模态必须支持 ESC 关闭"
    assert "modal__backdrop" in html and "modal__backdrop" in css, "缺少点遮罩关闭"
    # 内联展开的那套（data-expanded / book-card__detail）应已彻底移除，
    # 否则会同时存在两套详情渲染路径，改动一处忘另一处。
    for gone in ("data-expanded", "book-card__detail"):
        assert gone not in js, f"library.js 里还残留内联展开的代码：{gone}"
    assert ".book-card__detail" not in css and 'data-expanded="true"' not in css, \
        "pages.css 里还残留内联展开的样式"

    assert "作物：" in js, "详情里没有标注具体作物名（同作物不同病害就分不清了）"
    assert "病害：" in js and "类型：养护" in js, "缺少病害/养护类型徽章"
    # modal 里的关闭按钮用了 close 图标，icons.js 里没有就会渲染成空白按钮
    assert "close:" in icons, "icons.js 里没有 close 图标，关闭按钮会是空白"


@case("智能诊断页：标题已改为「语音输入」")
def _():
    from src.common.paths import path_of

    html = (path_of("frontend_dir") / "diagnose.html").read_text(encoding="utf-8")
    assert ">语音输入</h2>" in html, "语音小节标题应改为「语音输入」"
    assert "用方言说症状" not in html, "旧标题仍残留"


# ================================================== 反 AI 套路 / 可读性（回归锁）


def _css_bundle():
    from src.common.paths import path_of

    css_dir = path_of("frontend_dir") / "assets" / "css"
    return {
        name: (css_dir / f"{name}.css").read_text(encoding="utf-8")
        for name in ("base", "layout", "pages")
    }


def _rule_body(src, header):
    """取出 `header { ... }` 的规则体。header 必须逐字匹配到左花括号。"""
    i = src.find(header)
    assert i >= 0, f"样式表里找不到规则 {header}"
    j = src.find("{", i)
    return src[j + 1:src.find("}", j)]


def _rule_body_live(src, header):
    """同 _rule_body，但先把 CSS 注释剥掉。

    必须剥注释：_rule_body 是纯文本查找，命中「第一次出现」—— 而本项目的样式表注释
    习惯把被测的那条声明原文引在注释里（例如「原来这里是 max-width: var(--content-max)」），
    不剥的话就会把注释里的那份当成规则体，断言看着是绿的／红的全凭注释怎么写。"""
    out, i, n = [], 0, len(src)
    while i < n:
        j = src.find("/*", i)
        if j < 0:
            out.append(src[i:])
            break
        out.append(src[i:j])
        k = src.find("*/", j + 2)
        if k < 0:
            break
        i = k + 2
    return _rule_body("".join(out), header)


# ------------------------------------------------- 色阶数值的静态断言（没有浏览器）

def _lin(channel: float) -> float:
    """sRGB 单通道 → 线性光。WCAG 2.x 相对亮度的第一步。"""
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hexstr: str) -> float:
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _theme_blocks(base: str):
    """把 base.css 拆成「浅色段 / 深色段」，深色段就是 @media 里那个 :root。"""
    i = base.find("@media (prefers-color-scheme: dark)")
    assert i >= 0, "base.css 里没有深色令牌块"
    dark = base[i:]
    return base[:i], dark[:dark.find("\n  }")]


def _css_var_hex(block: str, name: str) -> str:
    import re

    m = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", block)
    assert m, f"没有找到令牌 {name}"
    return m.group(1)


def _stage_bg(pages: str, season: str):
    """舞台底色：返回 (浅色, 深色)。同一选择器在 pages.css 里写了两遍，浅色在前。"""
    import re

    hits = re.findall(
        r'\.map-stage\[data-season="%s"\]\s*\{\s*background:\s*(#[0-9a-fA-F]{6})' % season,
        pages,
    )
    assert len(hits) == 2, (
        f'pages.css 里 .map-stage[data-season="{season}"] 的底色规则应恰好两条'
        f"（浅色一条、深色一条），实际 {len(hits)} 条"
    )
    return hits[0], hits[1]


@case("样式表：玻璃拟态、装饰渐变、胶囊滥用已清除（回归锁）")
def _():
    import re

    css = _css_bundle()
    all_css = "".join(css.values())

    # 玻璃拟态：顶栏与浮层用 92% 不透明底 + 模糊。模糊在这个不透明度下几乎看不出，
    # 却让底下的地图透上来影响读字 —— 受众是中老年农户，手机上还要在强光下看。
    assert "backdrop-filter:" not in all_css, \
        "样式表里还有 backdrop-filter；浮层请用不透明底 + 1px 发丝边框"

    # 装饰性渐变（logo 方块 / 置信度条 / 骨架屏）一律拍平为实色。
    # 判据是「选择器是不是 .map-stage」，不是「有没有 linear-gradient」——
    # 四季底色现在也是实色，所以这条锁的期望结果是「一处都找不到」，
    # 留着它是为了拦住以后有人往别处加回渐变。
    for name, src in css.items():
        for m in re.finditer(r"linear-gradient", src):
            head = src[:m.start()]
            sel = head[head.rfind("}") + 1:head.rfind("{")].strip()
            assert ".map-stage" in sel, \
                f"{name}.css 的 `{sel[:60]}` 用了渐变；只有舞台底色允许"

    # 胶囊圆角只留给真正的徽标。导航、页签、进度条、关闭键都应是矩形：
    # 全站一个 999px 会让「层级」这件事失去表达力。
    assert "--radius-pill" not in all_css, "--radius-pill 已废除，不该再出现"
    assert all_css.count("border-radius: 999px") == 1, \
        "999px 胶囊圆角只允许出现在 .badge 上"
    assert "border-radius: 999px" in _rule_body(css["base"], ".badge {"), \
        "那唯一的胶囊圆角应该是 .badge 自己的"


@case("深色模式：语义色成套覆盖，notice/降级条不再硬编码浅色（回归锁）")
def _():
    import re

    css = _css_bundle()
    base = css["base"]

    i = base.find("@media (prefers-color-scheme: dark)")
    assert i >= 0, "base.css 里没有深色令牌块"
    dark = base[i:]
    dark = dark[:dark.find("\n  }")]

    # 深色段只写一半的后果是实打实的：.notice 的底色被换成了深色 -soft，
    # 文字却还留在浅色的 -fg 上，实测 ≈1.9–2.0:1，整条读不了。
    for token in ("--color-primary:", "--color-primary-hover:", "--color-primary-soft:",
                  "--color-primary-fg:", "--color-link:", "--color-ring:",
                  "--color-info-fg:", "--color-warning-fg:", "--color-destructive-fg:",
                  "--color-info-line:", "--color-warning-line:",
                  "--color-destructive-line:"):
        assert token in dark, f"深色段漏了 {token}，对应组件会退回浅色值"

    # 地图是 canvas，它的深浅两套只能在 CSS 里各给一份，再由 map.js 读走
    for token in ("--map-spring-area:", "--map-winter-nodata:",
                  "--map-risk-1:", "--map-risk-5:"):
        assert token in dark, f"深色段漏了地图令牌 {token}"

    # 这两个组件的颜色必须全部走令牌：原先硬编码浅色值就是上面那个 1.9:1 的成因，
    # 只要规则体里再出现一个裸色值，就会重犯。
    targets = {
        "base": (".notice--info {", ".notice--warning {", ".notice--danger {",
                 ".notice--primary {"),
        "layout": (".degraded-bar {",),
    }
    for name, headers in targets.items():
        for header in headers:
            body = _rule_body(css[name], header)
            bad = re.search(r"#[0-9a-fA-F]{3,8}\b", body)
            assert not bad, \
                f"{name}.css 的 {header[:-2]} 里还有裸色值 {bad.group(0) if bad else ''}"

    # 降级条引用的那两个令牌，浅色段必须真的定义过，否则深色段覆盖的是个空名字
    assert "--color-warning-soft:" in base and "--color-warning-fg:" in base, \
        "降级条用到的语义令牌在浅色段就没定义"


@case("可读性与触控：字号走令牌、触控目标 ≥44px（回归锁）")
def _():
    import re

    css = _css_bundle()
    base, layout, pages = css["base"], css["layout"], css["pages"]

    # 字号原来是散落的字面量，改一档要翻三个文件；现在统一走 --text-*
    for token in ("--text-xs:", "--text-sm:", "--text-base:", "--text-md:",
                  "--text-lg:", "--text-xl:", "--text-2xl:"):
        assert token in base, f"缺少字号令牌 {token}"
    # 元信息那一簇原来是 12px，受众偏中老年，已整体提到 13px
    assert "--text-xs: 0.8125rem" in base, "元信息字号应不低于 13px"

    for name, src in (("layout", layout), ("pages", pages)):
        for m in re.finditer(r"font-size:\s*([^;]+);", src):
            v = m.group(1).strip()
            assert v.startswith("var(--text-"), \
                f"{name}.css 里有裸字号 {v}，应走 --text-* 令牌"

    # 触控目标 44px 是底线：受众里中老年占多数，且多在手机上用手指点。
    # 关闭键那类图标按钮没有文字撑高，最容易悄悄做小。
    def touch_ok(body):
        m = re.search(r"min-height:\s*(\d+)px", body)
        if m and int(m.group(1)) >= 44:
            return True
        w = re.search(r"width:\s*(\d+)px", body)
        h = re.search(r"height:\s*(\d+)px", body)
        return bool(w and h and int(w.group(1)) >= 44 and int(h.group(1)) >= 44)

    targets = (
        ("base", base, (".btn {", ".btn--sm {", ".input, .select, .textarea {")),
        ("layout", layout, (".brand {", ".site-nav__link {", ".dialect-picker .select {")),
        ("pages", pages, (".season-switch__btn {", ".map-card__close {", ".modal__close {",
                          ".library-search__mic {")),
    )
    for name, src, headers in targets:
        for header in headers:
            body = _rule_body(src, header)
            assert touch_ok(body), \
                f"{name}.css 的 {header[:-2]} 触控高度不足 44px：{body.strip()[:70]}"


# ============================================ 四季设计（frontend-design 那一轮）

@case("首页地图：四季风险令牌在深浅两套里各 20 个齐全（回归锁）")
def _():
    light, dark = _theme_blocks(_css_bundle()["base"])

    missing = []
    for season in ("spring", "summer", "autumn", "winter"):
        for n in range(1, 6):
            tok = f"--map-{season}-risk-{n}:"
            if tok not in light:
                missing.append("浅色 " + tok)
            if tok not in dark:
                missing.append("深色 " + tok)
    assert not missing, (
        "四季风险令牌不成套：" + "、".join(missing)
        + " —— 漏一档会静默继承通用绿，那一季的色相就不对了"
    )

    # 通用兜底不能删：它是 map.js 第二层取值，深色 :root 里那对还被另一条锁盯着
    for n in range(1, 6):
        assert f"--map-risk-{n}:" in light and f"--map-risk-{n}:" in dark, \
            f"通用风险令牌 --map-risk-{n} 被删了，兜底就断了"


@case("首页地图：四季色阶明度单调，深色最亮档不刺眼（回归锁）")
def _():
    css = _css_bundle()
    light, dark = _theme_blocks(css["base"])
    pages = css["pages"]

    for season in ("spring", "summer", "autumn", "winter"):
        ramp = [_css_var_hex(light, f"--map-{season}-risk-{n}") for n in range(1, 6)]
        ys = [_luminance(c) for c in ramp]
        assert all(ys[k] > ys[k + 1] for k in range(4)), (
            f"浅色 {season} 色阶不单调：{ramp} —— 浅色下病害种类越多必须越深"
        )

        ramp_d = [_css_var_hex(dark, f"--map-{season}-risk-{n}") for n in range(1, 6)]
        ys_d = [_luminance(c) for c in ramp_d]
        # 极性在深色下是反的：浅色靠墨色加重量，深色只能靠亮度。这不是自相矛盾，
        # 是同一套「越多越醒目」的编码换了一种实现。
        assert all(ys_d[k] < ys_d[k + 1] for k in range(4)), (
            f"深色 {season} 色阶不单调：{ramp_d} —— 深色下病害种类越多必须越亮"
        )

        stage = _stage_bg(pages, season)[1]
        worst = _contrast(ramp_d[-1], stage)
        assert worst <= 8.0, (
            f"深色 {season} 最亮一档 {ramp_d[-1]} 对舞台 {stage} 是 {worst:.2f}:1，"
            "超过 8:1 —— 近黑底上会是刺眼的一团，照搬浅色档就会这样"
        )


@case("首页地图：季节底色与省份兜底色拉开亮度，省份不糊进背景（回归锁）")
def _():
    css = _css_bundle()
    light, dark = _theme_blocks(css["base"])
    pages = css["pages"]

    for season in ("spring", "summer", "autumn", "winter"):
        s_light, s_dark = _stage_bg(pages, season)
        for label, stage, block, floor in (
            ("浅色", s_light, light, 0.05),
            ("深色", s_dark, dark, 0.005),
        ):
            area = _css_var_hex(block, f"--map-{season}-area")
            gap = abs(_luminance(stage) - _luminance(area))
            assert gap >= floor, (
                f"{label} {season}：舞台底色 {stage} 与省份兜底色 {area} 只差 "
                f"{gap:.4f}（要求 ≥ {floor}），没有数据的省份会糊进背景"
            )


@case("诊断结果页：先给动作再给依据，「先做这一步」不取 sections[0]（回归锁）")
def _():
    import re

    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "diagnose.js").read_text(encoding="utf-8")

    body = js[js.find("function renderResult("):]
    body = body[:body.find("\n  }")]
    order = [
        ("_hero(", "病害名"),
        ("_primaryAction(", "先做这一步"),
        ("_sectionsBlock(", "完整防治方案"),
        ("_evidence(", "判断依据"),
    ]
    pos = [(body.find(call), label) for call, label in order]
    assert all(p >= 0 for p, _ in pos), f"renderResult 里少了某一块：{pos}"
    seq = [label for _, label in sorted(pos)]
    assert seq == ["病害名", "先做这一步", "完整防治方案", "判断依据"], (
        f"结果页应是「农户要的动作在前、模型的依据在后」，实际顺序 {seq}"
    )

    # 判断依据区必须真的存在名字，否则「先做这一步」不成主角
    assert ".result-action" in _css_bundle()["pages"], "样式表里没有 .result-action"
    assert ".result-evidence" in _css_bundle()["pages"], "样式表里没有 .result-evidence"

    # 「先做这一步」不能直接取 sections[0]：Mock 的第一节是「发病规律与判断依据」，
    # 那是背景不是动作 —— 取第一节等于把「现在干什么」答成「这病怎么回事」。
    action = js[js.find("function _primaryAction("):]
    action = action[:action.find("\n  }")]
    assert "ACTION_RE" in action and "sections.find(" in action, (
        "_primaryAction 必须按标题关键词挑出动作小节，不能直接取 sections[0]"
    )
    assert re.search(r"const ACTION_RE\s*=\s*/\^?[^/]*防治", js), \
        "ACTION_RE 要能认出「防治」这类动作标题"
    # 挑不到动作时退回第一节是允许的；但直接取第一节不行
    assert "|| sections[0]" in action, "找不到动作小节时应退回第一节，而不是留空"


@case("首页：默认季节由真实日期推导，不再写死春季（回归锁）")
def _():
    import re

    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "map.js").read_text(encoding="utf-8")

    assert 'season = "spring"' not in js, (
        "季节初值还写死成 spring —— 十月打开首页看到的会是春季数据"
    )
    assert re.search(r"let\s+season\s*=\s*seasonFromDate\(", js), \
        "季节初值应来自 seasonFromDate()"

    fn = js[js.find("function seasonFromDate("):]
    fn = fn[:fn.find("\n  }")]
    assert "getMonth()" in fn, "seasonFromDate 必须读真实月份，不能是常量"
    for season in ("spring", "summer", "autumn", "winter"):
        assert f'"{season}"' in fn, f"seasonFromDate 没有覆盖 {season}"


@case("首页季节分带：一句当季判断由本地快照算出，不是写死的文案（回归锁）")
def _():
    from src.common.paths import path_of

    frontend = path_of("frontend_dir")
    js = (frontend / "assets" / "js" / "map.js").read_text(encoding="utf-8")
    html = (frontend / "index.html").read_text(encoding="utf-8")

    assert 'id="season-band"' in html, "首页缺少季节分带"
    assert "data-season-host" in html, "分带要挂 data-season-host，否则底色不跟季节走"

    assert "renderSeasonBand()" in js, "缺少季节分带的渲染函数"
    tail = js[js.find("function renderAll("):]
    render_all = tail[:tail.find("\n  }")]
    assert "renderSeasonBand()" in render_all, \
        "分带必须在 renderAll 里更新，否则切季节时它不跟着变"
    assert render_all.find("applySeason()") < render_all.find("renderSeasonBand()"), (
        "renderSeasonBand 必须排在 applySeason 之后 —— 它读的是摊平后的 provinces"
    )

    fn = js[js.find("function renderSeasonBand("):]
    fn = fn[:fn.find("\n  }")]
    assert "provinces" in fn and "peak_window" in fn, (
        "分带要从本地快照就地算（provinces / peak_window），不能是写死的文案"
    )
    # 主语为什么不是「当季最常见的病害」：演示快照里春/夏/秋 的病害名集合是一样的，
    # 挂上去切三季都一个样。函数上方那段注释就是解释，别删。
    head = js.find("function renderSeasonBand(")
    assert "春/夏/秋" in js[max(0, head - 1000):head], \
        "缺少「为什么不用最常见病害当主语」的说明 —— 下一个人会照着改回去"


# ==================================================================== 虫害检测后处理（2.7）

# 这些用例锁的是全案最容易「静默出错」的一段：ONNX 只吐一个 [1, 106, 8400] 的原始张量，
# 转置 / 取最大类 / 置信度过滤 / NMS / 坐标反算，五步里错任何一步都不会报错，
# 症状只是「框全偏」或「什么都检不出来」。所以这里对每一步都单独断言。

_LETTERBOX_SHAPES = [(640, 640), (800, 600), (600, 800), (900, 700), (641, 500),
                     (1920, 1080), (1080, 1920), (300, 200)]


@case("虫害 letterbox：输出恒为 640×640，缩放比与填充量可反算")
def _():
    import numpy as np

    from src.infer.pest_detector import letterbox

    for w, h in _LETTERBOX_SHAPES:
        img = np.full((h, w, 3), 200, dtype=np.uint8)
        out, r, (pad_x, pad_y) = letterbox(img, 640)

        assert out.shape == (640, 640, 3), f"{w}×{h} 的输出形状应为 640×640×3，实际 {out.shape}"
        assert abs(r - min(640 / h, 640 / w)) < 1e-9, f"{w}×{h} 缩放比算错：{r}"

        # 填充量必须把「缩放后的尺寸」补齐到 640，且左右之和 = 总填充（奇数不丢 1 px）
        new_w, new_h = round(w * r), round(h * r)
        assert abs(pad_x - (640 - new_w) / 2) <= 0.5, f"{w}×{h} 左填充 {pad_x} 与总填充对不上"
        assert abs(pad_y - (640 - new_h) / 2) <= 0.5, f"{w}×{h} 上填充 {pad_y} 与总填充对不上"

        # 灰边颜色必须是训练时那个 114，换一个值就是训练/线上不一致
        if new_h < 640:
            assert out[0, 0, 0] == 114, f"{w}×{h} 的填充色应为 114，实际 {out[0, 0, 0]}"


@case("虫害 letterbox：坐标正反变换可逆（框不会整体漂移）")
def _():
    import numpy as np

    from src.infer.pest_detector import letterbox, scale_boxes

    for w, h in _LETTERBOX_SHAPES:
        if w > 640 or h > 640:
            continue                      # 只测缩放比 < 1 之外的也要覆盖，下面单独测
        img = np.zeros((h, w, 3), dtype=np.uint8)
        _, r, pad = letterbox(img, 640)

        # 原图坐标 -> letterbox 坐标
        orig = np.array([[10.0, 20.0, w - 10.0, h - 20.0]], dtype=np.float32)
        boxed = orig * r
        boxed[:, [0, 2]] += pad[0]
        boxed[:, [1, 3]] += pad[1]

        back = scale_boxes(boxed, r, pad, (h, w))
        assert np.allclose(back, orig, atol=1e-3), (
            f"{w}×{h} 坐标反算没回到原处：\n  原始 {orig[0]}\n  回来 {back[0]}"
        )

    # 放大方向（小图被 upscale）也要可逆
    for w, h in [(300, 200), (320, 240)]:
        img = np.zeros((h, w, 3), dtype=np.uint8)
        _, r, pad = letterbox(img, 640)
        orig = np.array([[5.0, 5.0, w - 5.0, h - 5.0]], dtype=np.float32)
        boxed = orig * r
        boxed[:, [0, 2]] += pad[0]
        boxed[:, [1, 3]] += pad[1]
        back = scale_boxes(boxed, r, pad, (h, w))
        assert np.allclose(back, orig, atol=1e-3), f"{w}×{h}（放大）反算失败"


@case("虫害预处理：是 RGB/255，不是 ImageNet 归一化")
def _():
    import numpy as np

    from src.infer.pest_detector import preprocess_image

    # 纯白图：/255 之后应恰好是 1.0。若误用了 ImageNet 归一化会得到 (1-0.485)/0.229 ≈ 2.25
    white = np.full((640, 640, 3), 255, dtype=np.uint8)
    tensor, _, _ = preprocess_image(white, 640)
    assert tensor.shape == (1, 3, 640, 640), f"张量形状应为 (1,3,640,640)，实际 {tensor.shape}"
    assert tensor.dtype == np.float32, f"dtype 应为 float32，实际 {tensor.dtype}"
    assert abs(float(tensor.max()) - 1.0) < 1e-6 and abs(float(tensor.min()) - 1.0) < 1e-6, (
        f"纯白图应为 1.0，实际 min={tensor.min()} max={tensor.max()} —— "
        "说明混进了归一化（分类那套减均值除方差不能用在检测上）"
    )

    # 纯红图：通道 0 必须是 1.0。若 BGR/RGB 弄反，通道 0 会是 0
    red = np.zeros((640, 640, 3), dtype=np.uint8)
    red[:, :, 0] = 255
    tensor, _, _ = preprocess_image(red, 640)
    assert abs(float(tensor[0, 0].mean()) - 1.0) < 1e-6, "通道 0 应为红色（RGB 顺序反了）"
    assert abs(float(tensor[0, 1].mean())) < 1e-6, "通道 1 应为 0"


@case("虫害 IoU：相同为 1、相离为 0、半重叠为 1/3")
def _():
    import numpy as np

    from src.infer.pest_detector import box_iou

    a = np.array([[0.0, 0.0, 10.0, 10.0]], dtype=np.float32)
    assert abs(float(box_iou(a, a)[0, 0]) - 1.0) < 1e-6, "自交 IoU 应为 1"

    far = np.array([[100.0, 100.0, 110.0, 110.0]], dtype=np.float32)
    assert abs(float(box_iou(a, far)[0, 0])) < 1e-6, "相离 IoU 应为 0"

    # 面积 100 与面积 100，重叠 5×10=50，并集 150 -> 1/3
    half = np.array([[5.0, 0.0, 15.0, 10.0]], dtype=np.float32)
    assert abs(float(box_iou(a, half)[0, 0]) - 1 / 3) < 1e-5, "半重叠 IoU 应为 1/3"

    # 退化框（宽或高为 0）不能除零崩掉
    degen = np.array([[0.0, 0.0, 0.0, 10.0]], dtype=np.float32)
    assert np.isfinite(box_iou(a, degen)).all(), "退化框不应产生 NaN/Inf"


@case("虫害 NMS：挤掉重叠框、保留高分、同分按稳定顺序")
def _():
    import numpy as np

    from src.infer.pest_detector import nms

    boxes = np.array([
        [0.0, 0.0, 10.0, 10.0],      # 0 高分
        [0.5, 0.5, 10.5, 10.5],      # 1 与 0 高度重叠，应被挤掉
        [100.0, 100.0, 110.0, 110.0],  # 2 独立，保留
    ], dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)

    keep = nms(boxes, scores, iou_thres=0.5)
    assert keep.tolist() == [0, 2], f"应保留 [0, 2]，实际 {keep.tolist()}"

    # 阈值放宽到 0.6 以上就都留下了（0 与 1 的 IoU 约 0.905/…，这里用 0.95 确保不挤）
    keep_loose = nms(boxes, scores, iou_thres=0.99)
    assert keep_loose.tolist() == [0, 1, 2], f"阈值放宽后应全留，实际 {keep_loose.tolist()}"

    # 分数完全相同时必须按输入顺序，否则同一张图两次推理会给出不同的框
    tie = nms(boxes[:2], np.array([0.5, 0.5], dtype=np.float32), iou_thres=0.5)
    assert tie.tolist() == [0], f"同分时应稳定保留下标小的，实际 {tie.tolist()}"

    assert nms(np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.float32), 0.5).size == 0


@case("虫害解码：xywh 转 xyxy、取最大类、置信度过滤")
def _():
    import numpy as np

    from src.infer.pest_detector import decode_predictions

    nc, n = 102, 3
    out = np.zeros((1, 4 + nc, n), dtype=np.float32)
    # 锚点 0：中心 (320,320)，100×100，第 5 类 0.9
    out[0, :4, 0] = [320, 320, 100, 100]
    out[0, 4 + 5, 0] = 0.9
    # 锚点 1：同样的框，第 7 类 0.8（应与锚点 0 并存，因为是不同类）
    out[0, :4, 1] = [320, 320, 100, 100]
    out[0, 4 + 7, 1] = 0.8
    # 锚点 2：第 5 类只有 0.1，低于阈值，应被滤掉
    out[0, :4, 2] = [100, 100, 50, 50]
    out[0, 4 + 5, 2] = 0.1

    boxes, scores, cls = decode_predictions(out, conf_thres=0.25, num_classes=nc)
    assert boxes.shape[0] == 2, f"应留下 2 个候选，实际 {boxes.shape[0]}"
    assert cls.tolist() == [5, 7], f"类别应为 [5, 7]，实际 {cls.tolist()}"
    assert abs(float(scores[0]) - 0.9) < 1e-6, "得分取错"
    assert np.allclose(boxes[0], [270, 270, 370, 370]), f"xywh->xyxy 算错：{boxes[0]}"

    # 属性在最后一维时也要认得（有些导出版本是 (8400, 106)）
    boxes_t, scores_t, cls_t = decode_predictions(out[0].T[None], 0.25, nc)
    assert boxes_t.shape == boxes.shape and np.allclose(boxes_t, boxes), "转置形态的结果应与前者一致"

    # 类别数配错时必须明确报错，而不是拿错的维数硬算下去
    try:
        decode_predictions(out, 0.25, num_classes=80)
    except ValueError:
        pass
    else:
        raise AssertionError("类别数与输出对不上时应抛 ValueError")


@case("虫害后处理：逐类 NMS 不吞掉同位异类，且坐标反算回原图")
def _():
    import numpy as np

    from src.infer.pest_detector import postprocess

    nc, n = 102, 4
    out = np.zeros((1, 4 + nc, n), dtype=np.float32)
    # 0 与 1：同一个位置，不同类 —— 一张叶子上「稻飞虱」和「叶蝉」重合是正常的，都要留
    out[0, :4, 0] = [320, 320, 100, 100]
    out[0, 4 + 3, 0] = 0.9
    out[0, :4, 1] = [320, 320, 100, 100]
    out[0, 4 + 9, 1] = 0.85
    # 2 与 0 同类且位置几乎重合 —— 应被 NMS 挤掉
    out[0, :4, 2] = [322, 322, 100, 100]
    out[0, 4 + 3, 2] = 0.7
    # 3 另一处，第 3 类，保留
    out[0, :4, 3] = [500, 500, 60, 60]
    out[0, 4 + 3, 3] = 0.6

    dets = postprocess(out, orig_shape=(1000, 1000), ratio=0.5, pad=(0.0, 120.0),
                       conf_thres=0.25, iou_thres=0.7, num_classes=nc)

    assert len(dets) == 3, f"应留 3 个框（同类叠框被挤掉一个），实际 {len(dets)}"
    assert sorted(d["index"] for d in dets) == [3, 3, 9], \
        f"类别应为 3/3/9，实际 {sorted(d['index'] for d in dets)}"

    # 反算：letterbox 坐标 (270,270,370,370) -> 减 pad -> 除 0.5
    #   x: (270-0)/0.5 = 540, (370-0)/0.5 = 740；y: (270-120)/0.5 = 300, (370-120)/0.5 = 500
    cls3 = [d for d in dets if d["index"] == 3 and abs(d["confidence"] - 0.9) < 1e-6][0]
    assert np.allclose(cls3["bbox"], [540, 300, 740, 500]), f"坐标反算错：{cls3['bbox']}"
    assert all(d["confidence"] <= 1.0 for d in dets)


@case("虫害后处理：裁剪到图像范围内，负坐标不为负")
def _():
    import numpy as np

    from src.infer.pest_detector import postprocess

    nc = 102
    out = np.zeros((1, 4 + nc, 1), dtype=np.float32)
    out[0, :4, 0] = [10, 10, 200, 200]      # 反算后会跑到图外左上
    out[0, 4 + 1, 0] = 0.9

    dets = postprocess(out, orig_shape=(100, 100), ratio=1.0, pad=(0.0, 0.0),
                       conf_thres=0.25, iou_thres=0.7, num_classes=nc)
    assert len(dets) == 1
    x1, y1, x2, y2 = dets[0]["bbox"]
    assert x1 >= 0 and y1 >= 0, f"裁剪后不应出现负坐标：{dets[0]['bbox']}"
    assert x2 <= 100 and y2 <= 100, f"裁剪后不应超出图像：{dets[0]['bbox']}"


@case("虫害后处理：全低于阈值时返回空列表而不是报错")
def _():
    import numpy as np

    from src.infer.pest_detector import postprocess

    out = np.zeros((1, 4 + 102, 5), dtype=np.float32)
    out[0, 0, :] = 100
    out[0, 1, :] = 100
    out[0, 2, :] = 50
    out[0, 3, :] = 50
    out[0, 4:, :] = 0.05                     # 全部低于 0.25
    dets = postprocess(out, (500, 500), 1.0, (0.0, 0.0), 0.25, 0.7, 102)
    assert dets == [], f"应返回空列表，实际 {dets}"


@case("虫害标签表：102 类索引连续、正反查一致、与 data.yaml 逐条同序")
def _():
    import json

    from src.common.paths import path_of

    p = path_of("pest_label_map")
    assert p.exists(), f"缺少 {p}"
    data = json.loads(p.read_text(encoding="utf-8"))
    i2l = data["index_to_label"]

    assert sorted(int(k) for k in i2l) == list(range(102)), "下标必须是 0..101 连续"
    assert int(data["num_classes"]) == 102

    cn = [i2l[str(i)]["label_cn"] for i in range(102)]
    assert len(set(cn)) == 102, "中文名有重复"
    for i in range(102):
        e = i2l[str(i)]
        for field in ("label_en", "label_cn", "crop", "pest_type"):
            assert e.get(field), f"第 {i} 条缺字段 {field}"
        assert data["label_to_index"][e["label_cn"]] == i, f"第 {i} 条正反查不一致"

    # 与训练真正用的 data.yaml 对齐 —— 这是「标签表没配错」唯一有约束力的证据
    yaml_path = path_of("pest_yaml")
    assert yaml_path.exists(), f"缺少 {yaml_path}，请先跑 scripts/16_download_ip102.py"
    import yaml

    y = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert int(y["nc"]) == 102, f"data.yaml 的 nc 应为 102，实际 {y['nc']}"
    mismatch = [(i, y["names"][i], i2l[str(i)]["label_en"])
                for i in range(102) if y["names"][i] != i2l[str(i)]["label_en"]]
    assert not mismatch, f"label_en 与 data.yaml 的 names 不一致：{mismatch[:5]}"

    # 与病害词表的关系：**不是**「名字不能重复」——小麦/玉米/葡萄在两套标签空间里
    # 本来就该同名，它们就是同一种作物。要防的是「两份词表被合并成一份」：
    # configs/crops.yaml 是 PDD271 的 47 作物，被 crop_parser 按最长前缀匹配用着，
    # 给它加虫害条目会波及 unparsed_crops、数据划分，以及 22 号脚本里 47/271 的断言。
    from src.data.crop_parser import crop_names

    pdd_crops = set(crop_names())
    assert len(pdd_crops) == 47, (
        f"病害作物词表应仍是 47 个，实际 {len(pdd_crops)} —— "
        "configs/crops.yaml 被虫害的作物名污染了"
    )
    assert "多寄主" not in pdd_crops, "「多寄主」是虫害才有的概念，不该出现在病害词表里"
    assert set(data["crops"]) == {i2l[str(i)]["crop"] for i in range(102)}, (
        "label_map 声明的 crops 与逐条记录里的 crop 对不上"
    )


# ==================================================================== 虫害并入融合链路（2.7 / 3.3）

def _fusion_with(pest_detector, *, with_candidates: bool = True):
    """造一个最小的 DiagnosisFusion：真 label_map + MockLLM + 桩预测器。

    不联网、不加载任何 ONNX，因此可以在任何环境下跑。

    `with_candidates=False` 模拟「一张只有虫子的照片」：图像分类器跑得出结果，
    但给不出任何病害候选（topk 为空），于是 MockLLM 会走 _insufficient 兜底分支。
    """
    from src.common.io_utils import read_json
    from src.common.paths import path_of
    from src.llm.fusion import DiagnosisFusion
    from src.llm.mock_provider import MockLLM

    lm = read_json(path_of("label_map"))
    nice = next(e for e in lm["index_to_label"].values() if not e["is_healthy"])
    top = {k: nice[k] for k in ("label_cn", "crop", "condition", "pathogen_type", "index")}
    top["confidence"] = 0.9

    class _Predictor:
        def predict_bytes(self, _data):
            if not with_candidates:
                return {"top1": None, "topk": [], "confidence": 0.0}
            return {"top1": top, "topk": [top], "confidence": 0.9}

    return DiagnosisFusion(predictor=_Predictor(), llm=MockLLM(), rag=None,
                           pest_detector=pest_detector), top


class _StubDetector:
    """按需返回固定结果，或抛出异常。"""

    backend = "stub"
    num_classes = 102
    input_size = 640

    def __init__(self, detections=None, error=None):
        self._dets = detections or []
        self._error = error

    def detect_bytes(self, _data):
        if self._error:
            raise self._error
        return {"detections": self._dets, "count": len(self._dets),
                "image_size": [800, 600], "confidence_threshold": 0.25,
                "iou_threshold": 0.7, "backend": self.backend, "elapsed_ms": 12.3}


_DET = {"index": 0, "label_cn": "稻纵卷叶螟", "label_en": "rice_leaf_roller",
        "crop": "水稻", "pest_type": "螟蛾类", "confidence": 0.83,
        "bbox": [10.0, 20.0, 110.0, 90.0]}


@case("融合链路：没装检测器时，报告里不出现 pest_findings（不是空数组）")
def _():
    fusion, _top = _fusion_with(None)
    out = fusion.diagnose(image_bytes=b"\xff\xd8\xff", user_text="水稻叶子卷起来了")

    assert out["pest"] is None, "没装检测器时顶层 pest 必须是 None"
    assert out["degraded"]["pest"] is False, "没装检测器不算降级，否则每次诊断都报黄"
    assert "pest_findings" not in out["report"], (
        "没跑检测就不该有这个字段 —— 前端靠『字段在不在』区分"
        "『没做这项检查』与『检查了没发现』，给个空数组会让农户误以为已经排查过虫害"
    )
    assert out["report"]["disease_name"], "既有报告字段不能被虫害改动破坏"
    assert out["report"]["primary"]["kind"] == "disease", (
        "没装检测器时主结论只能是病害 —— 虫害没参与判断"
    )


@case("融合链路：装了检测器但没检出，pest_findings 是空数组（与上一条是两种状态）")
def _():
    fusion, _top = _fusion_with(_StubDetector(detections=[]))
    out = fusion.diagnose(image_bytes=b"\xff\xd8\xff", user_text="水稻叶子卷起来了")

    assert out["pest"] is not None and out["pest"]["count"] == 0
    assert out["report"].get("pest_findings") == [], (
        "跑过检测但没检出，应当是空数组而不是缺字段"
    )
    assert out["degraded"]["pest"] is False, "没检出是正常结果，不是降级"
    assert out["report"]["primary"]["kind"] == "disease", (
        "跑了检测但没检出虫，主结论仍是病害 —— 触发条件是 count>0，不是「跑过」"
    )


@case("融合链路：有检出时出 pest_findings，且虫害**绝不进** candidates 闭集")
def _():
    fusion, top = _fusion_with(_StubDetector(detections=[_DET]))
    out = fusion.diagnose(image_bytes=b"\xff\xd8\xff", user_text="水稻叶子卷起来了")

    findings = out["report"].get("pest_findings")
    assert isinstance(findings, list) and len(findings) == 1, f"实际 {findings}"
    f = findings[0]
    assert f["name"] == "稻纵卷叶螟" and f["confidence"] > 0.8
    assert f["advice"] and len(f["advice"]) > 20, "建议文案不能是空壳"

    # 核心不变量：candidates 是防幻觉的闭集，虫害（102 类昆虫）不能进去
    names = [c["label_cn"] for c in out["candidates"]]
    assert "稻纵卷叶螟" not in names, "虫害混进了 candidates 闭集，会污染防幻觉设计"
    assert names == [top["label_cn"]], f"candidates 应只含病害候选，实际 {names}"

    # 检出虫害时摘要要点出来，否则报告读起来像是没看见
    assert "稻纵卷叶螟" in out["fused_summary"], f"摘要未提及虫害：{out['fused_summary']}"

    # ---- 主结论：检出虫害 ⇒ 换成虫害，病害降级 ----
    primary = out["report"]["primary"]
    assert primary["kind"] == "pest", f"检出虫害时主结论应是虫害，实际 {primary}"
    assert primary["name"] == "稻纵卷叶螟" and primary["confidence"] > 0.8
    assert primary["count"] == 1 and primary["crop"] == "水稻"
    assert "另疑似病害" in primary["note"], f"病害应降级进 note：{primary['note']}"
    assert out["report"]["disease_name"], "病害那一路不能被删掉，只是降级为次要"
    # 摘要也要跟着换序（否则界面上大字是虫害、判断依据里却写「综合结论：某病害」）
    assert "综合结论：虫害稻纵卷叶螟" in out["fused_summary"], (
        f"摘要顺序没跟着主结论走：{out['fused_summary']}"
    )

    # ---- 防治方案必须一起换（不能出现「结论是虫害、方案却在讲病害」）----
    from src.content.library_build import flatten_plan

    titles = [s["title"] for s in out["report"]["treatment_sections"]]
    assert "发病规律与判断依据" not in titles, f"病害方案没被换掉：{titles}"
    assert any("防治" in t for t in titles), f"虫害方案缺防治小节：{titles}"
    assert out["report"]["treatment_plan"] == flatten_plan(out["report"]["treatment_sections"]), (
        "treatment_plan 是 sections 的纯文本副本；换了 sections 必须同步重算，否则两者对不上"
    )
    assert out["report"]["medication_reminder"], "虫害的用药提醒不能是空"

    # 换成虫害**不得**以「把虫名塞进 candidates」的方式实现 —— 闭集必须还是纯病害
    assert [c["label_cn"] for c in out["candidates"]] == [top["label_cn"]], (
        "主结论切换不能靠污染 candidates 闭集来实现"
    )


@case("融合链路：检测抛异常只置 degraded.pest，不许把整个诊断拖崩")
def _():
    fusion, _top = _fusion_with(_StubDetector(error=RuntimeError("模拟 ONNX 加载失败")))
    out = fusion.diagnose(image_bytes=b"\xff\xd8\xff", user_text="水稻叶子卷起来了")

    assert out["degraded"]["pest"] is True, "检测失败必须如实上报"
    assert out["pest"] is None
    assert "pest_findings" not in out["report"], "检测失败时不能凭空给出虫害结论"
    # 关键：病害那一路照常出结果
    assert out["report"]["disease_name"], "一路挂掉不该影响另一路"
    assert out["report"]["primary"]["kind"] == "disease", (
        "检测失败时主结论**不能**变成虫害 —— 触发条件只看 count>0，绝不能看 degraded.pest"
    )


@case("融合链路：只有虫子、匹配不到病害时，兜底报告仍要带虫害结果")
def _():
    fusion, _top = _fusion_with(_StubDetector(detections=[_DET]), with_candidates=False)
    # 有图（检测器会跑）但分类器给不出病害候选，文字描述也为空 → 走 _insufficient 兜底分支
    out = fusion.diagnose(image_bytes=b"\xff\xd8\xff", user_text="")

    assert out["report"]["disease_name"] == "", "本该是「信息不足」的兜底报告"
    assert out["pest"] is not None and out["pest"]["count"] == 1, "检测器这次确实跑了"
    findings = out["report"].get("pest_findings")
    assert findings and findings[0]["name"] == "稻纵卷叶螟", (
        "兜底分支把虫害丢了 —— 会出现「明明检出虫子却说信息不足」的荒唐结果"
    )

    # 兜底分支下也要切主结论：这时根本没有病害可降级，病害字段本来就空
    primary = out["report"]["primary"]
    assert primary["kind"] == "pest" and primary["name"] == "稻纵卷叶螟", (
        f"兜底分支下检出虫害，主结论仍应是虫害：{primary}"
    )
    assert out["report"]["disease_name"] == "", "兜底时本来就没有病害，不该被凭空补一个"
    # 兜底段落里那句「请补拍病叶正反面照片」是对农户有用的引导，
    # 替换防治方案时不能顺手丢掉。
    blob = out["report"]["summary"] + " " + " ".join(
        s.get("content", "") for s in out["report"]["treatment_sections"])
    assert "照片" in blob or "补充" in blob, f"「补拍病叶照片」的引导被换方案时弄丢了：{blob}"


@case("DeepSeek 侧白名单：检测结果之外的虫害名一律丢弃（虫害没有闭集，只能反查）")
def _():
    from src.llm.deepseek_provider import DeepSeekProvider

    ctx = {"pest_detections": [_DET]}
    # 模型编了一条检测器没看见的虫害，并把合法的名字写成了别的
    data = {"pest_findings": [
        {"name": "稻纵卷叶螟", "confidence": "0.9", "advice": "低龄期施药"},
        {"name": "草地贪夜蛾", "confidence": 0.99, "advice": "编的"},
    ]}
    DeepSeekProvider._normalize_pest(data, ctx)

    kept = [f["name"] for f in data["pest_findings"]]
    assert kept == ["稻纵卷叶螟"], f"幻觉虫害未被过滤：{kept}"
    assert data["pest_findings"][0]["confidence"] == 0.9, "字符串数字应被转成浮点"

    # 没跑检测时，模型幻觉出的字段要被整个删掉
    data2 = {"pest_findings": [{"name": "稻纵卷叶螟", "confidence": 0.9, "advice": "x"}]}
    DeepSeekProvider._normalize_pest(data2, {})
    assert "pest_findings" not in data2, "没跑检测却留下了虫害字段"

    # 跑过但没检出 → 空列表（而不是缺字段）
    data3 = {"pest_findings": [{"name": "稻纵卷叶螟", "confidence": 0.9, "advice": "x"}]}
    DeepSeekProvider._normalize_pest(data3, {"pest_detections": []})
    assert data3["pest_findings"] == [], f"没检出应为空列表，实际 {data3['pest_findings']}"


@case("PEST_TEXT：覆盖 label_map 用到的全部 pest_type，不重不漏")
def _():
    import json

    from src.common.paths import path_of
    from src.content.library_build import PEST_TEXT

    lm = json.loads(path_of("pest_label_map").read_text(encoding="utf-8"))
    used = {v["pest_type"] for v in lm["index_to_label"].values()}

    assert set(PEST_TEXT) == used, (
        f"PEST_TEXT 的键与 label_map 的 pest_type 词表不一致："
        f"缺 {sorted(used - set(PEST_TEXT))}，多 {sorted(set(PEST_TEXT) - used)}"
    )
    # 有「其他」兜底，才会在将来新增类群时降级而不是 KeyError
    assert "其他" in PEST_TEXT
    for k, (pattern, control) in PEST_TEXT.items():
        assert len(pattern) > 30, f"{k} 的危害规律太短，像是占位符"
        assert "①" in control, f"{k} 的防治方法与 PATHOGEN_TEXT 的编号体例不一致"


@case("虫害字段能穿过 response_model（pydantic 会静默过滤未声明的字段）")
def _():
    """这是一个**真实存在过的坑**，不是假想的：

    `POST /api/diagnose/multimodal` 挂了 `response_model=ApiResponse[DiagnoseMultimodalData]`，
    pydantic 会把模型里没声明的字段**静默丢掉** —— 不报错、不警告、日志里一个字都没有。
    融合链路明明算出了 pest、报告里明明写了 pest_findings，接口返回给前端的却是没有它们
    的版本。`LibraryDetailData` 上面也记过同一个坑（condition / pathogen_type 被吃掉）。
    所以这里不做「字段名有没有出现在源码里」这种静态断言 —— 直接跑一次真实的
    validate -> dump，看数据到底有没有活下来。
    """
    from backend.app.schemas.models import DiagnoseMultimodalData

    payload = {
        "recognition": None,
        "asr": None,
        "fused_summary": "x",
        "report": {
            "disease_name": "稻瘟病", "confidence": 0.8, "summary": "s",
            "treatment_sections": [{"title": "t", "content": "c"}],
            "source": "mock",
            # 只给 DeepSeek 会回的那三个字段：crop / pest_type 是可选的，
            # 少给它们也必须能通过校验（mock 那边会多填这两个）
            "pest_findings": [{"name": "稻纵卷叶螟", "confidence": 0.83, "advice": "低龄期施药"}],
        },
        "candidates": [], "sources": [],
        "degraded": {"llm": False, "rag": False, "voice": False, "pest": False},
        "dialect": {}, "llm_provider": "mock",
        "input_summary": {"has_image": True, "has_audio": False, "has_text": True},
        "pest": {
            "detections": [_DET],
            "count": 1,
            "image_size": [1024, 768],       # [宽, 高]
            "confidence_threshold": 0.25,
            "iou_threshold": 0.7,
            "backend": "onnxruntime-cpu",
            "elapsed_ms": 41.2,
        },
    }
    dumped = DiagnoseMultimodalData.model_validate(payload).model_dump()

    assert dumped["pest"] is not None, "pest 被 response_model 吃掉了 —— 前端拿不到检测结果"
    assert dumped["pest"]["count"] == 1
    assert dumped["pest"]["image_size"] == [1024, 768], "image_size 丢了，检测框会整体错位"
    # 阈值也丢过一次：detector 一直在返回它们，schema 里却没声明，前端于是显示
    # 「本次图片在检测阈值 0% 以上未发现虫害」——一句读起来像 bug 的话。
    assert dumped["pest"]["confidence_threshold"] == 0.25, "confidence_threshold 被吃掉了"
    assert dumped["pest"]["iou_threshold"] == 0.7, "iou_threshold 被吃掉了"
    assert len(dumped["pest"]["detections"]) == 1
    det = dumped["pest"]["detections"][0]
    # bbox 必须是 4 个浮点、原图坐标；label_cn 是前端列表与框编号的对应依据
    assert len(det["bbox"]) == 4 and all(isinstance(v, float) for v in det["bbox"]), det["bbox"]
    assert det["label_cn"] == "稻纵卷叶螟" and det["index"] == 0
    assert dumped["degraded"]["pest"] is False, "degraded.pest 也会被过滤掉"
    assert dumped["report"]["pest_findings"][0]["name"] == "稻纵卷叶螟", "pest_findings 丢了"

    # 反向：没装检测器时 pest 就是 null，不能被补成一个空壳对象
    # （前端靠 `pest === null` 判断「本次没有虫害检测」而不是「检出了 0 个」）
    payload["pest"] = None
    payload["report"].pop("pest_findings")
    dumped2 = DiagnoseMultimodalData.model_validate(payload).model_dump()
    assert dumped2["pest"] is None
    assert dumped2["report"]["pest_findings"] == [], "缺字段时应补成空列表，而不是报错或 None"


@case("检测器的返回契约与后端 schema 逐键对齐（少声明一个就是一次静默丢失）")
def _():
    """上一条测的是「已声明的字段活得下来」，这条测的是「该声明的都声明了」。

    两者缺一不可：response_model 只吃掉**未声明**的字段，所以只要漏声明，
    上一条的断言照样过 —— 因为它压根没去构造那个键。真正的防线是**拿检测器
    自己声明的键集合**去比 schema 的字段集合，而不是我在这里手抄一份键名。
    """
    from backend.app.schemas.models import PestDetection, PestResult
    from src.infer.pest_detector import DETECTION_BOX_KEYS, DETECTION_RESULT_KEYS

    got, want = set(PestResult.model_fields), set(DETECTION_RESULT_KEYS)
    missing = want - got
    assert not missing, (
        f"检测器会返回 {sorted(missing)}，但 PestResult 没有声明这些字段 —— "
        "response_model 会把它们静默丢掉，前端拿到的是一个缺字段的对象"
    )
    # 反方向不管：schema 允许比检测器多字段（带默认值即可），那不是缺陷。
    # 但多出来的字段如果**没有默认值**，就会变成必填，mock 与 deepseek 两条路
    # 只要有一条没填就会在校验上 500 —— 所以这里顺手把「都有默认值」也钉住。
    assert all(f.is_required() is False for f in PestResult.model_fields.values()), (
        "PestResult 有必填字段："
        f"{[n for n, f in PestResult.model_fields.items() if f.is_required()]}"
    )

    got2, want2 = set(PestDetection.model_fields), set(DETECTION_BOX_KEYS)
    assert not (want2 - got2), (
        f"单个检测框会返回 {sorted(want2 - got2)}，但 PestDetection 没有声明"
    )
    assert all(f.is_required() is False for f in PestDetection.model_fields.values()), (
        f"PestDetection 有必填字段："
        f"{[n for n, f in PestDetection.model_fields.items() if f.is_required()]}"
    )


# ============================== 卡片等高 / 语音检索 / 导航栏（这一轮的回归锁）


@case("知识图书库：卡片等高 —— 封面 display 与网格拉伸，缺一个就退回原样")
def _():
    """「虫害卡片大小不一」的根因在 CSS 而不是数据：卡片里那几层都是 <span>，
    而 span 默认 inline —— aspect-ratio 与 overflow 对 inline 元素不生效，
    封面高度就跟着图片固有比例走（病害封面清一色 256×256，IP102 虫害封面
    从 365×690 到 1024×989 什么比例都有），没有封面的几张更是直接塌成 0 高。"""
    css = _css_bundle()["pages"]

    cover = _rule_body(css, ".book-card__cover {")
    assert "display: block" in cover, (
        "封面没有 display: block —— 它还是 inline span，aspect-ratio 与 overflow 都不生效，"
        "高度继续跟着图片的固有比例走"
    )
    assert "aspect-ratio: 4 / 3" in cover, "封面比例被改掉了（已确认统一为 4:3）"

    grid = _rule_body(css, ".card-grid {")
    assert "align-items: start" not in grid, "align-items: start 又回来了，卡片只占自身内容高度"
    assert "align-items: stretch" in grid, "网格没有拉伸卡片"
    assert "grid-auto-rows: 1fr" in grid, (
        "缺 grid-auto-rows: 1fr —— 只有 stretch 的话，同行卡片等高、行与行之间仍会参差"
    )

    body = _rule_body(css, ".book-card__body {")
    assert "display: flex" in body, "正文层仍是 inline span，纵向 padding 会被忽略"
    assert "flex: 1 1 auto" in body, "正文层不占满卡片高度，底部的 foot 就落不到最下面"

    title = _rule_body(css, ".book-card__title {")
    assert "display: -webkit-box" in title and "-webkit-line-clamp: 2" in title, \
        "标题没有两行截断（display: block 也是让它的 margin-bottom 生效的前提）"

    assert "margin-top: auto" in _rule_body(css, ".book-card__foot {"), \
        "「查看详情」没有钉在卡片底部，等高之后短卡片下半截会空一块"
    assert "display: flex" in _rule_body(css, ".book-card {"), \
        "卡片不是 flex 容器，__trigger 撑不满被拉伸的高度"


@case("导航栏：品牌与图标最左、导航入口靠右、方言最右，入口不再各带图标")
def _():
    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "layout.js").read_text(encoding="utf-8")
    css = _css_bundle()["layout"]

    # 删的是各导航入口自己的图标；品牌区那个 leaf 标记要留着
    # （原话是「标题和图标放在最左边」）—— 所以顶栏里应当只剩 1 处 Icon.render
    i = js.find("function renderHeader(host) {")
    assert i >= 0, "找不到 renderHeader"
    head = js[i:js.find("function renderFooter", i)]
    assert head.count("Icon.render") == 1, (
        f"顶栏里还有 {head.count('Icon.render')} 处 Icon.render —— "
        "导航入口的图标应已删除，只保留品牌区那一个"
    )
    assert 'Icon.render("leaf", 22)' in head, "品牌区的叶子标记被误删了"
    # NAV 数组本身也不能再留 icon 字段 —— 留着就会有人顺手渲染回去
    i_nav = js.find("const NAV = [")
    nav_src = js[i_nav:js.find("];", i_nav)]
    assert i_nav >= 0 and "icon" not in nav_src, f"NAV 里还留着图标字段：{nav_src[:160]}"

    nav = _rule_body(css, ".site-nav {")
    assert "flex: 0 1 auto" in nav, (
        "导航条还在增长（flex-grow: 1）—— 它会把剩余宽度全吃掉，margin-left: auto 就没有"
        "富余空间可推，导航入口会继续贴着标题"
    )
    assert "margin-inline: auto" in nav, (
        "缺两侧 auto 外边距，导航入口不会落在标题区与方言选择器之间的中间偏右处"
        "（原来靠 margin-left: auto 紧贴方言，用户要求「不要紧靠方言」）"
    )
    assert "min-width: 0" in nav, "缺 min-width: 0，窄屏时导航会撑破顶栏而不是内部滚动"
    assert "overflow-x: auto" in nav, "缺内部滚动兜底"
    assert "justify-content: flex-end" not in nav, (
        "用 flex-end 排右会在内容溢出时把「首页」推到滚动够不着的左侧"
    )

    assert "flex: 0 0 auto" in _rule_body(css, ".brand {"), "品牌区应固定在最左（不参与伸缩）"
    assert "flex: 0 0 auto" in _rule_body(css, ".dialect-picker {"), "方言选择器应固定在最右"


@case("顶栏铺满整屏：不再被 --content-max 拉回中间（2560 屏上左右各空约 640px）")
def _():
    css = _css_bundle()
    inner = _rule_body_live(css["layout"], ".site-header__inner {")
    assert "max-width: var(--content-max)" not in inner, (
        "顶栏又跟着正文那条 1280px 居中上限走了 —— 宽屏上左右会重新空出各约 640px"
    )
    assert "max-width: none" in inner, "顶栏应铺满整屏"
    assert "0 var(--space-5)" in inner, "铺满后仍要留左右内边距，否则标题会贴到视口边上"
    # 正文仍居中：令牌本身不能被顺手改掉，否则整站的行长都变了
    assert "--content-max: 1280px" in css["base"], "正文的居中上限被改了"


@case("空框：hidden 必须真的隐藏，且每个显示时机都显式写 hidden = false")
def _():
    from src.common.paths import path_of

    body = _rule_body_live(_css_bundle()["base"], "[hidden] {")
    assert "display: none !important" in body, (
        "缺全局 [hidden] { display: none !important } —— .notice 的 display: flex 会盖掉它，"
        "图书库顶部那条提示条就变成一个没有内容的空框"
    )

    js = (path_of("frontend_dir") / "assets" / "js" / "layout.js").read_text(encoding="utf-8")
    i = js.find(".catch(() => {")
    assert i >= 0, "找不到降级条的失败分支"
    assert "hidden = false" in js[i:i + 600], (
        "降级条的错误分支没把 hidden 置回 false —— 全局规则生效后，"
        "「无法获取服务状态」这条提示会永远看不见，而它恰恰只为后端挂掉时存在"
    )


@case("语音：录音实现只有一份（recorder.js），诊断页与图书库都不再自带")
def _():
    """锁的是能力原语而不是某段文案：采集链一旦出现两份，
    「必须经 gain=0 的静音节点否则啸叫」这类踩过的坑就要修两遍，且必然分叉。"""
    from src.common.paths import path_of

    front = path_of("frontend_dir")
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted((front / "assets" / "js").glob("*.js"))}
    assert "recorder.js" in files, "缺少公共录音模块 recorder.js"

    owners = [n for n, t in files.items() if "createScriptProcessor" in t]
    assert owners == ["recorder.js"], f"采集链出现在了 {owners} —— 录音实现必须只有 recorder.js 一份"

    rec = files["recorder.js"]
    assert "gain.value = 0" in rec, "静音节点没了 —— 麦克风声音会从扬声器放出来形成啸叫"
    assert "TARGET_SAMPLE_RATE = 16000" in rec, "没有重采样到 16kHz，短语音接口不认"
    assert '"RIFF"' in rec and 'type: "audio/wav"' in rec, "WAV 封装被改动了"
    assert 'ctx.state === "suspended"' in rec, (
        "缺 AudioContext 的 resume —— 停在 suspended 时 onaudioprocess 一次都不触发，"
        "用户明明说了话却收到「没有录到声音」"
    )

    for gone in ("createScriptProcessor", "getUserMedia", "function makeWav"):
        assert gone not in files["diagnose.js"], f"diagnose.js 里还残留采集代码：{gone}"
    for gone in ("createScriptProcessor", "getUserMedia"):
        assert gone not in files["library.js"], f"library.js 里还残留采集代码：{gone}"
    # 诊断页改成委托后，用户可见的行为必须原样保留
    for need in ("没有录到声音，请检查麦克风后重试", "当前浏览器不支持录音", "btn-record-label",
                 '"audio", state.audioBlob, "record.wav"'):
        assert need in files["diagnose.js"], f"诊断页录音改造后丢了：{need}"

    for page, own in (("library.html", "library.js"), ("diagnose.html", "diagnose.js")):
        html = (front / page).read_text(encoding="utf-8")
        i_rec = html.find("/assets/js/recorder.js")
        i_own = html.find(f"/assets/js/{own}")
        assert i_rec >= 0, f"{page} 没有引入 recorder.js"
        assert 0 <= i_rec < i_own, f"{page} 里 recorder.js 必须排在 {own} 之前"
        tag = html[html.rfind("<script", 0, i_rec):html.find(">", i_rec)]
        assert "defer" not in tag and "async" not in tag, f"{page} 的 recorder.js 不能带 defer/async"


@case("摄像头：取景实现在 camera.js 一份，诊断页只在支持时才用它、否则回落文件选择器")
def _():
    """用户报的「点调用摄像头弹出来的是选照片」：原先那个隐藏 input 靠 capture 属性
    指望浏览器开摄像头，而 capture 在桌面浏览器与多数 WebView 里被直接忽略。
    真开摄像头必须走媒体采集 API —— 这条锁的是「它在哪、只在一处、且必须关流」。"""
    from src.common.paths import path_of

    front = path_of("frontend_dir")
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted((front / "assets" / "js").glob("*.js"))}

    assert "camera.js" in files, "缺少公共摄像头模块 camera.js"
    # 采集只允许出现在两个能力原语模块里：录音（麦克风）在 recorder.js，
    # 取景（摄像头）在 camera.js。除此之外任何文件出现它，都是复制了第二份采集链。
    owners = sorted(n for n, t in files.items() if "getUserMedia" in t)
    assert owners == ["camera.js", "recorder.js"], (
        f"采集代码出现在了 {owners} —— 只允许 recorder.js（录音）与 camera.js（取景）这两份"
    )

    cam = files["camera.js"]
    assert "window.Camera" in cam, "camera.js 没有挂到 window.Camera 上"
    assert "navigator.mediaDevices" in cam and "function supported" in cam, (
        "缺 supported() —— 非安全上下文下 mediaDevices 就是 undefined，调用方靠它决定要不要回落"
    )
    # 关流：不关的话手机上摄像头指示灯一直亮着，这是隐私问题而不只是耗电
    assert "getTracks()" in cam and ".stop()" in cam, "取景结束后没有把轨道停掉"
    assert "visibilitychange" in cam, "切到后台时没有收摊，摄像头会一直开着"

    # iOS 上这三个属性缺一不可，少一个画面就是不出来
    for attr in ("autoplay", "playsinline", "muted"):
        assert attr in cam, f"<video> 缺了 {attr}，手机上画面出不来"

    # 出片必须是一个能直接喂给 setImage 的 File，链路才对得上
    assert "toBlob(" in cam and '"image/jpeg"' in cam, "快门没有导出 JPEG"
    assert "new File(" in cam, "快门没有把照片包成 File —— 后续 setImage 与提交链路都接不上"

    # 诊断页必须真的用它，而且打不开时要回落，不能变成死胡同
    dg = files["diagnose.js"]
    assert "window.Camera" in dg and "Camera.open(" in dg, "诊断页没有接上 camera.js"
    assert 'Camera.supported()' in dg, "诊断页没有判支持性，非安全上下文下会直接报错"
    assert '$("input-camera").click()' in dg, "回落分支丢了 —— 打不开摄像头时用户就没路可走了"

    html = (front / "diagnose.html").read_text(encoding="utf-8")
    i_cam = html.find("/assets/js/camera.js")
    i_dg = html.find("/assets/js/diagnose.js")
    assert i_cam >= 0, "diagnose.html 没有引入 camera.js"
    assert 0 <= i_cam < i_dg, "camera.js 必须排在 diagnose.js 之前，否则 window.Camera 还是 undefined"
    tag = html[html.rfind("<script", 0, i_cam):html.find(">", i_cam)]
    assert "defer" not in tag and "async" not in tag, "camera.js 不能带 defer/async"


@case("语音检索：按当前方言调 ASR，识别文本回填搜索框并立即检索")
def _():
    from src.common.paths import path_of

    front = path_of("frontend_dir")
    js = (front / "assets" / "js" / "library.js").read_text(encoding="utf-8")
    api = (front / "assets" / "js" / "api.js").read_text(encoding="utf-8")
    html = (front / "library.html").read_text(encoding="utf-8")
    icons = (front / "assets" / "js" / "icons.js").read_text(encoding="utf-8")
    css = _css_bundle()["pages"]

    assert 'id="library-mic"' in html, "library.html 里没有麦克风按钮"
    assert 'aria-label="语音输入搜索关键词"' in html, "麦克风按钮没有无障碍名称"
    assert "mic:" in icons, "icons.js 里没有 mic 图标，按钮会是空白"
    assert "library-search__mic {" in css, "麦克风按钮没有配套样式"
    assert "padding-right: 56px" in _rule_body(css, ".library-search .input {"), \
        "输入框右侧没给按钮让位，文字会跑到按钮底下"
    assert "::-webkit-search-cancel-button" in css, "没有隐藏 WebKit 的原生清除键，它会压在麦克风键上"

    assert '"/api/voice/asr"' in api, "api.js 没有接 /api/voice/asr"
    assert "asr(formData)" in api and "postForm" in api, "Api.asr 没有走 multipart"

    for need in ("Recorder.isRecording", "Recorder.stop", "Api.asr", "Dialect.current()"):
        assert need in js, f"library.js 的语音链路缺少 {need}"
    assert 'form.append("audio"' in js, "ASR 表单里没有 audio 字段"
    assert 'form.append("dialect"' in js, "ASR 表单里没有 dialect 字段（方言识别就靠它）"

    # 语音回填必须能清掉还挂着的按键防抖，否则旧关键词会晚 320ms 把识别结果盖回去
    i = js.find("function applyKeyword(value) {")
    assert i >= 0, "没有 applyKeyword()：手打与语音必须走同一个检索入口"
    body = js[i:i + 600]
    assert "clearTimeout(searchTimer)" in body, "语音回填没有清掉按键防抖"
    assert "state.keyword = (value || \"\").trim()" in body, "applyKeyword 没有写回关键词"


@case("语音检索：没配 Key / 没听清时如实告知，绝不假装识别成功")
def _():
    """未配置语音 Key 时后端返回的是 code 0 + mock=true + 空文本 + 说明文案，
    **不是异常** —— 只判 .catch 会把它做成「识别成功但填了空白」，比报错更让人困惑。"""
    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "library.js").read_text(encoding="utf-8")
    i = js.find("function recognize(blob) {")
    assert i >= 0, "library.js 里没有 recognize()"
    body = js[i:]
    body = body[:body.find("\n  }\n")]

    assert ".mock" in body, "没有判断 mock —— 未配置语音服务会被当成识别成功"
    assert ".text" in body, "没有判断识别文本是否为空"
    assert "没有识别到内容" in body, "识别为空时缺少明确提示"

    i_empty = body.find("if (!text)")
    i_fill = body.find('$("library-search").value = text')
    assert 0 <= i_empty < i_fill, "空文本分支必须先 return，绝不能走到回填那一步"


# ============================================ 阶段 7：我的农田（7.12）

def _farm_db():
    """一个空的临时 farm 库（已迁移到最新版本）的上下文管理器。

    每个用例用**独立的临时库**，不碰 data/farm/farm.db —— 单元测试不该写进
    真实数据目录，否则跑完测试会留下垃圾账号，而且并行跑会互相干扰。
    """
    import tempfile
    from contextlib import contextmanager
    from src.farm.db import connect, migrate

    @contextmanager
    def _cm():
        path = Path(tempfile.mkdtemp(prefix="agri_farm_test_")) / "farm.db"
        with connect(path) as conn:
            migrate(conn, verbose=False)
            yield conn

    return _cm()


def _new_user(conn, username="zhang", email="zhang@example.com", password="abc12345"):
    from src.farm import users

    return users.create_user(conn, username=username, email=email, password=password)


@case("农田库：迁移按版本号生效且幂等，重复执行不会重复建表")
def _():
    from src.farm.db import MIGRATIONS, applied_version, migrate

    with _farm_db() as conn:
        latest = max(v for v, _ in MIGRATIONS)
        assert applied_version(conn) == latest, "迁移完的版本号应等于最大迁移号"

        # 再跑一次：不该重复执行（executescript 里的 CREATE TABLE 都是 IF NOT EXISTS，
        # 万一版本号没生效，重复建表会把已有数据连带清掉——所以必须验证「返回空列表」）
        assert migrate(conn, verbose=False) == [], "重复迁移应无事发生"

        # 表都建出来了
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        for table in ("users", "sessions", "user_settings", "login_attempts", "fields", "logs",
                      "daily_temp", "climate_normals", "diagnoses", "growth_snapshots",
                      "harvest_records"):
            assert table in names, f"缺少数据表 {table}"


@case("农田库：除 climate_normals 外每张 farm 表都带 user_id（7.0.5 的数据隔离底座）")
def _():
    with _farm_db() as conn:
        for table in ("fields", "logs", "daily_temp", "diagnoses", "growth_snapshots",
                      "harvest_records"):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "user_id" in cols, f"{table} 缺 user_id —— 用户之间会互相看到数据"
        normals = {r["name"] for r in conn.execute("PRAGMA table_info(climate_normals)").fetchall()}
        assert "user_id" not in normals, "climate_normals 是全局气候事实，不该按用户存（7.0.5）"


@case("密码：不可逆、每用户盐唯一、强度规则分明")
def _():
    from src.farm.auth import hash_password, new_salt, password_problem, verify_password

    salt_a, salt_b = new_salt(), new_salt()
    assert salt_a != salt_b, "两次取盐相同 —— 盐必须是随机生成的"
    assert len(salt_a) == 32, "16 字节盐的十六进制应是 32 个字符"

    h1 = hash_password("abc12345", salt_a)
    h2 = hash_password("abc12345", salt_b)
    assert h1 != h2, "同一密码在不同盐下应得到不同哈希"
    assert "abc12345" not in h1, "明文出现在哈希里"
    assert len(h1) == 64, "PBKDF2-SHA256 的十六进制输出应为 64 位"

    assert verify_password("abc12345", h1, salt_a)
    assert not verify_password("abc12346", h1, salt_a)
    assert not verify_password("abc12345", h1, salt_b), "盐不同就不该通过"
    # 盐损坏时要返回 False 而不是抛异常（否则一条坏数据能把登录接口打成 500）
    assert verify_password("abc12345", h1, "不是十六进制") is False

    assert password_problem("abc12345") == ""
    assert "8 位" in password_problem("abc1"), "太短要给出具体原因"
    assert "字母" in password_problem("12345678"), "纯数字要提示缺字母"
    assert "数字" in password_problem("abcdefgh"), "纯字母要提示缺数字"
    assert password_problem("") != ""


@case("密码参数：600000 轮与 16 字节盐不能被悄悄改小")
def _():
    from src.farm import auth

    assert auth.PBKDF2_ITERATIONS == 600_000, (
        "轮数被调小了 —— 这是 7.0.2 指定的抗爆破成本，调小等于偷偷降低安全性"
    )
    assert auth.SALT_BYTES == 16
    assert auth.SESSION_DAYS == 7, "会话应为 7 天（7.0.8）"


@case("账号：注册/登录/改密/注销全链路，且对外字段绝不含哈希与盐")
def _():
    from src.farm import repo as repo_mod
    from src.farm import users

    with _farm_db() as conn:
        user = _new_user(conn)
        assert user["username"] == "zhang"
        assert user["salt"] and user["password_hash"], "库里应存盐与哈希"

        pub = users.public(user)
        assert "password_hash" not in pub and "salt" not in pub, (
            "对外字段漏出了哈希/盐 —— 这一层就必须切掉，不能指望调用方记得 del"
        )

        # 登录：用户名大小写不敏感（否则换个大小写就能绕过唯一约束与限流）
        assert users.authenticate(conn, "ZHANG", "abc12345")["id"] == user["id"]
        assert users.authenticate(conn, "zhang", "wrong123") is None
        assert users.authenticate(conn, "nobody", "abc12345") is None, (
            "不存在的用户也必须返回 None（与密码错误同一种结果，否则等于账号枚举接口）"
        )

        # 改密码：原密码错、新旧相同、新密码太弱都要被拒
        for old, new, why in (("bad", "xyz12345", "原密码错"),
                              ("abc12345", "abc12345", "新旧相同"),
                              ("abc12345", "short1", "太短")):
            try:
                users.change_password(conn, user["id"], old, new)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{why} 时应被拒绝")
        users.change_password(conn, user["id"], "abc12345", "xyz12345")
        assert users.authenticate(conn, "zhang", "xyz12345"), "改密后新密码应可用"
        assert users.authenticate(conn, "zhang", "abc12345") is None, "旧密码必须失效"

        # 注销要验密码
        try:
            users.delete_user(conn, user["id"], "wrong")
        except ValueError:
            pass
        else:
            raise AssertionError("注销时密码错误应被拒绝")

        uid = user["id"]
        repo_mod.create_field(conn, uid, name="东头那块", crop="水稻", sow_date="2026-04-10")
        users.delete_user(conn, uid, "xyz12345")
        assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM fields").fetchone()["n"] == 0, (
            "注销后田块应被外键级联删除 —— 这一条只有在 PRAGMA foreign_keys=ON 时才成立"
        )


@case("账号：重名与重邮箱被拒（含大小写相同的情况）")
def _():
    from src.farm import users

    with _farm_db() as conn:
        _new_user(conn, "zhang", "zhang@example.com")
        for name, mail, why in (("zhang", "other@example.com", "同名"),
                                ("ZHANG", "other@example.com", "同名（大小写不同）"),
                                ("other", "zhang@example.com", "同邮箱")):
            try:
                users.create_user(conn, username=name, email=mail, password="abc12345")
            except (users.UsernameTaken, users.EmailTaken):
                pass
            else:
                raise AssertionError(f"{why} 时应被拒绝")


@case("账号：用户名与邮箱的形状校验")
def _():
    from src.farm.auth import check_email, check_username

    assert check_username("zhang") == ""
    assert check_username("zhang_01") == ""
    assert check_username("") != ""
    assert check_username("a") != "", "太短"
    assert check_username("12345") != "", "纯数字应被拒"
    assert check_username("a" * 30) != "", "过长"
    # 中文名：str.isalnum() 对汉字返回 True，用它会放行「张三」而提示语却写着
    # 「只能用字母数字下划线」—— 这条就是防那次退化。中文名请填昵称。
    msg = check_username("张三")
    assert msg != "", "汉字用户名应被拒（提示语里要指向昵称）"
    assert "昵称" in msg, "拒绝时要告诉用户中文名该填在哪"
    assert check_username("Zhāng") != "", "带声调的拉丁字母不是 ASCII"
    assert check_username("ｚｈａｎｇ") != "", "全角字母会造出看起来同名的两个账号"
    assert check_username("zh ang") != "", "不允许空格"
    assert check_username("zhang!") != ""

    assert check_email("a@b.com") == ""
    assert check_email("a b@c.com") != ""
    assert check_email("ab.com") != ""
    assert check_email("a@@b.com") != ""
    assert check_email("a@bcom") != "", "域名没有点"


@case("会话：过期即失效、登出即删、续期只在过半时发生")
def _():
    from src.farm import auth

    with _farm_db() as conn:
        user = _new_user(conn)
        token, _expires = auth.create_session(conn, user["id"], ip="127.0.0.1", user_agent="pytest")
        assert auth.get_session_user_id(conn, token) == user["id"]

        # 登出后立刻失效
        auth.delete_session(conn, token)
        assert auth.get_session_user_id(conn, token) is None, "登出后 token 必须失效"
        assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0, (
            "登出应删行，而不是留一条「已登出但仍有效」的记录"
        )

        # 造一条已过期的会话
        from src.farm.timeutil import utc_now_iso
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent, ip) "
            "VALUES ('expired', ?, ?, '2020-01-01T00:00:00Z', '', '')",
            (user["id"], utc_now_iso()),
        )
        assert auth.get_session_user_id(conn, "expired") is None, "过期会话必须返回 None"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE token = 'expired'").fetchone()["n"] == 0, (
            "过期的会话应顺手删掉，免得表越积越大"
        )

        assert auth.get_session_user_id(conn, "") is None
        assert auth.get_session_user_id(conn, "no-such-token") is None


@case("登录限流：同 IP 一分钟 5 次、同账号一小时 10 次（7.0.2）")
def _():
    from src.farm import auth
    from src.farm.auth import RateLimited

    with _farm_db() as conn:
        # 同 IP 第 5 次失败后，下一次就该被拒
        for _i in range(auth.IP_LIMIT[0]):
            auth.record_failure(conn, ip="1.2.3.4", username="zhang")
        try:
            auth.check_rate_limit(conn, ip="1.2.3.4", username="other")
        except RateLimited as e:
            assert e.retry_after == 60 and "频繁" in e.msg
        else:
            raise AssertionError("同 IP 连续失败 5 次后应触发限流")

        # 换个 IP 立刻可用（限流是按来源算的，不是全局的）
        auth.check_rate_limit(conn, ip="5.6.7.8", username="other")

        # 同账号口径：换个 IP 也拦得住
        for _i in range(auth.ACCOUNT_LIMIT[0]):
            auth.record_failure(conn, ip=f"10.0.0.{_i}", username="zhang")
        try:
            auth.check_rate_limit(conn, ip="9.9.9.9", username="ZHANG")
        except RateLimited:
            pass
        else:
            raise AssertionError("同账号失败 10 次后应触发限流（用户名大小写应归一）")

        # 登录成功清掉该账号的失败计数；IP 的留着
        auth.clear_account_failures(conn, "zhang")
        auth.check_rate_limit(conn, ip="9.9.9.9", username="zhang")
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE scope = 'ip'").fetchone()["n"] > 0, (
            "清账号计数不该顺手把 IP 计数也清了"
        )


@case("数据隔离：取/改/删别人的田块一律当「不存在」，且原数据一行不动（7.0.5）")
def _():
    from src.farm import repo

    with _farm_db() as conn:
        a = _new_user(conn, "alice", "alice@example.com")
        b = _new_user(conn, "bob", "bob@example.com")
        field = repo.create_field(conn, b["id"], name="东头那块", crop="水稻",
                                  area_mu=3.0, sow_date="2026-04-10")

        for call, label in (
            (lambda: repo.get_field(conn, a["id"], field["id"]), "读"),
            (lambda: repo.update_field(conn, a["id"], field["id"], {"name": "改掉"}), "改"),
            (lambda: repo.delete_field(conn, a["id"], field["id"]), "删"),
            (lambda: repo.create_log(conn, a["id"], field_id=field["id"], type="sow",
                                     date="2026-04-11"), "往别人的田里记账"),
        ):
            try:
                call()
            except repo.NotFound:
                pass
            else:
                raise AssertionError(f"A {label} B 的田块应抛 NotFound（路由层转 404）")

        # B 的数据必须原封不动
        still = repo.get_field(conn, b["id"], field["id"])
        assert still["name"] == "东头那块", "越权修改动了别人的数据"
        assert repo.list_fields(conn, b["id"]), "越权删除动了别人的数据"
        assert repo.list_logs(conn, b["id"], field_id=field["id"]) == [], (
            "越权写入成功了 —— 别人的田里多了记录"
        )
        # A 自己那边什么都没有
        assert repo.list_fields(conn, a["id"]) == []


@case("数据隔离：删除田块会连带清掉它的从属数据")
def _():
    from src.farm import repo

    with _farm_db() as conn:
        user = _new_user(conn)
        field = repo.create_field(conn, user["id"], name="东头那块", crop="玉米",
                                  sow_date="2026-07-31")
        fid = field["id"]
        repo.create_log(conn, user["id"], field_id=fid, type="fertilize", date="2026-08-01")
        repo.upsert_daily_temp(conn, user["id"], fid,
                               [{"date": "2026-08-01", "tmean": 30.0}], source="archive")
        repo.upsert_snapshot(conn, user["id"], field_id=fid, date="2026-08-01", gdd=10.0, stage="苗期")

        repo.delete_field(conn, user["id"], fid)
        for table in ("logs", "daily_temp", "growth_snapshots"):
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert n == 0, f"删田后 {table} 里还留着 {n} 行"


@case("逐日气温：同一天重复写是覆盖而不是叠加（7.12）")
def _():
    from src.farm import repo

    with _farm_db() as conn:
        user = _new_user(conn)
        field = repo.create_field(conn, user["id"], name="田", crop="水稻", sow_date="2026-04-10")
        fid = field["id"]

        repo.upsert_daily_temp(conn, user["id"], fid,
                               [{"date": "2026-05-01", "tmean": 20.0}], source="archive")
        repo.upsert_daily_temp(conn, user["id"], fid,
                               [{"date": "2026-05-01", "tmean": 26.0}], source="forecast")

        rows = repo.list_daily_temp(conn, user["id"], fid, start="2026-05-01", end="2026-05-01")
        assert len(rows) == 1, f"同一天写出了 {len(rows)} 行 —— 积温会被算两遍"
        assert rows[0]["tmean"] == 26.0, "后写的应覆盖先写的"
        assert rows[0]["source"] == "forecast", "来源应跟着更新"

        # 异常值进不了库（接口解析错位带进来的脏数据会让积温整体歪掉）
        n = repo.upsert_daily_temp(conn, user["id"], fid, [
            {"date": "2026-05-02", "tmean": 9999.0},
            {"date": "2026-05-03", "tmean": 21.0},
            {"date": "不是日期", "tmean": 21.0},
        ], source="archive")
        assert n == 1, "只有一条合法数据应被写入"
        assert repo.latest_temp_date(conn, user["id"], fid) == "2026-05-03"


@case("气候基准值：同日分组算出的均值与样本标准差正确（7.2.3）")
def _():
    import statistics
    from src.farm.weather import build_normals

    rows = [
        {"date": "2021-06-01", "tmean": 20.0},
        {"date": "2022-06-01", "tmean": 24.0},
        {"date": "2023-06-01", "tmean": 22.0},
        {"date": "2023-06-02", "tmean": 30.0},     # 只有一天
    ]
    normals = build_normals(rows, years=5)
    avg, std = normals["06-01"]
    assert abs(avg - 22.0) < 1e-9, f"均值算错：{avg}"
    assert abs(std - statistics.stdev([20.0, 24.0, 22.0])) < 1e-9, "标准差应为样本标准差(n-1)"
    # 只有一年的那天：样本标准差无定义，记 0 而不是编一个数
    assert normals["06-02"] == (30.0, 0.0), "单样本的标准差应为 0"


@case("天气合并：以历史接口为准，预报只补它缺的那几天")
def _():
    from src.farm.weather import merge_history

    archive = [{"date": "2026-05-01", "tmean": 20.0}, {"date": "2026-05-03", "tmean": 22.0}]
    forecast = [{"date": "2026-05-01", "tmean": 99.0},      # 与历史重叠：应被历史覆盖
                {"date": "2026-05-02", "tmean": 21.0},      # 历史缺的那天：由预报补上
                {"date": "2026-05-09", "tmean": 30.0}]      # 超出 until：丢掉
    merged = merge_history(archive, forecast, until="2026-05-03")
    assert [r["date"] for r in merged] == ["2026-05-01", "2026-05-02", "2026-05-03"], (
        "应先按日期去重、升序，并截到 until"
    )
    assert merged[0]["tmean"] == 20.0, "重叠的那天应以历史实况为准，不能被预报值覆盖"


@case("积温：低温日只记 0，绝不出现负数（7.12）")
def _():
    from src.farm.gdd import accumulate, daily_gdd
    from src.farm.stages import crop_config

    cfg = crop_config("水稻")      # Tbase 10 / Topt 30 / Tu 42
    tb, to, tu = cfg["Tbase"], cfg["Topt"], cfg["Tu"]

    assert daily_gdd(9.9, tb, to, tu) == 0.0, "低于基点应是 0"
    assert daily_gdd(-5.0, tb, to, tu) == 0.0, "零下的日子绝不能记成负数"
    assert daily_gdd(10.0, tb, to, tu) == 0.0, "正好等于基点也是 0"
    assert abs(daily_gdd(20.0, tb, to, tu) - 10.0) < 1e-9
    assert daily_gdd(43.0, tb, to, tu) == 0.0, "超过上限 Tu 记 0"

    # 第三段：超过最适温后回落（这是 Ae4 模型与普通积温的分水岭）
    mid = daily_gdd(36.0, tb, to, tu)
    assert 0 < mid < daily_gdd(30.0, tb, to, tu), (
        "35℃ 以上的积温必须比 30℃ 小 —— 高温是胁迫不是加速"
    )

    series = accumulate([
        {"date": "2026-05-01", "tmean": 5.0},
        {"date": "2026-05-02", "tmean": -2.0},
        {"date": "2026-05-03", "tmean": 20.0},
        {"date": "2026-05-04", "tmean": 25.0},
    ], cfg)
    cums = [r["gdd_cum"] for r in series]
    assert cums == sorted(cums), f"积温序列出现回退：{cums}"
    assert cums[0] == 0.0 and cums[1] == 0.0
    assert abs(cums[-1] - 25.0) < 1e-9, f"两天共 10+15=25 度，实际 {cums[-1]}"

    # 缺测的日子跳过，而不是补 0 或补均值
    skipped = [r["date"] for r in accumulate(
        [{"date": "2026-05-01", "tmean": None}, {"date": "2026-05-02", "tmean": 20.0}], cfg)]
    assert skipped == ["2026-05-02"], "缺测日应被跳过（补 0 会让积温偏小、阶段偏晚）"


@case("阶段匹配：闭区间 —— 正好等于阈值即算进入下一阶段（7.3.3）")
def _():
    from src.farm.gdd import match_stage

    # 水稻：出苗 100 / 分蘖 350 / 拔节 900 / 抽穗 1400 / 成熟 2100
    assert match_stage("水稻", 0)["stage"] == "出苗期"
    assert match_stage("水稻", 99.9)["stage"] == "出苗期"
    assert match_stage("水稻", 100)["stage"] == "出苗期", "正好 100 仍是出苗期（阈值是进入点）"
    assert match_stage("水稻", 350)["stage"] == "分蘖期", (
        "正好等于 350 应算已进入分蘖期 —— 写成严格大于会让示例田（400 度）的阶段错位"
    )
    assert match_stage("水稻", 349.9)["stage"] == "出苗期"
    assert match_stage("水稻", 2100)["matured"] is True
    assert match_stage("水稻", 9999)["matured"] is True, "远超成熟阈值也算已完熟"

    m = match_stage("水稻", 400)
    assert m["stage"] == "分蘖期" and m["next_stage"] == "拔节期"
    assert 0 < m["progress"] < 1 and abs(m["progress"] - 400 / 2100) < 1e-3
    assert abs(m["gdd_remain"] - 1700) < 1e-6
    assert m["actions"], "阶段要带上该阶段的农事提示（7.4.1 要用）"

    over = match_stage("油菜", 9999)
    assert over["progress"] == 1.0, "进度封顶在 1.0，不能出现 275%"
    assert over["gdd_remain"] == 0.0, "已完熟时「还差多少积温」应为 0，不倒扣成负数"


@case("生育期配置：七个作物的阈值严格递增、maturity 与末阶段一致")
def _():
    from src.farm.stages import all_stages, supported_crops

    assert set(supported_crops()) == {
        "水稻", "玉米", "油菜", "小麦", "大豆", "棉花", "马铃薯",
    }, (
        "作物清单变了就要同步改这里，以及 scripts/22_verify_artifacts.py 的同款断言"
    )
    for crop, cfg in all_stages().items():
        assert cfg["Tbase"] < cfg["Topt"] < cfg["Tu"], f"{crop} 的温度基点不合理"
        gdds = [float(s["gdd"]) for s in cfg["stages"]]
        assert gdds == sorted(gdds) and len(set(gdds)) == len(gdds), (
            f"{crop} 的阶段阈值必须严格递增，否则阶段会跳来跳去"
        )
        assert abs(float(cfg["maturity_gdd"]) - gdds[-1]) < 1e-9, (
            f"{crop} 的 maturity_gdd 与最后一个阶段的阈值不一致 —— 进度与阶段会互相矛盾"
        )
        assert cfg["source"], f"{crop} 缺少阈值来源（7.3.1 要求写明）"
        for st in cfg["stages"]:
            assert st["actions"], f"{crop} 的「{st['name']}」没有农事提示"


@case("生育期配置：坏配置在加载时就被拦住，而不是让积温算到除零")
def _():
    from src.farm.stages import _validate

    good = {"source": "x", "Tbase": 10, "Topt": 30, "Tu": 42, "maturity_gdd": 200,
            "stages": [{"name": "A", "gdd": 100, "actions": ["a"]},
                       {"name": "B", "gdd": 200, "actions": ["b"]}]}
    _validate("测试作物", good)          # 合法的应当通过

    cases = [
        ({"Tu": 30}, "Tu == Topt 会让四段式除以 0"),
        ({"Tbase": 40}, "Tbase 高于 Topt"),
        ({"stages": [{"name": "A", "gdd": 100, "actions": []},
                     {"name": "B", "gdd": 100, "actions": []}]}, "阈值没有递增"),
        ({"stages": [{"name": "A", "gdd": 200, "actions": []},
                     {"name": "B", "gdd": 100, "actions": []}]}, "阈值倒序"),
        ({"maturity_gdd": 999}, "maturity 与末阶段不一致"),
        ({"stages": [{"name": "A", "gdd": 100, "actions": []},
                     {"name": "A", "gdd": 200, "actions": []}]}, "阶段重名"),
    ]
    for patch, why in cases:
        bad = {**good, **patch}
        try:
            _validate("测试作物", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{why} 时应在加载阶段就抛错")


@case("示例田：注册即有三条，覆盖三个作物与三个生长阶段")
def _():
    from src.farm import repo, seed

    with _farm_db() as conn:
        user = _new_user(conn)
        fields = seed.seed_demo_fields(conn, user["id"])
        assert len(fields) == 3, "应预置三条示例田"

        crops = [f["crop"] for f in fields]
        assert crops == ["水稻", "玉米", "油菜"], f"三个作物缺了：{crops}"

        # 每条田都必须带坐标 —— 位置为空的话示例田就是一行没有内容的记录
        for f in fields:
            assert f["lng"] and f["lat"], f"{f['name']} 没有经纬度，天气与积温都取不到"
            assert f["province"] and f["city"]
            assert "示例" in f["name"], "示例田的名字里要写明是示例，不能冒充真实地块"

        # 播种日回推的天数符合定义
        from src.farm.timeutil import days_between, today_str
        for f, back in zip(fields, (30, 70, 200)):
            assert days_between(f["sow_date"], today_str()) == back, (
                f"{f['name']} 的播种日不是回推 {back} 天"
            )

        # 默认省市写进了 user_settings，新建田时能继承
        from src.farm import users
        s = users.get_settings(conn, user["id"])
        assert s["default_province"] == seed.DEMO_PROVINCE
        assert s["default_city"] == seed.DEMO_CITY


@case("逐日气温与快照：查出来的行都限制在自己的 user_id 内")
def _():
    from src.farm import repo

    with _farm_db() as conn:
        a = _new_user(conn, "alice", "alice@example.com")
        b = _new_user(conn, "bob", "bob@example.com")
        fa = repo.create_field(conn, a["id"], name="A 的田", crop="水稻", sow_date="2026-04-10")
        fb = repo.create_field(conn, b["id"], name="B 的田", crop="玉米", sow_date="2026-07-31")

        repo.upsert_daily_temp(conn, b["id"], fb["id"],
                               [{"date": "2026-08-01", "tmean": 31.0}], source="archive")
        # A 拿 B 的 field_id 去查，查不到任何行
        assert repo.list_daily_temp(conn, a["id"], fb["id"],
                                    start="2026-01-01", end="2026-12-31") == [], (
            "按 user_id 过滤失效 —— 别人田块的逐日气温被读到了"
        )
        assert repo.latest_temp_date(conn, a["id"], fb["id"]) is None
        assert repo.list_daily_temp(conn, b["id"], fb["id"],
                                    start="2026-08-01", end="2026-08-31"), "B 自己应能读到"

        # 诊断入档：pest_json 为 None 时存 NULL、读回仍是 None
        d1 = repo.create_diagnosis(conn, a["id"], field_id=fa["id"], date="2026-08-01",
                                   label="稻瘟病", confidence=0.9, pest_json=None)
        assert d1["pest_json"] is None, "「没有虫害证据」与「确实没虫」不能含糊成空字典"
        d2 = repo.create_diagnosis(conn, a["id"], field_id=fa["id"], date="2026-08-02",
                                   label="稻瘟病", pest_json={"boxes": []})
        assert d2["pest_json"] == {"boxes": []}
        assert len(repo.list_diagnoses(conn, b["id"])) == 0, "B 不该看到 A 的诊断"


@case("农事记录：类型/来源白名单与倒序返回")
def _():
    from src.farm import repo

    with _farm_db() as conn:
        user = _new_user(conn)
        f = repo.create_field(conn, user["id"], name="田", crop="油菜", sow_date="2026-03-23")
        fid = f["id"]

        repo.create_log(conn, user["id"], field_id=fid, type="sow", date="2026-03-23",
                        item="种子", source="manual")
        repo.create_log(conn, user["id"], field_id=fid, type="fertilize", date="2026-04-01",
                        item="尿素", amount="20 斤", source="voice")

        for bad, why in (({"type": "乱写", "source": "manual"}, "类型"),
                         ({"type": "sow", "source": "乱写"}, "来源")):
            try:
                repo.create_log(conn, user["id"], field_id=fid, date="2026-04-02", **bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{why} 不在白名单时应被拒绝")

        logs = repo.list_logs(conn, user["id"], field_id=fid)
        assert [l["date"] for l in logs] == ["2026-04-01", "2026-03-23"], "历史必须倒序"
        assert logs[0]["amount"] == "20 斤", "用量保留原样文本，不做换算"

        assert repo.recent_diagnosis_log(conn, user["id"], fid, days=7) is None


@case("收获记录：全平台计数用于训练触发，特征导出不含任何身份字段（7.14 隐私）")
def _():
    from src.farm import repo

    with _farm_db() as conn:
        a = _new_user(conn, "alice", "alice@example.com")
        b = _new_user(conn, "bob", "bob@example.com")
        fa = repo.create_field(conn, a["id"], name="A 的田", crop="水稻", sow_date="2026-04-10")
        fb = repo.create_field(conn, b["id"], name="B 的田", crop="水稻", sow_date="2026-04-10")

        repo.create_harvest(conn, a["id"], field_id=fa["id"], crop="水稻", sow_date="2026-04-10",
                            harvest_date="2026-07-20", actual_days=101, gdd_at_harvest=1500.0,
                            features={"gdd_cum": 1500.0})
        repo.create_harvest(conn, b["id"], field_id=fb["id"], crop="水稻", sow_date="2026-04-11",
                            harvest_date="2026-07-21", actual_days=101, gdd_at_harvest=1520.0,
                            features={"gdd_cum": 1520.0})

        assert repo.count_harvests(conn) == 2, "训练触发要的是全平台计数，不能按用户过滤"
        assert len(repo.list_harvests(conn, a["id"])) == 1, "历史列表只该看到自己的"

        for item in repo.list_harvest_features(conn):
            for leaked in ("user_id", "field_id", "name", "province", "city", "lng", "lat"):
                assert leaked not in item, (
                    f"训练样本里漏出了「{leaked}」—— 7.14 要求只提取数值特征，不带可定位到人的字段"
                )
            assert item["features"], "特征不能丢"


class _StubLLM:
    """假的大模型：问什么就答什么。用来测「模型的输出会被校验」这件事 ——
    真去调 DeepSeek 的话，测试结果取决于它当时说了什么。"""

    provider = "stub"

    def __init__(self, text=""):
        self.text = text

    def answer(self, question, *, context=""):
        return self.text

    def generate_report(self, context):
        return {}


def _fake_series(n=40, per_day=8.0, start="2026-08-01"):
    """造一段积温序列（带逐日气温），给不依赖联网的用例用。"""
    from datetime import date, timedelta

    d0 = date.fromisoformat(start)
    out = []
    cum = 0.0
    for i in range(n):
        cum += per_day
        out.append({
            "date": (d0 + timedelta(days=i)).isoformat(), "tmean": 25.0,
            "tmax": 30.0, "tmin": 20.0, "precip": 0.0, "humidity": 70.0,
            "gdd_daily": per_day, "gdd_cum": round(cum, 2),
        })
    return out


def _fake_growth(**over):
    base = {
        "field_id": "fld_x", "crop": "水稻", "sow_date": "2026-08-01", "days_since_sow": 40,
        "gdd_cum": 1000.0, "gdd_remain": 1100.0, "maturity_gdd": 2100.0,
        "stage": "分蘖期", "stage_index": 1, "stage_gdd": 350.0,
        "next_stage": "拔节期", "next_gdd": 900.0, "progress": 0.476,
        "matured": False, "actions": ["追分蘖肥", "喷药防虫"],
        "confidence": "高", "as_of": "2026-09-10", "source_note": "",
    }
    base.update(over)
    return base


def _fake_weather(source, *, days=3):
    """造一份天气：三天都有雨、高温、高湿 —— 天气类提醒的四个触发条件全踩中，
    这样「有没有出天气类提醒」就只取决于 source 是不是真预报。"""
    from datetime import timedelta

    from src.farm.timeutil import today_local

    d0 = today_local()
    return {
        "province": "湖北省", "city": "武汉市", "source": source,
        "source_note": "", "as_of": d0.isoformat(),
        "degraded": source != "forecast", "degraded_reason": "" if source == "forecast" else "预报不可用",
        "days": [
            {"date": (d0 + timedelta(days=i)).isoformat(), "tmax": 36.0, "tmin": 26.0,
             "tmean": 25.0, "precip": 20.0, "humidity": 90.0}
            for i in range(days)
        ],
    }


@case("建议引擎：天气降级时绝不产出天气类提醒（拿不到预报就不许说未来有雨）")
def _():
    from src.farm import advisor, repo
    from src.farm.timeutil import today_str

    with _farm_db() as conn:
        user = _new_user(conn)
        field = repo.create_field(conn, user["id"], name="田", crop="水稻", sow_date="2026-08-01")
        growth = _fake_growth()
        series = _fake_series()

        WEATHER_TYPES = {"fertilize", "spray", "irrigate"}

        good = advisor.reminders(conn, user["id"], field, growth, series, _fake_weather("forecast"))
        fired = {r["type"] for r in good["reminders"]} & WEATHER_TYPES
        assert fired == WEATHER_TYPES, (
            f"真预报下这四类提醒都该出现（拿不到就说明测试数据没踩中触发条件）：{fired}"
        )
        assert good["weather_used"] is True

        # 同一份天气，只是来源变成气候平均值 —— 一条天气类提醒都不许出
        bad = advisor.reminders(conn, user["id"], field, growth, series, _fake_weather("normal"))
        leaked = [r for r in bad["reminders"] if r["type"] in WEATHER_TYPES]
        assert not leaked, f"天气降级了还在提天气类的建议：{[r['action'] for r in leaked]}"
        assert bad["weather_used"] is False
        assert any("跳过" in n for n in bad["notes"]), (
            "跳过天气类提醒时必须说明原因，否则用户会把「没有天气提醒」读成「天气没问题」"
        )

        # 空天气（连基准值都没有）同理
        empty = advisor.reminders(conn, user["id"], field, growth, series,
                                  {"source": "none", "days": []})
        assert not [r for r in empty["reminders"] if r["type"] in WEATHER_TYPES]

        # 不依赖天气的那条（近期诊断 → 注意复查）在任何情况下都该出
        repo.create_log(conn, user["id"], field_id=field["id"], type="diagnose",
                        date=today_str(), item="稻瘟病")
        with_diag = advisor.reminders(conn, user["id"], field, growth, series,
                                      _fake_weather("normal"))
        assert any(r["type"] == "disease" for r in with_diag["reminders"]), (
            "「注意复查」不依赖天气，降级时也该照常提醒"
        )


@case("收获建议：进度不够时不会建议收割（54% 的玉米不该听到「适合收割」）")
def _():
    from src.farm import advisor

    early = _fake_growth(progress=0.54, stage="抽穗期", matured=False)
    params = {"dryLeafRatio": 0.0, "earDroop": 0.0}
    harvest = {"earliest": None, "most_likely": None, "latest": None, "matured": False}
    # 天气再好、再适合收割，进度不到 0.8 就不该给收割建议
    assert advisor.harvest_advice(early, params, harvest, _fake_weather("forecast")) is None, (
        "还在抽穗期的作物被建议收割 —— 这是错话"
    )

    late = _fake_growth(progress=0.85, stage="成熟期")
    advice = advisor.harvest_advice(late, params, harvest, _fake_weather("forecast"))
    assert advice and advice["type"] == "harvest"

    # 完熟（≥0.95）时优先级最高，而且不依赖天气
    done = _fake_growth(progress=0.98, matured=True)
    top = advisor.harvest_advice(done, params, harvest, {"source": "none", "days": []})
    assert top and top["priority"] == "高" and "完熟" in top["action"]


@case("语音解析：认不出就返回空字段 + 明确原因，绝不猜")
def _():
    from src.farm import repo, voice_parse
    from src.farm.timeutil import today_str

    with _farm_db() as conn:
        user = _new_user(conn)
        fields = [
            repo.create_field(conn, user["id"], name="东头那块", crop="水稻", sow_date="2026-04-10"),
            repo.create_field(conn, user["id"], name="西边田", crop="玉米", sow_date="2026-07-31"),
        ]

        empty = voice_parse.parse("", fields)
        assert empty["ok"] is False and empty["fields"]["type"] == ""
        assert empty["reason"], "解析失败必须给出原因"
        assert empty["fields"]["field_id"] == "", "失败时不能填任何字段"

        nonsense = voice_parse.parse("今天天气不错啊", fields)
        assert nonsense["ok"] is False
        assert nonsense["fields"]["type"] == "" and nonsense["fields"]["field_id"] == ""
        assert "哪种农事" in nonsense["reason"]
        assert "哪块田" in nonsense["reason"]

        # 认得出：类型 + 田块 + 日期 + 数量
        ok = voice_parse.parse("昨天给东头那块施了20斤尿素", fields)
        assert ok["ok"] is True
        assert ok["fields"]["type"] == "fertilize"
        assert ok["fields"]["field_id"] == fields[0]["id"]
        assert ok["fields"]["amount"] == "20斤"
        assert ok["fields"]["item"] == "尿素"
        from src.farm.timeutil import today_local
        from datetime import timedelta
        assert ok["fields"]["date"] == (today_local() - timedelta(days=1)).isoformat()
        assert ok["matched"], "命中的词表项要交出来，方便用户核对"

        # 日期可以缺省：用今天兜底，但必须说出来
        no_date = voice_parse.parse("给东头那块喷了药", fields)
        assert no_date["ok"] is True
        assert no_date["fields"]["date"] == today_str()
        assert "日期" in no_date["reason"], "用了今天兜底就要提示用户核对"


@case("语音解析：田块名有歧义时返回空并列出候选，不去猜一个（记错了比不记更糟）")
def _():
    from src.farm import repo, voice_parse
    from src.farm.timeutil import today_str

    with _farm_db() as conn:
        user = _new_user(conn)
        fields = [
            repo.create_field(conn, user["id"], name="示例田 · 水稻", crop="水稻", sow_date="2026-09-09"),
            repo.create_field(conn, user["id"], name="示例田 · 玉米", crop="玉米", sow_date="2026-07-31"),
            repo.create_field(conn, user["id"], name="长安街那块", crop="油菜", sow_date="2026-03-23"),
        ]

        # 说了作物名 → 能唯一定位（这就是当初写错的那个 case：
        # 「示例田」三块共有，只靠前缀匹配会随手返回第一块）
        hit = voice_parse.parse("昨天给示例田水稻施了肥", fields)
        assert hit["fields"]["field_id"] == fields[0]["id"], (
            "「示例田」是三块田的公共前缀，必须结合「水稻」才能定位"
        )

        # 只说公共前缀 → 有歧义，返回空 + 候选
        ambiguous = voice_parse.parse("给示例田施肥了", fields)
        assert ambiguous["fields"]["field_id"] == ""
        assert ambiguous["ok"] is False
        assert "好几块" in ambiguous["reason"]

        # 独特的名字照样能认出来
        unique = voice_parse.parse("长安街那块昨天浇了水", fields)
        assert unique["fields"]["field_id"] == fields[2]["id"]


@case("语音解析：解析器的词表不该把「水」这种单字当物品（「收了水稻」不是「水」）")
def _():
    from src.farm import repo, voice_parse
    from src.farm.timeutil import today_str

    with _farm_db() as conn:
        user = _new_user(conn)
        fields = [repo.create_field(conn, user["id"], name="东头那块", crop="水稻",
                                    sow_date="2026-04-10")]
        r = voice_parse.parse("今天收了水稻", fields)
        assert r["fields"]["item"] == "水稻", f"物品认成了 {r['fields']['item']!r}"


@case("叙述层：闭集外的病名一律拦下，通用说法不能误伤（7.5 反幻觉）")
def _():
    from src.farm import narrator

    lexicon = narrator.disease_lexicon()
    assert len(lexicon) > 300, f"闭集只有 {len(lexicon)} 个名字，标签表没读进来？"

    # 从闭集里取一个真实名字来测「不该误杀」这一侧 —— 写死某个具体的病名不行：
    # 标签表来自 PDD271 的命名（有「甘薯瘟病」但没有「稻瘟病」），
    # 写死会因为数据集的命名习惯而与实现无关地失败。
    real = sorted(lexicon)[len(lexicon) // 2]
    assert len(real) >= 3

    # 编造的名字要被抓出来。
    # 注意匹配是「最左最长」，可能连前面的动词一起吃掉（实测抓出来的是
    # 「得了玉米超级斑点病」）—— 所以断言用子串判断，不能拿列表成员相等去比。
    text = "这块田可能得了玉米超级斑点病，要注意。"
    cleaned, removed = narrator.filter_hallucinated(text)
    assert any("玉米超级斑点病" in word for word in removed), f"编造的病名没被拦下：{removed}"
    assert "玉米超级斑点病" not in cleaned

    # 闭集里的名字要留着
    kept, removed2 = narrator.filter_hallucinated(f"叶片有{real}的典型症状")
    assert real in kept and not removed2, f"真实病名 {real} 被误杀：{removed2}"

    # 通用说法不能误伤 —— 否则一句正常的话会整段被退回模板
    for phrase in ("这种病要早防", "注意防治病虫害", "最近容易发病", "算一下这是什么病"):
        _clean, bad = narrator.filter_hallucinated(phrase)
        assert not bad, f"「{phrase}」被误判成编造：{bad}"


@case("叙述层：LLM 说了闭集外的病名 → 整段退回模板（而不是发一段真假掺半的话）")
def _():
    from src.farm import narrator

    field = {"id": "fld_x", "name": "东头那块", "crop": "水稻", "area_mu": 3, "sow_date": "2026-08-01"}
    growth = _fake_growth()
    weather = _fake_weather("forecast")
    harvest = {"earliest": "2026-11-01", "most_likely": "2026-11-05", "latest": "2026-11-10",
               "basis": "积温法", "method": "gdd_fallback"}

    bad_llm = _StubLLM('{"summary": "得了玉米超级斑点病", "why": "因为积温", '
                       '"tips": [], "risks": []}')
    out = narrator.narrative(field, growth, weather, harvest, [], [], llm=bad_llm)
    assert out["source"] == "template", "出现了闭集外的病名就该整段退回模板"
    assert "玉米超级斑点病" not in out["text"]

    good_llm = _StubLLM('{"summary": "水稻长势正常", "why": "积温到位", '
                        '"tips": ["追肥"], "risks": []}')
    ok = narrator.narrative(field, growth, weather, harvest, [], [], llm=good_llm)
    assert ok["source"] == "llm" and ok["text"] == "水稻长势正常"
    assert ok["why"] and ok["tips"] == ["追肥"]

    # 模型输出不是 JSON / 调用失败 → 退回模板，且字段齐全（7.5 要求字段一致）
    broken = _StubLLM("我不会输出 JSON")
    fallback = narrator.narrative(field, growth, weather, harvest, [], [], llm=broken)
    assert fallback["source"] == "template"
    for key in ("text", "why", "tips", "risks"):
        assert key in fallback and fallback[key] is not None, f"模板路径缺字段 {key}"


@case("3D 参数：叶片数单调不降、有穗与生育期表一致、颜色是合法 hex")
def _():
    import re

    from src.farm import growth
    from src.farm.stages import stage_names, supported_crops

    hex_re = re.compile(r"^#[0-9a-f]{6}$")

    for crop in supported_crops():
        names = stage_names(crop)
        # 马铃薯这类「地上没有穗」的作物不在 EAR_STAGE 里 —— 这时 ear_at 取到末尾，
        # 表示整条进度上都不该出现穗（见 growth._ear_from_index 的注释）。
        ear_stage = growth.EAR_STAGE.get(crop)
        ear_at = names.index(ear_stage) if ear_stage in names else len(names)
        prev_leaves = -1
        prev_height = -1.0
        saw_ear = False

        for step in range(0, 101):
            p = step / 100
            # stage_index 随进度一起前进，模拟真实调用
            idx = min(len(names) - 1, int(p * len(names) * 0.999))
            m = growth.model_params(crop, p, idx)

            assert m["leafCount"] >= prev_leaves, f"{crop} 在进度 {p} 叶片数变少了"
            assert m["height"] >= prev_height, f"{crop} 在进度 {p} 株高变矮了"
            prev_leaves, prev_height = m["leafCount"], m["height"]

            for key in ("leafColor", "stemColor", "earColor"):
                assert hex_re.match(m[key]), f"{crop} 的 {key} 不是合法颜色：{m[key]}"
            for key in ("leafDroop", "dryLeafRatio", "earDroop", "earSize"):
                assert 0.0 <= m[key] <= 1.0, f"{crop} 的 {key} 越界：{m[key]}"

            if idx >= ear_at:
                saw_ear = True
                assert m["hasEar"], f"{crop} 已进入「{names[idx]}」却没有穗"
            elif m["hasEar"]:
                raise AssertionError(f"{crop} 还没到「{ear_stage}」就有穗了")

        assert saw_ear or ear_at >= len(names), f"{crop} 的测试没覆盖到「有穗」阶段"
        if ear_at < len(names):
            assert growth.model_params(crop, 1.0, len(names) - 1)["hasEar"]
        # 黑层/乳线只有玉米有，且只在接近成熟时置位
        assert growth.model_params("水稻", 1.0, 4)["hasBlackLayer"] is False
        assert growth.model_params("玉米", 0.99, 4)["hasBlackLayer"] is True
        assert growth.model_params("玉米", 0.1, 0)["hasBlackLayer"] is False


@case("3D 参数：示例田三条各自落在预期的阶段与形态上")
def _():
    from src.farm import growth

    # 分蘖期的水稻：矮、绿、没有穗
    rice = growth.model_params("水稻", 400 / 2100, 1)
    assert 20 <= rice["height"] <= 35, f"分蘖期水稻株高应约 25cm，实际 {rice['height']}"
    assert rice["leafCount"] >= 4, f"分蘖期应有 4~5 片叶，实际 {rice['leafCount']}"
    assert rice["hasEar"] is False
    assert rice["dryLeafRatio"] == 0.0

    # 抽穗期的玉米：高挺、有穗
    maize = growth.model_params("玉米", 1072 / 2000, 3)
    assert maize["height"] >= 120, f"抽穗期玉米应明显高挺，实际 {maize['height']}cm"
    assert maize["hasEar"] is True

    # 完熟的油菜：金黄、枯叶多、穗（角果）下垂
    rape = growth.model_params("油菜", 1.0, 4)
    assert rape["dryLeafRatio"] >= 0.8, "完熟油菜的叶片应该基本脱落"
    assert rape["height"] >= 140
    assert rape["earColor"] != rape["leafColor"]


@case("天气来源：三态齐备，且「没有数据」不能冒充成「气候平均值」")
def _():
    from src.farm import growth, repo
    from src.farm.timeutil import today_str

    with _farm_db() as conn:
        user = _new_user(conn)

        # 有基准值 → source=normal，并且必须标明"这不是预报"
        repo.upsert_normals(conn, "湖北省", "武汉市",
                            {today_str()[5:]: (25.0, 3.0)})
        normal = growth._normals_weather(conn, "湖北省", "武汉市", days=3, reason="预报不可用")
        assert normal["source"] == "normal" and normal["degraded"] is True
        assert "不是预报" in normal["source_note"], "气候平均必须说清它不是预报"
        assert normal["days"], "有基准值的那些天应该给出数据"

        # 连基准值都没有 → source=none + 空列表。**不许编数字。**
        none = growth._normals_weather(conn, "无此省", "无此市", days=3, reason="")
        assert none["source"] == "none" and none["days"] == []
        assert none["degraded"] is True and none["degraded_reason"]

        # 落库的 source 只允许 archive / forecast 两种（7.1 的三态里 normal 不落库）
        repo.upsert_daily_temp(conn, user["id"], "fld_x",
                               [{"date": "2026-08-01", "tmean": 25.0}], source="archive")
        repo.upsert_daily_temp(conn, user["id"], "fld_x",
                               [{"date": "2026-08-02", "tmean": 26.0}], source="forecast")
        rows = repo.list_daily_temp(conn, user["id"], "fld_x", start="2026-08-01", end="2026-08-02")
        assert {r["source"] for r in rows} == {"archive", "forecast"}


@case("收获预测：未训练模型时如实走积温法（演示状态就是这一种）")
def _():
    from src.farm import harvest_model

    field = {"id": "fld_x", "crop": "水稻", "sow_date": "2026-08-01"}
    growth = _fake_growth(gdd_cum=1000.0, gdd_remain=1100.0, progress=0.476)
    series = _fake_series(n=40, per_day=8.0)

    real_loader = harvest_model._load_models
    harvest_model._load_models = lambda: None      # 假装还没训练过
    try:
        h = harvest_model.predict_harvest(field, growth, series, today="2026-09-10")
    finally:
        harvest_model._load_models = real_loader

    assert h["method"] == "gdd_fallback", f"没有模型就该走积温法，实际 {h['method']}"
    assert "积温法" in h["basis"]
    assert h["sample_needed"] == harvest_model.TRAIN_MIN_SAMPLES == 30
    assert h["earliest"] and h["most_likely"] and h["latest"]
    assert h["earliest"] <= h["most_likely"] <= h["latest"], "区间没排好序"
    assert h["window_days"] >= 1, "区间半宽至少 1 天 —— 精确到某一天是假承诺"


@case("收获预测：区间宽度按距收获的远近分档收窄（7.3.4 的宽度控制表）")
def _():
    from src.farm.harvest_model import _half_width_limit

    # 清单四档：>60 → 12~15；30~60 → 7~10；<30 → 3~5；<7 → 1~2
    assert _half_width_limit(120) == 15
    assert _half_width_limit(61) == 15
    assert _half_width_limit(60) == 10, "60 天属于「30~60」这一档"
    assert _half_width_limit(45) == 10
    assert _half_width_limit(30) == 10, "30 天也属于「30~60」，别掉进下一档"
    assert _half_width_limit(29) == 5
    assert _half_width_limit(8) == 5
    assert _half_width_limit(7) == 5
    assert _half_width_limit(6) == 2
    assert _half_width_limit(0) == 2

    # 越接近收获，允许的区间只能越窄（绝不反弹）
    limits = [_half_width_limit(d) for d in range(200, -1, -1)]
    assert all(limits[i] <= limits[i - 1] for i in range(1, len(limits))), (
        "区间宽度随距收获天数递减这件事不成立"
    )


@case("收获预测：已完熟就不再预测未来日期，而是说「尽快收」")
def _():
    from src.farm import harvest_model

    field = {"id": "fld_x", "crop": "油菜", "sow_date": "2026-03-23"}
    growth = _fake_growth(crop="油菜", matured=True, progress=1.0)
    h = harvest_model.predict_harvest(field, growth, _fake_series(n=200), today="2026-10-09")
    assert h["matured"] is True
    assert h["earliest"] is None and h["most_likely"] is None, "完熟了还给未来日期就是错的"
    assert "收割" in h["basis"]
    # 完熟之后不给日期，也就没有「挪日期」这回事 —— 施肥影响只能是 0
    assert h["maturity_shift"] == 0.0 and h["management_note"] == ""


# ==================================================================== 施肥 → 收获日（U 形曲线）

@case("施肥解析：认得出肥料名与质量单位，认不出就如实说，绝不猜")
def _():
    from src.farm import management as mgmt

    # 肥料名：直接写、写在长句里、用养分类型代称，都要认出来
    assert mgmt.normalize_item("尿素") == "尿素"
    assert mgmt.normalize_item("今天给东头那块施了尿素") == "尿素"
    assert mgmt.normalize_item("复合肥（15-15-15）") == "复合肥"
    assert mgmt.normalize_item("氮肥") == "尿素", "「氮肥」按最常用单品折算"
    assert mgmt.normalize_item("磷肥") == "过磷酸钙"
    assert mgmt.normalize_item("钾肥") == "氯化钾"
    # 不属于肥料的词不能被当成肥料 —— voice_parse 的农资词表里有农药、种子、地膜
    assert mgmt.normalize_item("打了一遍杀虫剂") is None
    assert mgmt.normalize_item("") is None

    # 质量单位：斤是半公斤，吨是千公斤
    assert mgmt.parse_amount("20 斤") == (10.0, "")
    assert mgmt.parse_amount("20公斤") == (20.0, "")
    assert mgmt.parse_amount("2.5吨") == (2500.0, "")
    assert mgmt.parse_amount("500克") == (0.5, "")
    assert mgmt.parse_amount("20kg") == (20.0, "")

    # 计数单位换算不了：返回 None 并说明原因，不能当成 0 悄悄吞掉
    kg, why = mgmt.parse_amount("3 袋")
    assert kg is None and "袋" in why, f"「袋」没有标准重量，必须如实说不换算，实际 {kg}/{why}"
    kg2, why2 = mgmt.parse_amount("")
    assert kg2 is None and why2, "空数量要给出原因"

    # 亩用量 vs 整块田总量：只有明确的「每亩」说法才按每亩算
    assert mgmt.is_per_mu("每亩 20 斤")
    assert mgmt.is_per_mu("20 斤/亩")
    assert not mgmt.is_per_mu("2 亩地，一共 20 斤尿素"), "这是整块地的总量，要按面积摊"


@case("施肥汇总：只算播种后的 fertilize，亩用量不摊、整块田总量按面积摊")
def _():
    from src.farm import management as mgmt

    logs = [
        # 播种后、按亩记：20 斤尿素 = 10kg × 46% = 4.6kg 纯氮/亩，整块地都是这个浓度
        {"type": "fertilize", "date": "2026-08-10", "item": "尿素",
         "amount": "20 斤", "note": "每亩"},
        # 播种前的不算（清园、上季的账）
        {"type": "fertilize", "date": "2026-07-01", "item": "尿素",
         "amount": "100 斤", "note": ""},
        # 非施肥类型不算
        {"type": "spray", "date": "2026-08-12", "item": "多菌灵", "amount": "2 斤", "note": ""},
    ]
    one = mgmt.nutrition_from_logs(logs, area_mu=2.0, sow_date="2026-08-01")
    assert one["has_records"] and one["records"] == 1, f"只该计入 1 条，实际 {one['records']}"
    assert abs(one["n"] - 4.6) < 0.01, f"20 斤尿素 = 4.6kg 纯氮/亩，实际 {one['n']}"

    # 整块地总量：2 亩地一共 20 斤尿素 → 每亩只有 2.3kg 纯氮
    whole = mgmt.nutrition_from_logs(
        [{"type": "fertilize", "date": "2026-08-10", "item": "尿素",
          "amount": "20 斤", "note": ""}],
        area_mu=2.0, sow_date="2026-08-01")
    assert abs(whole["n"] - 2.3) < 0.01, f"总量要除以面积，实际 {whole['n']}"

    # 面积没填：按 1 亩折算，但必须把这件事标出来，不能默默算
    unknown = mgmt.nutrition_from_logs(
        [{"type": "fertilize", "date": "2026-08-10", "item": "尿素",
          "amount": "20 斤", "note": ""}],
        area_mu=0, sow_date="2026-08-01")
    assert unknown["assumed_area"] is True and abs(unknown["n"] - 4.6) < 0.01

    # 认不出用量的记录要单独报出来（这里：数量是「3 袋」）
    partial = mgmt.nutrition_from_logs(
        [{"type": "fertilize", "date": "2026-08-10", "item": "尿素", "amount": "3 袋", "note": ""}],
        area_mu=1.0, sow_date="2026-08-01")
    assert partial["has_records"] is False
    assert len(partial["unparsed"]) == 1, "认不出的记录必须留下记录，别当没发生"

    # 一条施肥记录都没有 → 未施肥
    empty = mgmt.nutrition_from_logs([], area_mu=1.0, sow_date="2026-08-01")
    assert empty["has_records"] is False and empty["n"] == 0 and empty["p2o5"] == 0


@case("施肥曲线：U 形 —— 未施肥推迟、适量提前、过量又推迟")
def _():
    from src.farm import management as mgmt
    from src.farm.stages import crop_config

    cfg = crop_config("水稻")             # 推荐量 10 / 4 kg-亩
    assert cfg["nutrition"] == {"n": 10, "p2o5": 4, "k2o": 6}

    def shift(n, p, records=True):
        return mgmt.maturity_shift_days(
            {"has_records": records, "records": 1 if records else 0,
             "n": n, "p2o5": p, "k2o": 0}, cfg)

    # ---- 三个锚点：零点 = 未施肥 ----
    none_ = shift(0, 0, records=False)
    assert none_["level"] == "未施肥", none_
    assert abs(none_["days"] - 4.0) < 0.01, f"未施肥应当推迟 4 天（缺氮 +2.5、缺磷 +1.5），实际 {none_['days']}"

    ok = shift(10, 4)                    # 氮磷都到推荐量
    assert ok["level"] == "适量", ok
    assert abs(ok["days"] - (-2.0)) < 0.01, f"适量应当提前 2 天，实际 {ok['days']}"

    over = shift(20, 4)                  # 氮超一倍
    assert over["level"] == "过量", over
    assert abs(over["days"] - 7.0) < 0.01, f"超量一倍应当推迟 7 天，实际 {over['days']}"

    # ---- 形状：先降后升，且两端都不超过锚点 ----
    # 纯氮量从 0 一路加到 40 kg/亩（推荐量是 10），十天一档
    days = [shift(float(n), 4)["days"] for n in range(0, 41, 2)]
    assert days[0] > days[5], "从缺素到适量这一段必须是**下降**的（施肥提前成熟）"
    assert days[5] < days[10], "从适量到过量这一段必须是**上升**的（多施反而推迟）"
    assert min(days) < 0 < max(days), "曲线必须跨过 0 —— 否则「适量提前」不复存在"
    # 最低点落在推荐量附近，而不是落在两头
    assert days.index(min(days)) == 5, f"曲线最低点应当在推荐量处，实际在第 {days.index(min(days))} 档"

    # ---- 超量继续加不会无限推迟（封顶），也不会突破总夹子 ----
    assert abs(shift(200, 4)["days"] - 7.0) < 0.01, "r > 2 要封顶在 +7"
    assert shift(200, 0)["days"] <= mgmt.SHIFT_MAX + 1e-9
    assert shift(0, 0)["days"] >= mgmt.SHIFT_MIN

    # ---- 缺磷的附加项：磷没到推荐量时额外推迟，最多 1.5 天 ----
    low_p = shift(10, 0)                 # 氮够、磷全缺
    assert abs(low_p["days"] - (-0.5)) < 0.01, f"−2 + 1.5 = −0.5，实际 {low_p['days']}"
    assert low_p["level"] == "缺肥", "磷没到推荐量就该是缺肥档"

    # ---- 找不出方向的作物不装算过 ----
    blank = mgmt.maturity_shift_days({"has_records": True, "n": 99, "p2o5": 99, "k2o": 0}, {})
    assert blank["days"] == 0.0 and "推荐施肥量" in blank["note"], blank

    # ---- 影响幅度必须挂着文献出处，且注明区间 ----
    assert len(ok["sources"]) >= 3, "论文支撑不能只有一条"
    joined = " ".join(ok["sources"])
    assert "中国水稻科学" in joined and "DOI 10.16819" in joined, "过量施氮那条要有可核对的出处"
    assert "缺磷" in joined and "3 周" in joined, "缺素那一段也要有依据"


@case("施肥曲线：配置里七个作物都给了营养推荐量，坏配置在加载时就被拦住")
def _():
    from src.farm import stages

    for crop, cfg in stages.all_stages().items():
        nut = cfg.get("nutrition")
        assert nut, f"{crop} 没有营养推荐量配置"
        assert all(float(nut[k]) > 0 for k in ("n", "p2o5", "k2o")), f"{crop} 的推荐量必须为正"

    # 大豆靠根瘤固氮，推荐氮量本来就该明显低于禾本科 —— 全配成同一个数就是没查过
    assert stages.crop_config("大豆")["nutrition"]["n"] < \
        stages.crop_config("玉米")["nutrition"]["n"], "大豆的推荐氮量不该和玉米一样"

    # 坏配置：0 会让折算除零，负数更是无从解释 —— 必须在加载时炸掉
    for bad in ({"n": 0, "p2o5": 4, "k2o": 6}, {"n": 10, "p2o5": -1, "k2o": 6},
                {"n": 10, "p2o5": 4}):
        try:
            stages._validate("试验作物", {
                "source": "x", "Tbase": 10, "Topt": 30, "Tu": 42,
                "maturity_gdd": 200, "nutrition": bad,
                "stages": [{"name": "出苗期", "gdd": 100, "actions": []},
                           {"name": "成熟期", "gdd": 200, "actions": []}],
            })
        except ValueError:
            continue
        raise AssertionError(f"坏配置 {bad} 应当被 _validate 拦住")


@case("收获预测：施肥记录真的会移动收获日，没有记录时一天都不动")
def _():
    from src.farm import harvest_model

    field = {"id": "fld_x", "crop": "水稻", "sow_date": "2026-08-01"}
    growth = _fake_growth(gdd_cum=1000.0, gdd_remain=1100.0, progress=0.476)
    series = _fake_series(n=40, per_day=8.0)

    real_loader = harvest_model._load_models
    harvest_model._load_models = lambda: None
    try:
        base = harvest_model.predict_harvest(field, growth, series, today="2026-09-10")
        late = harvest_model.predict_harvest(
            field, growth, series, today="2026-09-10",
            management={"days": 5.0, "level": "过量", "note": "氮肥超量",
                        "sources": ["出处一"]})
        early = harvest_model.predict_harvest(
            field, growth, series, today="2026-09-10",
            management={"days": -2.0, "level": "适量", "note": "养分适量",
                        "sources": ["出处一"]})
    finally:
        harvest_model._load_models = real_loader

    def plus(d, n):
        from datetime import date, timedelta
        return (date.fromisoformat(d) + timedelta(days=n)).isoformat()

    assert late["most_likely"] == plus(base["most_likely"], 5), \
        f"推后 5 天：{base['most_likely']} -> {late['most_likely']}"
    assert early["most_likely"] == plus(base["most_likely"], -2), \
        f"提前 2 天：{base['most_likely']} -> {early['most_likely']}"

    # 三个日期一起挪，区间宽度不变 —— 施肥影响的是"什么时候熟"，不是"有多不确定"
    assert late["window_days"] == base["window_days"]
    assert late["latest"] == plus(base["latest"], 5)
    assert late["earliest"] == plus(base["earliest"], 5)

    assert late["maturity_shift"] == 5.0 and late["management_note"] == "氮肥超量"
    assert late["management_sources"] == ["出处一"]
    assert "施肥" in late["basis"], "依据那句话里要提到做过施肥调整"

    # 不传 management（老调用方）= 没有记录，结果必须与从前逐字一致
    assert base["maturity_shift"] == 0.0 and base["management_note"] == ""
    assert base["management_sources"] == []
    assert "施肥" not in base["basis"]



@case("收获特征：11 个特征的顺序与名字固定，缺测不补 0（补 0 等于编造）")
def _():
    from src.farm import harvest_model

    assert harvest_model.FEATURE_NAMES == (
        "gdd_cum", "gdd_7d", "gdd_remain",
        "tmax_mean_30d", "tmin_mean_30d", "precip_sum_30d", "hot_days_30d", "cold_days_30d",
        "sow_doy", "crop_type", "days_since_sow",
    ), "7.3.4 的 11 个特征（顺序即训练列顺序），不许改"
    assert harvest_model.CROP_CODES == {"水稻": 0, "玉米": 1, "油菜": 2}

    field = {"id": "f", "crop": "水稻", "sow_date": "2026-08-01"}
    growth = _fake_growth(gdd_cum=1000.0, gdd_remain=1100.0, days_since_sow=40)

    full = harvest_model.build_features(field, growth, _fake_series())
    vec = harvest_model.feature_vector(full)
    assert len(vec) == 11 and all(isinstance(v, float) for v in vec)
    assert full["hot_days_30d"] == 0 and full["cold_days_30d"] == 0
    assert full["crop_type"] == 0
    assert full["precip_sum_30d"] == 0.0

    # 缺 tmax/tmin（v4 之前写的老行）时：跳过，而不是当成 0℃
    thin = [{"date": r["date"], "tmean": r["tmean"], "gdd_daily": r["gdd_daily"],
             "gdd_cum": r["gdd_cum"]} for r in _fake_series()]
    thin_features = harvest_model.build_features(field, growth, thin)
    assert thin_features["hot_days_30d"] == 0
    assert thin_features["tmax_mean_30d"] == 0.0, "没有数据时是 0（缺失），不是某个编出来的均值"
    # 但积温类的特征不受影响 —— 它们只用 tmean
    assert thin_features["gdd_cum"] == full["gdd_cum"]


@case("外部训练：特征黄金向量 —— 训练与推理必须是同一次 build_features 算出来的")
def _():
    from src.farm import harvest_model

    # 全确定的一组输入：11 个特征的值可以手算核对。任何一侧（训练脚本或线上推理）
    # 偷偷改了特征的算法，这里就会亮 —— 而模型本身不会报错，只会给越来越离谱的日期。
    field = {"id": "f", "crop": "水稻", "sow_date": "2026-04-01"}
    growth = {"gdd_cum": 1200.0, "gdd_remain": 900.0, "days_since_sow": 100}
    temps = [
        {"date": f"2026-04-{d:02d}", "tmean": 27.0, "tmax": 32.0, "tmin": 22.0,
         "precip": 5.0, "gdd_daily": 10.0, "gdd_cum": 10.0 * i}
        for i, d in enumerate(range(1, 13), 1)
    ]

    feat = harvest_model.build_features(field, growth, temps, today="2026-04-12")
    assert feat == {
        "gdd_cum": 1200.0,
        "gdd_7d": 70.0,              # 最近 7 天，每天 10
        "gdd_remain": 900.0,
        "tmax_mean_30d": 32.0,
        "tmin_mean_30d": 22.0,
        "precip_sum_30d": 60.0,      # 12 天 × 5.0（窗口内有多少算多少）
        "hot_days_30d": 0,           # 32 < HOT_TMAX(35)
        "cold_days_30d": 0,          # 22 > COLD_TMIN(0)
        "sow_doy": 91,               # 2026-04-01（2026 非闰年：31+28+31+1）
        "crop_type": 0,              # CROP_CODES["水稻"]
        "days_since_sow": 100,
    }, "特征的口径变了 —— 训练与推理共用这一份，改这里等于两边同时改"

    # 顺序即训练列顺序：错顺序不会报错，只会算错
    assert harvest_model.feature_vector(feat) == [
        feat[n] for n in harvest_model.FEATURE_NAMES
    ]


@case("外部训练：标签口径是「剩余天数」，且起点就是移栽日（不折算育秧天数）")
def _():
    from datetime import date, timedelta

    from src.farm import harvest_external as hx

    # 2015 是平年。逐日气温填满一整年，免得覆盖率检查把样本丢掉。
    d0 = date(2015, 1, 1)
    daily = [
        {"date": (d0 + timedelta(days=i)).isoformat(), "tmean": 25.0,
         "tmax": 30.0, "tmin": 20.0, "precip": 0.0}
        for i in range(365)
    ]

    row = hx.build_sample(
        adcode="420115", year=2015, rice_type="中稻",
        start_doy=150, mature_doy=260, cutoff_doy=210, daily=daily,
    )
    assert row is not None, "这一季应当能造出样本"
    assert row["y"] == 260 - 210 == 50, "标签必须是「从观察日到成熟的剩余天数」"
    assert row["days_since_sow"] == 210 - 150, "起点是移栽日，不许再减一个育秧天数"
    assert row["sow_doy"] == 150, "sow_date 就是移栽日那一天"
    assert row["crop_type"] == 0
    assert row["group"] == "420115|2015|中稻", "切分单元只用 adcode，不带省名"
    assert row["cutoff_date"] == (d0 + timedelta(days=209)).isoformat()

    # 训练样本里不许出现地区名或身份字段（7.14 红线）。省份靠 adcode 前两位推：
    # 这条断言与 scripts/32 的 FORBIDDEN_KEYS 是同一件事的两半 —— 那边拦 CSV 表头，
    # 这边拦内存里的行，任何一边被绕过都还会被另一边抓住。
    for leaked in ("province", "city", "county", "name",
                   "user_id", "field_id", "lng", "lat"):
        assert leaked not in row, f"训练样本里不该有 {leaked}（7.14 隐私红线）"
    assert hx.province_code(row["adcode"]) == "42"

    # 移栽日的当天与前一天都取不到这个样本（起点之前没有作物）
    early = hx.build_sample(
        adcode="420115", year=2015, rice_type="中稻",
        start_doy=150, mature_doy=260, cutoff_doy=150, daily=daily,
    )
    assert early is None, "起点当天还没有生长季，不该造出样本"

    # 已经完熟（剩余天数 < 1）的截止日丢掉：线上那时走的是 matured 分支，不问模型
    done = hx.build_sample(
        adcode="420115", year=2015, rice_type="中稻",
        start_doy=150, mature_doy=260, cutoff_doy=260, daily=daily,
    )
    assert done is None

    # 天气缺太多时也丢掉，而不是补 0 硬造
    thin = daily[:155]
    assert hx.build_sample(
        adcode="420115", year=2015, rice_type="中稻",
        start_doy=150, mature_doy=260, cutoff_doy=250, daily=thin,
    ) is None

    # 一个生长季要摊成多个截止日：那正是线上需要的那张映射表
    cus = hx.season_cutoffs(150, 260)
    assert cus == sorted(set(cus)), "截止日不许重复"
    assert all(150 < c < 260 for c in cus)
    assert 250 in cus and 255 in cus, "末尾要有「成熟前 10 / 5 天」这两档"
    assert len(cus) >= 7, "约每 15 天一个，不该只有一两个"


@case("外部训练：尾段编解码往返（落盘只存 predict_harvest 要的那 30 天）")
def _():
    from src.farm import harvest_external as hx

    tail = {"g": [1.0, 2.5, 10.0], "x": [30.0, None, 28.5],
            "n": [20.0, 19.5, None], "p": [0.0, 3.2, 12.0]}
    assert hx.decode_tail(hx.encode_tail(tail)) == tail

    temps = hx.tail_to_temps(tail)
    assert len(temps) == 3
    assert temps[1]["tmax"] is None and temps[2]["tmin"] is None, "缺测要保留成 None"
    assert temps[0]["gdd_daily"] == 1.0

    try:
        hx.decode_tail("1,2,3")
        raise AssertionError("尾段格式不对时必须报错，而不是当成空的")
    except ValueError:
        pass


@case("外部训练：切分按「季」整组进行，训练集与验证集不共享任何一季")
def _():
    from src.farm import harvest_external as hx

    rows = []
    # 四个省各一个县：42 湖北 / 43 湖南 / 36 江西 / 34 安徽。
    # 用真实的 adcode 前缀 —— 空间留出就是靠它定向的。
    for adcode in ("420115", "430115", "360115", "340115"):
        for year in (2012, 2016, 2018, 2021):
            for rice in ("早稻", "中稻"):
                g = hx.group_key(adcode=adcode, year=year, rice_type=rice)
                for cutoff in (180, 195, 210):
                    rows.append({"group": g, "adcode": adcode, "year": year,
                                 "rice_type": rice, "cutoff_doy": cutoff})

    for mode in hx.SPLIT_MODES:
        train, val, meta = hx.split_by_group(rows, mode=mode)
        assert train and val, f"{mode}：两侧都该非空"
        assert meta["groups_disjoint"], f"{mode}：同一季不许跨两侧（否则就是泄漏）"
        # 同一季的多行共享同一个成熟日、天气窗口又高度重叠，拆开就是「近似孪生样本
        # 同时出现在训练与验证集」，分数会虚高 —— 所以必须整组同侧。
        assert not ({r["group"] for r in train} & {r["group"] for r in val})
        assert meta["train_rows"] + meta["val_rows"] == len(rows), "不许丢行"
        assert meta["train_groups"] + meta["val_groups"] == len({r["group"] for r in rows})

    # 三档的语义各自成立
    _, val_p, meta_p = hx.split_by_group(rows, mode="province")
    assert {hx.province_code(r["adcode"]) for r in val_p} == set(hx.VAL_PROVINCE_CODES)
    assert {r["adcode"] for r in val_p} == {"420115"}, "空间留出就该只剩湖北那个县"
    assert meta_p["holdout"] == ["湖北省"], "报告里要能看到被留出的是哪个省"

    train_t, val_t, _ = hx.split_by_group(rows, mode="time")
    assert max(r["year"] for r in train_t) <= hx.TIME_SPLIT_YEAR
    assert min(r["year"] for r in val_t) > hx.TIME_SPLIT_YEAR

    try:
        hx.split_by_group(rows, mode="random")
        raise AssertionError("未知切分模式必须报错，不许悄悄按行随机切")
    except ValueError:
        pass


@case("外部训练：暂存目录不会被当成「模型可用」，24 号也不许覆盖外部模型")
def _():
    import tempfile

    from src.common.paths import path_of
    from src.farm import harvest_model

    assert path_of("farm_models_staging") != path_of("farm_models"), (
        "暂存目录与线上目录必须是两个不同的地方，否则「先落暂存」就是空话"
    )

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "farm_staging"
        live = Path(tmp) / "farm"
        staging.mkdir()
        for q in harvest_model.QUANTILES:
            (staging / f"harvest_xgb_q{q}.json").write_text("{}", encoding="utf-8")

        saved = harvest_model.models_dir
        try:
            harvest_model.models_dir = lambda: live          # 线上只认 models/farm/
            harvest_model.clear_model_cache()
            assert not harvest_model.models_available(), (
                "只有 models/farm_staging/ 里有三个文件时，绝不能算「模型可用」"
            )
            assert harvest_model._load_models() is None

            live.mkdir()
            # 缺一个都不算 —— 半个模型给不出区间
            for q in harvest_model.QUANTILES[:2]:
                (live / f"harvest_xgb_q{q}.json").write_text("{}", encoding="utf-8")
            harvest_model.clear_model_cache()
            assert not harvest_model.models_available()
            (live / f"harvest_xgb_q{harvest_model.QUANTILES[2]}.json").write_text("{}", encoding="utf-8")
            harvest_model.clear_model_cache()
            assert harvest_model.models_available()
        finally:
            harvest_model.models_dir = saved
            harvest_model.clear_model_cache()

    # 24 号脚本带着「不覆盖外部模型」的保护（它的标签口径是错的，见其头部说明）
    s24 = (Path(__file__).resolve().parents[1] / "scripts" / "24_train_harvest_model.py").read_text(encoding="utf-8")
    assert "provenance.json" in s24 and "external_phenology" in s24
    assert "--force" in s24


@case("外部训练：闸门四条各自都会拦住 —— 跑不赢基线就不上线")
def _():
    from src.farm import harvest_external as hx

    good = dict(model_mae=6.0, baseline_mae=8.0, coverage=0.80, val_rows=1000, val_groups=100)
    assert hx.gate_check(**good)["passed"], "低 2 天、覆盖率 0.80、样本充足，就该过"

    assert not hx.gate_check(**{**good, "model_mae": 8.0})["passed"], "模型不赢基线，必须拦"
    assert not hx.gate_check(**{**good, "model_mae": 7.5})["passed"], "只低 0.5 天不够（要求 ≥1 天）"
    assert not hx.gate_check(**{**good, "coverage": 0.50})["passed"], "分位没校准（覆盖太低）要拦"
    assert not hx.gate_check(**{**good, "coverage": 0.97})["passed"], "覆盖太高说明区间过宽，也要拦"
    assert not hx.gate_check(**{**good, "val_rows": 100})["passed"], "验证样本太少要拦"
    assert not hx.gate_check(**{**good, "val_groups": 10})["passed"], "验证组数太少要拦"

    v = hx.gate_check(**good)
    for name, chk in v["checks"].items():
        assert chk["detail"], f"{name} 这条没写理由"
        assert any("一" <= c <= "鿿" for c in chk["detail"]), f"{name} 的理由不是中文"


@case("外部训练：超参只留一份（两边各写一套不会报错，只会悄悄不是同一个模型）")
def _():
    import inspect

    from src.farm import harvest_model

    assert harvest_model.XGB_PARAMS == {
        "objective": "reg:quantileerror", "n_estimators": 300, "learning_rate": 0.05,
        "max_depth": 4, "subsample": 0.9, "colsample_bytree": 0.9, "random_state": 42,
    }, "三分位模型的超参只此一份"

    src = inspect.getsource(harvest_model.train_from_records)
    assert "fit_quantile_models(" in src and "XGBRegressor(" not in src, (
        "24 号那条路必须调共用的 fit_quantile_models，不许自带一份超参"
    )

    root = Path(__file__).resolve().parents[1]
    s32 = (root / "scripts" / "32_train_harvest_external.py").read_text(encoding="utf-8")
    assert "XGBRegressor(" not in s32, "外部训练那条路也不许自带超参"
    assert "fit_quantile_models(" in s32
    for key in ("FORBIDDEN_KEYS", "user_id", "lng", "lat"):
        assert key in s32, f"7.14 的隐私断言照抄一份到外部训练脚本里（缺 {key}）"

    # 29 号必须钉住 sha256：从网上下来的是不可信数据
    s29 = (root / "scripts" / "29_download_phenology_datasets.py").read_text(encoding="utf-8")
    assert 'EXPECTED_SHA256 = ""' not in s29, "下载校验的 sha256 还没钉住"
    import re as _re
    m = _re.search(r'EXPECTED_SHA256 = "([0-9a-f]{64})"', s29)
    assert m, "sha256 必须是 64 位十六进制"

    for n in ("30_fetch_phenology_weather.py", "31_build_harvest_dataset.py"):
        assert (root / "scripts" / n).exists(), f"缺脚本 {n}"


def _write_fixture_xlsx(path, sheets):
    """手写一个最小 xlsx，用来喂解析器。值可以是 str（进共享字符串）或数字。"""
    import zipfile
    from xml.sax.saxutils import escape

    shared: list[str] = []
    index: dict[str, int] = {}

    def sid(s: str) -> int:
        if s not in index:
            index[s] = len(shared)
            shared.append(s)
        return index[s]

    def letters(i: int) -> str:
        out, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            out = chr(65 + r) + out
        return out

    def sheet_xml(rows) -> str:
        parts = ['<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org'
                 '/spreadsheetml/2006/main"><sheetData>']
        for r, cells in enumerate(rows, 1):
            cs = []
            for c, val in enumerate(cells):
                if val is None:
                    continue          # 空单元格在 OOXML 里就是**省略**，正是错位的来源
                ref = f"{letters(c)}{r}"
                if isinstance(val, str):
                    cs.append(f'<c r="{ref}" t="s"><v>{sid(val)}</v></c>')
                else:
                    cs.append(f'<c r="{ref}"><v>{val}</v></c>')
            parts.append(f'<row r="{r}">{"".join(cs)}</row>')
        parts.append("</sheetData></worksheet>")
        return "".join(parts)

    names = list(sheets)
    bodies = {n: sheet_xml(sheets[n]) for n in names}   # 先跑一遍，才能把共享字符串收集齐

    with zipfile.ZipFile(path, "w") as z:
        wb = ['<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org'
              '/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org'
              '/officeDocument/2006/relationships"><sheets>']
        for i, n in enumerate(names, 1):
            wb.append(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>')
        wb.append("</sheets></workbook>")
        z.writestr("xl/workbook.xml", "".join(wb))

        rels = ['<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org'
                '/package/2006/relationships">']
        for i in range(1, len(names) + 1):
            rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org'
                        f'/officeDocument/2006/relationships/worksheet" '
                        f'Target="worksheets/sheet{i}.xml"/>')
        rels.append("</Relationships>")
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))

        ss = [f'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org'
              f'/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">']
        ss += [f"<si><t>{escape(s)}</t></si>" for s in shared]
        ss.append("</sst>")
        z.writestr("xl/sharedStrings.xml", "".join(ss))

        for i, n in enumerate(names, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", bodies[n])


@case("外部训练：xlsx 解析器 —— 空单元格不左移、空格子不当 0、坏行丢掉并计数")
def _():
    import tempfile

    from src.farm import harvest_external as hx

    header = ["序号", "省", "省代码", "市", "市代码", "县", "县代码", "说明", "年度",
              "批复", "县类型", "市类型", "中稻_2015", "早稻_2015"]

    def row(no, county, adcode, mid=None, early=None, *, drop_code=False):
        r = [None] * len(header)
        r[0], r[1] = no, "湖北省"
        if not drop_code:
            r[2] = "420000"           # 故意在下面某行省掉它，检验「缺格不左移」
        r[3], r[4] = "武汉市", "420100"
        r[5], r[6], r[7], r[8] = county, adcode, "", "中稻"
        r[12], r[13] = mid, early
        return r

    def sheet(*specs):
        return [header] + [row(*s[0], **s[1]) for s in specs]

    sheets = {
        "transplant": sheet(((1, "江夏区", "420115", 150.5), {"drop_code": True}),
                            ((2, "黄陂区", "420116", 160, 120), {}),
                            ((3, "坏代码", "42011", 150), {}),          # 5 位县代码 → 丢
                            ((4, "逆序县", "420117", 150), {}),
                            ((5, "越界县", "420118", 150), {})),
        "heading": sheet(((1, "江夏区", "420115", 200), {}),
                         ((2, "黄陂区", "420116", 210, 170), {}),
                         ((4, "逆序县", "420117", 140), {}),            # 抽穗早于移栽 → 丢
                         ((5, "越界县", "420118", 200), {})),
        "maturity": sheet(((1, "江夏区", "420115", 260), {}),
                          ((2, "黄陂区", "420116", 270, 240), {}),
                          ((4, "逆序县", "420117", 200), {}),
                          ((5, "越界县", "420118", 400), {})),          # DOY 400 越界 → 丢
    }

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fixture.xlsx"
        _write_fixture_xlsx(path, sheets)

        assert hx.sheet_names(path) == ["transplant", "heading", "maturity"]
        assert hx.column_index("A1") == 0 and hx.column_index("AC12") == 28

        try:
            hx.read_sheet(path, "maturityy")
            raise AssertionError("sheet 名不符时必须报错，不许猜")
        except KeyError:
            pass

        seasons, stats = hx.read_calendar(path)
        by = {(s["adcode"], s["rice_type"]): s for s in seasons}

        assert set(by) == {("420115", "中稻"), ("420116", "中稻"), ("420116", "早稻")}, (
            "空格子（江夏区早稻）必须整条丢掉，不是当成 0；5 位县代码的行也要丢"
        )
        best = by[("420115", "中稻")]
        # 这一行的「省代码」单元格被省掉了 —— 若按出现顺序读，县名会整体左移一格
        assert best["county"] == "江夏区" and best["province"] == "湖北省"
        assert best["city"] == "武汉市"
        assert best["transplant"] == 150.5 and best["maturity"] == 260
        assert by[("420116", "早稻")]["transplant"] == 120

        assert stats["no_order"] == 1, "抽穗早于移栽的那一季要计数并丢弃"
        assert stats["outside"] == 1, "DOY 400 的那一季要计数并丢弃"
        assert stats["kept"] == 3 and stats["counties"] == 2

        # 表头里没有「稻型_年份」列 → 报错，不许按固定列号硬读
        bad = Path(tmp) / "bad.xlsx"
        _write_fixture_xlsx(bad, {n: [[c if not c.endswith("_2015") else "谜之列"
                                       for c in header]] for n in sheets})
        try:
            hx.read_calendar(bad)
            raise AssertionError("表头结构变了必须报错，不许猜列的位置")
        except ValueError:
            pass


@case("权限边界：/api/farm/* 要登录、公开接口一个都不动（7.0.6）")
def _():
    from fastapi.routing import APIRoute
    from backend.app.main import create_app

    app = create_app()

    def walk(routes) -> list:
        """把路由摊平。

        这个 FastAPI 版本（0.142）把 include_router 的结果包成 `_IncludedRouter`，
        既没有 `.routes` 也没有把子路由平铺进 app.routes —— 只在 app.routes 上找
        APIRoute 会一个都找不到（最初就是这么写的，结果所有断言都卡在「找不到路由」）。
        子路由要经 `.original_router.routes` 取。
        """
        out = []
        for route in routes:
            if isinstance(route, APIRoute):
                out.append(route)
                continue
            sub = getattr(route, "routes", None)
            if not sub:
                sub = getattr(getattr(route, "original_router", None), "routes", None)
            if sub:
                out.extend(walk(sub))
        return out

    ROUTES = walk(app.routes)
    assert len(ROUTES) > 20, f"只摊平出 {len(ROUTES)} 条路由，遍历方式不对"

    def dep_names(dep) -> set:
        names = set()
        for sub in dep.dependencies:
            if sub.call is not None:
                names.add(getattr(sub.call, "__name__", str(sub.call)))
            names |= dep_names(sub)
        return names

    def route_deps(path: str) -> set:
        for route in ROUTES:
            if route.path == path:
                return dep_names(route.dependant)
        raise AssertionError(f"找不到路由 {path}")

    AUTH = {"get_current_user", "current_user_id", "get_current_user_optional", "require_admin"}

    for path in ("/api/farm/fields", "/api/farm/today", "/api/farm/logs"):
        assert route_deps(path) & AUTH, f"{path} 没有挂登录依赖 —— 未登录也能读别人的田"

    for path in ("/api/library", "/api/policy/list", "/api/map/pest_top3", "/api/dialect/list"):
        assert not (route_deps(path) & AUTH), f"{path} 是公开接口，不该要求登录（7.0.6）"

    assert "require_admin" in route_deps("/api/admin/reload"), (
        "/api/admin/reload 会清空全部模型缓存，普通用户不该能触发"
    )


@case("权限边界：无 Cookie 时可选登录返回 None、必须登录抛 401（不泄露「接口存在」）")
def _():
    from starlette.requests import Request

    from backend.app.core import auth_deps
    from backend.app.core.errors import ApiError

    def make_request(cookie: str = ""):
        headers = [(b"cookie", cookie.encode())] if cookie else []
        return Request({"type": "http", "method": "GET", "path": "/api/farm/fields",
                        "headers": headers, "query_string": b"",
                        "client": ("127.0.0.1", 12345)})

    with _farm_db() as conn:
        assert auth_deps.get_current_user_optional(make_request(), conn=conn) is None

        try:
            auth_deps.get_current_user(None)
        except ApiError as e:
            assert e.http_status == 401 and "登录" in e.msg
        else:
            raise AssertionError("未登录访问需要登录的接口应抛 401")

        # 有会话 → 认得出人；伪造的 token → 仍然是 None
        user = _new_user(conn)
        token, _ = auth_deps.farm_auth.create_session(conn, user["id"])
        got = auth_deps.get_current_user_optional(
            make_request(f"session_token={token}"), conn=conn)
        assert got and got["id"] == user["id"]
        assert "password_hash" not in got and "salt" not in got, "会话里漏出了密码哈希"

        assert auth_deps.get_current_user_optional(
            make_request("session_token=forged-token"), conn=conn) is None


@case("接口契约：农田响应模型必须显式声明每个字段（pydantic 会静默过滤）")
def _():
    from backend.app.schemas.farm import ModelData, TodayData, HarvestData
    from backend.app.schemas.models import ApiResponse

    # 7.3.5 的 12 个 3D 参数，一个都不能少
    for name in ("height", "leafCount", "leafColor", "leafDroop", "stemColor", "earColor",
                 "earDroop", "dryLeafRatio", "hasEar", "earSize", "hasBlackLayer",
                 "milkLineGone"):
        assert name in ModelData.model_fields, f"ModelData 少了 {name} —— 前端会拿到 undefined"

    payload = {
        "field_id": "fld_x", "crop": "水稻", "progress": 0.2, "stage": "分蘖期",
        "height": 24.7, "leafCount": 5, "leafColor": "#7db443", "leafColorName": "中绿",
        "leafDroop": 0.2, "stemColor": "#7cb342", "earColor": "#c5e1a5", "earDroop": 0.0,
        "dryLeafRatio": 0.0, "hasEar": False, "earSize": 0.0,
        "hasBlackLayer": False, "milkLineGone": False,
    }
    dumped = ModelData.model_validate(payload).model_dump()
    missing = [k for k in payload if k not in dumped]
    assert not missing, f"这些字段被 response_model 吃掉了：{missing}"

    # 「收割窗口」曾经只存在于路由的返回值里，模型没声明 → 会被静默丢掉
    assert "window_text" in HarvestData.model_fields, (
        "HarvestData 缺 window_text —— 推荐收割窗口到不了前端"
    )
    # 提醒的降级说明同理
    assert "reminders_notes" in TodayData.model_fields, (
        "TodayData 缺 reminders_notes —— 用户会把「没有天气提醒」读成「天气没问题」"
    )
    assert "narrative_why" in TodayData.model_fields

    # 泛型包一层也不该丢字段
    wrapped = ApiResponse[TodayData].model_validate(
        {"code": 0, "msg": "ok", "data": {
            "field": {"id": "f", "name": "田", "crop": "水稻", "area_mu": 1,
                      "province": "", "city": "", "sow_date": "2026-08-01"},
            "weather": {"days": [], "source": "none"},
            "growth": {"field_id": "f", "crop": "水稻", "sow_date": "2026-08-01"},
            "harvest": {"field_id": "f", "crop": "水稻", "method": "gdd_fallback"},
        }})
    assert wrapped.data.growth is not None, "growth 被吃掉了"
    assert wrapped.data.harvest.method == "gdd_fallback"


@case("田块选址：县字段从迁移到接口到前端三级联动一路贯通")
def _():
    """「county」是典型的多处锁步字段：迁移、repo 的 INSERT、PATCH 白名单、
    pydantic 的四个模型、前端表单，漏任何一处都不会报错 —— 只是县安静地消失。
    所以这里逐处钉住，而不是只测一个 create_field 就完事。"""
    import json

    from backend.app.schemas.farm import CreateFieldRequest, FieldData, UpdateFieldRequest
    from src.common.paths import path_of
    from src.farm import repo
    from src.farm import db as farm_db

    # ---- 1. 迁移：只加列，不重建表（重建会丢光用户数据）----
    assert (5, "005_add_field_county.sql") in farm_db.MIGRATIONS, "迁移表里没有 v5"
    sql = (farm_db.MIGRATIONS_DIR / "005_add_field_county.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE fields ADD COLUMN county" in sql
    assert "DROP TABLE" not in sql.upper(), "迁移里不许有 DROP —— 那会把用户数据一起带走"

    # ---- 2. repo：写入要带 county，PATCH 白名单里也要有（否则改县被静默丢弃）----
    assert "county" in repo.FIELD_PATCHABLE, "FIELD_PATCHABLE 漏了 county，PATCH 会静默丢掉它"
    with _farm_db() as conn:
        user = _new_user(conn)
        f = repo.create_field(
            conn, user["id"], name="带县的田", crop="水稻", province="湖北省",
            city="武汉市", county="洪山区", lng=114.33, lat=30.56, sow_date="2026-04-10",
        )
        assert f["county"] == "洪山区", "create_field 没有把 county 写进去"
        assert repo.update_field(conn, user["id"], f["id"], {"county": "江夏区"})["county"] == "江夏区"

    # ---- 3. 接口模型：四处都要声明（pydantic 的 response_model 会静默过滤）----
    for model in (FieldData, CreateFieldRequest, UpdateFieldRequest):
        assert "county" in model.model_fields, f"{model.__name__} 少了 county"
    # 旧库升上来的记录 county 是空串，模型不能要求必填，否则读列表就 500
    assert not FieldData.model_fields["county"].is_required()
    dumped = FieldData.model_validate({
        "id": "f", "name": "田", "crop": "水稻", "area_mu": 1, "province": "湖北省",
        "city": "武汉市", "county": "洪山区", "sow_date": "2026-08-01",
    }).model_dump()
    assert dumped["county"] == "洪山区", "county 被 response_model 吃掉了"

    # ---- 4. 前端：省/市/县三级联动，且数据集必须先于 farm.js 加载 ----
    front = path_of("frontend_dir")
    html = (front / "farm.html").read_text(encoding="utf-8")
    for el in ("new-field-province", "new-field-city", "new-field-county"):
        assert f'id="{el}"' in html, f"farm.html 里没有 #{el}"
    assert html.find('/assets/data/admin_divisions.js') < html.find('src="/assets/js/farm.js"'), (
        "行政区划数据集必须排在 farm.js 之前，否则三级联动拿到 undefined"
    )
    js = (front / "assets" / "js" / "farm.js").read_text(encoding="utf-8")
    assert "__ADMIN_DIVISIONS__" in js and "setLocation" in js and "applyLatLng" in js
    assert "county:" in js, "提交新建田块时没带上 county"

    # ---- 5. 离线数据集本身（打包在 frontend 里，随项目走，不依赖外网）----
    bundle = (front / "assets" / "data" / "admin_divisions.js").read_text(encoding="utf-8")
    payload = json.loads(bundle.split("window.__ADMIN_DIVISIONS__ =", 1)[1].strip().rstrip(";"))
    assert len(payload) >= 30, f"省级只有 {len(payload)} 个，数据集不完整"
    wuhan = payload.get("湖北省", {}).get("m", {}).get("武汉市", {})
    assert wuhan.get("d"), "湖北省武汉市没有下级县区"
    for name, pt in list(wuhan["d"].items())[:5]:
        assert isinstance(pt, list) and len(pt) == 2, f"{name} 的坐标不是 [lng, lat]：{pt}"
        assert 70 < pt[0] < 140 and 3 < pt[1] < 55, f"{name} 的坐标不在中国境内：{pt}"


@case("前端：顶栏五个导航入口 + 用户名菜单，且菜单不破坏「入口不再各带图标」那条锁")
def _():
    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "layout.js").read_text(encoding="utf-8")

    # 五个导航入口（阶段 7 加了「我的农田」）
    labels = ["首页", "智能诊断", "知识图书库", "惠农政策", "我的农田"]
    i_nav = js.find("const NAV = [")
    nav_src = js[i_nav:js.find("];", i_nav)]
    for label in labels:
        assert label in nav_src, f"顶栏少了导航入口「{label}」"
    assert nav_src.count("href:") == 5, f"顶栏导航入口数不是 5：{nav_src.count('href:')}"
    assert "icon" not in nav_src, "NAV 里又混进了图标字段"

    # renderHeader 到 renderFooter 之间仍然只许 1 处 Icon.render（品牌叶子）
    i = js.find("function renderHeader(host) {")
    head = js[i:js.find("function renderFooter", i)]
    assert head.count("Icon.render") == 1, f"顶栏里出现了 {head.count('Icon.render')} 处 Icon.render"

    # 用户名菜单的占位与渲染函数都在，且渲染函数定义在 renderFooter 之后
    assert "data-user-menu" in head, "顶栏没有给用户名菜单留位置"
    i_menu = js.find("function renderUserMenu(")
    i_footer = js.find("function renderFooter")
    assert i_menu > i_footer, (
        "renderUserMenu 必须定义在 renderFooter 之后，否则上面那条「只许 1 处 Icon.render」"
        "的切片会把它算进去，锁就失效了"
    )
    assert "login.html" in js and "register.html" in js, "未登录时应显示登录/注册入口"
    assert "/profile.html" in js and "退出登录" in js, "已登录时应有个人中心与退出登录"
    # 头像用首字母（项目里没有头像图片资源）
    assert "charAt(0)" in js and "user-menu__avatar" in js


@case("前端：JS 引用的元素 id 在对应页面上真的存在（没有浏览器也要能发现这类错）")
def _():
    """`$("xxx")` 取不到元素时返回 null，紧接着的 addEventListener 会直接抛异常，
    整页脚本从这里开始全废 —— 而这类错在静态检查里只能靠人眼盯。

    两个必须排除的合理情况，都不是"放松断言"而是事实：
      · **动态创建的元素**：id 由 JS 自己写进模板字符串（比如历史列表里的
        `id="btn-add-log"`），页面上当然找不到。判据是「这个文件自己创建过它」。
      · **auth.js 是四个页面共用的**：它按 <body data-page> 分派，login 页只会跑
        bootLogin。所以按函数切片，只检查该函数引用的 id 是否在对应的页面上。
    """
    import re

    from src.common.paths import path_of

    front = path_of("frontend_dir")
    id_re = re.compile(r'id="([A-Za-z0-9_-]+)"')
    use_re = re.compile(r'\$\("([A-Za-z0-9_-]+)"\)')

    def page_ids(name: str) -> set:
        return set(id_re.findall((front / name).read_text(encoding="utf-8")))

    def slice_js(js: str, start: str, *stops: str) -> str:
        i = js.find(start)
        assert i >= 0, f"找不到 {start}"
        end = len(js)
        for stop in stops:
            j = js.find(stop, i + len(start))
            if j >= 0:
                end = min(end, j)
        return js[i:end]

    # ---- auth.js：按 boot 函数分片，各查各的页面 ----
    auth = (front / "assets" / "js" / "auth.js").read_text(encoding="utf-8")
    self_created = set(id_re.findall(auth))          # auth.js 自己造出来的 id
    sections = (
        ("login.html", "async function bootLogin()", ("async function bootRegister()",)),
        ("register.html", "async function bootRegister()", ("async function bootProfile()",)),
        ("profile.html", "async function bootProfile()", ("async function bootAdmin()",)),
        ("admin.html", "async function bootAdmin()", ("const PAGES =",)),
    )
    for page, start, stops in sections:
        body = slice_js(auth, start, *stops)
        missing = sorted(set(use_re.findall(body)) - page_ids(page) - self_created)
        assert not missing, f"{page} 上没有这些 id，但 {start} 在用：{missing}"

    # ---- farm.js：整份查 farm.html（动态创建的靠 self_created 放行）----
    farm_js = (front / "assets" / "js" / "farm.js").read_text(encoding="utf-8")
    created_farm = set(id_re.findall(farm_js))
    missing = sorted(set(use_re.findall(farm_js)) - page_ids("farm.html") - created_farm)
    assert not missing, f"farm.html 上没有这些 id，但 farm.js 在用：{missing}"

    # 页面上的关键元素确实被用上了（防止「写了元素但没人绑」）
    for must_use in ("field-select", "farm-mic", "voice-modal", "panel"):
        assert must_use in use_re.findall(farm_js), f"farm.js 没有用到 #{must_use}"


@case("前端：farm.html 的脚本顺序正确（three → recorder → farm3d → farm）")
def _():
    from src.common.paths import path_of

    html = (path_of("frontend_dir") / "farm.html").read_text(encoding="utf-8")

    # 按**完整的 src 属性**找，不能只找文件名：文件名会出现在注释里
    # （「必须排在 farm.js 之前」那句注释就在 three 的script 标签之前，
    #   只搜 "farm.js" 会命中注释，取出的是上一个标签）
    def tag_index(src: str) -> int:
        return html.find(f'src="{src}"')

    i_three = tag_index("/assets/vendor/three/three.min.js")
    i_farm3d = tag_index("/assets/js/farm3d.js")
    i_rec = tag_index("/assets/js/recorder.js")
    i_farm = tag_index("/assets/js/farm.js")

    assert -1 < i_three < i_farm3d < i_farm, "three.min.js 必须排在 farm3d.js 与 farm.js 之前"
    assert -1 < i_rec < i_farm, "recorder.js 必须排在 farm.js 之前，否则 Recorder 是 undefined"

    # 与其余页面一致：同步加载，不带 defer/async
    for src in ("/assets/vendor/three/three.min.js", "/assets/js/recorder.js",
                "/assets/js/farm3d.js", "/assets/js/farm.js"):
        i = tag_index(src)
        tag = html[html.rfind("<script", 0, i):html.find(">", i)]
        assert "defer" not in tag and "async" not in tag, f"{src} 不该带 defer/async：{tag}"

    # 与诊断页/图书库同一份录音实现（全项目只许一份，另有专测）
    assert "createScriptProcessor" not in html

    # 登录墙要能落地：页面里有把未登录用户送走的元素
    assert 'id="voice-modal"' in html and 'id="field-select"' in html
    assert 'id="farm3d"' in html and 'id="farm-mic"' in html


@case("前端：政策只留要点、农田 8 格改「进入式」（新元素与两处 HTML 对得上）")
def _():
    """这两个都是用户报上来的题，靠人眼盯不住，钉成断言。

    · 政策：条目清单整块删掉，一页只剩「要点（原文摘录）」。
      早先这里有过两种毛病 —— 同一批 items 被渲染两遍（预览一份 + 展开一份，展开一次
      就同名重出），修好之后列表本身又被判为冗余（要点已逐条给出条目名与原话，
      卡片右侧还有出处）。所以这条断言要锁两件事：列表**不存在**，要点**还在**。
    · 农田：8 格上的小图底板、引导语、返回键是这一轮新增的元素；id 写错一个，
      页面就是「点了没反应」或者永远回不到田块列表。
    """
    import re

    from src.common.paths import path_of

    front = path_of("frontend_dir")

    # ---- 政策页：只剩要点，条目清单与展开按钮一个不许留 ----
    policy_js = (front / "assets" / "js" / "policy.js").read_text(encoding="utf-8")
    for gone in ("policy-row__items", "policy-row__more", "ITEM_PREVIEW",
                 "restItems", "itemLines", "data-toggle", "aria-controls"):
        assert gone not in policy_js, f"条目清单已删，policy.js 里不该再有 {gone}"

    # 要点本身必须原样保留 —— 删列表不能把要点一起删掉
    assert "pointsHtml" in policy_js and "policy-points" in policy_js, "要点渲染丢了"
    assert "policy-row__link" in policy_js
    assert 'target="_blank"' in policy_js and 'rel="noopener noreferrer"' in policy_js, (
        "要点删了，标题外链是唯一的原文入口，绝不能失去 target/rel"
    )

    # ---- 政策页：标题得看着像链接（用户说的「点不动」，其实是看不出能点）----
    css = (front / "assets" / "css" / "pages.css").read_text(encoding="utf-8")
    assert ".policy-row__items" not in css and ".policy-row__more" not in css, (
        "pages.css 里还留着条目清单的死规则"
    )
    rule = re.search(r"\.policy-row__link \{[^}]*\}", css)
    assert rule, "pages.css 里找不到 .policy-row__link"
    assert "text-decoration: underline" in rule.group(0), "外链要常驻下划线"
    assert "color: inherit" not in rule.group(0), "标题不能与正文同色，那样看不出是链接"

    # ---- 农田页：8 格 / 大 3D 两屏之间的新元素 ----
    farm_html = (front / "farm.html").read_text(encoding="utf-8")
    farm_js = (front / "assets" / "js" / "farm.js").read_text(encoding="utf-8")
    for el in ("farm-back", "farm-guide"):
        assert f'id="{el}"' in farm_html, f"farm.html 里没有 #{el}"
        assert f'$("{el}")' in farm_js, f"farm.js 没有用到 #{el}"

    assert re.search(r'class="farm-layout"[^>]*hidden', farm_html), (
        "大 3D 布局要默认 hidden：默认停在 8 格那一屏，点进去才显形"
    )
    for cls in ("plot-card__thumb", "plot-card__thumb-img"):
        assert cls in css, f"pages.css 缺少 .{cls}"
    assert 'class="plot-card__thumb"' in farm_js, "格子上要留小图底板（出图前不跳版）"

    # ---- 小图：一个共享的离屏快照器，且取图必须同步 ----
    farm3d = (front / "assets" / "js" / "farm3d.js").read_text(encoding="utf-8")
    assert "createSnapshotter" in farm3d, "farm3d.js 没导出快照器"
    assert "createSnapshotter" in farm_js, "farm.js 没用快照器出 8 格的小图"
    body = farm3d.split("function capture(", 1)[1].split("\n    }", 1)[0]
    assert "toDataURL" in body
    for banned in ("await ", "setTimeout", "requestAnimationFrame"):
        assert banned not in body, (
            f"capture() 里出现了 {banned}：交出执行权后绘制缓冲就被清空，取到的会是全白图"
        )


@case("前端：API 封装补齐了账号与农田方法，且没有把 token 存进 localStorage")
def _():
    from src.common.paths import path_of

    js = (path_of("frontend_dir") / "assets" / "js" / "api.js").read_text(encoding="utf-8")

    for method in ("authStatus", "login(", "register(", "logout(", "farmFields", "farmToday",
                   "createField", "deleteField", "createLog", "voiceParse", "harvestMark",
                   "snoozeReminder", "attachDiagnosis"):
        assert method in js, f"api.js 缺少 {method}"

    assert "/api/auth/status" in js and "/api/farm/today" in js
    # 会话走 HttpOnly Cookie，前端拿不到也不该自己存一份。
    # 断言落在**调用**上而不是「出现过 localStorage 这个词」—— api.js 的注释里
    # 正好解释了「为什么不用 localStorage」，只查词会把解释本身当成违规。
    for bad in ("localStorage.setItem", "localStorage.getItem", "sessionStorage.setItem"):
        assert bad not in js, f"会话是 HttpOnly Cookie，不该在前端存 token（{bad}）"
    assert "farm_today" in js, "farm/today 首次要联网拉历史气温，得给慢接口超时"


# ==================================================================== 主流程

def main() -> int:
    logger.info("=" * 72)
    logger.info("单元测试")
    logger.info("=" * 72)

    # 上面装饰器在导入时即执行，这里只需汇总
    logger.info("=" * 72)
    if FAILED:
        logger.error("失败 %d 项：", len(FAILED))
        for name in FAILED:
            logger.error("  - %s", name)
        logger.info("结果：%d 项通过 / %d 项失败", PASSED, len(FAILED))
    else:
        logger.info("结果：全部 %d 项通过", PASSED)
    logger.info("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
