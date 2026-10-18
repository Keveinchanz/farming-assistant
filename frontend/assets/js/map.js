/* ==========================================================================
   任务 5.2 —— 首页：中国山川河流 3D 沙盘

   本文件管**逻辑**：数据、季节、卡片、图例、遮罩、深浅色。
   「把地图画出来」这一层在 map3d.js（three.js）里 —— ECharts 的 map series
   画的是平面填充图，没有地面和起伏，怎么调色都是贴纸；换成有厚度的地块、
   真实高程的地形和贴地的江河之后，才算得上「沙盘」。

   · 悬停省份：卡片预览该省当季高发病虫害 TOP3
   · 点击省份：卡片**停住**（不直接跳转），在卡片里选定一种病害才跳走
   · 左上角春/夏/秋/冬切换：数据、省份底色与按钮强调色同步更新

   卡片为什么是自己写的一块 DOM 而不是渲染引擎带的浮层：浮层容器不归我们管，
   盒模型得靠 !important 去抢，宽度只有 max-width 没有固定 width，行还不可换行 ——
   长病害名会被顶出卡片，TOP3 也不可能是真正可点的按钮。
   .map-card（见 index.html）自己做，宽度、换行、交互三样都归自己管。

   数据来源分两层：
   · 省界与各省数据在**本地预加载快照**里（assets/data/home_preload.js，由
     src/content/frontend_bundle.py 生成，index.html 在本文件之前同步加载），
     所以首屏不发接口请求，后端挂了也显示得出来；
   · 地形高程与水系是两份静态 JSON（assets/geo/china_relief.json /
     china_water.json，scripts/28_build_china_geo.py 生成），
     由 map3d.js 的 loadAssets() 取回来 —— 是本机的静态文件，不走接口。
   快照缺失时才退回「fetch china.json + /api/map/pest_top3」的老路径。

   关于失败处理：加载遮罩只有在整条链路全部成功时才会被隐藏，
   中途任何一环卡住或抛错都会停在一个转不完的圈上。所以所有失败路径
   都收敛到 showOverlayError()，一定会给出可读的原因和一个「重试」按钮。
   ========================================================================== */

