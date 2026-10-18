/* ==========================================================================
   阶段 7 / 7.7 + 7.8 —— 我的农田页

   布局（7.11）：左 68% 3D 田块（左上悬浮株高/叶数/积温/阶段，右上今天天气，
   左下麦克风），右 32% 信息面板七张卡片。

   数据流：一次 GET /api/farm/today 拿齐面板所需的全部内容（天气、生长、收获、
   提醒、历史、大模型叙述），3D 参数来自 GET /api/farm/model/{id}。
   分成两个请求是因为 3D 只需要 12 个参数、不需要叙述与历史，切换田块时
   3D 可以先画出来。两者都读同一份服务端算好的结果，前端不重算积温。

   语音录入（7.8）严格按清单的五步走，**不点确认绝不落库**：
   识别文字 → 解析成字段 → 文字与字段双向联动 → 二次确认 → 才 POST /logs。
   ========================================================================== */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    fields: [],
    field: null,
    today: null,
    model: null,
    three: null,
    ro3d: null,       // ResizeObserver：行高会随右侧面板变化，window.resize 抓不到
    recOffTick: null,
    voice: null,      // 语音录入的暂存（识别文字 + 解析结果）
    entered: false,   // 是否已经"进到某一块田里"（false = 停在 8 格这一屏）
    modelById: {},    // 预取到的 3D 参数：进田块时不用再等一次接口
  };

  // ---------------------------------------------------------------- 入口

  async function boot() {
    let status = null;
    try {
      status = await Api.authStatus();
    } catch (e) {
      status = null;
    }
    // 未登录（含后端不可用）→ 去登录页，登录后回跳（7.0.7）
    if (!status || !status.logged_in) {
      const back = encodeURIComponent(location.pathname + location.search);
      location.replace("/login.html?redirect=" + back);
      return;
    }

    // 先退化、再绑事件：degradeLocationInputs 会换掉三个下拉框的节点，
    // 换完再绑才绑得到新节点上。
    if (!ADMIN) degradeLocationInputs();
    bindStaticEvents();
    await loadFields();
  }

  // ---------------------------------------------------------------- 田块

  async function loadFields() {
    let data;
    try {
      data = await Api.farmFields();
    } catch (err) {
      renderFatal("读取田块失败：" + err.message);
      return;
    }

    state.fields = data.fields || [];
    renderFieldOptions(data);

    // 8 格一出来就并行预取几块田的 3D 参数：小图要用，点进去时也就不用再等接口
    prefetchModels();

    if (!state.fields.length) {
      renderEmpty();
      return;
    }

    // 打开页面默认停在 8 格这一屏（用户要的就是"先选田"）。
    // 只有带 ?field=<id> 的深链（从别处跳过来指定看某块田）才直接进那一块。
    const wanted = UI.query("field");
    const target = state.fields.find((f) => f.id === wanted);
    if (target) await enterField(target.id);
    else renderIdle();

    // 从诊断页跳过来的「记入我的农田」：?label=稻瘟病（只在这一屏已经进了某块田时才问）
    await attachFromQuery();

    // 8 格的小图不阻塞首屏：上面的渲染都做完了再渲图（用户先看到格子，图上得慢也看得见）
    renderFieldThumbs();
  }

  function renderFieldOptions(data) {
    const select = $("field-select");
    select.innerHTML = state.fields
      .map((f) => `<option value="${UI.escapeHtml(f.id)}">${UI.escapeHtml(f.name)}</option>`)
      .join("");

    // 新建田块的作物下拉：选项来自服务端（7.17）
    const cropSelect = $("new-field-crop");
    cropSelect.innerHTML = (data.supported_crops || [])
      .map((c) => `<option value="${UI.escapeHtml(c)}">${UI.escapeHtml(c)}</option>`)
      .join("");

    renderFieldGrid();

    // 默认省市继承用户设置（注册时写进去的武汉）
    if (locOk) {
      setLocation(data.default_province || "", data.default_city || "", "");
    } else {
      $("new-field-province").value = data.default_province || "";
      $("new-field-city").value = data.default_city || "";
    }
  }

  /* ------------------------------------------------------------ 两屏之间的状态
   *
   * 这一页现在有"两屏"：
   *   ① 8 格（选田）：一格一块地，各自带一张长势小图；「新建田地」占着剩余空位
   *   ② 大 3D（进到某块田里）：可拖动的大 3D + 右侧信息面板 + 「← 返回田块」
   * 默认停在 ① —— 用户原话是"点击进入后才真正显示可动的模型和信息"。
   */

  /** 摆好 8 格 / 大 3D / 返回键。mode：'plots' 只看 8 格，'field' 看大 3D，'solo' 只留右侧面板。 */
  function setLayoutMode(mode) {
    const layout = document.querySelector(".farm-layout");
    const grid = $("field-grid");
    const guide = $("farm-guide");
    const back = $("farm-back");
    if (layout) {
      layout.hidden = mode === "plots";
      layout.classList.toggle("farm-layout--solo", mode === "solo");
    }
    if (grid) grid.hidden = mode !== "plots";
    if (guide) guide.hidden = mode !== "plots";
    if (back) back.hidden = mode !== "field";
  }

  /** URL 里的 ?field= 始终跟着当前状态走：刷新/分享链接停在同一个地方。 */
  function syncFarmURL(id) {
    const url = new URL(location.href);
    if (id) url.searchParams.set("field", id);
    else url.searchParams.delete("field");
    history.replaceState(null, "", url);
  }

  /** 回到 8 格这一屏。 */
  function renderIdle() {
    state.entered = false;
    state.field = null;
    setLayoutMode("plots");
    markActivePlot(null);
    $("model-overlay").innerHTML = "";
    $("weather-overlay").innerHTML = "";
    $("panel").innerHTML = "";
    $("field-head").innerHTML = `<strong>我的田块</strong>`;
    teardown3D();
    syncFarmURL(null);
    // 进/出这一屏会改变页面高度（纵向滚动条出现或消失），出血基准要跟着重算
    syncFarmBleed();
  }

  /** 拆掉大 3D：停在 8 格时留着一个 WebGL 上下文（还带一张阴影贴图）纯属浪费，
   *  再进来时按当时的行高重建也更干净。 */
  function teardown3D() {
    if (state.ro3d) {
      state.ro3d.disconnect();
      state.ro3d = null;
    }
    if (state.three) {
      state.three.dispose();
      state.three = null;
    }
    const host = $("farm3d");
    if (host) host.innerHTML = "";
  }

  /** 进到某一块田里：8 格让位给大 3D。 */
  async function enterField(id) {
    const field = state.fields.find((f) => f.id === id);
    if (!field) return;
    state.entered = true;
    // 先显形再量尺寸：hidden 的元素 rect 是 0，先量会把画布设成 0×0
    setLayoutMode("field");
    syncFarmBleed();
    await selectField(id);
    resize3D();
  }

  /** 3D 那一行要宽到跟导航栏对齐，但正文其余部分仍按 1280px 居中 ——
   *  出血量 = 正文内容框左边距 − 导航栏的内边距。这样"3D 左边缘和 logo 对齐"
   *  不是靠写死 24px，而是量出来的（正文内边距或导航内边距改了也不会错位）。
   *  窄屏上量出来是负数 → 取 0，退化成"不突破"，也就不会顶出横向滚动条。 */
  function syncFarmBleed() {
    const page = document.querySelector(".page--farm");
    if (!page) return;
    const inner = document.querySelector(".site-header__inner");
    let pad = 24;
    if (inner) {
      const cs = window.getComputedStyle(inner);
      const px = parseFloat(cs.paddingLeft);
      if (!isNaN(px)) pad = px;
    }
    const pageCs = window.getComputedStyle(page);
    const pagePad = parseFloat(pageCs.paddingLeft) || 0;
    const contentLeft = page.getBoundingClientRect().left + pagePad;
    const bleed = Math.max(0, Math.round(contentLeft - pad));
    document.documentElement.style.setProperty("--farm-bleed", bleed + "px");
  }

  function bindFarmBleed() {
    let pending = false;
    const soon = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        syncFarmBleed();
        resize3D();
      });
    };
    window.addEventListener("resize", soon);
    // 右侧面板是异步填的：它一长高，行高就变，出血量也该跟着重算
    if (window.ResizeObserver) {
      new ResizeObserver(soon).observe(document.querySelector(".page--farm") || document.body);
    }
  }

  // ---------------------------------------------------------------- 8 格田块
  //
  // 新用户注册即带 3 条示例田，但一格一格的界面比一个下拉框更像"我的地"：
  // 已有的田各占一格，剩下的格子显示「新建田地」，点进去先填信息（用户的原话）。
  // 真实田块超过 8 块时格子跟着涨（不再补空位），不把用户的田藏起来。

  const MIN_PLOTS = 8;

  function renderFieldGrid() {
    const host = $("field-grid");
    if (!host) return;

    const cards = state.fields.map((f, i) => {
      const where = [f.city, f.county].filter(Boolean).join(" ");
      return `<button type="button" class="plot-card" data-field-id="${UI.escapeHtml(f.id)}"` +
        ` aria-pressed="false">` +
        // 小图先留一块底板（见 pages.css 的 .plot-card__thumb），出图后再由
        // renderFieldThumbs 把 <img> 追加进去 —— 布局不会因为出图而跳一下
        `<span class="plot-card__thumb" data-thumb="${UI.escapeHtml(f.id)}"></span>` +
        `<span class="plot-card__no">第 ${i + 1} 块</span>` +
        `<span class="plot-card__name">${UI.escapeHtml(f.name)}</span>` +
        `<span class="plot-card__meta">${UI.escapeHtml(f.crop)} · ${f.area_mu} 亩</span>` +
        `<span class="plot-card__meta">${UI.escapeHtml(where || "未填位置")}</span>` +
        `</button>`;
    });
    for (let i = state.fields.length; i < Math.max(MIN_PLOTS, state.fields.length); i++) {
      cards.push(
        `<button type="button" class="plot-card plot-card--new" data-new-plot>` +
        // 5 个空位共用同一张"空地"小图（只渲一次），由 renderFieldThumbs 贴进来
        `<span class="plot-card__thumb" data-thumb-new></span>` +
        `<span class="plot-card__plus" aria-hidden="true">+</span>` +
        `<span class="plot-card__name">新建田地</span>` +
        `<span class="plot-card__meta">点这里先填信息</span>` +
        `</button>`);
    }
    host.innerHTML = cards.join("");

    host.querySelectorAll("[data-field-id]").forEach((el) => {
      // 点格子 = 进这块田（8 格收起、大 3D 出现），不是"在下面选一下"
      el.addEventListener("click", () => enterField(el.getAttribute("data-field-id")));
    });
    host.querySelectorAll("[data-new-plot]").forEach((el) => {
      el.addEventListener("click", openNewForm);
    });
    if (state.field && state.entered) markActivePlot(state.field.id);
  }

  /* -------------------------------------------------- 8 格上的 3D 小图
   *
   * 一格一张"这块地现在的样子"。做法是**一个共享的离屏渲染器**顺序出图：
   * 3 块实田各渲一次 + 1 张空地（5 个空位复用）= 一共 4 次。
   * 不给每格一个 WebGLRenderer —— 一个上下文就是一整套场景加一张阴影贴图，
   * 8 个同时开会把浏览器的 WebGL 上下文用光（低端机上直接黑屏），而且
   * <canvas> 放进 <button> 会抢走拖动和滚轮，格子就点不动了。这里出的是 <img>。
   *
   * 三层降级，任何一层挂了，格子上"名称 / 作物 / 亩数"这些文字都还在：
   *   ① 不支持 WebGL、或这份 farm3d.js 没有 createSnapshotter → 不加图
   *   ② 快照器创建失败 → 不加图
   *   ③ 某一格出图失败（返回空串）→ 只跳过那一格
   */

  let thumbBatch = 0;   // 每刷一次网格 +1，作废上一批（正在跑的旧批次会自己停下）

  function putThumb(host, key, src, isNew) {
    if (!src) return;
    // 空位有 5 个，共用同一张图 —— 这里要**全部**贴上，不是只贴第一个
    const slots = host.querySelectorAll(
      isNew ? "[data-thumb-new]" : `[data-thumb="${key}"]`);
    slots.forEach((slot) => {
      const old = slot.firstChild;
      if (old) {
        old.src = src;    // 重渲（比如从田里记账回来）：原地换图，不闪也不跳版
        return;
      }
      const img = document.createElement("img");
      img.className = "plot-card__thumb-img";
      img.alt = "";
      img.src = src;
      slot.appendChild(img);
    });
  }

  async function renderFieldThumbs() {
    const host = $("field-grid");
    if (!host || !state.fields.length) return;
    // ① 没有 WebGL / 没有这个 API 就整批不做，格子照样能点
    if (!Farm3D.supported() || typeof Farm3D.createSnapshotter !== "function") return;

    const batch = ++thumbBatch;
    let shot;
    try {
      shot = Farm3D.createSnapshotter({});
    } catch (e) {
      return;   // ② 快照器都建不起来（比如上下文已经用满），静默降级
    }

    const real = state.fields.filter((f) => f.crop);
    try {
      for (const f of real) {
        if (batch !== thumbBatch) return;             // 网格已经重刷过，这一批作废
        const model = await ensureModel(f.id);        // 预取过就直接命中
        if (batch !== thumbBatch) return;
        if (!model) continue;
        // ③ 单格失败只跳过这一格
        let src = "";
        try {
          src = shot.shot(model, { areaMu: Number(f.area_mu) || 1 });
        } catch (e) {
          src = "";
        }
        putThumb(host, f.id, src, false);
        await nextFrame();                            // 让出一帧，别把出图卡成一段长任务
      }

      if (batch !== thumbBatch) return;
      // 空位只有一种图 → 只渲一次，5 个格子共用同一个 src
      const bare = real.length ? (Number(real[0].area_mu) || 1) : 1;
      let src = "";
      try {
        src = shot.shotBare({ areaMu: bare });
      } catch (e) {
        src = "";
      }
      if (batch === thumbBatch) putThumb(host, "", src, true);
    } finally {
      // 一批渲完立刻回收：忘掉这一步，每重刷一次网格就漏一个 WebGL 上下文
      shot.dispose();
    }
  }

  function nextFrame() {
    return new Promise((r) => requestAnimationFrame(() => r()));
  }

  /** 3D 参数。refresh=true 时**一定重新取**：记一笔、干完了、收了都会改这块田的
   *  长势，进田块看到的必须是新的 —— 缓存只服务于 8 格上的小图（它们不是"实时"的）。 */
  function modelFor(id, refresh) {
    if (!refresh && state.modelById[id]) return Promise.resolve(state.modelById[id]);
    return Api.farmModel(id).then((m) => {
      state.modelById[id] = m;
      return m;
    });
  }

  /** 出小图用：拿不到就当没有，不该把整批预览拖挂。 */
  function ensureModel(id) {
    return modelFor(id).catch(() => null);
  }

  /** 8 格一出来就把 3 块田的 3D 参数取回来：小图要用，点进去时也不用再等接口。 */
  function prefetchModels() {
    state.fields.forEach((f) => { ensureModel(f.id); });
  }

  function markActivePlot(id) {
    document.querySelectorAll("#field-grid [data-field-id]").forEach((el) => {
      const on = el.getAttribute("data-field-id") === id;
      el.classList.toggle("plot-card--active", on);
      el.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function openNewForm() {
    const form = $("new-field-form");
    form.hidden = false;
    form.scrollIntoView({ block: "nearest" });
    const name = $("new-field-name");
    if (name) name.focus();
  }

  // ---------------------------------------------------------------- 选址（省 / 市 / 县）
  //
  // 数据集在 /assets/data/admin_divisions.js（离线、随项目走）：
  //   { 省: { c:[lng,lat], m: { 市: { c:[lng,lat], d: { 县: [lng,lat] } } } } }
  // **经纬度不由用户抄**：按省、市、县三级中心点**取平均**推出来，
  // 缺哪一级就用手里有哪几级（县级缺失 → 省+市两级均值）。用户仍可手动微调。

  const ADMIN = window.__ADMIN_DIVISIONS__ || null;
  let locOk = !!ADMIN;

  function fillSelect(el, names, wanted) {
    const pick = names.indexOf(wanted) >= 0 ? wanted : (names[0] || "");
    el.innerHTML = names
      .map((n) => `<option value="${UI.escapeHtml(n)}">${UI.escapeHtml(n)}</option>`)
      .join("");
    el.value = pick;
    el.disabled = !names.length;
    return pick;
  }

  function setLocation(province, city, county) {
    if (!locOk) return;
    const provs = Object.keys(ADMIN);
    const p = fillSelect($("new-field-province"), provs, province);

    const cityMap = (ADMIN[p] && ADMIN[p].m) || {};
    const cities = Object.keys(cityMap);
    const c = fillSelect($("new-field-city"), cities, city);

    const distMap = (cityMap[c] && cityMap[c].d) || {};
    const d = fillSelect($("new-field-county"), Object.keys(distMap), county);

    applyLatLng();
    return { province: p, city: c, county: d };
  }

  function applyLatLng() {
    if (!locOk) return;
    const p = ADMIN[$("new-field-province").value];
    if (!p) return;
    const city = (p.m || {})[$("new-field-city").value] || {};
    const pts = [p.c, city.c, (city.d || {})[$("new-field-county").value]];
    const pts2 = pts.filter((q) => Array.isArray(q) && q.length === 2);
    if (!pts2.length) return;                       // 一级坐标都没有 → 留着让用户手填
    const avg = (i) => pts2.reduce((s, q) => s + q[i], 0) / pts2.length;
    $("new-field-lng").value = avg(0).toFixed(4);
    $("new-field-lat").value = avg(1).toFixed(4);
  }

  function degradeLocationInputs() {
    // 数据集没打进来（部署时漏了 admin_divisions.js）时，退回纯文本输入：
    // 至少还能手打地址与经纬度，而不是三个点不动的空下拉框。
    locOk = false;
    for (const id of ["new-field-province", "new-field-city", "new-field-county"]) {
      const el = $(id);
      if (!el) continue;
      const input = document.createElement("input");
      input.className = "input";
      input.id = id;
      input.maxLength = 20;
      el.replaceWith(input);
    }
  }

  async function selectField(id) {
    const field = state.fields.find((f) => f.id === id);
    if (!field) return;
    state.field = field;
    $("field-select").value = id;
    markActivePlot(id);

    // 田块卡上的基础信息立刻就能画（不用等接口）
    const where = [field.province, field.city, field.county].filter(Boolean).join(" ");
    $("field-head").innerHTML =
      `<strong>${UI.escapeHtml(field.name)}</strong>` +
      `<span class="text-muted">${UI.escapeHtml(field.crop)} · ${field.area_mu} 亩 · ` +
      `播于 ${UI.escapeHtml(field.sow_date)}</span>` +
      (where ? `<span class="text-muted">${UI.escapeHtml(where)}</span>` : "");

    // 把当前田同步进 URL：刷新后还停在同一块田，链接分享出去也是这一块
    syncFarmURL(id);
    setPanelLoading();

    // 3D 与面板并行取：3D 先到就先画，用户不用等叙述层写完。
    // 这里 refresh=true：进了田就要看现在这块地的样子，不吃预取缓存。
    const modelPromise = modelFor(id, true).then((m) => {
      state.model = m;
      render3D(m);
      renderModelOverlay(m);
    }).catch((err) => {
      $("farm3d").innerHTML =
        `<div class="notice notice--danger">${Icon.render("alert", 18)}` +
        `<span>3D 参数加载失败：${UI.escapeHtml(err.message)}</span></div>`;
    });

    try {
      const data = await Api.farmToday(id);
      state.today = data;
      renderPanel(data);
    } catch (err) {
      renderFatal("加载失败：" + err.message);
    }
    await modelPromise;
  }

  // ---------------------------------------------------------------- 3D

  function render3D(model) {
    const host = $("farm3d");
    if (!Farm3D.supported()) {
      Farm3D.renderFallback(host, model, state.today && state.today.growth);
      return;
    }
    // 画布被换掉过（比如先经过"还没有田块"那一屏）就重建，别往游离的 canvas 上画
    if (state.three && !host.querySelector("canvas")) {
      state.three.dispose();
      state.three = null;
    }
    if (!state.three) {
      host.innerHTML = `<canvas id="farm3d-canvas" aria-label="3D 田块视图"></canvas>`;
      const canvas = $("farm3d-canvas");
      state.three = Farm3D.create(canvas, {
        // 主视图的阴影图按机器给：桌面 2048 影子干净，低端/触屏退到 1024
        shadowMapSize: lowPowerDevice() ? 1024 : 2048,
        areaMu: () => (state.field ? state.field.area_mu : 1),
      });
      resize3D();
      // 行高现在由右侧面板的内容决定（面板写长/写短都会改高度），
      // window.resize 抓不到这种变化 —— 交给 ResizeObserver，rAF 去抖。
      if (window.ResizeObserver) {
        state.ro3d = new ResizeObserver(() => resize3DSoon());
        state.ro3d.observe(host);
      }
    }
    state.three.setModel(model);
  }

  function lowPowerDevice() {
    return !!(window.matchMedia &&
      window.matchMedia("(pointer: coarse)").matches) ||
      (navigator.hardwareConcurrency || 8) <= 4;
  }

  let resizePending = false;
  function resize3DSoon() {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => {
      resizePending = false;
      resize3D();
    });
  }

  function resize3D() {
    const host = $("farm3d");
    if (!host || !state.three || !host.querySelector("canvas")) return;
    const rect = host.getBoundingClientRect();
    // 停在 8 格那一屏时 .farm-layout 是 hidden，rect 是 0×0 —— 把画布设成 0 宽
    // 会让相机 aspect 变成 NaN，再进田块就是一片空白。小于 2px 一律当没量到。
    if (rect.width < 2 || rect.height < 2) return;
    state.three.setSize(rect.width, rect.height);
  }

  function renderModelOverlay(model) {
    $("model-overlay").innerHTML =
      `<div class="farm-overlay__row"><span>株高</span><strong>${model.height} cm</strong></div>` +
      `<div class="farm-overlay__row"><span>叶数</span><strong>${model.leafCount} 片</strong></div>` +
      `<div class="farm-overlay__row"><span>叶色</span><strong>` +
      `<i class="farm-swatch" style="background:${UI.escapeHtml(model.leafColor)}"></i>` +
      `${UI.escapeHtml(model.leafColorName)}</strong></div>` +
      `<div class="farm-overlay__row"><span>积温</span><strong>${Math.round(model.gdd_cum)} 度</strong></div>` +
      `<div class="farm-overlay__row"><span>阶段</span><strong>${UI.escapeHtml(model.stage)}</strong></div>` +
      (model.hasEar
        ? `<div class="farm-overlay__row"><span>穗</span><strong>` +
          `<i class="farm-swatch" style="background:${UI.escapeHtml(model.earColor)}"></i>` +
          `${model.matured ? "完熟" : "已抽穗"}</strong></div>`
        : "");
  }

  // ---------------------------------------------------------------- 信息面板

  function setPanelLoading() {
    $("panel").innerHTML =
      `<div class="card farm-card"><span class="spinner" role="status" aria-label="加载中"></span>` +
      `<span class="text-muted">正在读取这块田的数据…</span></div>`;
  }

  function renderPanel(data) {
    const g = data.growth || {};
    const w = data.weather || {};
    const h = data.harvest || {};

    $("field-head").innerHTML =
      `<strong>${UI.escapeHtml(data.field.name)}</strong>` +
      `<span class="text-muted">${UI.escapeHtml(data.field.crop)} · ${data.field.area_mu} 亩 · ` +
      `第 ${g.days_since_sow} 天 · ${UI.escapeHtml(g.stage)}</span>`;

    $("weather-overlay").innerHTML = renderWeatherBadge(w);

    $("panel").innerHTML = [
      renderWeatherCard(w),
      renderReminderCard(data.reminders || [], data.reminders_notes || []),
      renderHarvestCard(h, g),
      renderHarvestAdviceCard(data.harvest_advice, h),
      renderNarrativeCard(data),
      renderHistoryCard(data.logs || []),
    ].join("");

    bindPanelEvents();
  }

  function renderWeatherBadge(w) {
    const d = (w.days || [])[0];
    if (!d) {
      return `<div class="farm-overlay__row text-muted">天气数据不可用</div>`;
    }
    // 天气图标走 icons.js 的描边图标，不再用 emoji —— 那个文件头写着「刻意不使用 emoji
    // 当图标」（字形随系统变、不跟随主题色），这里是全站唯一一处破例，现已归队。
    // 用 Icon.has 兜底：万一图标没同步加上，也只是少一个图标，不会渲染出空白方块。
    const iconName = d.precip > 0.1 ? "cloudRain" : (d.tmax >= 32 ? "sun" : "cloudSun");
    const glyph = window.Icon && Icon.has(iconName) ? Icon.render(iconName, 20) : "";
    return (
      `<div class="farm-overlay__weather">${glyph}` +
      `<strong>${d.tmax}° / ${d.tmin}°</strong>` +
      `<span>${UI.escapeHtml((w.city || "") + (w.source === "normal" ? "（气候平均）" : ""))}</span>` +
      `</div>`
    );
  }

  function renderWeatherCard(w) {
    const days = w.days || [];
    const rows = days.map((d) => {
      const label = d.date.slice(5).replace("-", "/");
      const rain = d.precip > 0.1 ? `${d.precip}mm` : "—";
      return (
        `<div class="farm-weather__day">` +
        `<span>${label}</span>` +
        `<strong>${d.tmax}° / ${d.tmin}°</strong>` +
        `<span class="text-muted">雨 ${rain} · 湿 ${d.humidity}%</span>` +
        `</div>`
      );
    }).join("");

    const note = w.degraded
      ? `<div class="notice notice--warning farm-notice">${Icon.render("warning", 16)}` +
        `<span>${UI.escapeHtml(w.degraded_reason || w.source_note || "天气数据不可用")}</span></div>`
      : `<p class="text-sm text-muted farm-card__foot">${UI.escapeHtml(w.source_note || "")}</p>`;

    return (
      `<section class="card farm-card">` +
      `<div class="farm-card__title">${Icon.render("clock", 16)}天气</div>` +
      (rows || `<p class="text-muted">暂无天气数据</p>`) +
      note +
      `</section>`
    );
  }

  function renderReminderCard(reminders, notes) {
    if (!reminders.length) {
      return (
        `<section class="card farm-card">` +
        `<div class="farm-card__title">${Icon.render("check", 16)}今天该干啥</div>` +
        `<p class="text-muted">今天没有特别的农事提醒。</p>` +
        (notes.length
          ? `<p class="text-sm text-muted">${UI.escapeHtml(notes.join("；"))}</p>`
          : "") +
        `</section>`
      );
    }

    const items = reminders.map((r, i) => (
      `<div class="farm-reminder farm-reminder--${UI.escapeHtml(r.severity || "info")}">` +
      `<div class="farm-reminder__head">` +
      `<span class="badge ${r.priority === "高" ? "badge--danger" : "badge--info"}">${UI.escapeHtml(r.priority)}</span>` +
      `<strong>${UI.escapeHtml(r.action)}</strong></div>` +
      `<p class="farm-reminder__why">${UI.escapeHtml(r.reason || "")}</p>` +
      `<div class="farm-reminder__ops">` +
      `<button type="button" class="btn btn--sm btn--primary" data-done="${i}">干完了</button>` +
      `<button type="button" class="btn btn--sm btn--outline" data-snooze="${i}">改天</button>` +
      `</div></div>`
    )).join("");

    return (
      `<section class="card farm-card">` +
      `<div class="farm-card__title">${Icon.render("check", 16)}今天该干啥` +
      `<span class="badge">${reminders.length}</span></div>` +
      items +
      (notes.length
        ? `<p class="text-sm text-muted">${UI.escapeHtml(notes.join("；"))}</p>`
        : "") +
      `</section>`
    );
  }

  function renderHarvestCard(h, g) {
    let body;
    if (h.matured) {
      body =
        `<p class="farm-harvest__main">已完熟</p>` +
        `<p class="text-sm text-muted">${UI.escapeHtml(h.basis || "")}</p>`;
    } else if (!h.most_likely) {
      body = `<p class="text-muted">${UI.escapeHtml(h.basis || "暂时给不出收获日期")}</p>`;
    } else {
      body =
        `<p class="farm-harvest__main">${UI.escapeHtml(h.earliest)} ~ ${UI.escapeHtml(h.latest)}</p>` +
        `<p class="text-sm">最可能 <strong>${UI.escapeHtml(h.most_likely)}</strong> 前后 ` +
        `（±${h.window_days} 天）</p>`;
    }

    const method = h.method === "xgboost" ? "XGBoost 三分位" : "积温法";

    // 施肥影响：有记录才说，没有就明确说「未记录施肥，按缺素算」——
    // 沉默会让人以为「施不施肥都一样」，而这正是这一轮要修掉的问题。
    const shift = Number(h.maturity_shift || 0);
    const mgmtNote = h.management_note || "";
    const mgmtLine = mgmtNote
      ? `<p class="text-sm${shift ? "" : " text-muted"}">` +
        Icon.render("leaf", 14) + ` 施肥：${UI.escapeHtml(mgmtNote)}</p>`
      : "";
    const sources = (h.management_sources || []);
    const sourcesHtml = sources.length
      ? `<p class="text-sm text-muted">施肥影响的幅度依据：</p>` +
        `<ul class="farm-why__refs">` +
        sources.map((s) => `<li>${UI.escapeHtml(s)}</li>`).join("") +
        `</ul>`
      : "";

    return (
      `<section class="card farm-card">` +
      `<div class="farm-card__title">${Icon.render("sprout", 16)}收获预测` +
      `</div>` +
      body +
      (h.window_text ? `<p class="text-sm">推荐收割窗口：${UI.escapeHtml(h.window_text)}</p>` : "") +
      mgmtLine +
      `<details class="farm-why"><summary>为什么是这天 ▾</summary>` +
      `<p class="text-sm">${UI.escapeHtml(h.basis || "")}</p>` +
      `<p class="text-sm text-muted">方法：${method} · 全平台收获样本 ` +
      `${h.sample_count}/${h.sample_needed} 条` +
      (h.method === "gdd_fallback" ? "（样本够了会自动改用 XGBoost）" : "") +
      `</p>` +
      sourcesHtml +
      `</details>` +
      `</section>`
    );
  }

  function renderHarvestAdviceCard(advice, h) {
    if (!advice) return "";
    return (
      `<section class="card farm-card">` +
      `<div class="farm-card__title">${Icon.render("shield", 16)}收获建议</div>` +
      `<div class="farm-reminder farm-reminder--${UI.escapeHtml(advice.severity || "info")}">` +
      `<div class="farm-reminder__head">` +
      `<span class="badge ${advice.priority === "高" ? "badge--danger" : "badge--info"}">` +
      `${UI.escapeHtml(advice.priority)}</span>` +
      `<strong>${UI.escapeHtml(advice.action)}</strong></div>` +
      `<p class="farm-reminder__why">${UI.escapeHtml(advice.reason || "")}</p>` +
      `<div class="farm-reminder__ops">` +
      `<button type="button" class="btn btn--sm btn--primary" id="btn-harvested">我收了</button>` +
      `</div></div></section>`
    );
  }

  function renderNarrativeCard(data) {
    const text = data.narrative || "";
    const why = data.narrative_why || "";
    return (
      `<section class="card farm-card">` +
      `<div class="farm-card__title">${Icon.render("text", 16)}农技助手` +
      `<span class="badge">${data.narrative_source === "llm" ? "大模型" : "模板"}</span></div>` +
      `<p>${UI.escapeHtml(text)}</p>` +
      (why ? `<p class="text-sm text-muted">${UI.escapeHtml(why)}</p>` : "") +
      `</section>`
    );
  }

  function renderHistoryCard(logs) {
    const TYPE = {
      sow: "播种", fertilize: "施肥", spray: "打药", irrigate: "灌溉",
      harvest: "收获", diagnose: "诊断", other: "其他",
    };
    if (!logs.length) {
      return (
        `<section class="card farm-card">` +
        `<div class="farm-card__title">${Icon.render("clock", 16)}干过啥</div>` +
        `<p class="text-muted">还没有记录。点「记一笔」或按住麦克风说一句。</p>` +
        `<button type="button" class="btn btn--sm btn--outline" id="btn-add-log">+ 记一笔</button>` +
        `</section>`
      );
    }

    const rows = logs.map((l) => {
      const isDiagnosis = l.type === "diagnose";
      return (
        `<li class="farm-history__item${isDiagnosis ? " farm-history__item--alert" : ""}">` +
        `<span class="farm-history__date">${UI.escapeHtml(l.date.slice(5))}</span>` +
        `<span class="farm-history__type">${UI.escapeHtml(TYPE[l.type] || l.type)}</span>` +
        `<span class="farm-history__text">${UI.escapeHtml(l.item || l.note || "")}` +
        (l.amount ? ` <b>${UI.escapeHtml(l.amount)}</b>` : "") +
        `</span>` +
        (isDiagnosis ? `<span class="badge badge--danger">看病</span>` : "") +
        `</li>`
      );
    }).join("");

    return (
      `<section class="card farm-card">` +
      `<div class="farm-card__title">${Icon.render("clock", 16)}干过啥` +
      `<span class="badge">${logs.length}</span></div>` +
      `<ul class="farm-history">${rows}</ul>` +
      `<button type="button" class="btn btn--sm btn--outline" id="btn-add-log">+ 记一笔</button>` +
      `</section>`
    );
  }

  // ---------------------------------------------------------------- 面板交互

  function bindPanelEvents() {
    const data = state.today;
    if (!data) return;

    document.querySelectorAll("[data-done]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const r = (data.reminders || [])[Number(btn.dataset.done)];
        if (r) markDone(r, btn);
      });
    });

    document.querySelectorAll("[data-snooze]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const r = (data.reminders || [])[Number(btn.dataset.snooze)];
        if (r) snooze(r, btn);
      });
    });

    const harvested = $("btn-harvested");
    if (harvested) harvested.addEventListener("click", () => markHarvested(harvested));

    const addLog = $("btn-add-log");
    if (addLog) addLog.addEventListener("click", () => openVoiceModal());
  }

  /** [干完了] → 写一条农事记录（类型取自提醒本身）。 */
  async function markDone(reminder, btn) {
    const restore = UI.busy(btn, "记账中");
    try {
      await Api.createLog({
        field_id: state.field.id,
        type: reminder.type || "other",
        date: todayISO(),
        item: reminder.action || "",
        note: reminder.reason || "",
        source: "manual",
      });
      UI.toast("已记为完成");
      await selectField(state.field.id);
    } catch (err) {
      restore();
      UI.toast("记录失败：" + err.message, "error");
    }
  }

  async function snooze(reminder, btn) {
    const restore = UI.busy(btn, "");
    try {
      const res = await Api.snoozeReminder(state.field.id, reminder.key, 2);
      UI.toast(`已「改天」，${res.snooze_until} 之前不再提醒`);
      await selectField(state.field.id);
    } catch (err) {
      restore();
      UI.toast("操作失败：" + err.message, "error");
    }
  }

  async function markHarvested(btn) {
    if (!window.confirm("确认已经收获了吗？这会记入收获记录，并作为以后训练的样本。")) return;
    const restore = UI.busy(btn, "记录中");
    try {
      const res = await Api.harvestMark(state.field.id, todayISO(), "");
      UI.toast(`已记录：生长期 ${res.actual_days} 天。${res.note || ""}`);
      await selectField(state.field.id);
    } catch (err) {
      restore();
      UI.toast("记录失败：" + err.message, "error");
    }
  }

  function todayISO() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  // ---------------------------------------------------------------- 语音录入（7.8）

  function openVoiceModal(presetText) {
    state.voice = null;
    $("voice-text").value = presetText || "";
    $("voice-fields").hidden = true;
    $("voice-reason").textContent = "";
    $("voice-result").hidden = true;
    $("voice-modal").hidden = false;
    $("modal-voice-close").focus();
  }

  function closeVoiceModal() {
    if (Recorder.isRecording()) Recorder.cancel();
    stopTick();
    $("voice-modal").hidden = true;
  }

  function stopTick() {
    if (state.recOffTick) {
      state.recOffTick();
      state.recOffTick = null;
    }
  }

  async function toggleVoiceRecording() {
    const btn = $("voice-mic");

    if (Recorder.isRecording()) {
      const hit = await Recorder.stop();
      stopTick();
      $("voice-status").textContent = "";
      btn.setAttribute("aria-pressed", "false");
      if (!hit) {
        UI.toast("没有录到声音，请检查麦克风后重试", "error");
        return;
      }
      await recognize(hit.blob);
      return;
    }

    if (!Recorder.isSupported()) {
      UI.toast("当前浏览器不支持录音，请直接打字输入", "error");
      return;
    }

    try {
      await Recorder.start();
    } catch (e) {
      UI.toast(recordError(e), "error");
      return;
    }

    btn.setAttribute("aria-pressed", "true");
    $("voice-status").textContent = "正在录音 00:00";
    state.recOffTick = Recorder.on("tick", (ms) => {
      $("voice-status").textContent = "正在录音 " + Recorder.formatDuration(ms);
    });
  }

  function recordError(e) {
    const name = e && e.name;
    if (name === "unsupported") return "当前浏览器不支持录音，请直接打字输入";
    if (name === "insecure") return "录音需要 https 或 127.0.0.1 访问本页，请改用 127.0.0.1";
    if (name === "denied") return "无法访问麦克风：" + e.message + "（请检查浏览器权限）";
    return "录音启动失败：" + ((e && e.message) || "未知原因");
  }

  /** ② 识别 → 文字。未配语音 Key 时后端返回 200 + mock=true + 空文本，不是错误。 */
  async function recognize(blob) {
    const form = new FormData();
    form.append("audio", blob, "record.wav");
    form.append("dialect", window.Dialect ? Dialect.current() : "");

    $("voice-status").textContent = "正在识别…";
    const restore = UI.busy($("voice-reparse"), "");
    try {
      const data = await Api.asr(form);
      restore();
      $("voice-status").textContent = "";
      const text = ((data && data.text) || "").trim();
      if (!text) {
        $("voice-status").textContent = "";
        UI.toast(data && data.mock
          ? (data.message || "未配置语音服务，请直接打字输入")
          : "没有识别到内容，请再说一遍，或直接打字", "warning", 7000);
        return;
      }
      $("voice-text").value = text;
      await parseText(text);
    } catch (err) {
      restore();
      $("voice-status").textContent = "";
      UI.toast("语音识别失败：" + err.message, "error");
    }
  }

  /** ③ 解析成结构化字段（规则优先，LLM 兜底）。改文字后重新走这一步（④ 双向联动）。 */
  async function parseText(text, { reparse = false } = {}) {
    if (!text.trim()) {
      $("voice-reason").textContent = "请先说一句、或打一句农事记录";
      $("voice-fields").hidden = true;
      return;
    }
    const restore = UI.busy($("voice-reparse"), "解析中");
    try {
      const data = reparse
        ? await Api.voiceReparse(text, state.field.id)
        : await Api.voiceParse(text, state.field.id);

      state.voice = { text, parsed: data };
      renderParsed(data);
    } catch (err) {
      UI.toast("解析失败：" + err.message, "error");
    } finally {
      restore();
    }
  }

  function renderParsed(data) {
    const f = data.fields || {};
    $("voice-fields").hidden = false;

    // 田块：解析不出来就用当前田
    const fieldSelect = $("voice-field");
    fieldSelect.innerHTML = state.fields
      .map((x) => `<option value="${UI.escapeHtml(x.id)}">${UI.escapeHtml(x.name)}</option>`)
      .join("");
    fieldSelect.value = f.field_id || state.field.id;

    const typeSelect = $("voice-type");
    const types = [["sow", "播种"], ["fertilize", "施肥"], ["spray", "打药"],
                   ["irrigate", "灌溉"], ["harvest", "收获"], ["other", "其他"]];
    typeSelect.innerHTML = types
      .map(([v, n]) => `<option value="${v}">${n}</option>`).join("");
    typeSelect.value = f.type || "other";

    $("voice-date").value = f.date || todayISO();
    $("voice-item").value = f.item || "";
    $("voice-amount").value = f.amount || "";

    // 解析不完整时把原因说清楚，并且**不给"确认落库"这个错觉** ——
    // 按钮仍然可点（用户可以自己把字段补齐），但提示必须显眼
    $("voice-reason").textContent = data.ok ? "" : (data.reason || "没能完全听懂，请核对下面的字段");
    $("voice-result").hidden = false;

    // ④ 改文字 → 重新解析（双向联动）
    $("voice-reparse").onclick = () => parseText($("voice-text").value, { reparse: true });
  }

  /** ⑤ 确认落库。这一步之前数据库里什么都没有。 */
  async function confirmVoice() {
    const btn = $("voice-confirm");
    const restore = UI.busy(btn, "保存中");
    try {
      await Api.createLog({
        field_id: $("voice-field").value,
        type: $("voice-type").value,
        date: $("voice-date").value || todayISO(),
        item: $("voice-item").value.trim(),
        amount: $("voice-amount").value.trim(),
        // 原文存进 note：以后回头看时能知道这条记录是怎么来的
        note: (state.voice && state.voice.text) ? "语音：" + state.voice.text : "",
        source: state.voice ? "voice" : "manual",
      });
      UI.toast("已记入农事记录");
      closeVoiceModal();
      await selectField(state.field.id);
    } catch (err) {
      restore();
      UI.toast("保存失败：" + err.message, "error");
    }
  }

  /** 诊断页跳过来的「记入我的农田」：?label=<病害名> 直接用当前田落档。 */
  async function attachFromQuery() {
    const label = UI.query("label");
    // 只在"已经进到某块田里"时才问记档 —— 停在 8 格那一屏没有"当前田"
    if (!label || !state.entered || !state.field) return;
    const ok = window.confirm(
      `把这次诊断（${label}）记入「${state.field.name}」吗？\n\n` +
      "记入后它会出现在这块田的历史里，并参与以后的农事提醒。");
    if (!ok) return;
    try {
      await Api.attachDiagnosis({ field_id: state.field.id, date: todayISO(), label });
      UI.toast("已记入农田历史");
      await selectField(state.field.id);
    } catch (err) {
      UI.toast("记入失败：" + err.message, "error");
    }
  }

  // ---------------------------------------------------------------- 事件绑定

  function bindStaticEvents() {
    // 图标在 JS 里填（与其余页面一致），HTML 里只留空的占位节点
    if (window.Icon) {
      const mic = $("ico-farm-mic");
      if (mic) mic.innerHTML = Icon.render("mic", 20);
      const vmic = $("ico-voice-mic");
      if (vmic) vmic.innerHTML = Icon.render("mic", 24);
      const close = $("modal-voice-close");
      if (close) close.innerHTML = Icon.render("close", 20);
    }

    // 这个下拉是隐藏的（视觉上是 8 格），但仍是"当前田块"的唯一载体：
    // ?field= 深链、键盘、脚本都走它。换它就等于换田，所以要和点格子一样进那一块。
    $("field-select").addEventListener("change", (e) => {
      enterField(e.target.value);
    });

    // 「← 返回田块」：回 8 格那一屏，并把 URL 上的 ?field= 去掉
    $("farm-back").addEventListener("click", () => {
      renderIdle();
      // 在田里记过账、干完了、收了都会改长势，小图要跟着更新（原地换图，不闪）
      renderFieldThumbs();
      $("btn-new-field").focus();   // 键盘/读屏用户不会突然"什么都没发生"
    });

    // 3D 出血量与尺寸的重算（窗口变化 + 面板长高都会触发）
    bindFarmBleed();

    // 选址三级联动：改省 → 市/县重填；改市 → 县重填。每级改完都重算经纬度。
    $("new-field-province").addEventListener("change", (e) => {
      setLocation(e.target.value, "", "");
    });
    $("new-field-city").addEventListener("change", (e) => {
      setLocation($("new-field-province").value, e.target.value, "");
    });
    $("new-field-county").addEventListener("change", applyLatLng);

    // 新建田地（工具栏按钮与空格子都走 openNewForm）
    $("btn-new-field").addEventListener("click", () => {
      const form = $("new-field-form");
      if (form.hidden) openNewForm();
      else form.hidden = true;
    });

    $("new-field-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("new-field-submit");
      const restore = UI.busy(btn, "创建中");
      try {
        const field = await Api.createField({
          name: $("new-field-name").value.trim(),
          crop: $("new-field-crop").value,
          area_mu: Number($("new-field-area").value) || 0,
          province: $("new-field-province").value.trim(),
          city: $("new-field-city").value.trim(),
          county: $("new-field-county").value.trim(),
          lng: Number($("new-field-lng").value) || null,
          lat: Number($("new-field-lat").value) || null,
          sow_date: $("new-field-sow").value,
        });
        restore();
        $("new-field-form").reset();
        $("new-field-form").hidden = true;
        UI.toast("田块已创建");
        await loadFields();
        // 刚建好就进去看一眼（新建的田通常在列表最后，让用户自己再去 8 格里找反而绕）
        await enterField(field.id);
      } catch (err) {
        restore();
        UI.toast("创建失败：" + err.message, "error");
      }
    });

    // 麦克风（7.8）
    $("farm-mic").addEventListener("click", () => openVoiceModal());
    $("voice-mic").addEventListener("click", toggleVoiceRecording);
    $("voice-confirm").addEventListener("click", confirmVoice);
    $("voice-cancel").addEventListener("click", closeVoiceModal);
    $("modal-voice-close").addEventListener("click", closeVoiceModal);
    $("voice-modal-backdrop").addEventListener("click", closeVoiceModal);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("voice-modal").hidden) closeVoiceModal();
    });

    // 「扫一下」：跳到诊断页，带着回来记档的意图。
    // 停在 8 格这一屏时还没有"当前田"，先让用户点一块 —— 否则链接里的 field 是空的。
    $("farm-scan").addEventListener("click", () => {
      if (!state.field) {
        UI.toast("先点一块田，再扫病虫害", "warning");
        return;
      }
      location.href = "/diagnose.html?from=farm&field=" + encodeURIComponent(state.field.id);
    });

    window.addEventListener("beforeunload", () => {
      if (window.Recorder && Recorder.isRecording()) Recorder.cancel();
    });
  }

  function renderEmpty() {
    state.entered = false;
    state.field = null;
    // 一句话独占一行：8 格是空的、大 3D 也没什么可画，就让这段文字占满整行
    setLayoutMode("solo");
    $("panel").innerHTML =
      `<section class="card farm-card">` +
      `<div class="farm-card__title">${Icon.render("sprout", 16)}还没有田块</div>` +
      `<p class="text-muted">先新建一块田：填上作物、亩数与播种日，系统就会按积温推算它的生长阶段。</p>` +
      `<button type="button" class="btn btn--primary" id="btn-new-field-empty">新建田块</button>` +
      `</section>`;
    const btn = $("btn-new-field-empty");
    if (btn) btn.addEventListener("click", openNewForm);
  }

  function renderFatal(message) {
    // 同上：消息在右侧面板里，得先把面板露出来，否则报错页上什么都看不到
    setLayoutMode("solo");
    $("panel").innerHTML =
      `<section class="card farm-card">` +
      `<div class="notice notice--danger">${Icon.render("alert", 18)}` +
      `<span>${UI.escapeHtml(message)}</span></div>` +
      `<button type="button" class="btn btn--outline" onclick="location.reload()">重试</button>` +
      `</section>`;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
