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

  const state = {
    imageFile: null,
    audioBlob: null,
    audioDuration: 0,
    lastResult: null,
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
      setAudio(hit.blob, `录音_${new Date().toISOString().slice(11, 19).replace(/:/g, "")}.wav`);
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

  function setAudio(blob, name) {
    state.audioBlob = blob;
    const host = $("audio-preview");
    host.hidden = false;
    const seconds = state.audioDuration || 0;
    host.innerHTML =
      `<div class="preview-audio">` +
      `<div class="text-sm"><b>${UI.escapeHtml(name)}</b>　` +
      `<span class="text-muted">${(blob.size / 1024).toFixed(0)} KB` +
      (seconds ? `　时长约 ${seconds.toFixed(1)} 秒` : "") + `</span></div>` +
      `<audio controls preload="metadata"></audio>` +
      `<button type="button" class="btn btn--sm btn--outline" id="btn-clear-audio">移除语音</button>` +
      `</div>`;
    host.querySelector("audio").src = URL.createObjectURL(blob);
    $("btn-clear-audio").addEventListener("click", clearAudio);
  }

  function clearAudio() {
    state.audioBlob = null;
    state.audioDuration = 0;
    const host = $("audio-preview");
    host.hidden = true;
    host.innerHTML = "";
  }

  function bindAudioInputs() {
    $("btn-record").addEventListener("click", toggleRecording);

    $("btn-choose-audio").addEventListener("click", () => $("input-audio").click());
    $("input-audio").addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;

      const ext = (file.name.split(".").pop() || "").toLowerCase();
      if (!["wav", "pcm", "amr", "m4a"].includes(ext)) {
        UI.toast(
          "百度语音识别只支持 wav / pcm / amr 格式。" +
            "若是手机录音（m4a）或浏览器录音（webm），请先转码为 16kHz 单声道 WAV。",
          "warning",
          7000
        );
      }
      state.audioDuration = 0;
      setAudio(file, file.name);
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
    if (state.audioBlob) form.append("audio", state.audioBlob, "record.wav");
    form.append("text", $("input-text").value.trim());
    form.append("dialect", window.Dialect ? Dialect.current() : "");

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
          _confidenceBlock(confidencePct, lowConf)
      ) +
      _askBlock(report) +
      _actions(report) +
      `</div>`;

    form.hidden = true;
    root.scrollIntoView({ behavior: "smooth", block: "start" });

    Dialect.bindSpeakButton($("btn-speak"), () => buildReadText(report, sections, data));
    $("btn-restart").addEventListener("click", resetToInput);
    bindFarmButton(report);
    bindAsk(report);
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
    const name = (primary && primary.name) || report.disease_name || "未能确定病害";
    const crop = pestPrimary
      ? (primary.crop || "")
      : (top1 && top1.crop ? top1.crop : "");
    const patho = pestPrimary
      ? (primary.detail || "")
      : (top1 && top1.pathogen_type ? top1.pathogen_type : "");
    const healthy = !pestPrimary && top1 && top1.is_healthy;

    return (
      `<div class="result-hero">` +
      `<div class="result-hero__main">` +
      `<div class="result-hero__label">${pestPrimary ? "诊断结果（虫害）" : "诊断结果"}</div>` +
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
      .map(
        (s) =>
          `<li>${Icon.render("book", 14)}<span>${UI.escapeHtml(s.title)}` +
          (s.score != null ? ` <span class="source-list__score">相似度 ${(s.score * 100).toFixed(0)}%</span>` : "") +
          `</span></li>`
      )
      .join("");
    return (
      `<div class="result-block">` +
      `<div class="result-block__title">${Icon.render("book", 18)}知识库引用</div>` +
      `<ul class="source-list">${rows}</ul>` +
      `</div>`
    );
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
          p.className = "notice notice--primary";
          p.innerHTML = Icon.render("info", 18) + `<span></span>`;
          // 用 textContent 写入回答，避免把模型返回的文本当 HTML 解析
          p.querySelector("span").textContent =
            res.answer + (res.provider === "mock" ? "" : "");
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

  function _actions(report) {
    // 「记入我的农田」（阶段 7 / 7.9 入口1）：带上**主结论**的名字跳到农田页，
    // 那边会问「记入哪块田」再落档。
    // 未登录时农田页自己会把人送去登录、登录后带着这个 ?label= 回来 ——
    // 所以这里不用先查登录状态，多一次请求只是让按钮慢半拍。
    const label = _primaryName(report);
    const farmBtn = label
      ? `<button type="button" class="btn btn--outline" id="btn-to-farm">` +
        Icon.render("sprout", 16) + `<span>记入我的农田</span></button>`
      : "";

    return (
      `<div class="result-actions">` +
      `<span class="text-sm text-muted">` +
      `诊断结果仅供参考，重大病虫害请及时联系当地植保部门确认。` +
      `</span>` +
      `<div class="result-actions__ops">` +
      farmBtn +
      `<button type="button" class="btn btn--outline" id="btn-restart">` +
      Icon.render("refresh", 16) + `<span>重新诊断</span></button>` +
      `</div></div>`
    );
  }

  function bindFarmButton(report) {
    const btn = $("btn-to-farm");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const label = _primaryName(report);
      location.href = "/farm.html?label=" + encodeURIComponent(label);
    });
  }

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
    };
    Object.entries(icons).forEach(([id, name]) => {
      const el = $(id);
      if (el && window.Icon) el.innerHTML = Icon.render(name, 18);
    });

    bindImageInputs();
    bindAudioInputs();
    bindForm();

    // 离开页面时停止录音，避免麦克风一直占用（这里只能同步收尾，拿不到音频也不必要）
    window.addEventListener("beforeunload", () => {
      if (window.Recorder && Recorder.isRecording()) Recorder.cancel();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
