/* ==========================================================================
   任务 5.4 —— 知识图书库页
   · 顶部分类标签栏：病害防治知识 / 虫害防治知识 / 农作物养护知识
     （警告：分类键 pest 装的**全是病害**，虫害在 insect 里 —— 键名是历史遗留）
   · 响应式卡片网格，每张卡片上部封面图、下部名称标题
   · 点击卡片弹出**全屏模态层**展示详情（发病规律/危害规律、防治方法、养护要点、配图），
     模态内右上角「方言朗读」
   · 页面顶部搜索框，支持关键词检索与**方言语音输入**
   · 支持 ?open=<卡片id> 深链（从首页地图点击省份直接打开对应病害详情）
   · 首屏用 HTML 里同步引入的本地快照（window.__LIBRARY_PRELOAD__）**立刻**渲染，
     随后照常用 /api/library 校准；后端没起时保留快照内容而不是显示「加载失败」

   关于模态层：原来是卡片内联向下展开，但详情正文很长（含配图与多个小节），
   一展开就把网格撑开、后面几十张卡片全被推到屏幕外；关掉又要滚回原处。
   改成覆盖全屏的一层后，详情有了独立的滚动区，列表不再被搅动。

   关于离线：快照里**只有 brief**（列表页要用的字段，没有 sections，约 190 KB）。
   library_items.json 是 1.3 MB，同步脚本装不动也没必要装 ——
   正文仍在点开卡片时按 id 单独取，那条路必须有后端，所以离线时详情会失败，
   提示语里如实说明。
   ========================================================================== */

