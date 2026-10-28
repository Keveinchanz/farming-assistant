/* ==========================================================================
   快速版 · 仿微信聊天框
   --------------------------------------------------------------------------
   一条消息可以是：文字 / 图片 / 语音（语音同时带转写文字）。
   发送时只打一个接口 `POST /api/chat/send`，把图片、音频、文字一起交给后端，
   由后端复用诊断链路（ONNX + 方言 ASR + RAG + 大模型）压成一条简化介绍。

   三个刻意的取舍：
   · 语音走「按住说话」而不是点一下开始、再点一下停 —— 农户单手拿手机、另一只手
     扶叶子，按住松开是最省动作的交互（也是微信本身的做法）。
   · 语音气泡与转写文字同时保留：气泡能回放（证明「我确实说了这句」），文字能核对
     （方言识别难免有偏差），两者缺一个都会让用户怀疑结果。
   · 结果里的 `context` 原样留住，下一次文字追问带上它，模型才知道「这个病」指谁。
   ========================================================================== */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const log = $("chat-log");
  const input = $("chat-input");
  const sendBtn = $("btn-send");
  const modeBtn = $("btn-mode");
  const imageBtn = $("btn-image");
  const holdBtn = $("btn-hold");
  const fileInput = $("file-input");
  const attach = $("attach");
  const attachImg = $("attach-img");
  const attachRemove = $("attach-remove");
  const statusEl = $("composer-status");
  const tipEl = $("recording-tip");
  const zoom = $("chat-zoom");
  const zoomImg = $("chat-zoom-img");
  const standardLink = $("btn-standard");

  const DEFAULT_STATUS = "照片 + 一句话，判得更准 · AI 判断仅供参考，用药按标签说明";
  const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
  const MIN_VOICE_MS = 600;

  const state = {
    mode: "text",
    busy: false,
    context: "",        // 上一轮后端返回的结论摘要（原样回传）
    image: null,        // { file, url }
    recording: false,
    startPromise: null,
    playing: null,      // 当前正在播放的 <audio>
  };

  const esc = (s) => (window.UI ? UI.escapeHtml(String(s == null ? "" : s)) : String(s == null ? "" : s));

  /* ---------------------------------------------------------------- 基础渲染 */

  function scrollToEnd() {
    window.requestAnimationFrame(() => {
      log.scrollTop = log.scrollHeight;
    });
  }

  function avatarAi() {
    return `<span class="chat-avatar chat-avatar--ai" aria-hidden="true">${
      window.Icon ? Icon.render("leaf", 16) : "禾"
    }</span>`;
  }

  const AVATAR_ME = `<span class="chat-avatar" aria-hidden="true">我</span>`;

  /* 自己的消息用 row-reverse：DOM 里头像排第一，反向后就落在最右侧（微信里自己的
     头像在右）。所以「我的」把头像放前面，「对方」把头像放前面 —— 两者 DOM 顺序相同，
     靠 flex-direction 决定谁在左谁在右，不需要写两套结构。 */
  function appendRow(html, mine) {
    const row = document.createElement("div");
    row.className = "chat-row" + (mine ? " chat-row--me" : "");
    row.innerHTML = (mine ? AVATAR_ME : avatarAi()) + `<div class="chat-body">${html}</div>`;
    log.appendChild(row);
    scrollToEnd();
    return row;
  }

  function appendUserText(text) {
    appendRow(`<div class="bubble bubble--me">${esc(text)}</div>`, true);
  }

  function appendUserImage(file) {
    const url = URL.createObjectURL(file);
    const row = appendRow(
      `<div class="bubble bubble--me bubble--image">` +
      `<img src="${url}" alt="我上传的作物照片" data-zoom="${url}"></div>`,
      true
    );
    row.querySelector("img").addEventListener("click", () => openZoom(url));
  }

  /** 返回那条语音的「转写文字」节点，等后端返回后回填。 */
  function appendUserVoice(blob, durationMs) {
    const url = URL.createObjectURL(blob);
    const seconds = Math.max(1, Math.round((durationMs || 0) / 1000));
    const width = Math.min(210, 104 + seconds * 8);
    const row = appendRow(
      `<div class="bubble bubble--me" style="min-width:${width}px">` +
      `<button type="button" class="voice-bubble" data-src="${url}" aria-label="播放语音，${seconds} 秒">` +
      `<span class="voice-bubble__waves" aria-hidden="true"><i></i><i></i><i></i><i></i></span>` +
      `<span>${seconds}″</span></button></div>` +
      `<div class="voice-text voice-text--pending">正在识别语音…</div>`,
      true
    );
    const btn = row.querySelector(".voice-bubble");
    btn.addEventListener("click", () => playVoice(btn, url));
    return row.querySelector(".voice-text");
  }

  function setVoiceTranscript(node, text, mock) {
    if (!node) return;
    node.classList.remove("voice-text--pending");
    const said = String(text || "").trim();
    if (said) {
      node.textContent = said;
    } else {
      node.textContent = mock
        ? "（语音服务未配置，没识别出内容）"
        : "（没听清，能再说一遍吗）";
    }
  }

  function playVoice(btn, url) {
    if (state.playing) {
      state.playing.pause();
      const prev = document.querySelector('.voice-bubble[data-playing="1"]');
      if (prev) prev.dataset.playing = "0";
      if (state.playing.dataset.url === url) {
        state.playing = null;
        return;
      }
    }
    const audio = new Audio(url);
    audio.dataset.url = url;
    state.playing = audio;
    btn.dataset.playing = "1";
    audio.addEventListener("ended", () => {
      btn.dataset.playing = "0";
      state.playing = null;
    });
    audio.play().catch(() => {
      btn.dataset.playing = "0";
      state.playing = null;
      UI.toast("这段语音没法播放", "warning");
    });
  }

  function appendTyping() {
    const row = appendRow(
      `<div class="bubble bubble--ai"><span class="typing" role="status" aria-label="正在思考">` +
      `<i></i><i></i><i></i></span></div>`,
      false
    );
    return row;
  }

  /* ---------------------------------------------------------------- AI 回复 */

  /* 置信度给两样东西：数字和一句中文。数字是模型的原始口径，中文是给农户的翻译 ——
     "0.72 较高" 比一个绿色「置信度 72%」徽章更像读数，也更容易在答辩时解释清楚。 */
  function confWord(conf) {
    if (conf >= 0.85) return "高";
    if (conf >= 0.6) return "较高";
    if (conf >= 0.4) return "中等";
    return "偏低";
  }

  function stamp() {
    const d = new Date();
    const p2 = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ` +
      `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  }

  /* 回答末尾统一贴「数据来源」：政策给发布单位 + 原文地址，诊断给知识卡片 +
     训练数据集。没有来源的普通聊天不硬凑一条，那会变成装饰。 */
  function sourcesBlock(list) {
    if (!list.length) return "";
    const rows = list.map((s) => {
      const label = esc(s.label || "来源");
      const detail = s.detail ? `<span class="report__srcMeta">${esc(s.detail)}</span>` : "";
      const inner = s.url
        ? `<a href="${esc(s.url)}"${/^https?:/i.test(s.url)
            ? ' target="_blank" rel="noopener noreferrer"' : ""}>${label}</a>`
        : label;
      return `<li>${inner}${detail}</li>`;
    }).join("");
    return `<div class="report__sources">` +
      `<span class="report__sourcesLabel">数据来源</span>` +
      `<ul class="report__sourcesList">${rows}</ul></div>`;
  }

  function renderReply(reply, cards) {
    const r = reply || {};
    const list = cards || [];
    const hasCard = !!r.title && r.kind !== "chat";

    if (!hasCard) {
      const links = list.length
        ? `<ul class="report__more">` + list.map((c) =>
            `<li><a href="${esc(c.url)}">${esc(c.title)}</a></li>`).join("") + `</ul>`
        : "";
      return `<div class="bubble bubble--ai">${esc(r.text || "我没太听明白，能再说一句吗？")}` +
        `${links ? `<div style="margin-top:10px">${links}</div>` : ""}</div>` +
        speakTools(r.text);
    }

    const conf = Math.max(0, Math.min(1, Number(r.confidence || 0)));
    const pct = Math.round(conf * 100);
    const confHtml = conf > 0
      ? `<div class="report__conf">` +
          `<div class="report__bar" role="img" aria-label="置信度 ${pct}%">` +
          `<i style="width:${pct}%"></i></div>` +
          `<span class="report__num">${conf.toFixed(2)} ${confWord(conf)}</span>` +
        `</div>`
      : "";

    const steps = (r.points || []).length
      ? `<ol class="report__steps">` + r.points.map((p, i) =>
          `<li><span class="report__no">${i + 1}</span>` +
          `<span class="report__step">${esc(p)}</span></li>`).join("") + `</ol>`
      : "";

    const note = r.note ? `<p class="report__note">${esc(r.note)}</p>` : "";

    /* 命中的那张卡要显式露出来。结论下面紧跟"它来自哪张卡片"，
       这条链路才叫可回溯；塞在卡片最底部的脚注只是免责声明。
       卡片编号一起带上：评委想核对时，能直接按编号在图书库里找到同一张。 */
    const hit = list.find((c) => c.url === r.card_url) || (r.card_url ? list[0] : null);
    const fallbackSource = hit
      ? [{
          label: `知识图书库《${hit.title}》`,
          detail: hit.id ? "卡片编号 #" + String(hit.id).split("_").pop() : "",
          url: hit.url,
        }]
      : [];
    const sources = Array.isArray(r.sources) && r.sources.length ? r.sources : fallbackSource;

    const actions = [];
    if (r.card_url) {
      actions.push(`<a class="btn btn--primary btn--sm" href="${esc(r.card_url)}">查看详细知识</a>`);
    }
    // 虫害（kind=pest）和病害一样要给「完整诊断」：都是模型给的结论，
    // 都该有一条路回到诊断页看全量结果。
    if (r.kind === "diagnosis" || r.kind === "pest") {
      actions.push(`<a class="btn btn--outline btn--sm" href="/diagnose.html">完整诊断</a>`);
    }
    if (r.kind === "policy") {
      actions.push(`<a class="btn btn--outline btn--sm" href="/policy.html">全部政策</a>`);
    }

    const more = list.filter((c) => c.url !== r.card_url).slice(0, 2);
    const moreHtml = more.length
      ? `<ul class="report__more">` + more.map((c) =>
          `<li><a href="${esc(c.url)}">其他相关 · ${esc(c.title)}</a></li>`).join("") + `</ul>`
      : "";

    const metaMap = {
      diagnosis: "病害诊断", pest: "虫害检出", policy: "惠农政策",
      answer: "知识库", healthy: "未见病斑", unsure: "补拍建议",
    };
    const headMap = {
      policy: "政策依据", answer: "知识库检索",
      healthy: "巡检结论", unsure: "拍摄建议",
    };
    const meta = metaMap[r.kind] || "病害诊断";
    const headText = headMap[r.kind] || "诊断结论";

    return `<div class="report">` +
      `<div class="report__head">` +
        `<span>${headText}</span>` +
        `<span class="report__time">${stamp()}</span>` +
      `</div>` +
      `<div class="report__body">` +
        `<div class="report__titlerow">` +
          `<span class="report__title">${esc(r.title)}</span>` +
          `<span class="report__meta${r.kind === "healthy" ? " report__meta--ok" : ""}">${meta}</span>` +
        `</div>` +
        confHtml +
        (r.text ? `<p class="report__text">${esc(r.text)}</p>` : "") +
        steps + note + moreHtml +
      `</div>` +
      (actions.length ? `<div class="report__foot">${actions.join("")}</div>` : "") +
      sourcesBlock(sources) +
      `</div>` +
      speakTools([r.title, r.text].filter(Boolean).join("。"));
  }

  function speakTools(text) {
    if (!text) return "";
    return `<div class="bubble-tools">` +
      `<button type="button" class="bubble-tools__btn" data-speak>` +
      `${window.Icon ? Icon.render("volume", 14) : ""}朗读</button></div>`;
  }

  function appendAi(reply, cards) {
    const row = appendRow(
      `<div class="bubble bubble--ai">${esc((reply || {}).text || "")}</div>`,
      false
    );
    // 先在 DOM 里占位，再用渲染结果替换 —— 这样滚动位置与顺序都是对的
    // 结论卡比气泡需要的横向空间多一档，先给这一行打标，交给 CSS 放宽 max-width
    if ((reply || {}).title && reply.kind !== "chat") {
      row.classList.add("chat-row--report");
    }
    row.querySelector(".chat-body").innerHTML = renderReply(reply, cards);
    const speak = row.querySelector("[data-speak]");
    if (speak) {
      const said = [reply.title, reply.text].filter(Boolean).join("。");
      speak.addEventListener("click", () => {
        try {
          const p = Dialect.speak(said);
          if (p && p.catch) p.catch(() => {});
        } catch (_) {
          /* 朗读失败不影响对话 */
        }
      });
    }
    const imgs = row.querySelectorAll("[data-zoom]");
    imgs.forEach((img) => img.addEventListener("click", () => openZoom(img.dataset.zoom)));
    scrollToEnd();
  }

  function appendError(message) {
    appendAi({ kind: "chat", text: message }, []);
  }

  /* ---------------------------------------------------------------- 开场页 */

  const HINTS = [
    { label: "拍张照片看看", photo: true },
    { label: "水稻叶子上有褐色斑点" },
    { label: "叶片背面有好多小虫子" },
  ];

  /* 空对话的状态下给一块居中的开场，而不是一条孤零零贴在顶部的欢迎气泡。
     发出第一条消息时整块移除（见 hideHints）—— 对话要往上走，开场不占位。 */
  function renderHero() {
    const box = document.createElement("div");
    box.className = "chat-hero";
    box.id = "chat-hero";
    box.innerHTML =
      `<span class="chat-hero__mark" aria-hidden="true">${window.Icon ? Icon.render("leaf", 22) : ""}</span>` +
      `<span class="chat-hero__kicker">小禾 · 农技助手</span>` +
      `<h1 class="chat-hero__title">说说作物哪里不对劲</h1>` +
      `<p class="chat-hero__sub">拍张照片，或用方言说一句。我先给个初步判断，` +
      `详细的防治办法在结论里一键翻到图书库。</p>` +
      `<div class="chat-hero__list">` +
      HINTS.map((h) =>
        `<button type="button" class="chat-hint" data-hint="${esc(h.label)}" ` +
        `data-photo="${h.photo ? "1" : ""}">${esc(h.label)}</button>`).join("") +
      `</div>`;

    box.querySelectorAll("[data-hint]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.photo) {
          fileInput.click();
          return;
        }
        send({ text: btn.dataset.hint });
      });
    });

    log.appendChild(box);
    log.classList.add("is-empty");
  }

  function hideHints() {
    const box = $("chat-hero");
    if (box) box.remove();
    log.classList.remove("is-empty");
  }

  /* ---------------------------------------------------------------- 发送 */

  function setBusy(busy) {
    state.busy = busy;
    sendBtn.disabled = busy;
    modeBtn.disabled = busy;
    imageBtn.disabled = busy;
    input.disabled = busy;
    statusEl.textContent = busy ? "正在看这张照片…" : DEFAULT_STATUS;
  }

  async function send(opts) {
    const options = opts || {};
    const text = String(options.text || "").trim();
    const imageFile = options.imageFile || null;
    const audioBlob = options.audioBlob || null;
    if (state.busy) return;
    if (!text && !imageFile && !audioBlob) {
      UI.toast("说一句话，或者拍张照片", "warning");
      return;
    }

    hideHints();
    setBusy(true);

    let voiceNode = null;
    if (audioBlob) voiceNode = appendUserVoice(audioBlob, options.durationMs);
    if (imageFile) appendUserImage(imageFile);
    if (text) appendUserText(text);

    const typing = appendTyping();

    const form = new FormData();
    form.append("text", text);
    form.append("dialect", window.Dialect ? Dialect.current() : "");
    if (state.context) form.append("context", state.context);
    if (imageFile) form.append("image", imageFile, imageFile.name || "photo.jpg");
    if (audioBlob) form.append("audio", audioBlob, "voice.wav");

    try {
      const data = await Api.postForm("/api/chat/send", form, { timeout: 130000 });
      typing.remove();

      if (voiceNode) {
        const asr = (data && data.asr) || {};
        setVoiceTranscript(voiceNode, asr.text, asr.mock);
      }
      appendAi((data && data.reply) || {}, (data && data.cards) || []);
      if (data && data.context) state.context = data.context;
    } catch (err) {
      typing.remove();
      if (voiceNode) {
        voiceNode.classList.remove("voice-text--pending");
        voiceNode.textContent = "（服务没响应，这段语音没能识别）";
      }
      appendError(
        "没连上识别服务：" + (err && err.message ? err.message : "未知错误") +
        "。你可以先点下面的「查看详细知识」自己翻卡片。"
      );
    } finally {
      setBusy(false);
      clearPendingImage();
      input.focus();
    }
  }

  /* ---------------------------------------------------------------- 图片 */

  function setPendingImage(file) {
    clearPendingImage();
    state.image = { file: file, url: URL.createObjectURL(file) };
    attachImg.src = state.image.url;
    attach.hidden = false;
  }

  function clearPendingImage() {
    if (state.image) URL.revokeObjectURL(state.image.url);
    state.image = null;
    attach.hidden = true;
    attachImg.removeAttribute("src");
  }

  function openZoom(url) {
    zoomImg.src = url;
    zoom.hidden = false;
  }

  /* ---------------------------------------------------------------- 语音 */

  function setMode(mode, focus) {
    state.mode = mode;
    const voice = mode === "voice";
    input.hidden = voice;
    holdBtn.hidden = !voice;
    modeBtn.setAttribute("aria-pressed", voice ? "true" : "false");
    modeBtn.title = voice ? "切换打字" : "切换语音输入（按住说话）";
    modeBtn.innerHTML = window.Icon ? Icon.render(voice ? "text" : "mic", 20) : (voice ? "Aa" : "语音");
    // 首次进页面不抢焦点：一进来就套一圈焦点环，页面像被人点过一半；
    // 手机上还可能把键盘顶起来，把欢迎语挤掉一半。
    if (!voice && focus !== false) input.focus();
  }

  async function startHold(event) {
    if (event) event.preventDefault();
    if (state.busy || state.recording) return;
    if (!window.Recorder) {
      UI.toast("录音模块没加载成功，刷新页面再试", "error");
      return;
    }
    if (!Recorder.isSupported()) {
      UI.toast(
        Recorder.isSecureContext()
          ? "这个浏览器不支持录音，请换 Chrome / Edge / Safari"
          : "录音需要 https 或 localhost 打开，局域网 http 地址录不了",
        "warning", 7000
      );
      return;
    }

    state.recording = true;
    holdBtn.dataset.recording = "1";
    holdBtn.textContent = "松开即发送";
    tipEl.hidden = false;

    state.startPromise = Recorder.start();
    try {
      await state.startPromise;
    } catch (err) {
      state.recording = false;
      holdBtn.dataset.recording = "0";
      holdBtn.textContent = "按住说话";
      tipEl.hidden = true;
      UI.toast("麦克风打不开：" + (err && err.message ? err.message : ""), "error", 6000);
    }
  }

  async function endHold() {
    if (!state.recording) return;
    state.recording = false;
    holdBtn.dataset.recording = "0";
    holdBtn.textContent = "按住说话";
    tipEl.hidden = true;

    // 用户可能按得很快：等 start() 真正建好采集链再 stop，否则拿到 null
    if (state.startPromise) {
      try { await state.startPromise; } catch (_) { /* 已在 startHold 提示过 */ }
    }
    let clip = null;
    try {
      clip = await Recorder.stop();
    } catch (_) {
      clip = null;
    }
    if (!clip) {
      UI.toast("没录到声音，靠近一点再说一次", "warning");
      return;
    }
    if (clip.durationMs < MIN_VOICE_MS) {
      UI.toast("说话时间太短了", "warning");
      return;
    }
    send({
      text: input.value,
      imageFile: state.image ? state.image.file : null,
      audioBlob: clip.blob,
      durationMs: clip.durationMs,
    });
    input.value = "";
    autoGrow();
  }

  /* ---------------------------------------------------------------- 杂项 */

  function autoGrow() {
    input.style.height = "auto";
    // +2 是上下边框：scrollHeight 不含边框，直接赋给它会让实际高度每次少 2px
    const h = Math.min(Math.max(input.scrollHeight + 2, 44), 132);
    input.style.height = h + "px";
  }

  function resolveStandardUrl() {
    const KEY = "agri.chat.from";
    const fromQuery = UI.query("from");
    const candidates = [fromQuery, sessionStorage.getItem(KEY)];

    if (document.referrer) {
      try {
        const ref = new URL(document.referrer);
        if (ref.origin === location.origin && ref.pathname !== location.pathname) {
          candidates.push(ref.pathname + ref.search);
        }
      } catch (_) { /* referrer 不是合法 URL 就忽略 */ }
    }

    for (const c of candidates) {
      // 只接受站内绝对路径：`//evil.com` 这种协议相对地址会被浏览器当成外站
      if (c && c.charAt(0) === "/" && c.charAt(1) !== "/") return c;
    }
    return "/index.html";
  }

  function bind() {
    // 与标准版顶栏同一个标记尺寸（layout.js 里 brand__mark 用 22）
    $("brand-mark").innerHTML = window.Icon ? Icon.render("leaf", 22) : "";
    imageBtn.innerHTML = window.Icon ? Icon.render("image", 20) : "图";

    const back = resolveStandardUrl();
    standardLink.href = back;
    try { sessionStorage.setItem("agri.chat.from", back); } catch (_) { /* 隐私模式 */ }

    // 320px 级别的窄屏上输入框只有一百来像素，长占位语会被折行再裁掉半句
    if (window.matchMedia("(max-width: 380px)").matches) {
      input.placeholder = "说说症状…";
    }

    setMode("text", false);
    if (window.Dialect) Dialect.init();

    sendBtn.addEventListener("click", () => {
      const text = input.value;
      const imageFile = state.image ? state.image.file : null;
      if (!text.trim() && !imageFile) {
        UI.toast("说一句话，或者拍张照片", "warning");
        return;
      }
      send({ text: text, imageFile: imageFile });
      input.value = "";
      autoGrow();
    });

    input.addEventListener("input", autoGrow);
    input.addEventListener("keydown", (e) => {
      // 中文输入法组词时按回车是「选字」，不能当成发送
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendBtn.click();
      }
    });

    modeBtn.addEventListener("click", () => {
      setMode(state.mode === "voice" ? "text" : "voice");
    });

    holdBtn.addEventListener("pointerdown", startHold);
    document.addEventListener("pointerup", endHold);
    document.addEventListener("pointercancel", endHold);
    window.addEventListener("pagehide", () => {
      if (window.Recorder && Recorder.isRecording()) Recorder.cancel();
    });

    imageBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (!file) return;
      if (!/^image\//.test(file.type)) {
        UI.toast("只能上传图片", "warning");
        return;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        UI.toast("图片太大了（上限 8 MB），可以先在相册里裁一下", "warning", 6000);
        return;
      }
      setPendingImage(file);
    });
    attachRemove.addEventListener("click", clearPendingImage);

    zoom.addEventListener("click", () => {
      zoom.hidden = true;
      zoomImg.removeAttribute("src");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !zoom.hidden) {
        zoom.hidden = true;
        zoomImg.removeAttribute("src");
      }
    });

    renderHero();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
