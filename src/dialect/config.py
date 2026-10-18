"""任务 3.4 —— 方言配置管理：支持全局动态切换八大方言。

把 configs/dialect.yaml 封装成可查询的接口。语音识别（ASR）与语音合成（TTS）
都从这里取参数，前端导航栏的下拉列表也由这里驱动（GET /api/dialect/list）。

八种方言的选取标准是「讯飞 /v2/tts 接口确实有发音人」，且每一种都用真实 Key
实测过才留下，详见 dialect.yaml 顶部注释。

两个必须知道的事实（已从百度官方文档核实，写法见 dialect.yaml 顶部注释）：
    · 短语音识别 REST 只有 普通话1537 / 英语1737 / 粤语1637 / 四川话1837 四个模型。
      其余方言没有独立模型，只能回退到 1537 —— 本模块显式暴露 fallback 与 asr_supported，
      绝不假装支持不存在的模型。
    · 大模型音库的方言音色可能需要在控制台单独开通，且部分仅在流式合成接口可用；
      因此每个方言都配了 tts_per_fallback（基础音库，全账号可用），调用失败时可降级。
"""

from __future__ import annotations

from functools import lru_cache

from src.common.config import load_dialect_config
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.dialect")

DEFAULT_KEY = "mandarin"

# 讯飞侧没有可靠方言发音人时的兜底音色（普通话）。见 xfyun_tts_params()。
XFYUN_FALLBACK_VCN = "x5_lingxiaoxuan_flow"
XFYUN_FALLBACK_NAME = "讯飞聆小璇-普通话"


@lru_cache(maxsize=1)
def _table() -> dict[str, dict]:
    cfg = load_dialect_config()
    return {d["key"]: d for d in cfg.get("dialects", [])}


@lru_cache(maxsize=1)
def default_key() -> str:
    cfg = load_dialect_config()
    return str(cfg.get("default", DEFAULT_KEY))


def list_dialects(provider: str | None = None) -> list[dict]:
    """给前端导航栏下拉用的列表（含调用参数，便于前端展示"演示/真实"状态）。

    provider 影响 asr_supported 的判定 —— 同一方言在两个 provider 下结论不同：
        · 百度：短语音识别 REST 只有普通话/粤语/四川话三个方言模型，其余 6 种回退普通话。
        · 讯飞：方言大模型用 accent="mulacc" 一个模型覆盖 202 种方言，八种全部为 True。
    默认 None 时按百度口径返回（也不改变任何既有调用方的行为）。
    """
    iflytek = provider == "iflytek"

    out = []
    for d in load_dialect_config().get("dialects", []):
        xfyun_vcn = d.get("xfyun_vcn")
        xfyun_supported = bool(d.get("xfyun_tts_supported", False)) and bool(xfyun_vcn)
        out.append({
            "key": d["key"],
            "name": d["name"],
            "baidu_name": d.get("baidu_name", d["name"]),
            "regions": d.get("regions", []),
            "asr_supported": True if iflytek else bool(d.get("asr_supported", False)),
            # 讯飞没有 dev_pid 概念（一个 mulacc 覆盖所有方言），如实留空
            "asr_dev_pid": None if iflytek else d.get("asr_dev_pid"),
            "tts_voice_name": (
                d.get("xfyun_voice_name") or XFYUN_FALLBACK_NAME
            ) if iflytek else d.get("tts_voice_name", ""),
            "tts_library": (
                "讯飞在线合成" if iflytek else d.get("tts_library", "")
            ),
            "xfyun_vcn": xfyun_vcn if xfyun_supported else None,
            "xfyun_tts_supported": xfyun_supported,
        })
    return out


def get_dialect(key: str | None) -> dict:
    """取方言配置；未知 key 回退到默认方言并记一条告警（不抛异常，避免打断诊断流程）。"""
    table = _table()
    if not key:
        key = default_key()
    if key in table:
        return table[key]

    logger.warning("未知方言 key=%r，回退到默认方言 %s", key, default_key())
    return table.get(default_key()) or next(iter(table.values()))