(function () {
  "use strict";

  const PAGE_SIZE = 12;

  const state = {
    category: "",
    keyword: "",
    page: 1,
    total: 0,
    categories: [],
    items: [],
    loaded: [],          // 已渲染的卡片 id
    fromSnapshot: false, // 当前网格里画的是不是快照内容
    detailCache: new Map(),
  };

  const $ = (id) => document.getElementById(id);

  /* ---------------------------------------------------------------- 渲染 */

  function renderTabs() {
    const host = $("library-tabs");
    const all = state.categories.reduce((n, c) => n + c.count, 0);
    const tabs = [{ key: "", name: "全部知识", count: all }].concat(state.categories);

    host.innerHTML = tabs
      .map(
        (t) =>
          `<button type="button" class="library-tab" role="tab" data-key="${UI.escapeHtml(t.key)}" ` +
          `aria-selected="${t.key === state.category}">` +
          `${UI.escapeHtml(t.name)}<span class="library-tab__count">${t.count}</span></button>`
      )
      .join("");

    host.querySelectorAll("[data-key]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.key === state.category) return;
        state.category = btn.dataset.key;
        state.page = 1;
        state.loaded = [];
        leaveSnapshot();
        renderTabs();
        load(true);
      });
    });
  }

  function cardHtml(item) {
    const cover = item.cover
      ? `<img src="${UI.escapeHtml(item.cover)}" alt="${UI.escapeHtml(item.title)}封面" loading="lazy">`
      : "";
    // 三个分类各一个角标，一眼分得清是虫还是病 —— 两者用药时机完全不同
    const kindBadge =
      item.category === "care"
        ? `<span class="badge badge--info book-card__badge">养护</span>`
        : item.category === "insect"
          ? `<span class="badge badge--warning book-card__badge">虫害</span>`
          : `<span class="badge badge--primary book-card__badge">病害</span>`;

    return (
      `<article class="book-card" data-id="${UI.escapeHtml(item.id)}">` +
      `<button type="button" class="book-card__trigger">` +
      `<span class="book-card__cover">${cover}${kindBadge}</span>` +
      `<span class="book-card__body">` +
      `<span class="book-card__title">${UI.escapeHtml(item.title)}</span>` +
      `<span class="book-card__summary">${UI.escapeHtml(item.summary)}</span>` +
      `<span class="book-card__foot">` +
      (item.crop
        ? `<span class="badge book-card__crop">作物：${UI.escapeHtml(item.crop)}</span>`
        : "") +
      `<span class="book-card__toggle">查看详情${Icon.render("chevronRight", 14)}</span>` +
      `</span>` +
      `</span>` +
      `</button>` +
      `</article>`
    );
  }

  function renderCards(reset) {
    const grid = $("library-grid");
    if (reset) {
      grid.innerHTML = state.items.map(cardHtml).join("");
      bindCards();
    } else {
      const frag = state.items.slice(state.loaded.length);
      if (frag.length) {
        grid.insertAdjacentHTML("beforeend", frag.map(cardHtml).join(""));
        bindCards();
      }
    }
    state.loaded = state.items.map((i) => i.id);

    const more = $("btn-load-more");
    more.hidden = state.items.length >= state.total;

    $("total-badge").textContent =
      state.keyword || state.category
        ? `筛选出 ${state.total} 条`
        : `共 ${state.total} 张卡片`;
    $("search-summary").textContent = state.keyword
      ? `「${state.keyword}」匹配 ${state.total} 条`
      : "";
  }

  function bindCards() {
    document.querySelectorAll(".book-card").forEach((card) => {
      if (card.dataset.bound) return;
      card.dataset.bound = "1";
      card
        .querySelector(".book-card__trigger")
        .addEventListener("click", () => openDetail(card.dataset.id, card));
    });
  }

  /* ---------------------------------------------------------------- 模态层 */

  /** 关闭后要把焦点还给触发它的那张卡片，键盘用户才不会掉回页面顶部。 */
  let lastTrigger = null;

  function modalEl() {
    return $("library-modal");
  }

  function isOpen() {
    const el = modalEl();
    return !!el && !el.hidden;
  }

  function openDetail(id, triggerEl) {
    const el = modalEl();
    if (!el || !id) return;
    lastTrigger = triggerEl || null;
    el.hidden = false;
    // 锁住背景滚动，否则滚动会穿透到下面的网格
    document.body.classList.add("modal-open");
    const close = $("modal-close");
    if (close) close.focus();

    const body = $("modal-body");
    if (state.detailCache.has(id)) {
      body.innerHTML = state.detailCache.get(id);
      bindSpeak(body);
      return;
    }

    body.innerHTML =
      `<div class="text-center" style="padding:32px">` +
      `<span class="spinner" role="status" aria-label="详情加载中"></span></div>`;

    Api.libraryDetail(id)
      .then((item) => {
        const html = detailHtml(item);
        state.detailCache.set(id, html);
        // 请求返回时用户可能已经关掉了，别再往隐藏的容器里塞内容
        if (!isOpen()) return;
        body.innerHTML = html;
        bindSpeak(body, item);
      })
      .catch((err) => {
        if (!isOpen()) return;
        body.innerHTML =
          `<h2 class="modal-detail__title" id="modal-title">加载失败</h2>` +
          `<div class="notice notice--danger">${Icon.render("alert", 18)}` +
          `<span>详情加载失败：${UI.escapeHtml(err.message)}<br>` +
          // 正文没进快照（1.3 MB），所以离线时列表能看、详情必然失败。
          // 这时要说清是「后端没起」而不是「数据没生成」，否则用户会去重跑 07 号脚本。
          (state.fromSnapshot
            ? `当前展示的是本地快照，快照里不含卡片正文。<br>`
              + `请启动后端服务：<code>python scripts/20_run_backend.py</code>。`
            : `请确认已生成数据（<code>python scripts/07_build_library.py</code>）。`) +
          `</span></div>`;
      });
  }

  /** 缓存命中时没有 item 对象，朗读文本从 DOM 上挂的 data-speak-text 取。 */
  function bindSpeak(host, item) {
    const btn = host.querySelector(".detail-speak");
    if (!btn) return;
    const wrapper = host.querySelector(".modal-detail");
    const cached = wrapper ? wrapper.dataset.speakText || "" : "";
    Dialect.bindSpeakButton(btn, () =>
      item ? item.read_text || item.summary || item.title : cached
    );
  }

  function closeDetail() {
    const el = modalEl();
    if (!el || el.hidden) return;
    el.hidden = true;
    document.body.classList.remove("modal-open");
    $("modal-body").innerHTML = "";
    if (lastTrigger && document.contains(lastTrigger)) lastTrigger.focus();
    lastTrigger = null;
  }

  function bindModal() {
    const el = modalEl();
    if (!el) return;

    el.querySelectorAll("[data-modal-close]").forEach((node) =>
      node.addEventListener("click", closeDetail)
    );

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isOpen()) {
        e.preventDefault();
        closeDetail();
      }
    });

    // 模态遮罩上没有可点的东西，关闭按钮的图标在这里补上
    const close = $("modal-close");
    if (close && window.Icon) close.innerHTML = Icon.render("close", 20);
  }

  function detailHtml(item) {
    const isCare = item.category === "care";
    const isInsect = item.category === "insect";
    const speakText = item.read_text || item.summary || item.title;

    const sections = (item.sections || [])
      .map((s) => {
        const images = (s.images || []).length
          ? `<div class="detail-section__images">` +
            s.images
              .map((src) => `<img src="${UI.escapeHtml(src)}" alt="${UI.escapeHtml(item.title)}配图" loading="lazy">`)
              .join("") +
            `</div>`
          : "";
        return (
          `<div class="detail-section">` +
          `<div class="detail-section__title">${Icon.render(sectionIcon(s.type), 16)}${UI.escapeHtml(s.title)}</div>` +
          `<p class="detail-section__text">${UI.escapeHtml(s.text)}</p>` +
          images +
          `</div>`
        );
      })
      .join("");

    // 同一作物下的不同病害标题开头都一样（如「萝卜黑斑病防治」「萝卜霜霉病防治」），
    // 卡片上标题又被截断，所以把作物与病害单独拎出来做徽章，一眼能分清是哪一种。
    // 虫害卡片的 condition 是虫名（label_cn），徽章文案跟着换，别写成「病害：稻纵卷叶螟」。
    const cropBadge = item.crop
      ? `<span class="badge badge--primary">作物：${UI.escapeHtml(item.crop)}</span>`
      : "";
    const conditionBadge = isCare
      ? `<span class="badge badge--info">类型：养护</span>`
      : item.condition
        ? `<span class="badge badge--danger">${isInsect ? "虫害" : "病害"}：${UI.escapeHtml(item.condition)}</span>`
        : "";

    // 病害讲「病原类型」（真菌/细菌…），虫害讲「取食方式类群」（刺吸类/潜叶类…）——
    // 两套口径不能拼在一起：虫害卡片没有 pathogen_type，给它加个「性」字会读成
    // 「潜叶类性」。虫害这边顺带把拉丁学名也放出来（它是**看图认虫**时最可靠的锚点）。
    const taxonomyBadges = isInsect
      ? (item.pest_type
          ? `<span class="badge badge--warning">${UI.escapeHtml(item.pest_type)}</span>`
          : "") +
        (item.latin ? `<span class="badge"><i>${UI.escapeHtml(item.latin)}</i></span>` : "")
      : item.pathogen_type
        ? `<span class="badge badge--warning">${UI.escapeHtml(item.pathogen_type)}性</span>`
        : "";

    return (
      `<div class="modal-detail" data-speak-text="${UI.escapeHtml(speakText)}">` +
      `<h2 class="modal-detail__title" id="modal-title">${UI.escapeHtml(item.title)}</h2>` +
      `<div class="row modal-detail__badges">` +
      cropBadge +
      conditionBadge +
      (item.crop_category ? `<span class="badge">${UI.escapeHtml(item.crop_category)}</span>` : "") +
      taxonomyBadges +
      (item.is_mixed ? `<span class="badge badge--warning">复合病害</span>` : "") +
      `<button type="button" class="btn btn--sm btn--outline detail-speak">` +
      Icon.render("volume", 16) + `<span>方言朗读</span></button>` +
      `</div>` +
      sections +
      `</div>`
    );
  }

  function sectionIcon(type) {
    return (
      { pattern: "search", damage: "bug", control: "shield", care: "sprout",
        medication: "warning", notice: "info" }[type] ||
      "info"
    );
  }

  /* ------------------------------------------------- 本地快照（首次进入即加载） */

  /** HTML 里同步引入的 frontend/assets/data/library_preload.js 挂上来的数据。
   *  它是 build_library_preload() 生成的**列表页 brief**（不含正文），
   *  由 scripts/07_build_library.py 与 scripts/15_bundle_frontend_data.py 重打。 */
  function snapshot() {
    const d = window.__LIBRARY_PRELOAD__;
    return d && Array.isArray(d.items) && d.items.length ? d : null;
  }

  /** 快照兜底提示 —— 内容仍在屏幕上，只是得说清它从哪儿来。
   *
   *  **不能复用 [data-degraded-bar]**：那个节点归 layout.js 的 renderDegradedBar()
   *  所有（数据源 /api/health），两边都往同一处写会互相覆盖。 */
  function showSnapshotNotice() {
    const el = $("library-notice");
    if (!el) return;
    el.hidden = false;
    el.innerHTML =
      Icon.render("info", 18) +
      `<span>后端服务未启动，当前展示的是<b>本地快照</b>（共 ${state.total} 张卡片，` +
      `这里先列出前 ${state.items.length} 张）。检索、翻页与卡片正文需要后端服务：` +
      `<code>python scripts/20_run_backend.py</code>。</span>`;
  }

  function hideSnapshotNotice() {
    const el = $("library-notice");
    if (el) el.hidden = true;
  }

  /** 用快照把首屏**同步**画出来：不铺骨架屏、不发请求。
   *
   *  随后 boot() 照常 load(true) 请求接口，成功就用接口数据覆盖它（校准），
   *  失败则保留这一屏 —— 后端没起也能翻列表。
   *  返回 true 表示画上了；快照不存在时返回 false，后续行为与改造前完全一致。 */
  function renderFromSnapshot() {
    const d = snapshot();
    if (!d) return false;

    state.total = d.total || d.items.length;
    state.categories = d.categories || [];
    state.items = d.items.slice(0, PAGE_SIZE);

    renderTabs();
    renderCards(true);   // 顺带把 total-badge / 加载更多 一起定好
    state.fromSnapshot = true;
    return true;
  }

  /** 用户改了筛选条件（切分类 / 改关键词）就该丢掉快照这一屏的「现状」标记，
   *  让接下来的加载照常铺骨架屏；否则新条件会短暂地盖着旧条件的卡片。 */
  function leaveSnapshot() {
    state.fromSnapshot = false;
  }

  /* ---------------------------------------------------------------- 数据 */

  function load(reset) {
    const grid = $("library-grid");
    // 快照已经把内容画出来了就别再铺骨架屏 —— 那会把刚画好的卡片闪没一下
    if (reset && !state.fromSnapshot) {
      grid.innerHTML =
        Array.from({ length: 8 })
          .map(
            () =>
              `<article class="book-card"><span class="book-card__cover skeleton"></span>` +
              `<span class="book-card__body"><span class="skeleton" style="display:block;height:18px;margin-bottom:8px"></span>` +
              `<span class="skeleton" style="display:block;height:14px;width:70%"></span></span></article>`
          )
          .join("");
    }

    return Api.library({
      category: state.category,
      q: state.keyword,
      page: state.page,
      size: PAGE_SIZE,
    })
      .then((d) => {
        state.fromSnapshot = false;
        hideSnapshotNotice();
        state.total = d.total;
        state.categories = d.categories || state.categories;
        state.items = reset ? d.items : state.items.concat(d.items);
        if (reset) renderTabs();
        renderCards(reset);

        if (!state.items.length) {
          grid.innerHTML =
            `<div class="notice notice--warning" style="grid-column:1/-1">` +
            Icon.render("info", 18) +
            `<span>没有匹配「${UI.escapeHtml(state.keyword)}」的内容，换个关键词试试；` +
            `也可以只输入作物名（如「玉米」）或症状（如「皱缩」）。</span></div>`;
        }
      })
      .catch((err) => {
        // 快照内容还在屏幕上：保留它，只如实说明数据来源。
        // 换成「加载失败」等于把已经能看的东西擦掉 —— 离线时那正是最不该做的。
        if (state.fromSnapshot) {
          showSnapshotNotice();
          return;
        }
        grid.innerHTML =
          `<div class="notice notice--danger" style="grid-column:1/-1">` +
          Icon.render("alert", 18) +
          `<span>知识库加载失败：${UI.escapeHtml(err.message)}<br>` +
          `请确认已生成数据（<code>python scripts/07_build_library.py</code>）。</span></div>`;
      });
  }

  /* ---------------------------------------------------------------- 语音检索 */

  /* 录音采集在公共模块 recorder.js（与诊断页同一份实现），本模块只管界面与请求。
     识别走 /api/voice/asr，方言取导航栏当前选中的那个 —— 主入口是「选了四川话就说四川话」。 */
  let offRecTick = null;
  let searchTimer = null;

  /* 手打（防抖后）与语音识别（立即）都走这一个入口。
     先 clearTimeout 是关键：否则「打了两个字、又改用语音」时，那个还挂着的 320ms
     定时器会在识别结果落地之后拿旧关键词再检索一次，把识别结果盖掉。 */
  function applyKeyword(value) {
    window.clearTimeout(searchTimer);
    searchTimer = null;
    state.keyword = (value || "").trim();
    state.page = 1;
    state.loaded = [];
    leaveSnapshot();
    load(true);
  }

  /* 失败文案按 Recorder 抛出的 e.name 分开。非安全上下文那句是必须的：
     用 http://<局域网 IP>:8000 打开时 navigator.mediaDevices 根本不存在，
     混成「浏览器不支持录音」会让用户去换浏览器，而真实原因是访问方式。 */
  function voiceErrorMessage(e) {
    const name = e && e.name;
    if (name === "unsupported") return "当前浏览器不支持录音，请直接用键盘输入关键词";
    if (name === "insecure") return "录音需要 https 或 localhost 环境，请改用 127.0.0.1 访问本页，或直接用键盘输入关键词";
    if (name === "denied") return "无法访问麦克风：" + e.message + "（请检查浏览器权限设置）";
    return "录音启动失败：" + ((e && e.message) || "未知原因");
  }

  /* 录音中/识别中都复用 #search-summary 这个位置 —— 它平时显示「匹配 N 条」，
     正好在检索回来时被 renderCards() 覆盖，闭环不用另加元素与样式。 */
  function startRecordingUi() {
    $("library-mic").setAttribute("aria-pressed", "true");
    $("search-summary").innerHTML =
      `<span class="record-status"><span class="record-status__dot"></span>` +
      `<span>正在录音 <span id="lib-rec-time">00:00</span></span></span>`;
    if (!offRecTick) {
      offRecTick = Recorder.on("tick", (ms) => {
        const el = $("lib-rec-time");
        if (el) el.textContent = Recorder.formatDuration(ms);
      });
    }
  }

  function stopRecordingUi() {
    $("library-mic").setAttribute("aria-pressed", "false");
    if (offRecTick) {
      offRecTick();
      offRecTick = null;
    }
    $("search-summary").textContent = "";
  }

  async function onMicClick() {
    if (!window.Recorder || !Recorder.isSupported()) {
      UI.toast("当前浏览器不支持录音，请直接用键盘输入关键词", "error");
      return;
    }

    if (Recorder.isRecording()) {
      const hit = await Recorder.stop();
      stopRecordingUi();
      if (!hit) {
        UI.toast("没有录到声音，请检查麦克风后重试", "error");
        return;
      }
      recognize(hit.blob);
      return;
    }

    try {
      await Recorder.start();
    } catch (e) {
      UI.toast(voiceErrorMessage(e), "error");
      return;
    }
    startRecordingUi();
  }

  function recognize(blob) {
    const form = new FormData();
    form.append("audio", blob, "record.wav");
    form.append("dialect", window.Dialect ? Dialect.current() : "");

    const restore = UI.busy($("library-mic"), "");   // 识别期间禁用麦克风键，防重复点击
    $("search-summary").textContent = "正在识别…";

    Api.asr(form)
      .then((data) => {
        restore();
        const text = ((data && data.text) || "").trim();

        /* 未配置语音 Key 时后端返回的是 code 0 + mock=true + 空文本 + 说明文案，
           **不是异常** —— 只靠 .catch 会把「没配 Key」显示成「识别成功但没内容」。
           这种情况绝不填词、绝不检索，把后端给的说明原样告诉用户。 */
        if (!text) {
          $("search-summary").textContent = "";
          UI.toast(
            data && data.mock
              ? (data.message || "未配置语音服务，无法识别。")
              : "没有识别到内容，请再说一遍，或用键盘输入关键词",
            "warning",
            7000
          );
          return;
        }

        $("library-search").value = text;
        applyKeyword(text);
        // 该方言在当前语音服务下没有识别模型时如实说明一句（实际识别用的是回退模型）
        if (data.fallback && data.fallback_note) UI.toast(data.fallback_note, "warning", 6000);
      })
      .catch((err) => {
        restore();
        $("search-summary").textContent = "";
        UI.toast("语音识别失败：" + err.message, "error", 7000);
      });
  }

  /* ---------------------------------------------------------------- 绑定 */

  function boot() {
    const searchIcon = $("ico-search");
    if (searchIcon && window.Icon) searchIcon.innerHTML = Icon.render("search", 18);
    const micIcon = $("ico-mic");
    if (micIcon && window.Icon) micIcon.innerHTML = Icon.render("mic", 18);
    bindModal();

    // 搜索防抖，避免每敲一个字就请求一次；语音识别走 applyKeyword 直接检索，不等防抖
    $("library-search").addEventListener("input", (e) => {
      window.clearTimeout(searchTimer);
      const value = e.target.value.trim();
      searchTimer = window.setTimeout(() => applyKeyword(value), 320);
    });

    $("library-mic").addEventListener("click", onMicClick);

    $("btn-load-more").addEventListener("click", () => {
      state.page += 1;
      load(false);
    });

    // 也支持 ?q= 关键词（便于从别处直接检索）
    const q = UI.query("q");
    if (q) {
      state.keyword = q;
      $("library-search").value = q;
    }

    // 先用本地快照**同步**画出首屏 —— 打开页面立刻就有卡片，不必等 /api/library，
    // 后端没起也照样能浏览列表。紧接着的 load(true) 再用接口数据校准它
    // （服务端可能刚重建过知识库，快照与真值不一致时以接口为准）。
    // ?q= 带关键词时不画：快照那一屏是「全部知识」，与用户要检索的内容对不上。
    if (!state.keyword) renderFromSnapshot();

    load(true);

    // 支持首页地图点省份跳转过来时带着 ?open=lib_pest_123。
    // 模态层是按 id 独立取数的，不依赖目标卡片在当前列表/当前页里，
    // 所以这里直接开模态即可，不必再把它检索进网格。
    const openId = UI.query("open");
    if (openId) openDetail(openId);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
