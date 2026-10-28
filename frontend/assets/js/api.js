/* ==========================================================================
   API 封装：统一处理 {code, msg, data} 响应包、错误提示与加载状态
   ========================================================================== */

(function () {
  "use strict";

  /** 业务错误：携带后端返回的 code 与中文 msg。 */
  class ApiError extends Error {
    constructor(msg, code, status) {
      super(msg);
      this.name = "ApiError";
      this.code = code;
      this.status = status;
    }
  }

  /* 请求超时：没有它，一个挂住的响应会让 Promise 既不 resolve 也不 reject，
     调用方的 .then/.catch 都不触发，界面就永远停在加载态（首页的加载遮罩正是这么卡住的）。 */
  const DEFAULT_TIMEOUT = 15000;

  /* 慢接口单独放宽：这些接口本来就要跑十几秒到一分钟，
     用默认值会误伤，必须比服务端自己的耗时上限更宽。 */
  const SLOW_TIMEOUT = {
    diagnose: 120000, // 首次惰性加载模型 + LLM 调用
    ask: 90000,
    tts: 30000,
    asr: 30000,       // 上传音频 + 取 token + 识别，默认的 15s 偏紧
    farm_today: 60000, // 首次要联网拉历史气温 + 算积温 + 调大模型写叙述
    farm_llm: 90000,   // 语音解析的 LLM 兜底（与大模型同源）
  };

  async function request(path, options = {}) {
    const opts = Object.assign({ method: "GET" }, options);
    const timeout = typeof opts.timeout === "number" ? opts.timeout : DEFAULT_TIMEOUT;
    delete opts.timeout;

    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
      opts.body = JSON.stringify(opts.body);
    }

    let controller = null;
    let timer = null;
    if (timeout > 0 && typeof AbortController !== "undefined") {
      controller = new AbortController();
      opts.signal = controller.signal;
      timer = window.setTimeout(() => controller.abort(), timeout);
    }

    const timeoutError = () =>
      new ApiError(`请求超时（超过 ${Math.round(timeout / 1000)} 秒无响应）`, -1, 0);

    // 计时器要到读完 body 才清 —— 只等到响应头就清掉的话，
    // 「连上了但不发数据」依然会挂住，那正是超时想防的情况。
    let res;
    let payload = null;
    try {
      res = await fetch(path, opts);
      payload = await res.json();
    } catch (e) {
      if (timer !== null) window.clearTimeout(timer);
      if (e && e.name === "AbortError") throw timeoutError();
      if (res) throw new ApiError(`服务器返回了非 JSON 响应（HTTP ${res.status}）`, -1, res.status);
      throw new ApiError("无法连接到服务器，请确认后端服务已启动", -1, 0);
    }
    if (timer !== null) window.clearTimeout(timer);

    if (payload && typeof payload.code !== "undefined" && payload.code !== 0) {
      throw new ApiError(payload.msg || "请求失败", payload.code, res.status);
    }
    if (!res.ok) {
      throw new ApiError((payload && payload.msg) || `请求失败（HTTP ${res.status}）`, -1, res.status);
    }
    return payload ? payload.data : null;
  }

  const api = {
    ApiError,

    get(path, params, options) {
      const qs = params
        ? "?" + new URLSearchParams(
            Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined)
          ).toString()
        : "";
      return request(path + qs, options);
    },

    post(path, body, options) {
      return request(path, Object.assign({ method: "POST", body: body || {} }, options));
    },

    postForm(path, formData, options) {
      return request(path, Object.assign({ method: "POST", body: formData }, options));
    },

    patch(path, body, options) {
      return request(path, Object.assign({ method: "PATCH", body: body || {} }, options));
    },

    /* DELETE 带 body 在 HTTP 语义里是灰区，但 fetch 支持，FastAPI 也照收 ——
       注销账号要求的「再验一次密码」只能放在 body 里（放 query 会被写进访问日志）。
       函数名用 del 而不是 delete：delete 是保留字，写在对象字面量里虽然合法，
       但 api.delete(...) 这种调用读起来太别扭。 */
    del(path, body, options) {
      return request(path, Object.assign({ method: "DELETE", body: body || {} }, options));
    },

    health() {
      return api.get("/api/health");
    },

    dialects() {
      return api.get("/api/dialect/list");
    },

    library(params) {
      return api.get("/api/library", params);
    },

    libraryDetail(id) {
      return api.get("/api/library/" + encodeURIComponent(id));
    },

    mapPestTop3(params) {
      return api.get("/api/map/pest_top3", params);
    },

    policies(params) {
      return api.get("/api/policy/list", params);
    },

    tts(text, dialect) {
      return api.post("/api/voice/tts", { text, dialect }, { timeout: SLOW_TIMEOUT.tts });
    },

    /* 语音转文字。知识图书库的语音检索走这条（诊断页走 multimodal，一次做完图像+语音）。
       16kHz 单声道 WAV，multipart 字段名 audio + dialect，由调用方组装 FormData。
       注意未配置语音 Key 时后端**不是报错**：返回 code 0、mock=true、文本为空、
       message 里写明未配置 —— 调用方必须显式判断，不能当成识别成功。 */
    asr(formData) {
      return api.postForm("/api/voice/asr", formData, { timeout: SLOW_TIMEOUT.asr });
    },

    /* 诊断链路首次调用要惰性加载 ONNX 模型（实测 10 s 以上），之后还有一次 LLM 调用
       （服务端 LLM_TIMEOUT 60 s）。用默认的 15 s 会把正常的诊断请求掐断，
       前端报「请求超时」而服务端其实还在算 —— 那比不设超时更糟。 */
    diagnoseMultimodal(formData) {
      return api.postForm("/api/diagnose/multimodal", formData, {
        timeout: SLOW_TIMEOUT.diagnose,
      });
    },

    ask(question, context) {
      return api.post(
        "/api/diagnose/ask",
        { question, context },
        { timeout: SLOW_TIMEOUT.ask }
      );
    },

    /* ------------------------------------------------------------ 账号（7.0）
       会话是 HttpOnly Cookie，前端**看不到也读不到** token —— 所有请求靠浏览器
       自动带上 Cookie。因此这里没有「设置 Authorization 头」这类东西，
       也不需要把 token 存 localStorage（存了反而多一个被 XSS 偷走的地方）。 */

    authStatus() {
      return api.get("/api/auth/status");
    },

    login(username, password) {
      return api.post("/api/auth/login", { username, password });
    },

    register(body) {
      return api.post("/api/auth/register", body);
    },

    logout() {
      return api.post("/api/auth/logout", {});
    },

    me() {
      return api.get("/api/auth/me");
    },

    updateMe(body) {
      return api.patch("/api/auth/me", body);
    },

    changePassword(oldPassword, newPassword) {
      return api.post("/api/auth/change_password",
        { old_password: oldPassword, new_password: newPassword });
    },

    /* 注销是不可逆的，后端要求再验一次密码（防误点） */
    deleteMe(password) {
      return api.del("/api/auth/me", { password });
    },

    /* ------------------------------------------------------------ 我的农田（7.10） */

    farmFields() {
      return api.get("/api/farm/fields");
    },

    createField(body) {
      return api.post("/api/farm/fields", body);
    },

    updateField(id, body) {
      return api.patch("/api/farm/fields/" + encodeURIComponent(id), body);
    },

    deleteField(id) {
      return api.del("/api/farm/fields/" + encodeURIComponent(id));
    },

    farmLogs(params) {
      return api.get("/api/farm/logs", params);
    },

    createLog(body) {
      return api.post("/api/farm/logs", body);
    },

    /* 信息面板一次拿齐（天气 + 生长 + 收获 + 提醒 + 历史 + 叙述）。
       首次打开要联网拉历史气温并让大模型写叙述，所以用慢接口超时。 */
    farmToday(fieldId) {
      return api.get("/api/farm/today", fieldId ? { field_id: fieldId } : null,
        { timeout: SLOW_TIMEOUT.farm_today });
    },

    /* 农药登记库检索（记打药时选药剂 / 按病害找药）。
       q 与 target 都不给时后端直接返回空 —— 三千多条一次性拉下来没有意义。 */
    pesticides(params) {
      return api.get("/api/farm/pesticides", params);
    },

    /* 用药台账：按地块 / 按药剂 / 用药史，一次拿齐 */
    farmLedger(params) {
      return api.get("/api/farm/ledger", params);
    },

    /* 这块田当前受哪次施药约束（问诊单的「本地产收提醒」用）。
       返回 null = 没有任何一条记了间隔期的施药，是「不知道」而不是「可以摘」。 */
    farmSpray(fieldId) {
      return api.get("/api/farm/spray/" + encodeURIComponent(fieldId));
    },

    farmGrowth(id) {
      return api.get("/api/farm/growth/" + encodeURIComponent(id));
    },

    /* 3D 植株的 12 个参数（7.3.5） */
    farmModel(id) {
      return api.get("/api/farm/model/" + encodeURIComponent(id));
    },

    harvestMark(fieldId, harvestDate, note) {
      return api.post("/api/farm/harvest_mark",
        { field_id: fieldId, harvest_date: harvestDate || "", note: note || "" });
    },

    /* 7.7 的「[改天]」：按 key 暂缓某条提醒 */
    snoozeReminder(fieldId, reminderKey, days) {
      return api.post("/api/farm/reminder_snooze",
        { field_id: fieldId, reminder_key: reminderKey, days: days || 2 });
    },

    attachDiagnosis(body) {
      return api.post("/api/farm/attach_diagnosis", body);
    },

    /* 问诊单交付：存一份快照换一条短链（农户扫码可看，30 天过期） */
    shareSheet(payload) {
      return api.post("/api/diagnose/share", { payload });
    },

    /** 下载问诊单 PDF。
     *
     *  不能走 request()：那条路会 `res.json()`，而 PDF 是二进制。
     *  失败时后端仍然返回 JSON 错误包，所以这里要按 Content-Type 分流 ——
     *  否则「PDF 组件未安装」这种提示会变成一句看不懂的解析异常。 */
    async sheetPdf(payload) {
      let res;
      try {
        res = await fetch("/api/diagnose/report.pdf", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ payload }),
        });
      } catch (e) {
        throw new ApiError("无法连接到服务器，请确认后端服务已启动", -1, 0);
      }
      const type = res.headers.get("Content-Type") || "";
      if (!res.ok || type.indexOf("application/pdf") < 0) {
        let msg = `生成失败（HTTP ${res.status}）`;
        try {
          const body = await res.json();
          if (body && body.msg) msg = body.msg;
        } catch (e) { /* 非 JSON 就用手上的状态码 */ }
        throw new ApiError(msg, -1, res.status);
      }
      const disposition = res.headers.get("Content-Disposition") || "";
      const m = /filename="([^"]+)"/.exec(disposition);
      return { blob: await res.blob(), filename: m ? m[1] : "sheet.pdf" };
    },

    shareQrUrl(token) {
      return "/api/share/" + encodeURIComponent(token) + "/qr.svg";
    },

    /* 回访队列（农技员的待办）。date 可传一个「假今天」，演示与测试都用得上 */
    followups(date) {
      return api.get("/api/farm/followups", date ? { date } : null);
    },

    followupDone(diagnosisId) {
      return api.post("/api/farm/followups/done", { diagnosis_id: diagnosisId });
    },

    /* 语音录入：只解析，不落库（落库要用户点确认 → createLog）。
       LLM 兜底可能慢，给慢接口超时。 */
    voiceParse(text, fieldId) {
      return api.post("/api/farm/voice_parse",
        { text, field_id: fieldId || "" }, { timeout: SLOW_TIMEOUT.farm_llm });
    },

    voiceReparse(text, fieldId) {
      return api.post("/api/farm/voice_reparse",
        { text, field_id: fieldId || "" }, { timeout: SLOW_TIMEOUT.farm_llm });
    },
  };

  /* ---------------------------------------------------------------- 轻提示 */

  function toast(message, kind = "info", timeout = 4200) {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      document.body.appendChild(host);
    }

    const el = document.createElement("div");
    el.className = "toast" + (kind === "info" ? "" : " toast--" + kind);
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    const iconName = kind === "error" ? "alert" : kind === "warning" ? "warning" : "info";
    el.innerHTML =
      (window.Icon ? Icon.render(iconName, 18) : "") +
      `<span class="toast__text"></span>`;
    el.querySelector(".toast__text").textContent = message;
    host.appendChild(el);

    window.setTimeout(() => {
      el.style.transition = "opacity 200ms ease";
      el.style.opacity = "0";
      window.setTimeout(() => el.remove(), 220);
    }, timeout);
  }

  /* ---------------------------------------------------------------- 小工具 */

  /** 把按钮切成加载态，返回恢复函数。 */
  function busy(button, label) {
    if (!button) return () => {};
    const original = button.innerHTML;
    const disabled = button.disabled;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.innerHTML =
      '<span class="spinner" role="status" aria-label="加载中"></span>' +
      (label ? `<span>${label}</span>` : "");
    return function restore() {
      button.innerHTML = original;
      button.disabled = disabled;
      button.removeAttribute("aria-busy");
    };
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function percent(value) {
    const n = Number(value);
    if (!isFinite(n)) return "—";
    return (n * 100).toFixed(1) + "%";
  }

  /** 用 textContent 安全地填充元素。 */
  function setText(el, text) {
    if (el) el.textContent = text == null ? "" : String(text);
  }

  /** 读取 URL 查询参数。 */
  function query(name) {
    return new URLSearchParams(location.search).get(name) || "";
  }

  /** 渲染一行「来源：机构 + 文件」。没有 notes 时返回空串；不生成链接。 */
  function sourceNote(notes, label) {
    const list = (notes || []).filter((n) => n && (n.organization || n.document));
    if (!list.length) return "";
    const text = list
      .map((n) => `${n.organization || ""}${n.document || ""}`)
      .join("；");
    return `<p class="source-note">${escapeHtml(label || "来源")}：${escapeHtml(text)}</p>`;
  }

  /** 本地日期 YYYY-MM-DD。
   *
   *  **不要再用 new Date().toISOString().slice(0, 10)**：toISOString 是 UTC，
   *  东八区 08:00 之前取到的是昨天 —— 早上七点打的问诊单会写成前一天，
   *  归档、回访、巡检 CSV 全都跟着错一天。
   */
  function localDate(d) {
    const t = d instanceof Date ? d : new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`;
  }

  /** 今天往后 days 天的本地日期（用于回访、默认 harvest date）。 */
  function localDatePlus(days) {
    const t = new Date();
    t.setDate(t.getDate() + Number(days || 0));
    return localDate(t);
  }

  window.Api = api;
  window.UI = { toast, busy, escapeHtml, percent, setText, query, sourceNote,
    localDate, localDatePlus };
})();
