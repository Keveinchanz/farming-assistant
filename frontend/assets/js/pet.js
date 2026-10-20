/* ==========================================================================
   AI 助手「小禾」—— 页面右下角常驻的中华田园犬
   --------------------------------------------------------------------------
   只在五个主功能页（首页 / 诊断 / 图书库 / 政策 / 农田）加载。

   状态存储：
     · 交流方式与对话历史放 sessionStorage —— 同标签页内跨页面保留，
       关闭标签页自动清空，正好对应「关闭页面之前都是这个模式」。
     · 静音与最小化放 localStorage —— 属于长期偏好。
   ========================================================================== */

(function () {
  "use strict";

  const MODE_KEY = "agri.pet.mode";
  const HISTORY_KEY = "agri.pet.history";
  const MUTE_KEY = "agri.pet.mute";
  const MAX_HISTORY = 20;

  const PAGE = (() => {
    const p = location.pathname.replace(/^\/+/, "").replace(/\.html$/, "");
    return p === "" ? "index" : p;
  })();

  const PAGE_GREET = {
    index: "首页地图上点一个省份，就能看到当季的病虫害预警。",
    diagnose: "拍张照，或者用方言说说叶子上的症状，我帮你看看。",
    library: "想查哪种病害或虫害？说个名字就行。",
    policy: "想了解哪一类惠农政策？",
    farm: "今天的农事、积温和收获预测都在这一页。",
  };

  const QUICK = [
    { label: "查虫害", text: "帮我看看叶子上的虫子" },
    { label: "看政策", text: "有什么惠农补贴政策" },
    { label: "今天农事", text: "今天该干啥农活" },
  ];

  let root = null;
  let avatar = null;
  let bubble = null;
  let panel = null;
  let logEl = null;
  let chooserEl = null;
  let composerEl = null;
  let voiceEl = null;
  let inputEl = null;
  let sendBtn = null;
  let micBtn = null;
  let statusEl = null;
  let switchBtn = null;
  let muteBtn = null;
  let chipsEl = null;

  let mode = sessionStorage.getItem(MODE_KEY) || "";
  let history = loadHistory();
  let muted = localStorage.getItem(MUTE_KEY) === "1";
  let busy = false;
  let greeted = false;

  // ---------------------------------------------------------------- 工具

  function loadHistory() {
    try {
      const raw = sessionStorage.getItem(HISTORY_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.slice(-MAX_HISTORY) : [];
    } catch (_) {
      return [];
    }
  }

  function saveHistory() {
    try {
      sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-MAX_HISTORY)));
    } catch (_) {
      /* 隐私模式下写不进去就算了，不影响当前会话 */
    }
  }

  function setState(state) {
    if (root) root.dataset.state = state;
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function toast(text) {
    try { UI.toast(text); } catch (_) { /* UI 不可用时静默 */ }
  }

  // ---------------------------------------------------------------- 构建

  const DOG_SVG = `
<svg viewBox="0 0 120 120" aria-hidden="true">
  <g class="dog__tail">
    <path d="M84 94 q20 -4 16 -26 q-2 -11 -11 -9" fill="none" stroke="#B4763C"
          stroke-width="10" stroke-linecap="round"/>
  </g>
  <g class="dog__body">
    <ellipse cx="60" cy="94" rx="26" ry="20" fill="#C98A4B"/>
    <ellipse cx="60" cy="100" rx="15" ry="13" fill="#F3E4CB"/>
    <ellipse cx="45" cy="112" rx="8" ry="5" fill="#B4763C"/>
    <ellipse cx="75" cy="112" rx="8" ry="5" fill="#B4763C"/>
  </g>
  <g class="dog__head">
    <g class="dog__ears">
      <path d="M41 34 q-10 -13 1 -18 q9 -3 12 11 z" fill="#A96A32"/>
      <path d="M79 34 q10 -13 -1 -18 q-9 -3 -12 11 z" fill="#A96A32"/>
    </g>
    <circle cx="60" cy="46" r="23" fill="#C98A4B"/>
    <path d="M60 23 q-11 0 -15 11 q8 -5 15 -5 q7 0 15 5 q-4 -11 -15 -11z" fill="#B4763C"/>
    <ellipse cx="60" cy="58" rx="13" ry="10" fill="#F3E4CB"/>
    <ellipse cx="60" cy="53" rx="5" ry="4" fill="#2B2118"/>
    <g class="dog__eyes">
      <circle cx="51" cy="42" r="3.4" fill="#2B2118"/>
      <circle cx="69" cy="42" r="3.4" fill="#2B2118"/>
      <circle cx="52" cy="40.8" r="1.1" fill="#ffffff"/>
      <circle cx="70" cy="40.8" r="1.1" fill="#ffffff"/>
    </g>
    <path class="dog__mouth" d="M60 57 v4 M60 61 q-5 5 -9 2 M60 61 q5 5 9 2"
          fill="none" stroke="#2B2118" stroke-width="1.7" stroke-linecap="round"/>
  </g>
</svg>`;

  function build() {
    root = document.createElement("div");
    root.className = "pet";
    root.dataset.state = "idle";
    root.dataset.open = "false";
    root.innerHTML = `
      <div class="pet__bubble" role="button" tabindex="0" hidden>
        你好！你好！请点击和我对话！
      </div>
      <section class="pet__panel" role="dialog" aria-label="AI 助手小禾" hidden>
        <header class="pet__head">
          <strong>小禾</strong>
          <span class="pet__status" data-pet-status>中华田园犬 · 在线</span>
          <button type="button" class="pet__icon-btn" data-pet-min title="收起" aria-label="收起">—</button>
        </header>

        <div class="pet__chooser" data-pet-chooser hidden>
          <p>您想以什么形式和我交流？</p>
          <div class="pet__choices">
            <button type="button" class="pet__choice" data-mode="typing">
              <strong>打字</strong><span>键盘输入</span>
            </button>
            <button type="button" class="pet__choice" data-mode="voice">
              <strong>聊天</strong><span>按住说话</span>
            </button>
          </div>
        </div>

        <div class="pet__log" data-pet-log aria-live="polite"></div>
        <div class="pet__chips" data-pet-chips hidden></div>

        <form class="pet__composer" data-pet-composer hidden>
          <input class="input" data-pet-input type="text" maxlength="500"
                 placeholder="想查什么、想去哪一页？" autocomplete="off">
          <button type="submit" class="pet__send" data-pet-send>发送</button>
        </form>

        <div class="pet__voice" data-pet-voice hidden>
          <button type="button" class="pet__mic" data-pet-mic aria-label="按住说话">🎤</button>
          <div><small data-pet-voice-tip>点一下开始说话，再点一下结束</small></div>
        </div>

        <footer class="pet__foot">
          <button type="button" class="pet__icon-btn" data-pet-switch>切换为语音</button>
          <button type="button" class="pet__icon-btn" data-pet-mute>静音</button>
          <button type="button" class="pet__icon-btn" data-pet-clear>重新开始</button>
        </footer>
      </section>
      <button type="button" class="pet__avatar" aria-label="AI 助手小禾" aria-expanded="false">
        ${DOG_SVG}
      </button>`;
    document.body.appendChild(root);

    avatar = root.querySelector(".pet__avatar");
    bubble = root.querySelector(".pet__bubble");
    panel = root.querySelector(".pet__panel");
    logEl = root.querySelector("[data-pet-log]");
    chooserEl = root.querySelector("[data-pet-chooser]");
    composerEl = root.querySelector("[data-pet-composer]");
    voiceEl = root.querySelector("[data-pet-voice]");
    inputEl = root.querySelector("[data-pet-input]");
    sendBtn = root.querySelector("[data-pet-send]");
    micBtn = root.querySelector("[data-pet-mic]");
    statusEl = root.querySelector("[data-pet-status]");
    switchBtn = root.querySelector("[data-pet-switch]");
    muteBtn = root.querySelector("[data-pet-mute]");
    chipsEl = root.querySelector("[data-pet-chips]");

    avatar.addEventListener("click", onAvatar);
    bubble.addEventListener("click", openPanel);
    bubble.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPanel(); }
    });
    root.querySelector("[data-pet-min]").addEventListener("click", minimize);
    chooserEl.querySelectorAll("[data-mode]").forEach((b) => {
      b.addEventListener("click", () => chooseMode(b.dataset.mode));
    });
    composerEl.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = inputEl.value.trim();
      inputEl.value = "";
      send(text);
    });
    micBtn.addEventListener("click", toggleRecord);
    switchBtn.addEventListener("click", () => chooseMode(mode === "typing" ? "voice" : "typing"));
    muteBtn.addEventListener("click", toggleMute);
    root.querySelector("[data-pet-clear]").addEventListener("click", clearChat);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !panel.hidden) minimize();
    });

    applyMuted();
    if (mode) {
      openPanel();
    } else {
      bubble.hidden = false;
    }
  }

  // ---------------------------------------------------------------- 面板状态

  function onAvatar() {
    if (!panel.hidden) { minimize(); return; }
    openPanel();
  }

  function openPanel() {
    bubble.hidden = true;
    panel.hidden = false;
    root.dataset.open = "true";
    avatar.setAttribute("aria-expanded", "true");
    if (!mode) {
      chooserEl.hidden = false;
      composerEl.hidden = true;
      voiceEl.hidden = true;
      chipsEl.hidden = true;
      return;
    }
    chooserEl.hidden = true;
    composerEl.hidden = mode !== "typing";
    voiceEl.hidden = mode !== "voice";
    renderHistory();
    renderChips();
    if (mode === "typing") setTimeout(() => inputEl.focus(), 30);
    greetIfNeeded();
  }

  function minimize() {
    panel.hidden = true;
    root.dataset.open = "false";
    avatar.setAttribute("aria-expanded", "false");
    bubble.textContent = history.length ? "点我继续聊" : "你好！你好！请点击和我对话！";
    bubble.hidden = false;
    setState("idle");
  }

  function chooseMode(next) {
    mode = next === "voice" ? "voice" : "typing";
    try { sessionStorage.setItem(MODE_KEY, mode); } catch (_) { /* 忽略 */ }
    chooserEl.hidden = true;
    composerEl.hidden = mode !== "typing";
    voiceEl.hidden = mode !== "voice";
    switchBtn.textContent = mode === "typing" ? "切换为语音" : "切换为文字";
    statusEl.textContent = mode === "typing" ? "中华田园犬 · 打字中" : "中华田园犬 · 语音中";
    renderHistory();
    renderChips();
    greetIfNeeded();
    if (mode === "typing") setTimeout(() => inputEl.focus(), 30);
  }

  function greetIfNeeded() {
    if (greeted || history.length) return;
    greeted = true;
    const greet = PAGE_GREET[PAGE] || "我是小禾，可以带你去找诊断、图书库、政策或者我的农田。";
    appendMessage("assistant", greet);
  }

  // ---------------------------------------------------------------- 消息

  function appendMessage(role, content, route) {
    const item = { role, content: String(content || ""), route: route || "" };
    history.push(item);
    saveHistory();
    renderMessage(item);
    logEl.scrollTop = logEl.scrollHeight;
    return item;
  }

  function renderMessage(item) {
    const div = document.createElement("div");
    div.className = "pet__msg " + (item.role === "user" ? "pet__msg--user" : "pet__msg--bot");
    div.textContent = item.content;
    logEl.appendChild(div);
    if (item.route) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pet__route";
      btn.textContent = "带我去 →";
      btn.addEventListener("click", () => {
        setState("happy");
        setTimeout(() => { location.href = item.route; }, 260);
      });
      logEl.appendChild(btn);
    }
  }

  function renderHistory() {
    logEl.innerHTML = "";
    history.forEach(renderMessage);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function renderChips() {
    if (history.length > 2) { chipsEl.hidden = true; return; }
    chipsEl.innerHTML = "";
    QUICK.forEach((q) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pet__chip";
      b.textContent = q.label;
      b.addEventListener("click", () => send(q.text));
      chipsEl.appendChild(b);
    });
    chipsEl.hidden = false;
  }

  function clearChat() {
    history = [];
    saveHistory();
    logEl.innerHTML = "";
    greeted = false;
    renderChips();
    greetIfNeeded();
  }

  // ---------------------------------------------------------------- 发送

  async function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;

    if (mode !== "typing" && mode !== "voice") {
      // 还没选交流方式时，直接发消息视为选择打字
      chooseMode("typing");
    }

    busy = true;
    sendBtn.disabled = true;
    setState("thinking");
    statusEl.textContent = "中华田园犬 · 思考中";

    const prior = history.slice(-8).map((m) => ({ role: m.role, content: m.content }));
    appendMessage("user", text);
    chipsEl.hidden = true;

    try {
      const data = await Api.post("/api/assistant/chat", {
        message: text,
        page: PAGE,
        history: prior,
      }, { timeout: 30000 });
      const answer = (data && data.answer) || "我这会儿没想出合适的回答，再说一遍？";
      const route = data && data.intent ? data.intent.route : "";
      appendMessage("assistant", answer, route);
      speak(answer);
    } catch (err) {
      appendMessage("assistant", "我这会儿没连上服务，稍后再试试？");
    } finally {
      busy = false;
      sendBtn.disabled = false;
      statusEl.textContent = mode === "voice" ? "中华田园犬 · 语音中" : "中华田园犬 · 打字中";
      if (!root.dataset.state || root.dataset.state === "thinking") setState("idle");
    }
  }

  function speak(text) {
    if (muted) return;
    const content = (text || "").trim();
    if (!content) return;
    setState("speaking");
    const ms = Math.min(9000, 1400 + content.length * 55);
    window.setTimeout(() => {
      if (root.dataset.state === "speaking") setState("idle");
    }, ms);
    try {
      const p = Dialect.speak(content);
      if (p && typeof p.catch === "function") p.catch(() => setState("idle"));
    } catch (_) {
      setState("idle");
    }
  }

  // ---------------------------------------------------------------- 语音

  let recording = false;

  async function toggleRecord() {
    if (recording) { await stopRecord(); return; }
    if (typeof Recorder === "undefined" || !Recorder.isSupported()) {
      toast("浏览器不支持录音，请改用打字");
      return;
    }
    if (!Recorder.isSecureContext()) {
      toast("录音需要 https 或 localhost 环境");
      return;
    }
    if (Recorder.isRecording()) {
      toast("正在录音中，请先结束当前录音");
      return;
    }
    try {
      await Recorder.start();
      recording = true;
      micBtn.dataset.recording = "true";
      setState("listening");
      statusEl.textContent = "中华田园犬 · 正在听";
    } catch (e) {
      toast((e && e.message) || "麦克风打开失败");
    }
  }

  async function stopRecord() {
    if (!recording) return;
    recording = false;
    micBtn.dataset.recording = "false";
    setState("thinking");
    statusEl.textContent = "中华田园犬 · 识别中";

    let result = null;
    try { result = await Recorder.stop(); } catch (_) { /* 忽略 */ }
    if (!result || !result.blob) {
      setState("idle");
      statusEl.textContent = "中华田园犬 · 语音中";
      toast("没有录到声音，再说一次？");
      return;
    }

    const form = new FormData();
    form.append("audio", result.blob, "pet.wav");
    try { form.append("dialect", Dialect.current()); } catch (_) { /* 默认普通话 */ }

    try {
      const asr = await Api.postForm("/api/voice/asr", form, { timeout: 30000 });
      const text = (asr && asr.text) || "";
      if (!text) {
        toast("没听清，再说一次？");
        setState("idle");
        statusEl.textContent = "中华田园犬 · 语音中";
        return;
      }
      await send(text);
    } catch (e) {
      toast((e && e.message) || "语音识别失败");
      setState("idle");
      statusEl.textContent = "中华田园犬 · 语音中";
    }
  }

  // ---------------------------------------------------------------- 偏好

  function toggleMute() {
    muted = !muted;
    try { localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); } catch (_) { /* 忽略 */ }
    applyMuted();
  }

  function applyMuted() {
    muteBtn.textContent = muted ? "取消静音" : "静音";
    muteBtn.setAttribute("aria-pressed", muted ? "true" : "false");
  }

  // ---------------------------------------------------------------- 启动

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
