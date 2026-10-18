/* ==========================================================================
   任务 5.6 —— 全局方言切换组件
   · 导航栏最右侧下拉选择
   · 用 localStorage 跨页面持久化，切换后无需刷新即可让当前页的朗读按钮生效
   · 提供 Dialect.speak(text)：优先调用后端 TTS，未配置语音服务时
     自动降级为浏览器内置 speechSynthesis 朗读（并明确提示这只是普通话）
   ========================================================================== */

(function () {
  "use strict";

  const STORAGE_KEY = "agri.dialect";
  const speakers = [];        // 变更订阅者

  let dialects = [];
  let current = localStorage.getItem(STORAGE_KEY) || "";
  let ready = null;           // 初始化 Promise，避免重复请求

  /* ---------------------------------------------------------------- 初始化 */

  function init() {
    if (ready) return ready;
    ready = Api.dialects()
      .then((list) => {
        dialects = list || [];
        const keys = dialects.map((d) => d.key);
        if (!current || keys.indexOf(current) === -1) {
          current = keys.indexOf("mandarin") >= 0 ? "mandarin" : keys[0] || "";
          if (current) localStorage.setItem(STORAGE_KEY, current);
        }
        renderPickers();
        return dialects;
      })
      .catch((err) => {
        UI.toast("方言列表加载失败：" + err.message, "warning");
        dialects = [{ key: "mandarin", name: "普通话", asr_supported: true }];
        renderPickers();
        return dialects;
      });
    return ready;
  }

  function renderPickers() {
    document.querySelectorAll("[data-dialect-picker]").forEach((select) => {
      select.innerHTML = dialects
        .map((d) => {
          const suffix = d.asr_supported ? "" : "（识别回退普通话）";
          return `<option value="${UI.escapeHtml(d.key)}">${UI.escapeHtml(d.name)}${suffix}</option>`;
        })
        .join("");
      select.value = current;
      if (!select.dataset.bound) {
        select.dataset.bound = "1";
        select.addEventListener("change", () => set(select.value));
      }
    });
  }

  /* ---------------------------------------------------------------- 状态 */

  function get(key) {
    return dialects.find((d) => d.key === key) || null;
  }

  function currentInfo() {
    return get(current) || { key: current, name: "普通话", asr_supported: true };
  }

  function set(key) {
    if (!key || key === current) return;
    current = key;
    localStorage.setItem(STORAGE_KEY, key);
    document.querySelectorAll("[data-dialect-picker]").forEach((s) => {
      if (s.value !== key) s.value = key;
    });

    const info = currentInfo();
    UI.toast(
      "已切换到「" + info.name + "」" + (info.asr_supported ? "" : "，该方言语音识别将回退普通话模型"),
      info.asr_supported ? "info" : "warning"
    );

    speakers.forEach((cb) => {
      try {
        cb(key, info);
      } catch (e) {
        console.error(e);
      }
    });
    window.dispatchEvent(new CustomEvent("dialectchange", { detail: { key, info } }));
  }

  function onChange(cb) {
    speakers.push(cb);
    return () => {
      const i = speakers.indexOf(cb);
      if (i >= 0) speakers.splice(i, 1);
    };
  }

  /* ---------------------------------------------------------------- 朗读 */

  let audioEl = null;

  /**
   * 朗读文本。返回 Promise，resolve 时的对象说明用了哪条路径：
   *   { mode: "server" } 后端合成了方言语音
   *   { mode: "browser" } 降级为浏览器朗读（仅普通话）
   */
  function speak(text, button) {
    const content = (text || "").trim();
    if (!content) return Promise.reject(new Error("没有可朗读的内容"));

    const restore = UI.busy(button, "合成中");
    const info = currentInfo();

    return Api.tts(content.slice(0, 480), current)
      .then((res) => {
        restore();
        if (res && res.audio_url) {
          stopBrowserSpeech();
          if (!audioEl) audioEl = new Audio();
          audioEl.src = res.audio_url;
          audioEl.play().catch(() => {
            UI.toast("浏览器阻止了自动播放，请再点一次朗读按钮", "warning");
          });
          return { mode: "server", voice: res.voice_name, dialect: info.name };
        }
        // Mock：后端没有生成音频，用浏览器朗读兜底。
        // 优先用后端的 message —— 它会说清当前是哪家语音服务（讯飞/百度）没配好，
        // 这里再写死「百度」就会在讯飞下报错报错到别的服务上。
        const shown = browserSpeak(content);
        UI.toast(
          res.message ||
            (shown.ok
              ? "未配置语音服务，已改用浏览器朗读（仅普通话，非方言音色）"
              : "未配置语音服务，无法朗读"),
          "warning",
          6000
        );
        return { mode: "browser", dialect: info.name };
      })
      .catch((err) => {
        restore();
        const shown = browserSpeak(content);
        UI.toast(
          (shown.ok ? "语音合成失败，已改用浏览器朗读：" : "语音合成失败：") + err.message,
          "error",
          6000
        );
        return { mode: "browser", dialect: info.name, error: err.message };
      });
  }

  function browserSpeak(text) {
    if (!("speechSynthesis" in window)) return { ok: false };
    try {
      stopBrowserSpeech();
      const utter = new SpeechSynthesisUtterance(text);
      utter.lang = "zh-CN";
      utter.rate = 0.95;
      window.speechSynthesis.speak(utter);
      return { ok: true };
    } catch (e) {
      return { ok: false };
    }
  }

  function stopBrowserSpeech() {
    if ("speechSynthesis" in window) {
      try {
        window.speechSynthesis.cancel();
      } catch (e) {
        /* 忽略 */
      }
    }
  }

  /** 给「方言播报 / 方言朗读」按钮统一绑定点击行为。 */
  function bindSpeakButton(button, getText) {
    if (!button) return;
    button.addEventListener("click", () => {
      const text = typeof getText === "function" ? getText() : getText;
      speak(text, button).catch((e) => UI.toast(e.message, "error"));
    });
  }

  window.Dialect = {
    init,
    set,
    get,
    current: () => current,
    currentInfo,
    onChange,
    speak,
    bindSpeakButton,
    stop: stopBrowserSpeech,
    all: () => dialects.slice(),
  };
})();
