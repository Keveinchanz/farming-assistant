/* ==========================================================================
   任务 5.3 —— 智能诊断页
   · 三种输入：图片（拍摄/上传/拖拽/粘贴）、语音（录音/上传）、文字
   · 提交后隐藏输入区，切换为结果展示区
   · 结果区右上角「方言播报」

   关于录音格式（重要）：
       采集与 WAV 封装已抽到公共模块 /assets/js/recorder.js（window.Recorder），
       知识图书库的语音检索与这里共用同一份实现 —— 那段逻辑里有「必须经 gain=0 的
       静音节点，否则麦克风声音从扬声器放出来形成啸叫」这类真踩过的坑，
       两个页面各留一份迟早会分叉。本页只负责界面：按钮文案、计时器、预览、移除。
   ========================================================================== */

(function () {
  "use strict";

  const MAX_IMAGE_MB = 20;
  const MAX_AUDIO_MB = 20;

  /* 界面上认得的音频扩展名。能否真的解开由浏览器解码器决定，
     这份列表只用来挡住明显不是音频的文件（选错文件时立刻给提示）。 */
  const AUDIO_EXT = [
    "wav", "mp3", "m4a", "m4b", "aac", "ogg", "oga", "opus",
    "flac", "webm", "aiff", "aif", "amr", "pcm", "wma", "3gp",
  ];

  const state = {
    imageFile: null,
    audioBlob: null,
    audioName: "",
    audioDuration: 0,
    lastResult: null,
    // 用户这次填的文字。问诊单的「现场情况」要它，而 report 里只有模型改写后的
    // symptom_match —— 那已经把原话揉碎了，不像现场记录。
    lastText: "",
    // 归档结果（保存成功后才有）。问诊单上的编号 / 地块 / 回访日来自这里，
    // 没归档时那张纸照打，只是编号栏写「现场未归档」。
    archive: null,
    // 田块列表的加载状态。问诊单与归档块都要用它，所以放在模块作用域，
    // 免得同一页开两次问诊单就请求两次。
    fields: { loaded: false, list: [], province: "" },
    // 本省当季高发（问诊单第六段）。分享快照与 PDF 都要它，
    // 所以存起来而不是只写进 DOM —— 从 innerHTML 里反解析回来是不可接受的。
    regionAlert: null,
    // 本地产收提醒（问诊单最后一段）。同样是「DOM 只是渲染结果」：
    // 分享页、服务端 PDF、复制文本都要同一份数据，不能各自算一遍，
    // 更不能从 innerHTML 反解析。null 表示没拉到、或这块田没有可靠结论。
    sprayNote: null,
    // 已生成的分享（token/url）。同一张纸反复点「分享」不该每次都换一条链接 ——
    // 换一条，先前发给农户的那条就再也对不上了。
    sheetShare: null,
  };

  const $ = (id) => document.getElementById(id);

  /* ==================================================================
     图片输入
     ================================================================== */

  function setImage(file) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      UI.toast("请选择图片文件", "error");
      return;
    }
    if (file.size > MAX_IMAGE_MB * 1024 * 1024) {
      UI.toast(`图片过大（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 ${MAX_IMAGE_MB} MB`, "error");
      return;
    }

    state.imageFile = file;
    const host = $("image-preview");
    host.hidden = false;
    host.innerHTML =
      `<img class="preview-thumb" alt="待诊断图片预览" src="${URL.createObjectURL(file)}">` +
      `<div class="stack" style="flex:1 1 200px;min-width:0">` +
      `<div class="text-sm"><b>${UI.escapeHtml(file.name)}</b></div>` +
      `<div class="text-sm text-muted">${(file.size / 1024).toFixed(0)} KB</div>` +
      `<button type="button" class="btn btn--sm btn--outline" id="btn-clear-image">移除图片</button>` +
      `</div>`;
    $("btn-clear-image").addEventListener("click", clearImage);
  }

  function clearImage() {
    state.imageFile = null;
    const host = $("image-preview");
    host.hidden = true;
    host.innerHTML = "";
  }

  /* 「调用摄像头拍摄」按钮打不开摄像头时的说法；key 必须与 camera.js 的
     window.Camera.REASON 取值一一对应。 */
  const CAMERA_FAIL = {
    unsupported: "当前环境不支持直接开摄像头（手机上需用 https 打开），已切回选择图片",
    denied: "没有拿到摄像头权限，已切回选择图片",
    no_device: "没有找到可用的摄像头，已切回选择图片",
    error: "摄像头启动失败，已切回选择图片",
  };

  /* 真的开摄像头取景。
     原先这里只是一句 $("input-camera").click()，而那个隐藏 input 靠 capture 属性
     指望浏览器开摄像头 —— 桌面浏览器与多数 WebView 直接忽略 capture，
     于是弹出来的是「选择文件」（用户报的就是这个）。真正开摄像头只有媒体采集
     一条路，代码在公共模块 /assets/js/camera.js（window.Camera）里 ——
     那条测试锁着「采集能力原语只有一份」，本文件里连这个 API 的名字都不许出现
     （跟录音必须走 recorder.js 是同一个道理）。

     打不开就**回落**到文件选择器，不留死胡同；手机用 http://<局域网IP> 打开时
     navigator.mediaDevices 是 undefined，走的就是这条回落。 */
  function openCamera() {
    const fallback = () => $("input-camera").click();
    if (!window.Camera || !Camera.supported()) {
      fallback();
      return;
    }
    const started = Camera.open(setImage, (reason) => {
      UI.toast(CAMERA_FAIL[reason] || "摄像头打不开，已切回选择图片", "error");
      fallback();
    });
    if (!started) fallback();
  }

  function bindImageInputs() {
    $("btn-camera").addEventListener("click", openCamera);
    $("btn-choose-image").addEventListener("click", () => $("input-image").click());
    $("input-camera").addEventListener("change", (e) => setImage(e.target.files[0]));
    $("input-image").addEventListener("change", (e) => setImage(e.target.files[0]));

    const dz = $("image-dropzone");
    ["dragenter", "dragover"].forEach((evt) =>
      dz.addEventListener(evt, (e) => {
        e.preventDefault();
        dz.classList.add("is-dragover");
      })
    );
    ["dragleave", "drop"].forEach((evt) =>
      dz.addEventListener(evt, (e) => {
        e.preventDefault();
        dz.classList.remove("is-dragover");
      })
    );
    dz.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) setImage(file);
    });
    dz.addEventListener("click", () => $("input-image").click());
    dz.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        $("input-image").click();
      }
    });

    // 支持直接粘贴截图
    document.addEventListener("paste", (e) => {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const item of items) {
        if (item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            setImage(file);
            UI.toast("已从剪贴板读取图片", "info");
          }
          break;
        }
      }
    });
  }

  /* ==================================================================
     语音输入 —— 采集与 WAV 封装在公共模块 recorder.js，这里只管界面
     ================================================================== */

  let offTick = null;

  /* 失败文案按 Recorder 抛出的 e.name 分三种。第三句是新增的：
     以前用 http://<局域网 IP>:8000 打开时 navigator.mediaDevices 根本不存在，
     会被归到「浏览器不支持录音」，用户就去换浏览器了 ——
     真实原因是那种访问方式不是安全上下文，提示必须指对方向。 */
  function recordErrorMessage(e) {
    const name = e && e.name;
    if (name === "unsupported") return "当前浏览器不支持录音，请改用「上传本地音频」";
    if (name === "insecure") return "录音需要 https 或 localhost 环境，请改用「上传本地音频」，或用 127.0.0.1 访问本页";
    if (name === "denied") return "无法访问麦克风：" + e.message + "（请检查浏览器权限设置）";
    return "录音启动失败：" + ((e && e.message) || "未知原因");
  }

  function setRecordingUi(on) {
    $("record-status").hidden = !on;
    $("btn-record-label").textContent = on ? "停止录制" : "麦克风录制";
    $("btn-record").classList.toggle("btn--outline", !on);
    $("btn-record").classList.toggle("btn--primary", on);

    if (on) {
      $("record-time").textContent = "00:00";
      if (!offTick) {
        offTick = Recorder.on("tick", (ms) => {
          $("record-time").textContent = Recorder.formatDuration(ms);
        });
      }
    } else if (offTick) {
      offTick();
      offTick = null;
    }
  }

  async function toggleRecording() {
    if (Recorder.isRecording()) {
      const hit = await Recorder.stop();
      setRecordingUi(false);
      if (!hit) {
        UI.toast("没有录到声音，请检查麦克风后重试", "error");
        return;
      }
      state.audioDuration = hit.durationMs / 1000;
      setAudio(hit.blob, `录音_${new Date().toTimeString().slice(0, 8).replace(/:/g, "")}.wav`);
      return;
    }

    try {
      await Recorder.start();
    } catch (e) {
      UI.toast(recordErrorMessage(e), "error");
      return;
    }
    setRecordingUi(true);
  }

  function setAudio(blob, name, note) {
    state.audioBlob = blob;
    state.audioName = name || "record.wav";
    const host = $("audio-preview");
    host.hidden = false;
    const seconds = state.audioDuration || 0;
    host.innerHTML =
      `<div class="preview-audio">` +
      `<div class="text-sm"><b>${UI.escapeHtml(name)}</b>　` +
      `<span class="text-muted">${(blob.size / 1024).toFixed(0)} KB` +
      (seconds ? `　时长约 ${seconds.toFixed(1)} 秒` : "") +
      (note ? `　${UI.escapeHtml(note)}` : "") + `</span></div>` +
      `<audio controls preload="metadata"></audio>` +
      `<button type="button" class="btn btn--sm btn--outline" id="btn-clear-audio">移除语音</button>` +
      `</div>`;
    host.querySelector("audio").src = URL.createObjectURL(blob);
    $("btn-clear-audio").addEventListener("click", clearAudio);
  }

  function clearAudio() {
    state.audioBlob = null;
    state.audioName = "";
    state.audioDuration = 0;
    const host = $("audio-preview");
    host.hidden = true;
    host.innerHTML = "";
  }

  /* 上传的音频能是什么格式，取决于浏览器自带的解码器，而不是后端的白名单：
     统一先用 Recorder.fileToWav() 解成 16kHz 单声道 WAV 再提交，
     mp3 / m4a(aac) / ogg / opus / flac / webm / aiff / wav 因此都能直接用。
     只有浏览器解不开的（amr、wma 这类）才原样送上去，由后端判断能否识别。 */
  async function loadAudioFile(file) {
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const looksAudio = AUDIO_EXT.indexOf(ext) >= 0 || (file.type || "").startsWith("audio/");
    if (!looksAudio) {
      UI.toast("请选择音频文件", "error");
      return;
    }
    if (file.size > MAX_AUDIO_MB * 1024 * 1024) {
      UI.toast(
        `音频过大（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 ${MAX_AUDIO_MB} MB`,
        "error"
      );
      return;
    }

    if (typeof Recorder !== "undefined" && Recorder.fileToWav) {
      try {
        const converted = await Recorder.fileToWav(file);
        state.audioDuration = converted.durationMs / 1000;
        setAudio(converted.blob, file.name, "已转成 16kHz 单声道");
        return;
      } catch (err) {
        /* 解码失败不拦：amr / wma 这类浏览器不认识，后端或许还能处理 */
        UI.toast(
          ((err && err.message) || "音频解码失败") + "，将按原文件上传。",
          "warning",
          6000
        );
      }
    }

    state.audioDuration = 0;
    setAudio(file, file.name, "");
  }

  function bindAudioInputs() {
    $("btn-record").addEventListener("click", toggleRecording);

    $("btn-choose-audio").addEventListener("click", () => $("input-audio").click());
    $("input-audio").addEventListener("change", (e) => {
      const file = e.target.files[0];
      e.target.value = "";      // 清空后才能再次选中同一个文件
      if (!file) return;
      loadAudioFile(file);
    });
  }

  /* ==================================================================
     提交与结果
     ================================================================== */

  function bindForm() {
    const text = $("input-text");
    text.addEventListener("input", () => {
      $("text-count").textContent = String(text.value.length);
    });

    $("diagnose-form").addEventListener("submit", (e) => {
      e.preventDefault();
      submit();
    });
  }

  function submit() {
    if (!state.imageFile && !state.audioBlob && !$("input-text").value.trim()) {
      UI.toast("请至少提供一项信息：上传照片、录一段语音，或填写症状描述", "warning");
      return;
    }

    const form = new FormData();
    if (state.imageFile) form.append("image", state.imageFile, state.imageFile.name || "upload.jpg");
    if (state.audioBlob) form.append("audio", state.audioBlob, state.audioName || "record.wav");
    form.append("text", $("input-text").value.trim());
    form.append("dialect", window.Dialect ? Dialect.current() : "");

    state.lastText = $("input-text").value.trim();
    state.archive = null;      // 新的一次诊断，旧的归档信息不能跟着串到这一页上

    const restore = UI.busy($("btn-submit"), "诊断中…");
    $("result-root").hidden = true;

    Api.diagnoseMultimodal(form)
      .then((data) => {
        restore();
        state.lastResult = data;
        renderResult(data);
      })
      .catch((err) => {
        restore();
        UI.toast("诊断失败：" + err.message, "error", 7000);
      });
  }

  function renderResult(data) {
    const form = $("diagnose-form");
    const root = $("result-root");
    const report = data.report || {};
    const rec = data.recognition || {};
    const top1 = rec.top1 || null;
    const sections = report.treatment_sections || [];

    // 症状匹配说明里如果有候选人名，用高亮
    const degradedTags = [];
    if ((data.degraded || {}).llm) degradedTags.push("大模型：未配置");
    if ((data.degraded || {}).rag) degradedTags.push("知识库：未启用检索");
    if ((data.degraded || {}).voice) degradedTags.push("语音：未配置");
    if ((data.degraded || {}).pest) degradedTags.push("虫害检测：本次不可用");

    // 主结论可能是虫害（fusion._apply_pest_primary 在检出虫害时会切换）。
    // 置信度必须跟着主结论走：否则会出现「96% 的虫害」旁边挂一条「低置信度」警告 ——
    // 那个 25% 来自病害那一路，跟主结论没关系。
    const primary = report.primary || null;
    const pestPrimary = !!(primary && primary.kind === "pest");
    const shownConf = pestPrimary ? (primary.confidence || 0) : (report.confidence || 0);
    const confidencePct = Math.round(shownConf * 100);
    const lowConf = shownConf < 0.45;

    root.hidden = false;
    root.innerHTML =
      `<div class="card result-panel">` +
      // 方言播报按钮固定在结果区右上角
      `<button type="button" class="btn btn--outline btn--sm result-panel__speak" id="btn-speak">` +
      Icon.render("volume", 16) + `<span>方言播报</span></button>` +
      // 顺序就是「农户要什么」的顺序：这是什么病 → 现在该做什么 → 完整方案 →
      // 用药注意；「这个结论怎么来的」整块降到后面（见 _evidence）。
      _hero(report, top1, degradedTags, primary) +
      _primaryAction(sections) +
      // 农技员的交付物是「一张能发给农户的纸」，不是停在浏览器里的一句话。
      // 所以生成问诊单的入口紧跟结论之后的第二屏位置，而不是压在页面最底下。
      _handoutBar(report) +
      // 虫害紧跟在「先做这一步」之后：它是能直接改变处置方案的现场证据
      // （有虫就得先治虫，用药时机和病害完全不同），压在页面底下等于白做。
      // 检出虫害时它同时也是**主结论**，所以把 pestPrimary 传进去改措辞。
      _pestBlock(data.pest, report.pest_findings, pestPrimary) +
      _sectionsBlock(sections) +
      _textBlock("用药提醒", report.medication_reminder, "warning") +
      _evidence(
        _topkBlock(rec.topk || []) +
          _asrBlock(data.asr) +
          _textBlock("症状匹配说明", report.symptom_match, "info") +
          _sourcesBlock(data.sources || []) +
          _fusedSummary(data) +
          // 需复核时不显示置信度：那是「照片像健康」的把握，不是这条结论的把握，
          // 挂在「照片与描述不一致」下面会被读成「结论有 90% 的把握」。
          (report.needs_review ? "" : _confidenceBlock(confidencePct, lowConf))
      ) +
      _askBlock(report) +
      // 归档排在追问之后：追问答完了，这条结论才算定稿，落档的才是最终版
      _recordBlock(report) +
      _sourceNotes(report.source_notes) +
      _actions(report) +
      `</div>`;

    form.hidden = true;
    // 结果页要独占一屏：批量入口留在下面会让人以为「还能再传一批」，
    // 而此刻的上下文是「这一次诊断」，不是队列。
    $("batch-card").hidden = true;
    root.scrollIntoView({ behavior: "smooth", block: "start" });

    Dialect.bindSpeakButton($("btn-speak"), () => buildReadText(report, sections, data));
    $("btn-restart").addEventListener("click", resetToInput);
    bindHandout(report, data);
    bindAsk(report);
    bindRecord(report, data);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /* ==================================================================
     疑似虫害（IP102 / YOLOv8n 目标检测，与病害是两套独立标签）

     三条硬约束：

     1. **不引 canvas、不引新依赖**：检测框是百分比定位的 HTML 覆盖层。
        成立的前提是图片元素的渲染尺寸与被检图片的像素尺寸严格同比例 ——
        所以下面 <img> 用 width:100% + height:auto 走自然宽高比，
        外层 figure 不能加内边距、不能用 object-fit:cover、不能给固定高度，
        任何一条违反了覆盖层都会整体错位。

     2. 后端给的 image_size 是**原图像素尺寸**，bbox 也是原图坐标系，
        所以 left/top/width/height 就是「框坐标 ÷ 原图尺寸」，不需要再换算。

     3. 三种状态必须分清（契约见 src/llm/fusion.py）：
          pest 为 null  → 本次没跑检测，整块不渲染（说"未发现虫害"是撒谎）
          count === 0   → 跑了，确实没虫子，明说
          count > 0     → 画框 + 列表
     ================================================================== */

  // 列表最多列几类。检测器在一张图上可能出几十个框，全列出来会把结果页淹掉；
  // 超出部分只在图上画框不编号，并在末尾给一句说明，避免"图上有框、列表找不到"。
  const PEST_GROUP_LIMIT = 6;

  function _pestBlock(pest, findings, pestPrimary) {
    if (!pest) return "";

    const count = pest.count || 0;
    // 检出虫害时它就是本次的主结论，措辞不能再写「疑似」。
    const title = pestPrimary ? "虫害（本次主结论）" : "疑似虫害";
    const head = (countText) =>
      `<div class="result-pest__head">` +
      `<span class="result-pest__label">${Icon.render("bug", 18)}${UI.escapeHtml(title)}</span>` +
      `<span class="result-pest__count">${UI.escapeHtml(countText)}</span>` +
      `</div>`;

    // 没检出时给一句轻描淡写的话就够 —— 「没虫」是好消息，不该做成一个警示色大块。
    // 但也不能不吭声：农户会觉得"到底查了没"。
    if (!count) {
      return (
        `<div class="result-pest result-pest--clear">` +
        head("未检出") +
        `<p class="result-pest__note">本次图片在检测阈值（` +
        `${Math.round((pest.confidence_threshold || 0) * 100)}%）以上未发现虫害。` +
        `虫害与病害是两套独立的识别能力，这一条不影响上面的病害结论。</p>` +
        `</div>`
      );
    }

    // 按类别分组：同一种虫子在一张图上常有多个框，不分组的话列表会变成
    // 一长串重复的名字，农户根本看不出重点
    const groups = [];
    const byName = new Map();
    (pest.detections || []).forEach((d, i) => {
      const key = d.label_cn || String(d.index);
      let g = byName.get(key);
      if (!g) {
        g = { name: key, pestType: d.pest_type || "", crop: d.crop || "", best: 0, nums: [] };
        byName.set(key, g);
        groups.push(g);
      }
      g.nums.push(i + 1);
      if ((d.confidence || 0) > g.best) g.best = d.confidence || 0;
    });

    const adviceOf = new Map();
    (findings || []).forEach((f) => adviceOf.set(f.name, f.advice || ""));

    const shown = groups.slice(0, PEST_GROUP_LIMIT);
    const hiddenCount = groups.length - shown.length;
    const numbered = new Set();
    shown.forEach((g) => g.nums.forEach((n) => numbered.add(n)));

    const iw = (pest.image_size || [])[0] || 0;
    const ih = (pest.image_size || [])[1] || 0;
    const pct = (v, total) => (total > 0 ? (v / total) * 100 : 0);

    const boxes = (pest.detections || [])
      .map((d, i) => {
        const b = d.bbox || [0, 0, 0, 0];
        const style =
          `left:${pct(b[0], iw).toFixed(2)}%;top:${pct(b[1], ih).toFixed(2)}%;` +
          `width:${Math.max(pct(b[2] - b[0], iw), 0).toFixed(2)}%;` +
          `height:${Math.max(pct(b[3] - b[1], ih), 0).toFixed(2)}%`;
        const n = i + 1;
        return numbered.has(n)
          ? `<span class="pest-box" style="${style}"><i class="pest-box__no">${n}</i></span>`
          : `<span class="pest-box pest-box--muted" style="${style}"></span>`;
      })
      .join("");

    // 结果页是新的 DOM，原输入区的预览已经被隐藏了，所以这里重新挂一张同样的图
    const url = state.imageFile ? URL.createObjectURL(state.imageFile) : "";
    const figure = url
      ? `<figure class="pest-figure">` +
        `<img class="pest-figure__img" src="${url}" alt="虫害检测标注图">` +
        boxes +
        `</figure>`
      : "";

    const items = shown
      .map((g) => {
        const advice = adviceOf.get(g.name) || "";
        return (
          `<li class="pest-item">` +
          `<span class="pest-item__no">${g.nums.join("、")}</span>` +
          `<div class="pest-item__main">` +
          `<div class="pest-item__head">` +
          `<span class="pest-item__name">${UI.escapeHtml(g.name)}</span>` +
          (g.pestType ? `<span class="badge badge--warning">${UI.escapeHtml(g.pestType)}</span>` : "") +
          (g.crop ? `<span class="badge">${UI.escapeHtml(g.crop)}</span>` : "") +
          `<span class="pest-item__conf">置信度 ${Math.round(g.best * 100)}%</span>` +
          `</div>` +
          (advice ? `<p class="pest-item__advice">${UI.escapeHtml(advice)}</p>` : "") +
          `</div>` +
          `</li>`
        );
      })
      .join("");

    return (
      `<div class="result-pest">` +
      head(`检出 ${count} 处，涉及 ${groups.length} 类`) +
      `<div class="result-pest__body">` +
      figure +
      `<ol class="pest-list">${items}</ol>` +
      `</div>` +
      (hiddenCount > 0
        ? `<p class="result-pest__more">图上另有 ${hiddenCount} 类未在列表列出（对应未编号的框）。</p>`
        : "") +
      `<p class="result-pest__note">框中编号与列表编号一一对应。` +
      (pestPrimary
        ? `上面的防治方案与用药提醒已按本次检出的虫害给出；`
        : `检测结果仅供辅助判断，`) +
      `具体用药请以当地植保部门指导与农药登记标签为准。</p>` +
      `</div>`
    );
  }

  function _hero(report, top1, degradedTags, primary) {
    // 主结论可能是虫害：见 fusion._apply_pest_primary。此时标题写虫名，
    // 而「作物 / 病原类型 / 健康样本」这三个角标都来自**病害**那一路 ——
    // 分类器在虫害图上照样会吐一个病害 Top-1，拿它的作物与病原类型当角标是错的。
    const pestPrimary = !!(primary && primary.kind === "pest");
    // 健康是一个**结论**，不是「没判断出来」。不加这一条，标题会写「未能确定病害」，
    // 角标却写「健康样本」—— 两个自相矛盾的东西并排放在结果页最显眼的位置。
    // 需复核（图文冲突）时反倒必须先压掉 top1 的健康判定：照片是健康类，但用户说了
    // 具体病害，这时挂一个「健康样本」角标，等于替照片把用户的话否掉了。
    const review = !pestPrimary && !!report.needs_review;
    const healthy = !pestPrimary && !review
      && !!(report.healthy || (top1 && top1.is_healthy));
    const name = review
      ? "照片与描述不一致"
      : ((primary && primary.name) || report.disease_name
        || (healthy ? "未见明显病斑" : "未能确定病害"));
    const crop = pestPrimary
      ? (primary.crop || "")
      : (top1 && top1.crop ? top1.crop : "");
    const patho = pestPrimary
      ? (primary.detail || "")
      : (top1 && top1.pathogen_type ? top1.pathogen_type : "");

    return (
      `<div class="result-hero">` +
      `<div class="result-hero__main">` +
      `<div class="result-hero__label">${pestPrimary ? "诊断结果（虫害）" : (review ? "需人工复核" : (healthy ? "巡检结果" : "诊断结果"))}</div>` +
      `<h2 class="result-hero__name">${UI.escapeHtml(name || "—")}</h2>` +
      `<div class="result-hero__tags">` +
      (crop ? `<span class="badge badge--primary">${UI.escapeHtml(crop)}</span>` : "") +
      (patho ? `<span class="badge">${UI.escapeHtml(patho)}</span>` : "") +
      (healthy ? `<span class="badge badge--info">健康样本</span>` : "") +
      degradedTags.map((t) => `<span class="badge badge--warning">${UI.escapeHtml(t)}</span>`).join("") +
      `</div>` +
      (pestPrimary && primary.note
        ? `<p class="text-sm" style="margin-top:8px">${UI.escapeHtml(primary.note)}</p>`
        : "") +
      (report.summary ? `<p class="text-sm" style="margin-top:10px">${UI.escapeHtml(report.summary)}</p>` : "") +
      `</div>` +
      `</div>`
    );
  }

  /* 只有标题「像动作」的小节才配当「先做这一步」。
     不能直接取 sections[0]：Mock 的第一节是「发病规律与判断依据」，
     那是背景不是动作 —— 取错了等于把「现在干什么」答成「这病怎么回事」。

     收词时对着真实会出现的标题点过一遍：Mock 七种病原类型（真菌/细菌/病毒/
     生理性/药害肥害/其他/健康）加上 DeepSeek 的（农业防治 / 物理与生物防治 /
     化学防治）都能命中；「处置」是给「无候选」那条兜底留的 —— 那里的两个标题是
     「建议补充的信息」与「临时处置建议」，只有后者是动作，而它正是农户该先做的
     （隔离病株、别盲目施药）。 */
  const ACTION_RE = /防治|用药|处置|处理|措施|管理/;

  /** 「先做这一步」：结果页的主角，紧跟在病害名下面。 */
  function _primaryAction(sections) {
    if (!sections.length) return "";
    const action = sections.find((s) => ACTION_RE.test(s.title || "")) || sections[0];
    return (
      `<div class="result-action">` +
      `<div class="result-action__label">${Icon.render("shield", 18)}先做这一步</div>` +
      `<div class="result-action__title">${UI.escapeHtml(action.title || "")}</div>` +
      `<p class="result-action__text">${UI.escapeHtml(action.content || "")}</p>` +
      `</div>`
    );
  }

  /* 置信度从 hero 里降级到这里。「大标题 + 小标签 + 统计条」是被点名的通用默认款，
     而农户要的是「这病怎么治」—— 数据一个不删，只是不再占据第一眼。 */
  function _confidenceBlock(confidencePct, lowConf) {
    return (
      `<div class="confidence">` +
      `<div class="confidence__row"><span>置信度</span><span class="confidence__value">${confidencePct}%</span></div>` +
      `<div class="confidence__track" role="progressbar" aria-valuenow="${confidencePct}" ` +
      `aria-valuemin="0" aria-valuemax="100" aria-label="诊断置信度">` +
      `<div class="confidence__fill${lowConf ? " confidence__fill--low" : ""}" style="width:${confidencePct}%"></div>` +
      `</div>` +
      (lowConf
        ? `<p class="text-sm" style="margin:0;color:var(--color-warning)">` +
          `置信度偏低，建议补充更清晰的照片或更详细的症状描述</p>`
        : "") +
      `</div>`
    );
  }

  /* 判断依据：模型候选、语音转写、症状匹配、知识库引用、融合说明、置信度 ——
     全是「这个结论怎么来的」。给农技员和答辩看，不是农户的第一需求，
     所以整块移到后面、用分隔线隔开、标题字号压小（见 pages.css 的 .result-evidence）。
     内容一条不删，只是视觉重量明显低于上面的动作区。 */
  function _evidence(inner) {
    if (!inner) return "";
    return (
      `<div class="result-evidence">` +
      `<div class="result-evidence__head">判断依据</div>` +
      inner +
      `</div>`
    );
  }

  function _topkBlock(topk) {
    if (!topk.length) return "";
    const rows = topk
      .map((t) => {
        const pct = Math.round(t.confidence * 1000) / 10;
        return (
          `<div class="topk-item">` +
          `<span class="topk-item__name">${UI.escapeHtml(t.label_cn)}` +
          (t.is_healthy ? ` <span class="badge">健康</span>` : "") + `</span>` +
          `<span class="topk-item__bar"><span class="topk-item__fill" style="width:${pct}%"></span></span>` +
          `<span class="topk-item__pct">${pct.toFixed(1)}%</span>` +
          `</div>`
        );
      })
      .join("");
    return (
      `<div class="result-block">` +
      `<div class="result-block__title">${Icon.render("scan", 18)}模型候选（Top-${topk.length}）</div>` +
      `<div class="topk-list">${rows}</div>` +
      `</div>`
    );
  }

  function _asrBlock(asr) {
    if (!asr) return "";
    const text = asr.text || "";
    const terms = asr.matched_terms || [];
    let body;
    if (text) {
      body = `<div class="asr-box__text">${UI.escapeHtml(text)}</div>`;
    } else {
      body = `<div class="text-muted">${UI.escapeHtml(asr.message || "未获得语音转写文本")}</div>`;
    }

    let note = "";
    if (asr.fallback && asr.fallback_note) {
      note = `<p class="text-sm" style="margin:8px 0 0;color:var(--color-warning)">${UI.escapeHtml(asr.fallback_note)}</p>`;
    }
    if (terms.length) {
      note += `<p class="text-sm text-muted" style="margin:8px 0 0">识别到农业术语：` +
        terms.map((t) => `<span class="badge badge--primary">${UI.escapeHtml(t)}</span>`).join(" ") +
        `</p>`;
    }

    return (
      `<div class="result-block">` +
      `<div class="result-block__title">${Icon.render("mic", 18)}语音转写（${UI.escapeHtml(asr.dialect_name || "")}）</div>` +
      `<div class="asr-box">${body}${note}</div>` +
      `</div>`
    );
  }

  function _textBlock(title, text, kind) {
    if (!text) return "";
    const cls = kind === "warning" ? "detail-section__text" : "result-block__text";
    return (
      `<div class="result-block">` +
      `<div class="result-block__title">${Icon.render(kind === "warning" ? "warning" : "info", 18)}${UI.escapeHtml(title)}</div>` +
      `<p class="${cls}">${UI.escapeHtml(text)}</p>` +
      `</div>`
    );
  }

  function _sectionsBlock(sections) {
    if (!sections.length) return "";
    const items = sections
      .map(
        (s) =>
          `<div class="treatment-item">` +
          `<div class="treatment-item__title">${UI.escapeHtml(s.title)}</div>` +
          `<p class="treatment-item__text">${UI.escapeHtml(s.content)}</p>` +
          `</div>`
      )
      .join("");
    return (
      `<div class="result-block">` +
      `<div class="result-block__title">${Icon.render("shield", 18)}综合防治方案</div>` +
      `<div class="treatment-list">${items}</div>` +
      `</div>`
    );
  }

  function _sourcesBlock(sources) {
    if (!sources.length) return "";
    const rows = sources
      .map((s) => {
        // 知识库按作物分册，作物内资料不够时会补别的作物进来，必须标出来
        const cross =
          s.same_crop === false && s.crop
            ? ` <span class="source-list__score">其他作物：${UI.escapeHtml(s.crop)}</span>`
            : "";
        return (
          `<li>${Icon.render("book", 14)}<span>${UI.escapeHtml(s.title)}` +
          (s.score != null ? ` <span class="source-list__score">相似度 ${(s.score * 100).toFixed(0)}%</span>` : "") +
          cross +
          `</span></li>`
        );
      })
      .join("");
    return (
      `<div class="result-block">` +
      `<div class="result-block__title">${Icon.render("book", 18)}知识库引用</div>` +
      `<ul class="source-list">${rows}</ul>` +
      `</div>`
    );
  }

  function _sourceNotes(notes) {
    return UI.sourceNote(notes, "来源");
  }

  function _fusedSummary(data) {
    const bits = [];
    if (data.fused_summary) bits.push(data.fused_summary);
    const src = (data.sources || []).length;
    if (bits.length === 0 && !src) return "";
    return (
      `<div class="result-block">` +
      `<div class="notice notice--info">${Icon.render("info", 18)}` +
      `<span>${UI.escapeHtml(bits.join("　"))}</span></div>` +
      `</div>`
    );
  }

  function _askBlock(report) {
    return (
      `<div class="result-block">` +
      `<div class="result-block__title">${Icon.render("info", 18)}追问农技专家</div>` +
      `<div class="row">` +
      `<label class="visually-hidden" for="ask-input">追问内容</label>` +
      `<input class="input grow" id="ask-input" type="text" maxlength="500" ` +
      `placeholder="比如：什么时候打药最合适？会不会传染到旁边地块？">` +
      `<button type="button" class="btn btn--primary" id="btn-ask">提问</button>` +
      `</div>` +
      `<div id="ask-answer" class="stack" style="margin-top:12px"></div>` +
      `</div>`
    );
  }

  function bindAsk(report) {
    const input = $("ask-input");
    const button = $("btn-ask");
    const host = $("ask-answer");
    if (!input || !button || !host) return;

    function submit() {
      const q = input.value.trim();
      if (!q) {
        UI.toast("请先输入要追问的问题", "warning");
        return;
      }

      const primary = report.primary || {};
      const conf = primary.confidence || report.confidence || 0;
      const context = [
        primary.name
          ? `当前诊断结论：${primary.name}${primary.kind === "pest" ? "（虫害）" : ""}`
          : "",
        primary.name && conf ? `置信度 ${(conf * 100).toFixed(0)}%` : "",
        report.summary || "",
      ].filter(Boolean).join("；");

      const restore = UI.busy(button, "提问中");
      Api.ask(q, context)
        .then((res) => {
          restore();
          input.value = "";
          const p = document.createElement("div");
          p.className = "notice notice--primary notice--answer";
          p.innerHTML = Icon.render("info", 18) + `<span class="answer-text"></span>`;
          // 用 textContent 写入回答，避免把模型返回的文本当 HTML 解析；
          // 换行交给 .answer-text 的 white-space: pre-line 保留
          p.querySelector("span").textContent = res.answer;
          host.appendChild(p);
        })
        .catch((err) => {
          restore();
          UI.toast("提问失败：" + err.message, "error");
        });
    }

    button.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submit();
      }
    });
  }

  /** 主结论的名字。虫害当主结论时是虫名，否则是病名。
   *  收敛成一个函数，是因为「记入我的农田」「追问」「方言播报」三处都要用它 ——
   *  各自写一遍的话，漏掉一处就会把一份虫害记录存成病害。 */
  function _primaryName(report) {
    return (report && report.primary && report.primary.name) ||
           (report && report.disease_name) || "";
  }

  function _actions() {
    return (
      `<div class="result-actions">` +
      `<span class="text-sm text-muted">` +
      `诊断结果仅供参考，重大病虫害请及时联系当地植保部门确认。` +
      `</span>` +
      `<div class="result-actions__ops">` +
      `<button type="button" class="btn btn--outline" id="btn-restart">` +
      Icon.render("refresh", 16) + `<span>重新诊断</span></button>` +
      `</div></div>`
    );
  }

  /* ==================================================================
     一页纸问诊单

     为什么不做成「导出 PDF」而是「打印 / 复制成微信消息」：
       农技员真正的交付路径是**把手机递过去**，或者在地头把这张纸撕给农户。
       他手上没有转换工具，也不一定会用「导出」。window.print() 在手机上
       直接进系统打印（可存 PDF），复制成文字则能直接粘进微信 —— 这两条
       是这个场景里真正走得通的路，而不是看起来更专业的那个。

     诚实性：这张纸上**不给用药剂量**。剂量要按标签和当地指导来，AI 写一个
     数字上去，农户就照着打了 —— 那是这个作品里最不能犯的错。
     ================================================================== */

  /** 月份 → 季节 key（与 /api/map/pest_top3 对齐）。 */
  const MONTH_SEASON = ["winter", "winter", "spring", "spring", "spring",
    "summer", "summer", "summer", "autumn", "autumn", "autumn", "winter"];

  function _seasonNow() {
    return MONTH_SEASON[new Date().getMonth()];
  }

  function _isoPlusDays(days) {
    // 本地日期，不用 toISOString（那是 UTC，东八区早上会差一天）
    return UI.localDatePlus(days);
  }

  /** 回访间隔按结论性质给默认值：健康 3 天、看不清 2 天补拍、确诊 7 天看药效。 */
  function _defaultRecheckDays(report) {
    if (report.healthy) return 3;
    if (!_primaryName(report)) return 2;
    return 7;
  }

  function _handoutBar(report) {
    const name = _primaryName(report);
    const canSheet = !!name || !!report.healthy;
    return (
      `<div class="handout-bar">` +
      `<div class="handout-bar__main">` +
      `<div class="handout-bar__title">${Icon.render("file", 18)}这一页要交给农户</div>` +
      `<p class="handout-bar__desc">` +
      (canSheet
        ? `生成一页纸：结论、先做什么、用药提醒、几天后复查。可打印、可复制成微信消息。`
        : `这次没有明确结论，问诊单先不打 —— 一张写着「未能确定」的纸交给农户，只是把 AI 的不确定性转嫁给他。`) +
      `</p>` +
      `</div>` +
      `<div class="handout-bar__ops">` +
      (canSheet
        ? `<button type="button" class="btn btn--primary" id="btn-sheet">` +
          Icon.render("file", 16) + `<span>生成问诊单</span></button>`
        : "") +
      `<button type="button" class="btn btn--outline" id="btn-speak-short">` +
      Icon.render("volume", 16) + `<span>说给农户听</span></button>` +
      `</div>` +
      `</div>`
    );
  }

  function bindHandout(report, data) {
    const sheetBtn = $("btn-sheet");
    if (sheetBtn) sheetBtn.addEventListener("click", () => openSheet(report, data));

    const shortBtn = $("btn-speak-short");
    if (shortBtn) {
      shortBtn.addEventListener("click", () => {
        Dialect.speak(buildShortSpeech(report), shortBtn).catch((e) => UI.toast(e.message, "error"));
      });
    }
  }

  /* ---------------------------------------------------------------- 归档与回访 */

  function _recordBlock(report) {
    return (
      `<div class="result-block result-record" id="record-block">` +
      `<div class="result-block__title">${Icon.render("sprout", 18)}归档与回访</div>` +
      `<p class="result-record__desc">` +
      `这条结论该落在哪块地上？归档后会进那块田的档案，同时在回访队列里排一条待办 —— ` +
      `到回访日提醒你再看一次，不用自己记在纸上。` +
      `</p>` +
      `<div class="result-record__grid">` +
      `<div class="form-field">` +
      `<label class="form-label" for="archive-field">记到哪块田</label>` +
      `<select class="select" id="archive-field"><option value="">加载中…</option></select>` +
      `</div>` +
      `<div class="form-field">` +
      `<label class="form-label" for="archive-subject">服务对象</label>` +
      `<input class="input" id="archive-subject" maxlength="40" ` +
      `placeholder="农户 / 合作社，如：王守田">` +
      `</div>` +
      `<div class="form-field">` +
      `<label class="form-label" for="archive-recheck">回访日期</label>` +
      `<input class="input" id="archive-recheck" type="date" ` +
      `value="${_isoPlusDays(_defaultRecheckDays(report))}">` +
      `</div>` +
      `</div>` +
      `<div class="result-record__ops">` +
      `<button type="button" class="btn btn--primary" id="btn-archive">` +
      Icon.render("check", 16) + `<span>保存并排入回访</span></button>` +
      `<span class="text-sm text-muted" id="archive-status"></span>` +
      `</div>` +
      `</div>`
    );
  }

  /** 读田块列表。诊断页不强制登录（拍照就能诊断），所以这里必须处理「没登录」这一态。 */
  function loadFieldsOnce() {
    if (state.fields.loaded) return Promise.resolve(state.fields);
    return Api.authStatus()
      .then((st) => {
        if (!st || !st.logged_in) {
          state.fields = { loaded: true, list: [], province: "", needLogin: true };
          return state.fields;
        }
        return Api.farmFields().then((data) => {
          state.fields = {
            loaded: true,
            list: data.fields || [],
            province: data.default_province || "",
            needLogin: false,
          };
          return state.fields;
        });
      })
      .catch(() => {
        // 后端不可用 / 会话过期都不该把结果页搞崩：归档块退化成一句说明，
        // 上面的结论照样能看、问诊单照样能打。
        state.fields = { loaded: true, list: [], province: "", error: true };
        return state.fields;
      });
  }

  function bindRecord(report, data) {
    const select = $("archive-field");
    const status = $("archive-status");
    const submit = $("btn-archive");
    if (!select || !submit) return;

    const setStatus = (html) => {
      if (status) status.innerHTML = html;
    };

    let saved = false;

    loadFieldsOnce().then((f) => {
      if (f.needLogin) {
        select.innerHTML = `<option value="">未登录</option>`;
        select.disabled = true;
        submit.disabled = true;
        setStatus(
          `登录后可归档并排回访 · ` +
          `<a href="/login.html?redirect=${encodeURIComponent(location.pathname)}">去登录</a>`
        );
        return;
      }
      if (f.error) {
        select.innerHTML = `<option value="">读取失败</option>`;
        select.disabled = true;
        submit.disabled = true;
        setStatus("田块读取失败，稍后重试；不影响上面的诊断结论。");
        return;
      }
      if (!f.list.length) {
        select.innerHTML = `<option value="">还没有田块</option>`;
        select.disabled = true;
        submit.disabled = true;
        setStatus(`先去 <a href="/farm.html">我的农田</a> 新建一块田，才能把结论挂上去。`);
        return;
      }

      // 第一项永远是空：列表里没选中任何一项时，浏览器默认选第一个，
      // 于是一次没匹配上的归档会静静地记到「示例田 · 油菜」上。
      select.innerHTML = `<option value="">请选择田块</option>` +
        f.list
          .map((x) => `<option value="${UI.escapeHtml(x.id)}">` +
            `${UI.escapeHtml(x.name)}（${UI.escapeHtml(x.crop)}）</option>`)
          .join("");

      // 默认选中作物能对上的那块 —— 农技员一天看十几块地，
      // 让他每次手动挑一遍，这个功能就等于没有。
      const crop = (report.primary && report.primary.crop) ||
        (data.recognition && data.recognition.top1 && data.recognition.top1.crop) || "";
      const hit = crop && f.list.find((x) => x.crop === crop);
      if (hit) select.value = hit.id;
    });

    submit.addEventListener("click", async () => {
      if (saved) {
        UI.toast("这条结论已经归档过了", "info");
        return;
      }
      const fieldId = select.value;
      if (!fieldId) {
        UI.toast("请先选一块田，或用「生成问诊单」直接打一张纸", "warning");
        return;
      }

      const restore = UI.busy(submit, "保存中");
      try {
        const primary = report.primary || {};
        const conf = primary.confidence != null ? primary.confidence : report.confidence;
        const payload = {
          field_id: fieldId,
          date: UI.localDate(),
          label: _primaryName(report),
          confidence: typeof conf === "number" ? conf : null,
          subject: ($("archive-subject") && $("archive-subject").value.trim()) || "",
          recheck_date: ($("archive-recheck") && $("archive-recheck").value) || "",
          report: report,
          pest: data.pest || null,
        };

        const b64 = await imageToBase64(state.imageFile);
        if (b64) {
          payload.image_base64 = b64;
          payload.image_name = state.imageFile.name || "";
        }

        const res = await Api.attachDiagnosis(payload);
        restore();
        saved = true;
        submit.disabled = true;

        const fieldText = select.options[select.selectedIndex]
          ? select.options[select.selectedIndex].text : "该田块";
        state.archive = {
          id: res.diagnosis_id || "",
          fieldName: fieldText,
          fieldId: res.field_id || fieldId,
          subject: res.subject || payload.subject,
          date: payload.date,
          recheck: res.recheck_date || "",
        };
        setStatus(
          `已归入「${UI.escapeHtml(fieldText)}」，编号 ${UI.escapeHtml(state.archive.id)}` +
          (state.archive.recheck ? `；${UI.escapeHtml(state.archive.recheck)} 回访已排入队列` : "")
        );
        UI.toast("已归档，回访队列里加了一条待办", "info");
      } catch (err) {
        restore();
        setStatus(`保存失败：${UI.escapeHtml(err.message)}`);
        UI.toast("保存失败：" + err.message, "error", 7000);
      }
    });
  }

  /** 图片转 base64。超过 6 MB 就**不带图**：后端上限 6 MB，
   *  而结论本身比一张大图重要得多 —— 不能因为图太大就把整次归档弄失败。 */
  function imageToBase64(file) {
    if (!file) return Promise.resolve("");
    if (file.size > 6 * 1024 * 1024) {
      UI.toast("图片超过 6 MB，本次只存结论、不存图", "warning", 5000);
      return Promise.resolve("");
    }
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => {
        const s = String(reader.result || "");
        resolve(s.indexOf(",") >= 0 ? s.split(",")[1] : "");
      };
      reader.onerror = () => resolve("");
      reader.readAsDataURL(file);
    });
  }

  /* ---------------------------------------------------------------- 问诊单本体 */

  function openSheet(report, data) {
    const body = $("sheet-body");
    if (!body) return;

    // 每张纸都是新的一次交付：区域预警与旧链接都不能沿用上一张的
    state.regionAlert = null;
    state.sheetShare = null;

    body.innerHTML = _sheetHtml(report, data) +
      // 分享面板默认收起：这块只在农技员要走的时候才需要，一直摊着会把纸挤走
      `<section class="sheet-share" id="sheet-share" hidden>` +
      `<img class="sheet-share__qr" id="sheet-share-qr" alt="分享二维码" width="132" height="132">` +
      `<div class="sheet-share__main">` +
      `<div class="sheet-share__title">农技员走了，农户还能看</div>` +
      `<p class="sheet-share__desc">扫码打开这张问诊单（只读，不含原图）。` +
      `先把二维码存下来，或者把链接发给农户 —— 有效期 30 天。</p>` +
      `<div class="sheet-share__row">` +
      `<input class="input sheet-share__url" id="sheet-share-url" readonly>` +
      `<button type="button" class="btn btn--sm btn--outline" id="btn-share-copy">复制链接</button>` +
      `</div>` +
      `<p class="sheet-share__hint" id="sheet-share-hint"></p>` +
      `</div>` +
      `</section>` +
      `<div class="sheet-ops">` +
      `<button type="button" class="btn btn--primary" id="btn-sheet-print">` +
      Icon.render("file", 16) + `<span>打印 / 存 PDF</span></button>` +
      `<button type="button" class="btn btn--outline" id="btn-sheet-pdf">` +
      Icon.render("download", 16) + `<span>下载 PDF</span></button>` +
      `<button type="button" class="btn btn--outline" id="btn-sheet-share">` +
      Icon.render("link", 16) + `<span>分享给农户</span></button>` +
      `<button type="button" class="btn btn--outline" id="btn-sheet-copy">` +
      `<span>复制成微信消息</span></button>` +
      `</div>`;

    $("sheet-modal").hidden = false;
    // sheet-open 是打印的唯一判据：只要这张纸是打开着的，Ctrl+P 打出来就只印它
    document.body.classList.add("modal-open", "sheet-open");

    $("btn-sheet-print").addEventListener("click", doPrint);
    $("btn-sheet-copy").addEventListener("click", () => copySheetText(report, data));
    $("btn-sheet-pdf").addEventListener("click", (e) => {
      downloadSheetPdf(report, data, e.currentTarget);
    });
    $("btn-sheet-share").addEventListener("click", (e) => {
      createSheetShare(report, data, e.currentTarget);
    });
    $("btn-share-copy").addEventListener("click", () => {
      const input = $("sheet-share-url");
      if (!input || !input.value) {
        UI.toast("先点「分享给农户」生成链接", "warning");
        return;
      }
      const fallback = () => {
        input.select();
        try { document.execCommand("copy"); } catch (e) { /* 老浏览器没有就算了 */ }
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).catch(fallback);
      } else {
        fallback();
      }
      UI.toast("链接已复制，粘到微信里发给农户", "info");
    });

    loadRegionAlert();
    loadSprayNote();
  }

  /** 服务端出 PDF（不经过浏览器打印菜单）。
   *  手机微信里没有「打印」这一项，但「下载 → 发到群里」一直通 —— 这就是它存在的理由。 */
  async function downloadSheetPdf(report, data, btn) {
    const restore = UI.busy(btn, "生成中");
    try {
      const { blob, filename } = await Api.sheetPdf(_sheetPayload(report, data));
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename || "sheet.pdf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 立刻 revoke 会让部分浏览器来不及取数据，给一个足够的宽限
      window.setTimeout(() => URL.revokeObjectURL(url), 10000);
      UI.toast("已生成 PDF，去下载里找", "info");
    } catch (err) {
      UI.toast("PDF 生成失败：" + err.message, "error", 7000);
    } finally {
      restore();
    }
  }

  /** 生成（或复用）分享链接，并把二维码摆出来。 */
  async function createSheetShare(report, data, btn) {
    const panel = $("sheet-share");
    if (!panel) return;

    if (state.sheetShare) {
      panel.hidden = false;
      // 居中而不是 nearest：nearest 会在「刚露一条边」时就停住，
      // 而底部那条 sticky 操作栏正好压在那里
      panel.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }

    const restore = UI.busy(btn, "生成中");
    try {
      const info = await Api.shareSheet(_sheetPayload(report, data));
      state.sheetShare = info;
      $("sheet-share-url").value = info.url;
      $("sheet-share-qr").src = Api.shareQrUrl(info.token);
      $("sheet-share-hint").textContent =
        `有效期至 ${String(info.expires_at || "").slice(0, 10)}`;
      panel.hidden = false;
      // 居中而不是 nearest：nearest 会在「刚露一条边」时就停住，
      // 而底部那条 sticky 操作栏正好压在那里
      panel.scrollIntoView({ behavior: "smooth", block: "center" });
      UI.toast("链接已生成，可以发给农户了", "info");
    } catch (err) {
      UI.toast("生成分享链接失败：" + err.message, "error", 7000);
    } finally {
      restore();
    }
  }

  function closeSheet() {
    const modal = $("sheet-modal");
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove("modal-open", "sheet-open", "printing");
  }

  function doPrint() {
    // 不新开窗口：新窗口里样式表、字体、图都要重新加载，打印出来常常是错版的。
    // 隐藏「不是这张纸」的东西这件事交给 @media print（判据是 body.sheet-open），
    // 这里只负责把打印对话框叫起来。
    window.print();
  }

  /** 纸上要用的归档信息：**优先取已落库的，没落库就用屏幕上填着的**。
   *  农技员常见做法是先把纸打出来、再回机房归档；这时候服务对象已经填了、
   *  只是还没保存 —— 纸上却写「—」，他就得手写一遍，等于这个字段白填。 */
  function _liveMeta() {
    const a = state.archive || {};
    const subjectEl = $("archive-subject");
    const fieldEl = $("archive-field");
    const recheckEl = $("archive-recheck");
    return {
      id: a.id || "",
      date: a.date || "",
      subject: a.subject || (subjectEl && subjectEl.value.trim()) || "",
      // fieldId 给「本地产收提醒」用：只知道田块名字没法查用药记录
      fieldId: a.fieldId || (fieldEl && fieldEl.value) || "",
      fieldName: a.fieldName ||
        (fieldEl && fieldEl.selectedIndex >= 0 ? fieldEl.options[fieldEl.selectedIndex].text : "") || "",
      recheck: a.recheck || (recheckEl && recheckEl.value) || "",
    };
  }

  /** 一张问诊单的「事实」：屏幕版 / 分享快照 / PDF 三处共用同一份推导。
   *
   *  为什么必须共用：这三处都要回答「结论叫什么、置信度多少、先做什么」。
   *  各写一遍的话，迟早出现「纸上 85%、分享页 84%、PDF 无置信度」这种事故 ——
   *  而这种不一致恰恰是最伤信任的一种 bug：农户拿两张对不上号的纸来问。 */
  function _sheetFacts(report, data) {
    const primary = report.primary || {};
    const pestPrimary = primary.kind === "pest";
    const name = _primaryName(report);
    const healthy = !!report.healthy;
    const confRaw = pestPrimary ? primary.confidence : report.confidence;
    const conf = typeof confRaw === "number" ? Math.round(confRaw * 100) : null;
    const top1 = (data.recognition && data.recognition.top1) || {};
    const crop = (primary && primary.crop) || top1.crop || "";
    const sections = report.treatment_sections || [];
    const meta = _liveMeta();

    const now = new Date();
    const dateText = `${now.getFullYear()}年${now.getMonth() + 1}月${now.getDate()}日`;

    // 现场原始描述：用户打的字优先，其次语音转写。report.symptom_match 是模型
    // 改写过的，放在「判断依据」里，不能冒充现场记录。
    const scene = [];
    if (state.lastText) scene.push(state.lastText);
    const asrText = data.asr && data.asr.text ? data.asr.text : "";
    if (asrText && asrText !== state.lastText) {
      scene.push(`（${(data.asr && data.asr.dialect_name) || "方言"}语音）${asrText}`);
    }
    const sources = [];
    if (state.imageFile || (data.recognition && data.recognition.filename)) sources.push("现场照片");
    if (asrText) sources.push("方言语音");
    if (state.lastText) sources.push("文字描述");

    const action = sections.find((s) => ACTION_RE.test(s.title || "")) || sections[0] || null;
    const others = sections.filter((s) => s !== action);
    const ordered = [action].concat(others).filter(Boolean);

    const topk = (data.recognition && data.recognition.topk) || [];
    const basis = topk.slice(0, 3)
      .map((t) => `${t.label_cn} ${Math.round((t.confidence || 0) * 100)}%`)
      .join(" · ");

    return {
      primary, pestPrimary, name, healthy, conf, crop, sections, meta,
      dateText, scene, sources, action, others, ordered, basis,
      recheck: meta.recheck, asrText,
    };
  }

  /** 组装分享 / PDF 用的快照（结构见 backend/app/schemas/sheet.py）。
   *
   *  **不带图**：二维码背后是村里的 4G，农户要看的是结论和「先干什么」。
   *  区域预警取 state.regionAlert —— 打开问诊单时会异步拉一次，没拉到就留空，
   *  不能在分享页写一条编出来的预警。 */
  function _sheetPayload(report, data) {
    const f = _sheetFacts(report, data);
    const alert = state.regionAlert || {};
    return {
      v: 1,
      no: f.meta.id || "",
      date: f.meta.date || UI.localDate(),
      subject: f.meta.subject || "",
      field: f.meta.fieldName || "",
      crop: f.crop || "",
      sources: f.sources,
      scene: f.scene.join("\n"),
      result: {
        // 健康时不给病名：一张写着「未见明显病斑」却带着病名的纸，农户会只记住病名
        name: f.healthy ? "" : (f.name || ""),
        pest: !!f.pestPrimary,
        healthy: f.healthy,
        confidence: f.conf,
      },
      summary: report.summary || report.symptom_match || "",
      basis: f.basis,
      sections: f.ordered.map((s) => ({
        title: s.title || "",
        content: s.content || "",
      })),
      medication: report.medication_reminder || "",
      recheck: f.recheck || "",
      region: {
        province: alert.province || "",
        items: alert.items || [],
        demo: !!alert.demo,
      },
      // 本地产收提醒。以前只写进页面 DOM，分享出去的那张纸和 PDF 就没有这一段，
      // 而农户拿到的往往正是那一张 —— 「打完几天能吃」在纸上缺席，等于没提醒。
      harvest: _harvestPayload(state.sprayNote),
    };
  }

  /** 采收状态 → 快照字段。只挑纸上要印的几项，接口的其余字段不带进公开链接。 */
  function _harvestPayload(s) {
    if (!s || s.status === "none") return null;
    if (s.status === "unknown") {
      return {
        status: "unknown",
        date: s.pending_date || s.last_date || "",
        item: s.pending_item || s.last_item || "",
        interval_days: null,
        earliest_date: "",
        days_left: null,
      };
    }
    return {
      status: s.locked ? "locked" : "ok",
      date: s.date || "",
      item: s.item || "",
      interval_days: typeof s.interval_days === "number" ? s.interval_days : null,
      earliest_date: s.earliest_date || "",
      days_left: typeof s.days_left === "number" ? s.days_left : null,
    };
  }

  function _sheetHtml(report, data) {
    const f = _sheetFacts(report, data);
    const {
      pestPrimary, name, healthy, conf, crop, sections, meta,
      dateText, scene, sources, action, others, basis, recheck,
    } = f;


    return (
      `<div class="sheet" id="sheet-paper">` +
      `<div class="sheet__top">` +
      `<div>` +
      `<div class="sheet__brand">田头农技台</div>` +
      `<div class="sheet__sub">田间问诊单</div>` +
      `</div>` +
      `<div class="sheet__no">` +
      `<div>编号：<b>${UI.escapeHtml(meta.id || "现场未归档")}</b></div>` +
      `<div>日期：${UI.escapeHtml(meta.date || dateText)}</div>` +
      `</div>` +
      `</div>` +

      `<table class="sheet__meta"><tbody>` +
      `<tr>` +
      `<th>服务对象</th><td>${UI.escapeHtml(meta.subject || "—")}</td>` +
      `<th>地块</th><td>${UI.escapeHtml(meta.fieldName || "—")}</td>` +
      `</tr>` +
      `<tr>` +
      `<th>作物</th><td>${UI.escapeHtml(crop || "—")}</td>` +
      `<th>送检方式</th><td>${UI.escapeHtml(sources.join(" + ") || "—")}</td>` +
      `</tr>` +
      `</tbody></table>` +

      `<div class="sheet__sec">` +
      `<h3>一、现场情况</h3>` +
      `<p>${UI.escapeHtml(scene.join("\n") || "（未填写文字或语音，仅提交照片）")}</p>` +
      `</div>` +

      `<div class="sheet__sec">` +
      `<h3>二、诊断结论</h3>` +
      `<p class="sheet__result">` +
      `<b>${UI.escapeHtml(healthy ? "未见明显病斑" : (name || "未能确定"))}</b>` +
      (pestPrimary ? `　<span class="sheet__tag">虫害</span>` : "") +
      (conf != null ? `　置信度 ${conf}%` : "") +
      `</p>` +
      (report.summary
        ? `<p>${UI.escapeHtml(report.summary)}</p>`
        : `<p>${UI.escapeHtml(report.symptom_match || "")}</p>`) +
      (basis ? `<p class="sheet__note">模型候选：${UI.escapeHtml(basis)}</p>` : "") +
      `</div>` +

      `<div class="sheet__sec">` +
      `<h3>三、处置建议</h3>` +
      (action
        ? `<p class="sheet__first"><b>先做：${UI.escapeHtml(action.title || "")}</b></p>` +
          `<p>${UI.escapeHtml(action.content || "")}</p>`
        : `<p>本次没有可执行的处置建议。</p>`) +
      others.map((s) =>
        `<p><b class="sheet__sub2">${UI.escapeHtml(s.title || "")}</b>　` +
        `${UI.escapeHtml(s.content || "")}</p>`).join("") +
      `</div>` +

      (report.medication_reminder
        ? `<div class="sheet__sec">` +
          `<h3>四、用药提醒</h3>` +
          `<p>${UI.escapeHtml(report.medication_reminder)}</p>` +
          `</div>`
        : "") +

      `<div class="sheet__sec">` +
      `<h3>五、回访安排</h3>` +
      `<p>${recheck ? `建议 <b>${UI.escapeHtml(recheck)}</b> 回到同一块地复查` : "本次未排回访"}` +
      `${healthy ? "；同一片叶、同一角度再拍一张对比即可。" : "；拍一张同部位照片，对比用药前后的变化。"}</p>` +
      `</div>` +

      `<div class="sheet__sec">` +
      `<h3>六、本省当前高发（供参考）</h3>` +
      `<div id="sheet-alert" class="sheet__alert">正在读取本省预警…</div>` +
      `</div>` +

      // 本地产收提醒：只有这张诊断落在某块田上、且那块田记过带间隔期的打药时才出现。
      // 它把「该打什么药」和「打完几天能吃」接在一起 —— 农技员在地头被追问的正是后半句。
      `<div class="sheet__sec" id="sheet-spray" hidden>` +
      `<h3>采收提醒</h3>` +
      `<div id="sheet-spray-body"></div>` +
      `</div>` +

      `<div class="sheet__sign">` +
      `<span>农技员签字：____________</span>` +
      `<span>联系方式：____________</span>` +
      `<span>农户确认：____________</span>` +
      `</div>` +

      `<p class="sheet__foot">` +
      `本单由「田头农技台」辅助生成：结论基于${UI.escapeHtml(sources.join("、") || "提供的材料")}，` +
      `不构成全田诊断；用药请以农药登记标签和当地植保部门指导为准。` +
      `</p>` +
      `</div>`
    );
  }

  /** 区域预警：拉本省当季 TOP3。
   *  **必须带「演示数据」角标** —— province_pest_top3.json 自标 demo_data=true，
   *  是「全国趋势 + 各省作物分布」推导出来的，不是实时测报。少了这个角标，
   *  这张纸就等于给农户发了一条看起来很权威的假预警。 */
  function loadRegionAlert() {
    const box = $("sheet-alert");
    if (!box) return;

    loadFieldsOnce().then((f) => {
      const province = (f && f.province) || "";
      if (!province) {
        box.textContent = "未设置所在省份，无法给出本省预警。";
        return;
      }
      return Api.mapPestTop3({ season: _seasonNow(), province })
        .then((res) => {
          const p = (res.provinces || [])[0];
          const items = (p && p.top3) || [];
          if (!items.length) {
            box.textContent = `${province}当季暂无预警数据。`;
            state.regionAlert = { province, items: [], demo: !!res.demo_data };
            return;
          }
          // 一份字符串数组同时供三处用：纸上的正文、分享快照、PDF 第六段。
          const parts = items
            .map((t, i) => `${i + 1}. ${t.pest}（${t.crop}，${t.risk || "—"}风险，${t.peak_window || ""}）`);
          state.regionAlert = { province, items: parts, demo: !!res.demo_data };
          const list = parts.join("；");
          box.innerHTML =
            `<div>${UI.escapeHtml(list)}</div>` +
            `<div class="sheet__note">` +
            `来源：全国农技中心《2026 年全国农作物病虫害发生趋势预报》（全国维度）` +
            `+ 各省作物分布推导` +
            (res.demo_data ? `　<b>［演示数据，非实时测报］</b>` : "") +
            `</div>`;
        })
        .catch(() => {
          box.textContent = `${province}预警数据读取失败，可向当地植保站确认。`;
        });
    });
  }

  /** 本地产收提醒：这块田最近一次「记了间隔期」的施药解禁没有。
   *
   *  三种「不显示」，都是为了不打扰人：
   *    · 这张单子没挂到任何田块（路边随手拍一张）—— 没有「本地」可言；
   *    · 那块田一条带间隔期的打药都没有 —— 那是「不知道」，印上去反而像在担保；
   *    · 请求失败 —— 一张要交到农户手里的纸，不该因为一行增强信息而印不出来。
   */
  function loadSprayNote() {
    const box = $("sheet-spray");
    const body = $("sheet-spray-body");
    if (!box || !body) return;
    const meta = _liveMeta();
    if (!meta.fieldId) {
      box.hidden = true;
      return;
    }
    Api.farmSpray(meta.fieldId).then((s) => {
      if (!s || s.status === "none") {
        box.hidden = true;
        state.sprayNote = null;
        return;
      }
      // 存一份原始状态：分享快照、服务端 PDF、复制文本都从这里取，
      // 保证四处说的是同一句话，而不是各算各的。
      state.sprayNote = s;
      const what = `${UI.escapeHtml(s.date || s.last_date)} 施过 ` +
        `${UI.escapeHtml(s.item || s.last_item || "药剂")}` +
        (s.reg_no ? `（${UI.escapeHtml(s.reg_no)}）` : "");
      box.hidden = false;
      if (s.status === "unknown") {
        // 「不知道」不能印成「可以采收」，也不能印成「还在间隔期」——
        // 前者会让人提前采摘，后者是凭空造一条限制。
        body.innerHTML =
          `<p>这块田 ${UI.escapeHtml(s.pending_date || s.last_date)} 施过 ` +
          `${UI.escapeHtml(s.pending_item || s.last_item || "药剂")}，` +
          `但这次记录没有填安全间隔期，<b>无法判断哪天可以采收</b>。</p>` +
          `<p>请按瓶身标签补填间隔期后再看这张单。</p>`;
      } else if (s.locked) {
        body.innerHTML =
          `<p>这块田 ${what}，标签安全间隔期 ${s.interval_days} 天，` +
          `<b>最早可收 ${UI.escapeHtml(s.earliest_date)}</b>（距今 ${s.days_left} 天）。` +
          `在这之前不要采收。</p>`;
      } else {
        body.innerHTML =
          `<p>这块田 ${what}，间隔期 ${s.interval_days} 天，` +
          `已于 ${UI.escapeHtml(s.earliest_date)} 期满，可以采收。</p>`;
      }
    }).catch(() => {
      box.hidden = true;
      state.sprayNote = null;
    });
  }

  /** 本地产收提醒的一句话版本（复制文本用）。
   *  与 DOM、分享页、PDF 同源，措辞不另写一套。 */
  function _sprayNoteLine(s) {
    if (!s || s.status === "none") return "";
    if (s.status === "unknown") {
      return `采收：${s.pending_date || s.last_date} 施过 ` +
        `${s.pending_item || s.last_item || "药剂"}，未填安全间隔期，` +
        `无法判断可收日期，请核对标签后补填。`;
    }
    if (s.locked) {
      return `采收：${s.date} 施过 ${s.item || "药剂"}，间隔期 ${s.interval_days} 天，` +
        `最早可收 ${s.earliest_date}（距今 ${s.days_left} 天），之前不要采收。`;
    }
    return `采收：${s.date} 施过 ${s.item || "药剂"}，间隔期 ${s.interval_days} 天，` +
      `已于 ${s.earliest_date} 期满。`;
  }

  /** 复制成微信消息。微信里没有排版，所以只用纯文本 + 序号，
   *  并且**一段不超过手机两屏** —— 太长农户不会看。 */
  function _sheetText(report, data) {
    const primary = report.primary || {};
    const pestPrimary = primary.kind === "pest";
    const name = _primaryName(report);
    const healthy = !!report.healthy;
    const confRaw = pestPrimary ? primary.confidence : report.confidence;
    const conf = typeof confRaw === "number" ? Math.round(confRaw * 100) : null;
    const sections = report.treatment_sections || [];
    const action = sections.find((s) => ACTION_RE.test(s.title || "")) || sections[0] || null;
    const meta = _liveMeta();
    const recheck = meta.recheck;

    const lines = [];
    lines.push(`【田间问诊单】${meta.date || UI.localDate()}`);
    if (meta.subject) lines.push(`服务对象：${meta.subject}`);
    if (meta.fieldName) lines.push(`地块：${meta.fieldName}`);
    lines.push("");
    lines.push(`结论：${healthy ? "未见明显病斑" : (name || "未能确定")}` +
      (pestPrimary ? "（虫害）" : "") + (conf != null ? `，置信度 ${conf}%` : ""));
    if (report.summary) lines.push(report.summary);
    if (action) {
      lines.push("");
      lines.push(`先做：${action.title || ""}`);
      lines.push(action.content || "");
    }
    if (report.medication_reminder) {
      lines.push("");
      lines.push(`用药提醒：${report.medication_reminder}`);
    }
    // 采收提醒紧跟用药提醒：这两条是农户最常追问的「打完几天能吃」。
    const sprayLine = _sprayNoteLine(state.sprayNote);
    if (sprayLine) {
      lines.push("");
      lines.push(sprayLine);
    }
    if (recheck) {
      lines.push("");
      lines.push(`复查：${recheck} 回到同一块地，拍一张同部位的照片再发我。`);
    }
    lines.push("");
    lines.push("（本结论由田头农技台辅助生成，仅供参考；用药以标签和当地植保部门指导为准。）");
    return lines.join("\n").replace(/\n{3,}/g, "\n\n");
  }

  function copySheetText(report, data) {
    const text = _sheetText(report, data);
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (e) {
        ok = false;
      }
      ta.remove();
      UI.toast(ok ? "已复制，粘到微信里就能发" : "复制失败，请手动选中文字复制",
        ok ? "info" : "error");
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(() => UI.toast("已复制，粘到微信里就能发", "info"))
        .catch(fallback);
    } else {
      fallback();
    }
  }

  /** 「说给农户听」的那一版。
   *
   *  与 buildReadText 的区别是**它不是在念报告**：报告是给农技员看的，
   *  有置信度、有病原类型、有判断依据；农户要的只有三句 —— 这是什么、
   *  现在干什么、什么时候再看。念整篇报告，他听到第二句就走神了。
   *  这也正是「方言播报」这个功能真正的使用场景：农技员把手机递给农户。 */
  function buildShortSpeech(report) {
    const name = _primaryName(report);
    const healthy = !!report.healthy;
    const sections = report.treatment_sections || [];
    const action = sections.find((s) => ACTION_RE.test(s.title || "")) || sections[0] || null;
    const days = _defaultRecheckDays(report);

    if (healthy) {
      return `这张叶子看着没得病，先不用打药。${days}天以后，你用同一个角度再拍一张给我看；` +
        `要是长出新的斑点，马上拍给我。`;
    }
    if (!name) {
      return `这张照片我没看清楚，先别急着打药。你到地里把叶子翻过来，对着光再拍一张，我再看。`;
    }

    // 处置只取第一句（到第一个句号为止）：整段念完农户记不住，
    // 而「先做这一步」的第一句通常就是全部动作。
    let first = (action && action.content) || "";
    const stop = first.search(/[。；;]/);
    if (stop > 0) first = first.slice(0, stop + 1);
    if (first.length > 80) first = first.slice(0, 80) + "……";

    return `这个是${name}。先${first}${days}天以后，你回到同一块地，` +
      `拍一张同一个位置的照片再发给我，我看有没有好转。`;
  }

  /** 完整朗读稿（「方言播报」按钮用）：面向愿意听完整结论的人，通常是农技员自己。 */
  function buildReadText(report, sections, data) {
    const primary = report.primary || {};
    const pestPrimary = primary.kind === "pest";
    const name = _primaryName(report) || "未能确定";
    const conf = Math.round(
      (pestPrimary ? (primary.confidence || 0) : (report.confidence || 0)) * 100);
    const parts = [];
    parts.push(`诊断结果：${name}${pestPrimary ? "（虫害）" : ""}，置信度百分之${conf}。`);
    if (report.summary) parts.push(report.summary);
    if (report.symptom_match) parts.push(`判断依据：${report.symptom_match}`);
    // 虫害要读出来 —— 方言播报面向的是不看屏幕的农户，
    // 漏掉虫害等于把"还得治虫"这条关键信息吞了。
    // 但虫害已经当主结论时别再念一遍它的名字（第一句已经念过了），只补建议。
    (report.pest_findings || []).forEach((f) => {
      parts.push(pestPrimary && f.name === name
        ? (f.advice || "")
        : `另外检出虫害${f.name}，${f.advice || ""}`);
    });
    sections.forEach((s) => parts.push(`${s.title}：${s.content}`));
    if (report.medication_reminder) parts.push(report.medication_reminder);
    return parts.join(" ");
  }

  function resetToInput() {
    $("result-root").hidden = true;
    $("result-root").innerHTML = "";
    $("batch-root").hidden = true;
    $("batch-root").innerHTML = "";
    $("batch-card").hidden = false;
    $("diagnose-form").hidden = false;
    clearImage();
    clearAudio();
    $("input-text").value = "";
    $("text-count").textContent = "0";
    $("input-camera").value = "";
    $("input-image").value = "";
    $("input-audio").value = "";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /* ==================================================================
     批量巡检

     为什么单独一屏、而且结论只给一句话：
       下乡一次拍三十张，农技员要的不是三十篇防治方案，而是「哪几张必须细看」。
       一篇篇展开反而看不出重点。所以这里只出判读 + 结论名，点进去才看完整那份
       —— 详情直接复用单个诊断的结果页，不另写一套渲染。

     为什么顺序跑而不是并发：
       诊断链路上有 ONNX 会话与大模型调用，并发五张会把内存打满（这个项目上真
       踩过），而且顺序跑才能把进度按张数如实显示出来。
     ================================================================== */

  // token 是批次号：每次开始 +1，旧循环发现自己不是当前批次就退出（见 runBatch）。
  const batch = { files: [], rows: [], running: false, token: 0 };

  /* 一次最多 30 张：单张 3~5 秒，30 张已经是两分多钟。再多用户会以为卡死，
     而且他上传的时候本来也不会一次挑五十张。 */
  const BATCH_MAX = 30;

  function bindBatch() {
    const pick = $("btn-batch-choose");
    const input = $("input-batch");
    const start = $("btn-batch-start");
    if (!pick || !input || !start) return;

    pick.addEventListener("click", () => input.click());

    input.addEventListener("change", () => {
      const picked = Array.from(input.files || []).filter((f) => f.type.startsWith("image/"));
      if (picked.length > BATCH_MAX) {
        UI.toast(`一次最多 ${BATCH_MAX} 张，已取前 ${BATCH_MAX} 张`, "warning", 6000);
      }
      batch.files = picked.slice(0, BATCH_MAX);
      $("batch-count").textContent = batch.files.length
        ? `已选 ${batch.files.length} 张` : "未选择照片";
      start.disabled = batch.files.length === 0;
    });

    start.addEventListener("click", runBatch);
  }

  /** 判读口径与「健康 / 不确定出口」保持一致，不另立一套：
   *  健康（healthy 标记）/ 看不清（没有结论名）/ 检出（有病名或虫名）。 */
  function batchVerdict(data) {
    const report = (data && data.report) || {};
    const primary = report.primary || {};
    if (report.healthy) return { text: "未见病斑", kind: "ok" };
    const name = (primary && primary.name) || report.disease_name || "";
    if (!name) return { text: "看不清", kind: "warn" };
    return { text: "需细看", kind: "alert" };
  }

  function batchName(data) {
    const report = (data && data.report) || {};
    const primary = report.primary || {};
    const name = (primary && primary.name) || report.disease_name || "";
    if (name) return name + (primary.kind === "pest" ? "（虫害）" : "");
    return report.healthy ? "未见明显病斑" : "未能确定";
  }

  function batchConf(data) {
    const report = (data && data.report) || {};
    const primary = report.primary || {};
    const v = primary.kind === "pest" ? primary.confidence : report.confidence;
    return typeof v === "number" ? Math.round(v * 100) + "%" : "—";
  }

  async function runBatch() {
    if (batch.running || !batch.files.length) return;
    batch.running = true;
    batch.rows = [];
    // 批次号：用户在巡检过程中点「重新开始」，旧循环必须自己退出 ——
    // 否则它会把剩下几张继续跑完，再往已经清空的表格里追加行。
    const token = batch.token + 1;
    batch.token = token;

    $("diagnose-form").hidden = true;
    $("batch-card").hidden = true;
    const root = $("batch-root");
    root.hidden = false;
    renderBatchShell();
    root.scrollIntoView({ behavior: "smooth", block: "start" });

    const started = Date.now();
    for (let i = 0; i < batch.files.length; i += 1) {
      if (batch.token !== token) return;     // 已被「重新开始」作废
      const file = batch.files[i];
      setBatchProgress(i, batch.files.length, file.name);

      let row;
      try {
        const form = new FormData();
        form.append("image", file, file.name || `batch_${i}.jpg`);
        form.append("text", "");
        form.append("dialect", window.Dialect ? Dialect.current() : "");
        const data = await Api.diagnoseMultimodal(form);
        row = { file, data };
      } catch (err) {
        // 单张失败不该中断整批：农技员要的是「这张没跑成，其余照常」，
        // 而不是整批重来。
        row = { file, error: err.message };
      }
      // await 期间用户可能已经重新开始：这时这一行属于上一批，不能写进新表。
      if (batch.token !== token) return;
      batch.rows.push(row);
      appendBatchRow(row, batch.rows.length - 1);
    }

    batch.running = false;
    finishBatch(Math.round((Date.now() - started) / 1000));
  }

  function renderBatchShell() {
    const root = $("batch-root");
    root.innerHTML =
      `<div class="card result-panel">` +
      `<div class="batch-head">` +
      `<h2 class="batch-head__title">批量巡检结果</h2>` +
      `<span class="text-sm text-muted" id="batch-progress-text">准备中…</span>` +
      `</div>` +
      `<div class="batch-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" ` +
      `aria-valuenow="0" id="batch-bar"><span class="batch-bar__fill" id="batch-bar-fill"></span></div>` +
      `<p class="batch-summary" id="batch-summary">逐张诊断中，请勿关闭页面。</p>` +
      `<div class="batch-table-wrap">` +
      `<table class="batch-table">` +
      `<thead><tr><th>照片</th><th>判读</th><th>结论</th><th>置信度</th><th></th></tr></thead>` +
      `<tbody id="batch-rows"></tbody>` +
      `</table>` +
      `</div>` +
      `<div class="result-actions">` +
      `<span class="text-sm text-muted">判读口径：未见病斑 / 看不清（需补拍）/ 需细看。` +
      `批量只给结论名，处置方案请点「看详情」。</span>` +
      `<div class="result-actions__ops">` +
      `<button type="button" class="btn btn--outline" id="btn-batch-csv" disabled>导出 CSV</button>` +
      `<button type="button" class="btn btn--outline" id="btn-batch-again">继续巡检</button>` +
      `</div></div>` +
      `</div>`;

    $("btn-batch-csv").addEventListener("click", exportBatchCsv);
    $("btn-batch-again").addEventListener("click", resetBatch);

    // 行内「看详情」用委托：行是逐张追加的，不能每加一行绑一次
    $("batch-rows").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-detail]");
      if (!btn) return;
      const idx = Number(btn.getAttribute("data-detail"));
      const row = batch.rows[idx];
      if (!row || !row.data) return;
      state.imageFile = row.file;       // 让详情页的虫害标注图能用这张
      state.lastText = "";
      state.archive = null;
      $("batch-root").hidden = true;
      renderResult(row.data);
    });
  }

  function setBatchProgress(i, total, name) {
    const pct = Math.round((i / total) * 100);
    const text = $("batch-progress-text");
    if (text) text.textContent = `第 ${i + 1}/${total} 张 · ${name}`;
    const bar = $("batch-bar");
    const fill = $("batch-bar-fill");
    if (bar) bar.setAttribute("aria-valuenow", String(pct));
    if (fill) fill.style.width = pct + "%";
  }

  function appendBatchRow(row, idx) {
    const body = $("batch-rows");
    if (!body) return;

    let verdict;
    let name;
    let conf;
    if (row.error) {
      verdict = { text: "失败", kind: "warn" };
      name = row.error;
      conf = "—";
    } else {
      verdict = batchVerdict(row.data);
      name = batchName(row.data);
      conf = batchConf(row.data);
    }

    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="batch-table__file">${UI.escapeHtml(row.file.name)}</td>` +
      `<td><span class="batch-verdict batch-verdict--${verdict.kind}">` +
      `${UI.escapeHtml(verdict.text)}</span></td>` +
      `<td>${UI.escapeHtml(name)}</td>` +
      `<td>${UI.escapeHtml(conf)}</td>` +
      `<td class="batch-table__op">` +
      (row.error
        ? ""
        : `<button type="button" class="btn btn--sm btn--outline" ` +
          `data-detail="${idx}">看详情</button>`) +
      `</td>`;
    body.appendChild(tr);
  }

  function finishBatch(seconds) {
    const ok = batch.rows.filter((r) => !r.error);
    const healthy = ok.filter((r) => batchVerdict(r.data).kind === "ok").length;
    const unsure = ok.filter((r) => batchVerdict(r.data).kind === "warn").length;
    const found = ok.length - healthy - unsure;
    const failed = batch.rows.length - ok.length;

    const text = $("batch-progress-text");
    if (text) text.textContent = `完成 ${batch.rows.length} 张，用时 ${seconds} 秒`;
    const fill = $("batch-bar-fill");
    if (fill) fill.style.width = "100%";

    const sum = $("batch-summary");
    if (sum) {
      sum.innerHTML =
        `<b>${batch.rows.length}</b> 张：` +
        `<span class="batch-verdict batch-verdict--alert">需细看 ${found}</span>` +
        `<span class="batch-verdict batch-verdict--warn">看不清 ${unsure}</span>` +
        `<span class="batch-verdict batch-verdict--ok">未见病斑 ${healthy}</span>` +
        (failed ? `<span class="batch-verdict batch-verdict--warn">失败 ${failed}</span>` : "") +
        `　先处理「需细看」与「看不清」这两类。`;
    }

    const csv = $("btn-batch-csv");
    if (csv) csv.disabled = false;
    UI.toast(`巡检完成：${batch.rows.length} 张`, "info");
  }

  function exportBatchCsv() {
    if (!batch.rows.length) return;
    const head = ["文件名", "判读", "结论", "置信度", "备注"];
    const lines = [head.join(",")];
    batch.rows.forEach((r) => {
      if (r.error) {
        lines.push([r.file.name, "失败", "", "", r.error].map(csvCell).join(","));
        return;
      }
      const v = batchVerdict(r.data);
      const conf = batchConf(r.data);
      lines.push([r.file.name, v.text, batchName(r.data), conf, ""].map(csvCell).join(","));
    });

    // \ufeff 是给 Excel 的 BOM：没有它，中文在 Windows 上打开是乱码
    const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `巡检结果_${UI.localDate()}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  }

  function csvCell(value) {
    const s = String(value == null ? "" : value);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  function resetBatch() {
    // 巡检跑着的时候不许清空：清空只改前端状态，网络请求停不下来，
    // 结果就是「点了重新开始，几秒后又冒出一批结果」。
    if (batch.running) {
      UI.toast("巡检还在进行，等这一批跑完再重新开始", "warning");
      return;
    }
    batch.token += 1;          // 让可能残留的旧循环立刻作废
    batch.files = [];
    batch.rows = [];
    batch.running = false;
    const root = $("batch-root");
    if (root) {
      root.hidden = true;
      root.innerHTML = "";
    }
    const input = $("input-batch");
    if (input) input.value = "";
    const card = $("batch-card");
    if (card) card.hidden = false;
    const count = $("batch-count");
    if (count) count.textContent = "未选择照片";
    const start = $("btn-batch-start");
    if (start) start.disabled = true;
    const form = $("diagnose-form");
    if (form) form.hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /* ==================================================================
     启动
     ================================================================== */

  function boot() {
    const icons = {
      "ico-camera": "camera",
      "ico-image": "image",
      "ico-upload": "upload",
      "ico-mic": "mic",
      "ico-dropzone": "image",
      "ico-submit": "scan",
      "ico-batch": "upload",
    };
    Object.entries(icons).forEach(([id, name]) => {
      const el = $(id);
      if (el && window.Icon) el.innerHTML = Icon.render(name, 18);
    });

    bindImageInputs();
    bindAudioInputs();
    bindForm();
    bindBatch();
    bindSheetModal();

    // 离开页面时停止录音，避免麦克风一直占用（这里只能同步收尾，拿不到音频也不必要）
    window.addEventListener("beforeunload", () => {
      if (window.Recorder && Recorder.isRecording()) Recorder.cancel();
    });
  }

  /** 问诊单的开关。关闭入口有三个（遮罩、右上角按钮、Esc），
   *  每个都单独写一遍 close 会把 body 的 modal-open 类忘在其中一个上。 */
  function bindSheetModal() {
    const modal = $("sheet-modal");
    if (!modal) return;

    const closeBtn = $("sheet-close");
    if (closeBtn) closeBtn.innerHTML = Icon.render("close", 20);

    modal.querySelectorAll("[data-sheet-close]").forEach((el) => {
      el.addEventListener("click", closeSheet);
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.hidden) closeSheet();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