def asr_params(key: str | None) -> dict:
    """ASR 调用参数，明确告诉调用方是否发生了回退。"""
    import os

    d = get_dialect(key)
    dev_pid = d.get("asr_dev_pid")
    fallback = False
    if dev_pid is None:
        dev_pid = int(os.getenv("BAIDU_ASR_DEV_PID", 1537))
        fallback = True

    return {
        "dialect": d["key"],
        "dialect_name": d["name"],
        "dev_pid": int(dev_pid),
        "asr_supported": bool(d.get("asr_supported", False)),
        "asr_fallback": fallback,
        "fallback_note": (
            "该方言在百度短语音识别 REST 接口没有独立模型，已回退普通话模型（dev_pid=1537），"
            "识别结果可能不准；如需完整方言支持需改用实时语音识别 WebSocket API（dev_pid=15376）"
            if fallback else ""
        ),
    }


def tts_params(key: str | None) -> dict:
    """TTS 调用参数（发音人 per、语速/音量/音调、音库标识）。"""
    d = get_dialect(key)
    return {
        "dialect": d["key"],
        "dialect_name": d["name"],
        "per": str(d.get("tts_per", "0")),
        "per_fallback": str(d.get("tts_per_fallback", "0")),
        "voice_name": d.get("tts_voice_name", ""),
        "voice_library": d.get("tts_library", ""),
    }


# ---------------------------------------------------------------- 讯飞（方言大模型）
# 与百度最大的差别：讯飞侧**没有 dev_pid / per 这种"按方言选模型"的东西**。
#   · ASR：一个方言大模型用 accent="mulacc" 覆盖 202 种方言，八种方言调用参数完全相同。
#   · TTS：方言靠 vcn 发音人区分。
# 因此下面两个函数与上面那对（百度语义）分开，互不干扰。


def xfyun_asr_params(key: str | None) -> dict:
    """讯飞方言识别大模型的 ASR 参数。

    返回值里混了两类字段：前几项是要发给讯飞的业务参数（provider 里按
    ASR_WIRE_KEYS 挑出来），其余几项是回填给接口响应用的，保持与百度侧同名同义。
    """
    d = get_dialect(key)
    return {
        # —— 发给讯飞（parameter.iat）
        "domain": "slm",          # slm = 方言大模型
        "language": "zh_cn",
        "accent": "mulacc",       # 多方言统一模型，无需按方言切换
        "eos": 1800,              # 后端点静音判定（毫秒）
        "dwa": "wpgs",            # 动态修正：结果会分片下发，需按 pgs/rg 合并
        # 输出封装：utf8 文本 / 原始无压缩 / json。官方文档标为「非必传」（默认即这三项），
        # 但**方言大模型的服务实例不接受缺省** —— 少了它，服务端会以
        # code=10106 "wrapper output data invalid(key or type)" 直接拒收，且该报错文案
        # 里的 "output data" 很容易被误读成音频或 key/type 出错。实测：同一段音频、
        # 其余字节完全一样，只补上此节点即 code=0 正常出字。
        # 注意它属于 parameter.iat（响应字段配置），与 payload.audio.encoding（音频编码）不是一回事。
        "result": {"encoding": "utf8", "compress": "raw", "format": "json"},
        # —— 回填给接口响应
        "dialect": d["key"],
        "dialect_name": d["name"],
        "asr_supported": True,    # mulacc 覆盖全部八种方言
        "fallback": False,
        "fallback_note": "",
    }


def xfyun_tts_params(key: str | None) -> dict:
    """讯飞在线语音合成的 TTS 参数（方言靠发音人 vcn 区分）。"""
    return _xfyun_tts_of(get_dialect(key))


def _xfyun_tts_of(d: dict) -> dict:
    """由方言配置字典算出 TTS 参数（拆出来是为了能直接喂假配置测回退分支）。

    当前八种方言都已实测到发音人，所以正常配置下走不到回退分支；这个分支是给
    「以后新增方言但一时查不到发音人」留的：回退普通话音色并在返回值里显式标出，
    调用方据此在响应里如实告知用户，绝不假装播的是方言。
    """
    configured = str(d.get("xfyun_vcn") or "")
    supported = bool(d.get("xfyun_tts_supported", False)) and bool(configured)

    return {
        "dialect": d["key"],
        "dialect_name": d["name"],
        "vcn": configured if supported else XFYUN_FALLBACK_VCN,
        "vcn_fallback": XFYUN_FALLBACK_VCN,
        "voice_name": (
            d.get("xfyun_voice_name") or XFYUN_FALLBACK_NAME
        ) if supported else XFYUN_FALLBACK_NAME,
        "voice_library": "讯飞在线合成",
        "tts_supported": supported,
        "note": "" if supported else str(d.get("xfyun_note") or ""),
    }
