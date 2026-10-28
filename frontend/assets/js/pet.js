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
  const HINT_KEY = "agri.pet.hint";
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
  let voiceTipEl = null;
  let stopTick = null;

  let mode = sessionStorage.getItem(MODE_KEY) || "";
  let history = loadHistory();
  let muted = localStorage.getItem(MUTE_KEY) === "1";
  // 气泡引导只在同一个标签页里出现一次：换功能页时不该再打招呼一次。
  let hintShown = (() => {
    try { return sessionStorage.getItem(HINT_KEY) === "1"; } catch (_) { return false; }
  })();
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

  /* 中华田园犬：立耳、黄褐毛、白吻白胸、黑鼻。头部与身体分组名保持不变，
     pet.css 里的呼吸、摇尾、眨眼、口型与歪头动画全部沿用。 */
  const DOG_SVG = `
<svg viewBox="0 0 120 120" aria-hidden="true">
  <defs>
    <linearGradient id="pet-dog-fur" x1="0" y1="0" x2="0.25" y2="1">
      <stop offset="0" stop-color="#DDA664"/>
      <stop offset="1" stop-color="#B87B3E"/>
    </linearGradient>
    <linearGradient id="pet-dog-head" x1="0.15" y1="0" x2="0.2" y2="1">
      <stop offset="0" stop-color="#E2AE6D"/>
      <stop offset="1" stop-color="#C0864A"/>
    </linearGradient>
  </defs>

  <ellipse cx="60" cy="112.5" rx="30" ry="3.4" fill="#2B2118" opacity="0.13"/>

  <g class="dog__tail">
    <path d="M83 97 C99 100 109 89 105 76 C103 68 97 65 92.5 68.5"
          fill="none" stroke="#B4763C" stroke-width="10.5" stroke-linecap="round"/>
    <path d="M84 95.5 C96 97.5 104 89 102.5 79"
          fill="none" stroke="#D29A5B" stroke-width="3.6" stroke-linecap="round" opacity="0.5"/>
    <ellipse cx="93" cy="68" rx="5.2" ry="4.6" fill="#F5EAD7" transform="rotate(-24 93 68)"/>
  </g>

  <g class="dog__body">
    <ellipse cx="36" cy="97" rx="15" ry="14" fill="#B4793E"/>
    <ellipse cx="84" cy="97" rx="15" ry="14" fill="#B4793E"/>
    <path d="M60 59 C40 59 32.5 77 34.5 93 C36.5 104 47 108.5 60 108.5
             C73 108.5 83.5 104 85.5 93 C87.5 77 80 59 60 59 Z"
          fill="url(#pet-dog-fur)"/>
    <path d="M60 63 C49.5 68.5 46.5 86 52.5 98.5 C54.5 103.5 57.6 104.6 60 104.6
             C62.4 104.6 65.5 103.5 67.5 98.5 C73.5 86 70.5 68.5 60 63 Z"
          fill="#F8F0E1"/>
    <ellipse cx="47" cy="108" rx="9.5" ry="5.4" fill="#F1E3CB"/>
    <ellipse cx="73" cy="108" rx="9.5" ry="5.4" fill="#F1E3CB"/>
    <path d="M47 105.4 v4.8 M52.4 105.8 v4" fill="none" stroke="#CBB795"
          stroke-width="1" stroke-linecap="round" opacity="0.65"/>
    <path d="M73 105.4 v4.8 M78.4 105.8 v4" fill="none" stroke="#CBB795"
          stroke-width="1" stroke-linecap="round" opacity="0.65"/>
  </g>

  <g class="dog__head">
    <g class="dog__ears">
      <path d="M53 31 C47 20 39 11 36 10 C33 9 32.6 14 33.8 19 C35.4 27 38 35 41 41 Z" fill="#9A5F30"/>
      <path d="M49.6 30.5 C45.4 22.4 40.4 15.8 38.4 14.3 C37.2 13.7 37.2 16.3 38 19.8
               C39.3 25.7 41.2 31.2 43.2 35.6 Z" fill="#E3B385"/>
      <path d="M67 31 C73 20 81 11 84 10 C87 9 87.4 14 86.2 19 C84.6 27 82 35 79 41 Z" fill="#9A5F30"/>
      <path d="M70.4 30.5 C74.6 22.4 79.6 15.8 81.6 14.3 C82.8 13.7 82.8 16.3 82 19.8
               C80.7 25.7 78.8 31.2 76.8 35.6 Z" fill="#E3B385"/>
    </g>

    <ellipse cx="60" cy="48" rx="24" ry="23" fill="url(#pet-dog-head)"/>
    <path d="M38 49 C33.8 55 35 61.5 40 63.6 C42 58.4 41.2 52.8 38 49 Z" fill="#D29A5B" opacity="0.75"/>
    <path d="M82 49 C86.2 55 85 61.5 80 63.6 C78 58.4 78.8 52.8 82 49 Z" fill="#D29A5B" opacity="0.75"/>

    <path d="M60 41 C54.2 46.5 52.4 53.5 53.4 59.8 C55.2 64.9 64.8 64.9 66.6 59.8
             C67.6 53.5 65.8 46.5 60 41 Z" fill="#F8F0E1" opacity="0.92"/>
    <ellipse cx="60" cy="60" rx="14.4" ry="10.8" fill="#F8F0E1"/>

    <circle cx="48" cy="37.2" r="1.5" fill="#F2DDBB" opacity="0.7"/>
    <circle cx="72" cy="37.2" r="1.5" fill="#F2DDBB" opacity="0.7"/>

    <g class="dog__eyes">
      <path d="M44.4 45.6 C46.8 41.6 51.8 41.4 54.6 45.2 C51.8 47.5 47.2 47.8 44.4 45.6 Z" fill="#3A2A1D"/>
      <path d="M65.4 45.2 C68.2 41.4 73.2 41.6 75.6 45.6 C72.8 47.8 68.2 47.5 65.4 45.2 Z" fill="#3A2A1D"/>
      <circle cx="47.8" cy="44.1" r="1.2" fill="#FFFFFF"/>
      <circle cx="68.8" cy="44.1" r="1.2" fill="#FFFFFF"/>
    </g>

    <path d="M54.6 55.2 C54.6 52.4 56.9 51 60 51 C63.1 51 65.4 52.4 65.4 55.2
             C65.4 58 62.9 59.8 60 59.8 C57.1 59.8 54.6 58 54.6 55.2 Z" fill="#2B2118"/>
    <ellipse cx="57.8" cy="53.8" rx="1.5" ry="1" fill="#8A7A6A" opacity="0.55"/>

    <g class="dog__mouth">
      <ellipse cx="60" cy="65.4" rx="3.2" ry="2.2" fill="#E8837F"/>
      <path d="M60 59.8 v3.2 M60 63 C57.2 65.8 53.6 65.8 51.6 63.6
               M60 63 C62.8 65.8 66.4 65.8 68.4 63.6"
            fill="none" stroke="#4A3425" stroke-width="1.6" stroke-linecap="round"/>
    </g>
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
    voiceTipEl = root.querySelector("[data-pet-voice-tip]");

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
    /* 换到另一个功能页时只保留右下角的小禾，面板不自动展开。
       早先这里写的是 mode ? openPanel() : bubble.hidden = false —— 而 mode 存在
       sessionStorage 里、跨页面保留，结果用户在诊断页聊过一句，之后每点一次导航，
       小禾的整块面板都会跟着弹出来挡住正文。面板改为只在用户点头像/气泡时打开；
       气泡引导在同一个标签页里只出现一次（首次进入），之后切页不再重复打招呼。 */
    panel.hidden = true;
    root.dataset.open = "false";
    avatar.setAttribute("aria-expanded", "false");
    if (!hintShown) {
      bubble.hidden = false;
      hintShown = true;
      try { sessionStorage.setItem(HINT_KEY, "1"); } catch (_) { /* 忽略 */ }
    } else {
      bubble.hidden = true;
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
    if (recording) cancelRecording();
    panel.hidden = true;
    root.dataset.open = "false";
    avatar.setAttribute("aria-expanded", "false");
    bubble.textContent = history.length ? "点我继续聊" : "你好！你好！请点击和我对话！";
    bubble.hidden = false;
    setState("idle");
  }

  function chooseMode(next) {
    if (recording) cancelRecording();
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
  const VOICE_TIP = "点一下开始说话，再点一下结束";

  function setVoiceTip(text) {
    if (voiceTipEl) voiceTipEl.textContent = text;
  }

  function releaseTick() {
    if (typeof stopTick === "function") stopTick();
    stopTick = null;
  }

  /* 收起面板或切回打字时调用：同步停掉采集并丢弃这段音频，
     否则浏览器的录音指示灯会一直亮着。 */
  function cancelRecording() {
    if (!recording) return;
    recording = false;
    releaseTick();
    micBtn.dataset.recording = "false";
    setVoiceTip(VOICE_TIP);
    try { if (typeof Recorder !== "undefined") Recorder.cancel(); } catch (_) { /* 忽略 */ }
    setState("idle");
    statusEl.textContent = mode === "voice" ? "中华田园犬 · 语音中" : "中华田园犬 · 打字中";
  }

  async function toggleRecord() {
    if (recording) { await stopRecord(); return; }
    /* 顺序有讲究：先判断「页面到底有没有加载录音模块」，再判断浏览器能力。
       否则首页、政策页漏引 recorder.js 时，会把「页面缺模块」误报成「浏览器不支持」。 */
    if (typeof Recorder === "undefined") {
      toast("当前页面没有加载录音模块，请刷新页面或改用打字");
      return;
    }
    if (!Recorder.isSecureContext()) {
      toast("录音需要 https 或 localhost 环境");
      return;
    }
    if (!Recorder.isSupported()) {
      toast("浏览器不支持录音，请改用打字");
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
      setVoiceTip("00:00 · 再点一下结束");
      stopTick = Recorder.on("tick", (ms) => {
        const label = Recorder.formatDuration(ms);
        setVoiceTip(label + " · 再点一下结束");
        statusEl.textContent = "中华田园犬 · 正在听 " + label;
      });
    } catch (e) {
      setVoiceTip(VOICE_TIP);
      toast((e && e.message) || "麦克风打开失败");
    }
  }

  async function stopRecord() {
    if (!recording) return;
    recording = false;
    releaseTick();
    micBtn.dataset.recording = "false";
    setVoiceTip(VOICE_TIP);
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