(function () {
  "use strict";

  const SEASON_LABEL = { spring: "春", summer: "夏", autumn: "秋", winter: "冬" };

  /* 默认季节按真实日期算，不再写死 spring —— 不然十月份打开首页，看到的还是春季数据。
     分档按气象季节：春 3–5 / 夏 6–8 / 秋 9–11 / 冬 12–2。
     函数声明会提升，所以上面的 let season 可以先用它。 */
  function seasonFromDate(d) {
    const m = (d || new Date()).getMonth() + 1;   // getMonth() 从 0 起，+1 才是月份
    if (m >= 3 && m <= 5) return "spring";
    if (m >= 6 && m <= 8) return "summer";
    if (m >= 9 && m <= 11) return "autumn";
    return "winter";
  }

  /* 分级上色：按「病害种类规模」分档。
     之所以不用风险等级：每省 TOP1 都是「高」，全国会同一个颜色，地图就失去信息量。 */
  const PIECES = [
    { min: 0, max: 29, label: "较少（<30 种）", risk: 1, color: "#d7ede0" },
    { min: 30, max: 44, label: "偏少（30–44 种）", risk: 2, color: "#a8d8b9" },
    { min: 45, max: 59, label: "中等（45–59 种）", risk: 3, color: "#6cbe8b" },
    { min: 60, max: 74, label: "较多（60–74 种）", risk: 4, color: "#2f8f52" },
    { min: 75, max: 999, label: "很多（≥75 种）", risk: 5, color: "#17663a" },
  ];

  /* 四季主题：改的是非语义配色 —— 底色、省界、省份名、无数据省的颜色，
     好让四季一眼可辨。四季的风险色阶**改色相、不改明度顺序**：
     1 档永远最浅、5 档永远最深（深色模式反过来，1 最暗、5 最亮），
     深浅编码的仍然是「病害种类多少」，色相只回答「这是哪一季」——
     图例（renderLegend）和地图读的是同一份令牌，所以两边不可能对不上。
     不跟着季节变的只剩 emphasis 高亮 —— 那是「可交互」的信号，不是季节的信号。

     这里的十六进制值是**浅色模式的兜底**。实际取值走 CSS 令牌（见下面 theme()），
     因为 ECharts 画的是 canvas，CSS 的 @media 根本管不到它 ——
     深色模式下舞台变暗而省份还是浅粉绿，就是这么来的。 */
  const SEASON_THEME = {
    spring: { areaColor: "#eaf5ec", borderColor: "#ffffff", labelColor: "#3d5a49", noData: "#e4f1e7" },
    summer: { areaColor: "#e7f4ea", borderColor: "#ffffff", labelColor: "#37543f", noData: "#dcefdf" },
    autumn: { areaColor: "#f7f1e2", borderColor: "#fffdf6", labelColor: "#5a4a2c", noData: "#f0e6cd" },
    winter: { areaColor: "#e9eff7", borderColor: "#ffffff", labelColor: "#3a4a5e", noData: "#dfe8f3" },
  };

  /* 季节 → base.css 里那几个令牌的名字。深浅两套配色在 CSS 里各给一份，
     这边只负责把「读哪个令牌」这件事写清楚，不再各存一套色值。 */
  const SEASON_VARS = {
    spring: { areaColor: "--map-spring-area", borderColor: "--map-spring-border",
              labelColor: "--map-spring-label", noData: "--map-spring-nodata",
              risk1: "--map-spring-risk-1", risk2: "--map-spring-risk-2", risk3: "--map-spring-risk-3",
              risk4: "--map-spring-risk-4", risk5: "--map-spring-risk-5" },
    summer: { areaColor: "--map-summer-area", borderColor: "--map-summer-border",
              labelColor: "--map-summer-label", noData: "--map-summer-nodata",
              risk1: "--map-summer-risk-1", risk2: "--map-summer-risk-2", risk3: "--map-summer-risk-3",
              risk4: "--map-summer-risk-4", risk5: "--map-summer-risk-5" },
    autumn: { areaColor: "--map-autumn-area", borderColor: "--map-autumn-border",
              labelColor: "--map-autumn-label", noData: "--map-autumn-nodata",
              risk1: "--map-autumn-risk-1", risk2: "--map-autumn-risk-2", risk3: "--map-autumn-risk-3",
              risk4: "--map-autumn-risk-4", risk5: "--map-autumn-risk-5" },
    winter: { areaColor: "--map-winter-area", borderColor: "--map-winter-border",
              labelColor: "--map-winter-label", noData: "--map-winter-nodata",
              risk1: "--map-winter-risk-1", risk2: "--map-winter-risk-2", risk3: "--map-winter-risk-3",
              risk4: "--map-winter-risk-4", risk5: "--map-winter-risk-5" },
  };

  /** 读一个 CSS 令牌。读不到就退回调用方给的浅色常量 ——
      样式表没加载或令牌名写错时，地图顶多是不变深，不会变成一片空白。 */
  function cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name);
      return (v && v.trim()) || fallback;
    } catch (e) {
      return fallback;
    }
  }

  let map = null;                     // Map3D 的句柄（3D 沙盘）
  let mapBound = false;               // 沙盘的事件只挂一次（重试时 init 会被再调一次）
  let season = seasonFromDate();
  let source = null;                  // {kind:"snapshot"|"api", seasons, provinces}
  let provinces = [];                 // 当前季节的省份数据（已按季节摊平）
  let byName = new Map();

  let cardName = null;                // 卡片当前展示的省份名
  let cardPinned = false;             // true = 停住态（可交互），false = 悬停预览
  let cardSize = null;                // 卡片尺寸缓存：内容没变就别每帧重新量（会强制重排）
  let cardAnchor = null;              // 停住时的屏幕锚点：内容变高变矮后要按它重新摆位

  /** 当前季节的配色：优先读 CSS 令牌（深浅两套都在 CSS 里），读不到退回浅色常量。 */
  function theme() {
    const base = SEASON_THEME[season] || SEASON_THEME.spring;
    const vars = SEASON_VARS[season] || SEASON_VARS.spring;
    return {
      areaColor: cssVar(vars.areaColor, base.areaColor),
      borderColor: cssVar(vars.borderColor, base.borderColor),
      labelColor: cssVar(vars.labelColor, base.labelColor),
      noData: cssVar(vars.noData, base.noData),
    };
  }

  /* 风险档位 → 颜色，三层取值：当季令牌 → 通用令牌 → 写死的浅色常量。
     三层不是保险起见，是各有用途：当季令牌让色相跟着季节走，通用令牌兜住
     「某一季漏定义了一档」这种笔误，十六进制常量兜住样式表整体没加载。 */
  function pieceColor(p) {
    const vars = SEASON_VARS[season] || SEASON_VARS.spring;
    return cssVar(vars["risk" + p.risk], cssVar("--map-risk-" + p.risk, p.color));
  }

  /* ---------------------------------------------------------------- 快照 */

  function localGeo() {
    return window.__CHINA_GEO__ || null;
  }

  function localSnapshot() {
    return window.__PROVINCE_PEST__ || null;
  }

  function errorText(err) {
    if (!err) return "未知错误";
    return err.message ? err.message : String(err);
  }

  /* ---------------------------------------------------------------- 遮罩 */

  function overlayEl() {
    return document.getElementById("map-overlay");
  }

  function showOverlayLoading(text) {
    const el = overlayEl();
    if (!el) return;
    el.hidden = false;
    el.innerHTML =
      `<div><span class="spinner" role="status" aria-label="地图数据加载中"></span>` +
      `<p class="mt-0" style="margin-top:12px">${UI.escapeHtml(text || "正在加载各省病虫害数据…")}</p></div>`;
  }

  function hideOverlay() {
    const el = overlayEl();
    if (el) el.hidden = true;
  }

  /** 唯一的失败出口：给出原因 + 重试按钮，绝不留下一个转不完的圈。 */
  function showOverlayError(message, hintHtml) {
    const el = overlayEl();
    if (!el) return;
    el.hidden = false;
    el.innerHTML =
      `<div class="notice notice--danger map-overlay__box">` +
      (window.Icon ? Icon.render("alert", 20) : "") +
      `<span>${UI.escapeHtml(message)}${hintHtml ? "<br>" + hintHtml : ""}</span>` +
      `<button type="button" class="btn btn--outline btn--sm" id="map-retry">重试</button>` +
      `</div>`;
    const btn = document.getElementById("map-retry");
    if (btn) btn.addEventListener("click", () => { source = null; init(); });
  }

  /* ------------------------------------------------------------ 3D 沙盘 */

  /** 沙盘句柄。没挂上就抛错 —— 调用方（init）会把它接到可重试的错误出口上。 */
  function ensureMap() {
    if (!map) throw new Error("3D 沙盘未能启动");
    return map;
  }

  /** 省名 → 当季风险色。档位由 threat_scope 落进 PIECES，颜色一律经 pieceColor
      读 CSS 令牌 —— 四季与深浅色因此都只是「换一份令牌」，这里不存任何色值。
      沙盘里没有对应数据的省（如南海诸岛）由 map3d 自己用 noData 上色。 */
  function colorMap() {
    const out = {};
    provinces.forEach((p) => {
      const piece = PIECES.find((x) => p.threat_scope >= x.min && p.threat_scope <= x.max);
      if (piece) out[p.name] = pieceColor(piece);
    });
    return out;
  }

  /* ------------------------------------------------------------ 省份卡片 */

  function cardEl() {
    return document.getElementById("map-card");
  }

  function stageEl() {
    return document.querySelector(".map-stage");
  }

  /* 把当前季节写到所有 [data-season-host] 元素上 —— 舞台底色和季节分带都靠它选样式。
     一处写、多处读：以后再加一块随季节变色的区域，只要在 HTML 上挂个标记，不会漏。 */
  function syncSeasonAttr() {
    document.querySelectorAll("[data-season-host]").forEach((el) => {
      el.dataset.season = season;
    });
  }

  /** 沙盘给的是相对 canvas 的像素；卡片挂在 .map-stage 上。
      两者原点一般重合（#china-map 铺满舞台），仍走 rect 求差，
      以后给舞台加内边距也不会算错。 */
  function canvasOffset() {
    const stage = stageEl();
    const dom = map && map.canvas;
    if (!stage || !dom) return { x: 0, y: 0 };
    const sr = stage.getBoundingClientRect();
    const dr = dom.getBoundingClientRect();
    return { x: dr.left - sr.left, y: dr.top - sr.top };
  }

  function toStage(ev) {
    const off = canvasOffset();
    return { x: ev.x + off.x, y: ev.y + off.y };
  }

  /** 转动 / 缩放 / 改窗口之后，把停住的卡片贴回它那个省 ——
      否则一转头它就会浮在别的省上头。锚点取该省的几何中心在屏幕上的投影。 */
  function reanchor() {
    if (!cardPinned || !cardName || !map) return;
    const pt = map.anchor(cardName);
    if (!pt) return;
    const off = canvasOffset();
    cardAnchor = { x: pt.x + off.x, y: pt.y + off.y };
    placeCard(cardAnchor.x, cardAnchor.y, cardSize);
  }

  function measureCard() {
    const el = cardEl();
    return el ? { w: el.offsetWidth, h: el.offsetHeight } : { w: 0, h: 0 };
  }

  /** 把卡片摆到 (x, y) 旁边，并保证整张卡都在舞台内：
      右边放不下就翻到左边，下边放不下就翻到上边，最后再钳一次。
      size 传入已量好的尺寸，免得跟随光标时每帧都读 offsetWidth（强制重排）。 */
  function placeCard(x, y, size) {
    const el = cardEl();
    const stage = stageEl();
    if (!el || !stage) return;
    const pad = 16;
    const gap = 16;
    const sw = stage.clientWidth;
    const sh = stage.clientHeight;
    const box = size || measureCard();
    const cw = box.w;
    const ch = box.h;

    let left = x + gap;
    if (left + cw > sw - pad) left = Math.max(pad, x - gap - cw);
    left = Math.min(Math.max(pad, left), Math.max(pad, sw - cw - pad));

    let top = y + gap;
    if (top + ch > sh - pad) top = Math.max(pad, y - gap - ch);
    top = Math.min(Math.max(pad, top), Math.max(pad, sh - ch - pad));

    el.style.left = left + "px";
    el.style.top = top + "px";
  }

  /** 卡片内容。停住态下 TOP3 每一行都是可点的按钮 —— 选定哪一种病害就跳哪一种，
      底部另给一个直接跳首要病害的主按钮。 */
  function renderCard(p) {
    const el = cardEl();
    if (!el) return;

    const closeBtn =
      `<button type="button" class="map-card__close" data-card-close aria-label="关闭卡片">` +
      (window.Icon ? Icon.render("close", 16) : "×") +
      `</button>`;

    if (!p) {
      el.innerHTML =
        `<div class="map-card__head"><span class="map-card__name">该地区</span>${closeBtn}</div>` +
        `<div class="map-card__empty">暂无该地区的数据</div>`;
      bindCardEvents();
      return;
    }

    const items = (p.top3 || [])
      .map((t) => {
        const riskClass = t.risk === "高" ? "badge--danger" : "badge--warning";
        return (
          `<li class="map-card__item">` +
          `<button type="button" class="map-card__pick" data-open="${UI.escapeHtml(t.library_id || "")}">` +
          `<span class="map-card__row">` +
          `<span class="map-card__rank">${UI.escapeHtml(String(t.rank))}</span>` +
          `<span class="map-card__pest">${UI.escapeHtml(t.pest)}</span>` +
          `<span class="badge ${riskClass}">风险${UI.escapeHtml(t.risk)}</span>` +
          `<span class="map-card__go">查看防治</span>` +
          `</span>` +
          `<span class="map-card__meta">${UI.escapeHtml(t.crop)}，高发时段 ${UI.escapeHtml(t.peak_window)}。${UI.escapeHtml(t.note)}</span>` +
          `</button>` +
          `</li>`
        );
      })
      .join("");

    const top1 = (p.top3 || [])[0];
    el.innerHTML =
      `<div class="map-card__head">` +
      `<span class="map-card__name">${UI.escapeHtml(p.short_name || p.name)}</span>` +
      `<span class="map-card__season">${SEASON_LABEL[season]}季，共 ${UI.escapeHtml(String(p.threat_scope))} 种病害</span>` +
      closeBtn +
      `</div>` +
      `<div class="map-card__crops">主要作物：${UI.escapeHtml((p.crops || []).join("、"))}</div>` +
      (items
        ? `<ul class="map-card__list">${items}</ul>`
        : `<div class="map-card__empty">该季节暂无可跳转的病害数据</div>`) +
      (top1
        ? `<button type="button" class="btn btn--primary btn--sm map-card__primary" ` +
          `data-open="${UI.escapeHtml(top1.library_id || "")}">查看首要病害防治详情</button>`
        : "") +
      `<div class="map-card__hint">` +
      (cardPinned
        ? "点击任意一种病害，进入知识图书库查看防治详情"
        : "点击该省停住卡片，再选一种病害跳转") +
      `</div>`;

    bindCardEvents();
  }

  function bindCardEvents() {
    const el = cardEl();
    if (!el) return;
    el.querySelectorAll("[data-open]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (!cardPinned) return;              // 预览态卡片是穿透的，点了也不算数
        openLibrary(btn.dataset.open);
      });
    });
    const close = el.querySelector("[data-card-close]");
    if (close) {
      close.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeCard();
      });
    }
  }

  /** 页面上唯一受理「选定病害」的地方。省份的点击不再直接跳转，都要先经过这里。 */
  function openLibrary(libraryId) {
    if (!libraryId) {
      UI.toast("该病害没有对应的防治详情", "warning");
      return;
    }
    location.href = "/library.html?open=" + encodeURIComponent(libraryId);
  }

  /** 弹出卡片。pinned=false 是悬停预览（跟随光标），true 是点击后停住。 */
  function showCard(p, x, y, pinned) {
    const el = cardEl();
    if (!el) return;
    cardPinned = !!pinned;
    el.dataset.pinned = cardPinned ? "true" : "false";
    // 预览态做成提示语义，停住态才是真正的对话区
    if (cardPinned) {
      el.setAttribute("role", "dialog");
    } else {
      el.removeAttribute("role");
    }
    el.hidden = false;                        // 必须先可见，下面才量得到真实尺寸
    renderCard(p);
    cardSize = measureCard();
    cardAnchor = { x, y };
    placeCard(x, y, cardSize);
  }

  function closeCard() {
    const el = cardEl();
    cardPinned = false;
    cardName = null;
    cardSize = null;
    cardAnchor = null;
    // 卡片收了，地块上的高亮也要跟着收，不然会独独亮着一个省
    if (map && typeof map.setHover === "function") map.setHover(null);
    if (!el) return;
    el.hidden = true;
    el.dataset.pinned = "false";
    el.removeAttribute("role");
    el.innerHTML = "";
  }

  /** 季节切换后，停住的卡片要换成新季节的数据，而不是继续显示上一季的。 */
  function refreshPinnedCard() {
    if (!cardPinned || !cardName) return;
    const p = byName.get(cardName);
    if (!p) {
      closeCard();
      return;
    }
    renderCard(p);
    cardSize = measureCard();                 // 换季后行数与文字长度都可能变，尺寸要重新量
    // 尺寸变了就得按原锚点重新摆一次：换季可能让卡片变高，不重摆就会顶出舞台下沿
    if (cardAnchor) placeCard(cardAnchor.x, cardAnchor.y, cardSize);
  }

  /* ------------------------------------------------------------ 季节主题 */

  /** 舞台加 data-season（驱动季节按钮的强调色），沙盘的非语义配色也跟着季节走：
      底板、省界描边、省名标签、水的明暗。风险色阶不在这里 —— 那是"数据"那一层，
      跟着 applyData 走。 */
  function applySeasonTheme() {
    syncSeasonAttr();
    ensureMap().setTheme(theme());
  }

  /* ---------------------------------------------------------------- 渲染 */

  function renderLegend() {
    const host = document.getElementById("legend-items");
    if (!host) return;
    host.innerHTML = PIECES.filter((p) => p.max !== 999)
      .map(
        (p) =>
          `<div class="map-legend__item">` +
          `<span class="map-legend__swatch" style="background:${pieceColor(p)}"></span>` +
          `<span>${p.label}</span></div>`
      )
      .join("");

    const note = document.getElementById("legend-note");
    if (!note) return;
    note.textContent =
      "颜色深浅表示该省主要作物涉及的病害种类规模，全国范围 21～74 种。";
  }

  function syncSeasonButtons() {
    document.querySelectorAll("#season-switch [data-season]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.season === season))
    );
  }

  function renderSeasonSwitch(meta) {
    const host = document.getElementById("season-switch");
    if (!host) return;
    // 只在季节清单变化时重建 DOM，否则每次切换都重建会把按钮焦点抖掉
    const sig = (meta || []).map((s) => s.key).join(",");
    if (host.dataset.sig !== sig) {
      host.dataset.sig = sig;
      host.innerHTML = (meta || [])
        .map(
          (s) =>
            `<button type="button" class="season-switch__btn" data-season="${UI.escapeHtml(s.key)}" ` +
            `aria-pressed="${s.key === season}">${UI.escapeHtml(s.name)}</button>`
        )
        .join("");

      host.querySelectorAll("[data-season]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const next = btn.dataset.season;
          if (next === season) return;
          loadSeason(next);
        });
      });
    }
    syncSeasonButtons();
  }

  /* 季节分带：一句当季判断，从本地快照就地算 —— 不加接口、不新造数字。

     为什么主语不是「当季最常见的病害」：演示快照里春/夏/秋 三季的病害名集合
     是完全一样的（季节性只做在了冬季），以它为主语的话切三季都是一个样，
     正好落进「看着像没变化」那个坑。所以挂到真正随季节变的字段上：
     发病时段（peak_window，四季各不相同）和病原类型构成（pathogen_type，
     冬季是生理性为主，其余三季几乎全是真菌）。 */
  function renderSeasonBand() {
    const nameEl = document.getElementById("season-band-season");
    const textEl = document.getElementById("season-band-text");
    if (!nameEl || !textEl) return;

    const meta = ((source && source.seasons) || []).find((s) => s.key === season);
    nameEl.textContent = (meta && meta.name ? meta.name : SEASON_LABEL[season] || "") + "季";

    const rows = [];
    provinces.forEach((p) => (p.top3 || []).forEach((it) => rows.push(it)));
    if (!rows.length) {
      textEl.textContent = "";
      return;
    }

    const kinds = {};
    rows.forEach((it) => {
      const k = it.pathogen_type || "其他";
      kinds[k] = (kinds[k] || 0) + 1;
    });
    const ranked = Object.keys(kinds).sort((a, b) => kinds[b] - kinds[a]);
    const kindsText = ranked
      .slice(0, 2)
      .map((k) => `${k} ${Math.round((kinds[k] / rows.length) * 100)}%`)
      .join("、");

    textEl.textContent =
      (rows[0].peak_window ? rows[0].peak_window + " · " : "") +
      `${provinces.length} 省区市 · ${rows.length} 条高发病害记录 · 主要类型：${kindsText}`;
  }

  /** 省份上色 + 图例。颜色走当季令牌（见 colorMap），图例读的是同一份 ——
      两边不可能对不上。 */
  function applyData() {
    ensureMap().setColors(colorMap());
    renderLegend();
  }

  /* ---------------------------------------------------------------- 交互 */

  /** 交互三件事：悬停预览、点击停住、移出收起。
      点击省份**不直接跳转** —— 跳转只发生在卡片里选定某一种病害之后（openLibrary）。 */
  function bindMapEvents() {
    if (mapBound) return;                   // renderAll 每次换季都会走这里，事件只能挂一次
    mapBound = true;
    const m = ensureMap();

    m.on("hover", (ev) => {
      const el = cardEl();
      if (!el || cardPinned) return;        // 停住期间不理会悬停，免得把钉好的内容改掉
      if (!ev) {
        if (cardName) closeCard();
        return;
      }
      const p = byName.get(ev.name);
      if (!p) return;
      const pt = toStage(ev);
      if (cardName === p.name && !el.hidden) {
        placeCard(pt.x, pt.y, cardSize);    // 还是这个省：只挪位置，不重画内容
        return;
      }
      cardName = p.name;
      showCard(p, pt.x, pt.y, false);
    });

    m.on("pick", (ev) => {
      const p = ev.name ? byName.get(ev.name) : null;
      if (!p) {                             // 点到空白（海面、底板）：收起停住的卡片
        if (cardPinned) closeCard();
        return;
      }
      if (!(p.top3 || []).length) {
        UI.toast(`${p.name} 在本季节暂无可跳转的病害数据`, "warning");
      }
      const pt = toStage(ev);
      cardName = p.name;
      cardAnchor = pt;
      showCard(p, pt.x, pt.y, true);
    });

    // 光标整体移出沙盘就收起预览卡；停住的不收
    m.on("leave", () => {
      if (!cardPinned) closeCard();
    });

    // 转动 / 缩放之后把停住的卡片贴回去，否则它会浮到别的省上面
    m.on("view", reanchor);
  }

  /* ---------------------------------------------------------------- 数据 */

  /** 把 source 摊平成「当前季节」的省份数组。
      快照里是 provinces[].seasons{spring|summer|…}，接口已经按季节拍平成 top3。 */
  function applySeason() {
    if (!source) return;
    provinces = (source.provinces || []).map((p) => ({
      name: p.name,
      short_name: p.short_name || "",
      adcode: p.adcode || "",
      crops: p.crops || [],
      threat_scope: Number(p.threat_scope) || 0,
      top3: source.kind === "snapshot" ? (p.seasons || {})[season] || [] : p.top3 || [],
    }));
    byName = new Map(provinces.map((p) => [p.name, p]));
  }

  function renderAll() {
    applySeason();
    applySeasonTheme();
    renderSeasonSwitch(source.seasons);
    renderSeasonBand();          // 必须排在 applySeason 之后 —— 它读摊平后的 provinces
    applyData();
    bindMapEvents();
    refreshPinnedCard();
    hideOverlay();
  }

  /** 切换季节。本地快照下是纯内存切片，不发请求。 */
  function loadSeason(next) {
    season = next;
    syncSeasonButtons();
    if (source && source.kind === "snapshot") {
      renderAll();
      return Promise.resolve();
    }
    return fetchFromApi(next);
  }

  function fetchFromApi(next) {
    showOverlayLoading();
    return Api.mapPestTop3({ season: next })
      .then((d) => {
        source = {
          kind: "api",
          seasons: d.seasons || [],
          provinces: d.provinces || [],
        };
        renderAll();
      })
      .catch((err) => {
        showOverlayError(
          "地图数据加载失败：" + errorText(err),
          "请确认已生成省市数据（<code>python scripts/04_gen_province_pest.py</code>）。"
        );
      });
  }

  /** 本地快照是生成物，可能与后端数据不一致（改了省份数据但没重新打包）。
      首屏渲染完成后在后台核对一次，不一致就静默切到接口数据并提示，而不是长期显示旧数据。 */
  function verifySnapshot(snap, renderedSeason) {
    Api.mapPestTop3({ season: renderedSeason })
      .then((d) => {
        const same =
          (snap.provinces || []).length === (d.provinces || []).length &&
          (snap.seasons || []).length === (d.seasons || []).length;
        if (same) return;
        source = {
          kind: "api",
          seasons: d.seasons || [],
          provinces: d.provinces || [],
        };
        renderAll();
        UI.toast(
          "首页本地快照已过期，已自动改用后端数据；重跑 scripts/15_bundle_frontend_data.py 可更新快照",
          "warning"
        );
      })
      .catch(() => {
        // 后端没起也无所谓 —— 本地快照本来就是为这种情况准备的
      });
  }

  /* ---------------------------------------------------------------- 启动 */

  /** 起沙盘。**这一步必须早于任何一次 setColors/setTheme** ——
      颜色是写进顶点色数组里的，没有 mesh 就无处可写。

      syncSeasonAttr 要抢在这里：后面 startFromSnapshot → renderAll → applySeasonTheme
      才补得上首屏那次，而舞台底色与季节分带（CSS 的 [data-season=…]）首帧就要用。 */
  function mount3D(geo, assets) {
    if (typeof Map3D === "undefined" || !Map3D.mount) {
      throw new Error("3D 沙盘渲染层未加载（assets/js/map3d.js）");
    }
    syncSeasonAttr();
    map = Map3D.mount(document.getElementById("china-map"), {
      geo: geo,
      relief: assets.relief,
      water: assets.water,
    });
  }

  /** 本地快照就位：不发接口请求，直接渲染（沙盘已在 mount3D 里建好）。 */
  function startFromSnapshot(snap) {
    source = {
      kind: "snapshot",
      seasons: snap.seasons || [],
      provinces: snap.provinces || [],
    };
    if (!source.provinces.length) throw new Error("本地快照里没有省份数据");
    renderAll();
    verifySnapshot(snap, season);
  }

  /** 快照缺失：退回「fetch 底图 + 接口取数」的老路径，但这次有超时也有错误出口。 */
  function bootFromNetwork() {
    showOverlayLoading();
    Promise.all([
      fetch("/assets/geo/china.json").then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }),
      Map3D.loadAssets("/assets/geo/"),
    ])
      .then((res) => {
        mount3D(res[0], res[1]);
        return fetchFromApi(season);
      })
      .catch((err) => {
        showOverlayError(
          "地图底图加载失败：" + errorText(err),
          "请确认 <code>frontend/assets/geo/china.json</code> 与两份地形数据都在，" +
          "或重跑 <code>python scripts/15_bundle_frontend_data.py</code> 生成首页预加载快照。"
        );
      });
  }

  function init() {
    const geo = localGeo();
    const snap = localSnapshot();
    if (geo && snap) {
      // 地形那两份 JSON 是本机静态文件，但仍要取一次，遮罩得先亮着
      showOverlayLoading();
      Map3D.loadAssets("/assets/geo/")
        .then((assets) => {
          mount3D(geo, assets);
          startFromSnapshot(snap);
        })
        .catch((err) => {
          showOverlayError(
            "地形数据加载失败：" + errorText(err),
            "请确认 <code>frontend/assets/geo/china_relief.json</code> 与 " +
            "<code>china_water.json</code> 都在（<code>python scripts/28_build_china_geo.py</code> 生成）。"
          );
        });
      return;
    }

    bootFromNetwork();
  }

  // ESC 收起停住的卡片（与图书库模态的既有做法一致）
  document.addEventListener("keydown", (ev) => {
    if ((ev.key === "Escape" || ev.key === "Esc") && cardPinned) closeCard();
  });

  /* 跟随系统深浅色。沙盘的颜色是写进顶点色与材质里的，CSS 的 @media 换不动它，
     所以媒体查询一变就得主动重读令牌并重绘：否则舞台黑了、省份还留在浅粉绿，
     整块地图像贴上去的一张白天的图。
     applySeasonTheme 换的是底板/描边/标签/水的明暗与深浅档位，
     applyData 换的是省份风险色与图例 —— 两样都要，少一样就与舞台对不上。 */
  if (window.matchMedia) {
    const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
    const onSchemeChange = () => {
      if (!map) return;          // 沙盘还没挂上就不用管，首次渲染自然会读到当时的配色
      applySeasonTheme();
      applyData();
    };
    if (darkQuery.addEventListener) darkQuery.addEventListener("change", onSchemeChange);
    else if (darkQuery.addListener) darkQuery.addListener(onSchemeChange);   // 旧版 Safari
  }

  /* 先写季节属性再等 DOMContentLoaded。脚本标签就排在这两块容器后面，
     DOM 已经解析到它们了，所以这里能立刻拿到元素；晚到 init 才写的话，
     首屏会先按 CSS 的兜底色（春季绿）画一遍再跳成当季色 —— 十月里就是一闪的绿。
     写属性只碰一个 attribute，不读布局，不算提前执行初始化。 */
  syncSeasonAttr();

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
