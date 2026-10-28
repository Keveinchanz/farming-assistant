/* ==========================================================================
   任务 5.2 —— 首页：中国山川河流 3D 沙盘

   本文件管**逻辑**：图层、数据、季节、卡片、图例、圆点、遮罩、深浅色。
   「把地图画出来」这一层在 map3d.js（three.js）里 —— ECharts 的 map series
   画的是平面填充图，没有地面和起伏，怎么调色都是贴纸；换成有厚度的地块、
   真实高程的地形和贴地的江河之后，才算得上「沙盘」。

   顶部那颗选项控件切三个图层，同一块沙盘换一套内容：
   · 山川地貌（默认）：省色 = 本省收录的地貌条目数，沙盘上钉 52 个圆点，
     悬停 / 点击某一处看它的一句话简介；
   · 主要作物：省色 = 该省主要作物的主导类别，图例里还能按具体作物筛一遍；
   · 当季病害：省色 = 病害种类规模，悬停 / 点击看该省当季 TOP3 并跳图书库。

   切换图层是纯内存重算：不重建沙盘、不发请求，所以「即时生效」是真的即时。

   交互约定（三层共用）：
   · 悬停省份 / 圆点：卡片预览（跟随光标）
   · 点击省份 / 圆点：卡片**停住**；病害层里还要在卡片里选定一种病害才跳走

   卡片为什么是自己写的一块 DOM 而不是渲染引擎带的浮层：浮层容器不归我们管，
   盒模型得靠 !important 去抢，宽度只有 max-width 没有固定 width，行还不可换行 ——
   长病害名会被顶出卡片，TOP3 也不可能是真正可点的按钮。
   .map-card（见 index.html）自己做，宽度、换行、交互三样都归自己管。

   数据来源分三层：
   · 省界与各省数据在**本地预加载快照**里（assets/data/home_preload.js，由
     src/content/frontend_bundle.py 生成，index.html 在本文件之前同步加载），
     所以首屏不发接口请求，后端挂了也显示得出来；
   · 山川地貌词典与作物类别表在 assets/data/home_layers.js（同样同步加载的静态 JS）；

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

  /* ------------------------------------------------------------ 三个图层 */

  /* 沙盘顶部那颗选项控件的三项，顺序即界面顺序，**第一项默认选中**：
     山川地貌 → 主要作物 → 当季病害。
     三层共用同一个沙盘、同一套投影，只是「上色依据」「图例」「卡片内容」「圆点」
     四样东西各自换一套 —— 所以切换是纯内存重算，不需要重建沙盘、不需要请求。 */
  const LAYERS = [
    { key: "terrain", name: "山川地貌" },
    { key: "crop", name: "主要作物" },
    { key: "pest", name: "当季病害" },
  ];

  /* 地形图层的色阶：颜色深浅 = 本省收录的地貌条目数。
     分档写成闭区间而不是「除以 5」，是因为条目总数只有 52 处，等距切分会出现
     「1 处」和「2 处」同色、「9 处」独占一档这种偏斜；按实际分布手工分档更均匀。 */
  const TERRAIN_STEPS = [
    { min: 1, max: 2, label: "1～2 处", n: 1 },
    { min: 3, max: 4, label: "3～4 处", n: 2 },
    { min: 5, max: 6, label: "5～6 处", n: 3 },
    { min: 7, max: 8, label: "7～8 处", n: 4 },
    { min: 9, max: 999, label: "9 处以上", n: 5 },
  ];

  /* 作物类别。key 与 __CROP_CATEGORY__ 的取值一一对应；颜色走 CSS 令牌，
     深色模式下由 base.css 里那份深色值接管，这里只留浅色兜底。 */
  const CROP_CATS = [
    { key: "粮食作物", short: "粮食", varName: "--map-crop-grain", fallback: "#dcc46c" },
    { key: "蔬菜", short: "蔬菜", varName: "--map-crop-veg", fallback: "#8fbf7e" },
    { key: "果树", short: "果树", varName: "--map-crop-fruit", fallback: "#d9938a" },
    { key: "经济作物", short: "经济", varName: "--map-crop-cash", fallback: "#a99bd0" },
  ];

  /* 非病害图层的兜底主题：令牌读不到时用，与 base.css 的浅色值同源。 */
  const LAYER_THEME = {
    terrain: { areaColor: "#efe7d6", borderColor: "#fffdf7", labelColor: "#5b4a30", noData: "#e9e0cd" },
    crop: { areaColor: "#ecf2e6", borderColor: "#ffffff", labelColor: "#3c5136", noData: "#e4eddd" },
  };


  let map = null;                     // Map3D 的句柄（3D 沙盘）
  let mapBound = false;               // 沙盘的事件只挂一次（重试时 init 会被再调一次）
  let season = seasonFromDate();
  let source = null;                  // {kind:"snapshot"|"api", seasons, provinces}
  let provinces = [];                 // 当前季节的省份数据（已按季节摊平）
  let byName = new Map();

  /* 图层状态。layer 决定上色依据、图例、卡片内容与圆点，
     另外两个是可选的「在这层里再筛一下」：地形按类型、作物按具体作物。 */
  let layer = "terrain";              // 默认第一项：山川地貌
  let terrainType = null;             // null = 全部六类
  let cropFilter = null;              // null = 不筛，只按主导类别上色
  let terrainIndex = null;            // 省短名 → 该省的地貌条目（首次用到时建）
  let catRarity = null;               // 作物类别 → 全国出现次数（并列排序用，随省份数据清空）

  let cardName = null;                // 卡片当前展示的省份名（地貌卡片时为 null）
  let cardTarget = null;              // {kind:"province",p} | {kind:"feature",f} —— 卡片正在讲什么
  let cardPinned = false;             // true = 停住态（可交互），false = 悬停预览
  let cardSize = null;                // 卡片尺寸缓存：内容没变就别每帧重新量（会强制重排）
  let cardAnchor = null;              // 停住时的屏幕锚点：内容变高变矮后要按它重新摆位

  /** 当前图层的配色：优先读 CSS 令牌（深浅两套都在 CSS 里），读不到退回浅色常量。
      「当季病害」那一层仍按季节换色相；另两层各有自己的中性配色 ——
      地形偏土黄（沙盘质感）、作物偏淡绿（与田块呼应），也免得和四季色混在一起。 */
  function theme() {
    if (layer !== "pest") {
      const fb = LAYER_THEME[layer] || LAYER_THEME.terrain;
      return {
        areaColor: cssVar("--map-" + layer + "-area", fb.areaColor),
        borderColor: cssVar("--map-" + layer + "-border", fb.borderColor),
        labelColor: cssVar("--map-" + layer + "-label", fb.labelColor),
        noData: cssVar("--map-" + layer + "-nodata", fb.noData),
      };
    }
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
      `<p class="mt-0" style="margin-top:12px">${UI.escapeHtml(text || "正在加载中国地形与农业数据…")}</p></div>`;
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

  /* ------------------------------------------------- 图层词典（地形 / 作物） */

  function terrainData() {
    const d = window.__CHINA_TERRAIN__;
    return d && d.items && d.items.length ? d : { types: [], items: [] };
  }

  function terrainTypeName(key) {
    const t = terrainData().types.find((x) => x.key === key);
    return t ? t.name : "";
  }

  /** 省短名 → 该省的地貌条目。索引建一次，之后上色、图例、卡片都是查表。
      键用短名（「内蒙古」「新疆」），因为词典里写的就是短名 ——
      这里不做名称换算，换算一次就多一处会写错的地方。 */
  function terrainOf(shortName) {
    if (!terrainIndex) {
      terrainIndex = new Map();
      terrainData().items.forEach((it) => {
        (it.provinces || []).forEach((p) => {
          if (!terrainIndex.has(p)) terrainIndex.set(p, []);
          terrainIndex.get(p).push(it);
        });
      });
    }
    return terrainIndex.get(shortName) || [];
  }

  /** 当前类型筛选下仍然可见的条目（null 筛选 = 全部）。 */
  function terrainVisible(it) {
    return !terrainType || it.type === terrainType;
  }

  function terrainCountOf(shortName) {
    return terrainOf(shortName).filter(terrainVisible).length;
  }

  function cropCategoryOf(crop) {
    const dict = window.__CROP_CATEGORY__ || {};
    return dict[crop] || "";
  }

  function cropCatMeta(key) {
    return CROP_CATS.find((c) => c.key === key) || null;
  }

  /** 该省的主导作物类别：在它的主要作物里数一数哪一类出现得最多。
      并列时取**全国范围内更少见**的那一类（见 categoryRarity）：省份的主要作物
      大多横跨两三类别，若一律按 CROP_CATS 的顺序取胜方，结果会全部倒向粮食作物，
      果树与经济作物一省都轮不到，四种颜色只用了两种 —— 那不是数据的样子，
      是排序的样子。稀有优先后，陕西这种「粮食与果树各半」的省才会落到果树色上。 */
  function dominantCropCategory(p) {
    const rarity = categoryRarity();
    const tally = {};
    (p.crops || []).forEach((c) => {
      const k = cropCategoryOf(c);
      if (k) tally[k] = (tally[k] || 0) + 1;
    });
    let best = null;
    CROP_CATS.forEach((c) => {
      const n = tally[c.key] || 0;
      if (!n) return;
      if (!best || n > tally[best]) {
        best = c.key;
      } else if (n === tally[best] && rarity[c.key] < rarity[best]) {
        best = c.key;
      }
    });
    return best;
  }

  /** 四类作物在全国各省的主要作物里各出现多少次。只用来给并列项排序，
      不参与上色 —— 所以省份数据一变就在 applySeason 里清空重算。 */
  function categoryRarity() {
    if (catRarity) return catRarity;
    const n = {};
    CROP_CATS.forEach((c) => {
      n[c.key] = 0;
    });
    provinces.forEach((p) => {
      (p.crops || []).forEach((c) => {
        const k = cropCategoryOf(c);
        if (k) n[k] = (n[k] || 0) + 1;
      });
    });
    catRarity = n;
    return n;
  }

  /** 省名 → 颜色。三个图层各一套依据，但都只产出「省名 → 色值」这一张表 ——
      沙盘那边只管上色，不需要知道这是病害、地貌还是作物。 */
  function colorMap() {
    const out = {};

    if (layer === "terrain") {
      const items = terrainData().items;
      // 词典没加载（离线包缺文件 / 顺序不对）时不上色，宁可整块留白也不假装有数据
      if (!items.length) return out;
      provinces.forEach((p) => {
        const n = terrainCountOf(p.short_name || p.name);
        if (!n) return;                       // 没收录条目的省交给 map3d 的 noData
        const step = TERRAIN_STEPS.find((s) => n >= s.min && n <= s.max);
        if (step) out[p.name] = cssVar("--map-terrain-count-" + step.n, "#dfcda6");
      });
      return out;
    }

    if (layer === "crop") {
      provinces.forEach((p) => {
        // 选了某一种作物时，没种它的省直接留白 —— 这一层要回答的是「它分布在哪些省」
        if (cropFilter && !(p.crops || []).includes(cropFilter)) return;
        const cat = cropFilter ? cropCategoryOf(cropFilter) : dominantCropCategory(p);
        const meta = cropCatMeta(cat);
        if (meta) out[p.name] = cssVar(meta.varName, meta.fallback);
      });
      return out;
    }

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
      否则一转头它就会浮在别的省上头。锚点取该省的几何中心在屏幕上的投影；
      地貌卡片则贴回那处地貌的坐标。 */
  function reanchor() {
    if (!cardPinned || !cardTarget || !map) return;
    let pt = null;
    if (cardTarget.kind === "feature") {
      pt = map.projectPoint(cardTarget.f.lon, cardTarget.f.lat);
    } else if (cardName) {
      pt = map.anchor(cardName);
    }
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

  /* ---- 另外两个图层的卡片 ----
     三个图层共用同一个 #map-card，只是内容换一套。与病害卡不同，这两层没有「选中
     一条记录就跳走」这回事（地貌与作物都不是能跳的实体），所以卡片里不放 data-open，
     只在作物卡里放一个「看该省当季病害」的换层按钮。 */

  /* 类型 → 单字标记。地块上的圆点只有颜色没有文字，卡片里用这个字把类型补回来 ——
     依赖记忆去认六种颜色的含义不现实。 */
  const TERRAIN_GLYPH = {
    mountain: "山", plateau: "原", basin: "盆",
    plain: "平", river: "河", lake: "湖",
  };

  function terrainSwatch(type) {
    return (
      `<span class="map-card__rank" ` +
      `style="background:var(--map-terrain-${UI.escapeHtml(type)});color:#fff">` +
      `${UI.escapeHtml(TERRAIN_GLYPH[type] || "地")}</span>`
    );
  }

  /** 一处地貌的卡片：名字 + 类型 + 一句话简介 + 所在省区市。 */
  function renderFeatureCard(f) {
    const el = cardEl();
    if (!el || !f) return;
    const closeBtn =
      `<button type="button" class="map-card__close" data-card-close aria-label="关闭卡片">` +
      (window.Icon ? Icon.render("close", 16) : "×") +
      `</button>`;
    el.innerHTML =
      `<div class="map-card__head">` +
      `<span class="map-card__name">${UI.escapeHtml(f.name)}</span>` +
      `<span class="map-card__season">${UI.escapeHtml(terrainTypeName(f.type))}</span>` +
      closeBtn +
      `</div>` +
      `<p class="map-card__note">${UI.escapeHtml(f.note || "")}</p>` +
      `<div class="map-card__crops">所在省区市：${UI.escapeHtml((f.provinces || []).join("、"))}</div>` +
      `<div class="map-card__hint">` +
      (cardPinned
        ? "再点沙盘上的其他圆点可以对比着看"
        : "点击该圆点可以停住这张卡片") +
      `</div>`;
    bindCardEvents();
  }

  /** 地形图层的省份卡片：这一省收录了哪些地貌。 */
  function renderTerrainCard(p) {
    const el = cardEl();
    if (!el) return;
    const closeBtn =
      `<button type="button" class="map-card__close" data-card-close aria-label="关闭卡片">` +
      (window.Icon ? Icon.render("close", 16) : "×") +
      `</button>`;

    const list = terrainOf(p.short_name || p.name).filter(terrainVisible);
    const rows = list
      .map(
        (f) =>
          `<li class="map-card__item">` +
          `<button type="button" class="map-card__pick" data-feature="${UI.escapeHtml(f.name)}">` +
          `<span class="map-card__row">` +
          terrainSwatch(f.type) +
          `<span class="map-card__pest">${UI.escapeHtml(f.name)}</span>` +
          `<span class="map-card__go">${UI.escapeHtml(terrainTypeName(f.type))}</span>` +
          `</span>` +
          `<span class="map-card__meta">${UI.escapeHtml(f.note || "")}</span>` +
          `</button>` +
          `</li>`
      )
      .join("");

    el.innerHTML =
      `<div class="map-card__head">` +
      `<span class="map-card__name">${UI.escapeHtml(p.short_name || p.name)}</span>` +
      `<span class="map-card__season">` +
      `山川地貌 · ${list.length} 处` +
      (terrainType ? `（只看${UI.escapeHtml(terrainTypeName(terrainType))}）` : "") +
      `</span>` +
      closeBtn +
      `</div>` +
      (rows
        ? `<ul class="map-card__list">${rows}</ul>`
        : `<div class="map-card__empty">这一省暂未收录地名条目</div>`) +
      `<div class="map-card__hint">` +
      (cardPinned ? "点一条可以看它的一句话简介" : "点击该省停住卡片，再点条目看简介") +
      `</div>`;
    bindCardEvents();
  }

  /** 作物图层的省份卡片：主要作物按类别分组。 */
  function renderCropCard(p) {
    const el = cardEl();
    if (!el) return;
    const closeBtn =
      `<button type="button" class="map-card__close" data-card-close aria-label="关闭卡片">` +
      (window.Icon ? Icon.render("close", 16) : "×") +
      `</button>`;

    const crops = p.crops || [];
    // 地图上的颜色是这个类别决定的，卡片里得写出来 —— 否则「湖南是紫色」在卡片里
    // 找不到依据（卡片按类别分组列了 5 种作物，却没有一句说明哪一类算主导）。
    const dominant = dominantCropCategory(p);
    const headMeta = cropFilter ? `只看 ${cropFilter}` : dominant ? `主导类别 ${dominant}` : "";
    const groups = CROP_CATS.map((cat) => {
      const inCat = crops.filter((c) => cropCategoryOf(c) === cat.key);
      if (!inCat.length) return "";
      const chips = inCat
        .map((c) => {
          const on = cropFilter === c ? " is-on" : "";
          return (
            `<button type="button" class="map-card__chip${on}" data-crop="${UI.escapeHtml(c)}">` +
            `${UI.escapeHtml(c)}</button>`
          );
        })
        .join("");
      return (
        `<div class="map-card__group">` +
        `<div class="map-card__groupLabel">` +
        `<span class="map-legend__swatch" style="background:var(${UI.escapeHtml(cat.varName)})"></span>` +
        `${UI.escapeHtml(cat.key)} · ${inCat.length} 种</div>` +
        `<div class="map-card__chips">${chips}</div>` +
        `</div>`
      );
    }).join("");

    el.innerHTML =
      `<div class="map-card__head">` +
      `<span class="map-card__name">${UI.escapeHtml(p.short_name || p.name)}</span>` +
      `<span class="map-card__season">主要作物 ${crops.length} 种` +
      (headMeta ? ` · ${UI.escapeHtml(headMeta)}` : "") +
      `</span>` +
      closeBtn +
      `</div>` +
      (groups || `<div class="map-card__empty">暂无该省的主要作物数据</div>`) +
      ((p.top3 || []).length
        ? `<button type="button" class="btn btn--primary btn--sm map-card__primary" ` +
          `data-goto-layer="pest">看这一省当季的病害</button>`
        : "") +
      `<div class="map-card__hint">` +
      `作物分类取自知识图书库的口径；点一个作物名可以只看它分布在哪些省` +
      `</div>`;
    bindCardEvents();
  }

  /** 卡片分发。target 有两种：{kind:"province", p} 与 {kind:"feature", f}。 */
  function renderAnyCard(target) {
    if (!target) {
      renderCard(null);
      return;
    }
    if (target.kind === "feature") {
      renderFeatureCard(target.f);
      return;
    }
    if (layer === "terrain") {
      renderTerrainCard(target.p);
      return;
    }
    if (layer === "crop") {
      renderCropCard(target.p);
      return;
    }
    renderCard(target.p);
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
    // 地形卡片里的「某一处地貌」：不是跳转，只是把这张卡换成那处地貌的简介
    el.querySelectorAll("[data-feature]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (!cardPinned) return;
        const f = terrainData().items.find((x) => x.name === btn.dataset.feature);
        if (!f) return;
        cardName = null;
        cardTarget = { kind: "feature", f };
        cardAnchor = cardAnchor || null;
        renderAnyCard(cardTarget);
        cardSize = measureCard();
        if (cardAnchor) placeCard(cardAnchor.x, cardAnchor.y, cardSize);
      });
    });
    // 作物卡片里的「看这一省当季的病害」：换到病害层，并把这一省的卡片接着停住
    el.querySelectorAll("[data-goto-layer]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (!cardPinned) return;
        const name = cardTarget && cardTarget.kind === "province" ? cardTarget.p.name : cardName;
        setLayer(btn.dataset.gotoLayer);
        if (name) focusProvince(name);
      });
    });
    // 作物名的点选：与图例里的作物按钮同一件事（切筛选），走同一个入口
    el.querySelectorAll("[data-crop]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (!cardPinned) return;
        toggleCrop(btn.dataset.crop);
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

  /** 弹出卡片。pinned=false 是悬停预览（跟随光标），true 是点击后停住。
      target 见 renderAnyCard：省或地貌点。 */
  function showCard(target, x, y, pinned) {
    const el = cardEl();
    if (!el) return;
    cardPinned = !!pinned;
    cardTarget = target || null;
    el.dataset.pinned = cardPinned ? "true" : "false";
    // 预览态做成提示语义，停住态才是真正的对话区
    if (cardPinned) {
      el.setAttribute("role", "dialog");
    } else {
      el.removeAttribute("role");
    }
    el.hidden = false;                        // 必须先可见，下面才量得到真实尺寸
    renderAnyCard(cardTarget);
    cardSize = measureCard();
    cardAnchor = { x, y };
    placeCard(x, y, cardSize);
  }

  function closeCard() {
    const el = cardEl();
    cardPinned = false;
    cardName = null;
    cardTarget = null;
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

  /** 切季节 / 切筛选之后，停住的卡片要换成新内容，而不是继续显示上一份。 */
  function refreshPinnedCard() {
    if (!cardPinned || !cardTarget) return;
    if (cardTarget.kind === "province") {
      const p = byName.get(cardTarget.p.name);
      if (!p) {
        closeCard();
        return;
      }
      cardTarget = { kind: "province", p };   // 换季后要拿摊平后的新对象
    }
    renderAnyCard(cardTarget);
    cardSize = measureCard();                 // 换季后行数与文字长度都可能变，尺寸要重新量
    // 尺寸变了就得按原锚点重新摆一次：换季可能让卡片变高，不重摆就会顶出舞台下沿
    if (cardAnchor) placeCard(cardAnchor.x, cardAnchor.y, cardSize);
  }

  /** 把卡片停到某个省上（换层之后接着看这一省时用）。 */
  function focusProvince(name) {
    const p = byName.get(name);
    if (!p || !map) return;
    const pt = map.anchor(name);
    if (!pt) return;
    const off = canvasOffset();
    cardName = p.name;
    cardAnchor = { x: pt.x + off.x, y: pt.y + off.y };
    showCard({ kind: "province", p: p }, cardAnchor.x, cardAnchor.y, true);
  }

  /* ------------------------------------------------------------ 季节主题 */

  /** 舞台加 data-season（驱动季节按钮的强调色），沙盘的非语义配色也跟着季节走：
      底板、省界描边、省名标签、水的明暗。风险色阶不在这里 —— 那是"数据"那一层，
      跟着 applyData 走。 */
  function applySeasonTheme() {
    syncSeasonAttr();
    syncLayerAttr();
    ensureMap().setTheme(theme());
  }

  /** 图层标记写到所有 [data-season-host] 上：舞台与季节分带的底色按图层换
      （地形偏土黄、作物偏淡绿、病害仍按四季），季节切换器也只在病害层里出现。 */
  function syncLayerAttr() {
    document.querySelectorAll("[data-season-host]").forEach((el) => {
      el.dataset.layer = layer;
    });
  }

  /* ---------------------------------------------------------------- 渲染 */

  function renderLegend() {
    if (layer === "terrain") renderTerrainLegend();
    else if (layer === "crop") renderCropLegend();
    else renderPestLegend();
  }

  function legendHost() {
    return document.getElementById("legend-items");
  }

  function legendTitle(text) {
    const el = document.getElementById("legend-title");
    if (el) el.textContent = text;
  }

  function legendNote(text) {
    const el = document.getElementById("legend-note");
    if (el) el.textContent = text;
  }

  /** 当季病害的图例：色阶 = 病害种类规模。 */
  function renderPestLegend() {
    const host = legendHost();
    if (!host) return;
    legendTitle("病虫害种类规模");
    host.innerHTML = PIECES.filter((p) => p.max !== 999)
      .map(
        (p) =>
          `<div class="map-legend__item">` +
          `<span class="map-legend__swatch" style="background:${pieceColor(p)}"></span>` +
          `<span>${p.label}</span></div>`
      )
      .join("");
    legendNote("颜色深浅表示该省主要作物涉及的病害种类规模，全国范围 21～74 种。");
  }

  /** 山川地貌的图例：六类各一个筛选钮（带条目数）+ 条目数色阶。
      筛选钮不改变数据，只改变「哪些圆点显示、省色按哪一类数」—— 和图层切换一样是纯内存重算。 */
  function renderTerrainLegend() {
    const host = legendHost();
    const data = terrainData();
    if (!host) return;
    legendTitle("地貌类型");

    const counts = {};
    data.items.forEach((it) => {
      counts[it.type] = (counts[it.type] || 0) + 1;
    });

    const all =
      `<button type="button" class="map-legend__chip${terrainType ? "" : " is-on"}" ` +
      `data-terrain-type="">全部<span class="map-legend__num">${data.items.length}</span></button>`;
    const chips = data.types
      .map(
        (t) =>
          `<button type="button" class="map-legend__chip${terrainType === t.key ? " is-on" : ""}" ` +
          `data-terrain-type="${UI.escapeHtml(t.key)}">` +
          `<span class="map-legend__dot" style="background:var(--map-terrain-${UI.escapeHtml(t.key)})"></span>` +
          `${UI.escapeHtml(t.name)}<span class="map-legend__num">${counts[t.key] || 0}</span></button>`
      )
      .join("");

    const ramp = TERRAIN_STEPS.map(
      (s) =>
        `<span class="map-legend__swatch" style="background:var(--map-terrain-count-${s.n})" ` +
        `title="${UI.escapeHtml(s.label)}"></span>`
    ).join("");

    host.innerHTML =
      `<div class="map-legend__chips">${all}${chips}</div>` +
      `<div class="map-legend__ramp">${ramp}` +
      `<span class="map-legend__rampText">1 处 → 9 处以上</span></div>`;

    legendNote(
      terrainType
        ? `只看${terrainTypeName(terrainType)}：${data.items.filter(terrainVisible).length} 处。颜色＝本省这类地貌的条目数。`
        : "颜色＝本省收录的名山、高原、盆地、平原与江河湖泊条目数；圆点颜色＝地貌类型。点圆点看简介。"
    );
  }

  /** 主要作物的图例：四类各一个色块（带省数）+「按作物看」的作物钮。 */
  function renderCropLegend() {
    const host = legendHost();
    if (!host) return;
    const tally = cropTally();
    const provCount = {};
    CROP_CATS.forEach((c) => {
      provCount[c.key] = 0;
    });
    provinces.forEach((p) => {
      const cat = dominantCropCategory(p);
      if (cat) provCount[cat] += 1;
    });

    legendTitle("主导作物类别");
    const cats = CROP_CATS.map(
      (c) =>
        `<div class="map-legend__item">` +
        `<span class="map-legend__swatch" style="background:var(${c.varName})"></span>` +
        `<span>${UI.escapeHtml(c.key)}</span>` +
        `<span class="map-legend__num">${provCount[c.key]} 省</span></div>`
    ).join("");

    const chips = tally
      .slice(0, 12)
      .map(
        (x) =>
          `<button type="button" class="map-legend__chip${cropFilter === x.crop ? " is-on" : ""}" ` +
          `data-crop="${UI.escapeHtml(x.crop)}">${UI.escapeHtml(x.crop)}` +
          `<span class="map-legend__num">${x.n}</span></button>`
      )
      .join("");

    host.innerHTML =
      cats +
      `<div class="map-legend__sub">按作物看（省数为主要作物含该作物的省区市个数）</div>` +
      `<div class="map-legend__chips">${chips}</div>`;

    if (cropFilter) {
      const hit = tally.find((x) => x.crop === cropFilter);
      legendNote(`只看${cropFilter}：${hit ? hit.n : 0} 个省区市，其余省已留白。再点一次取消。`);
    } else {
      legendNote(
        "颜色＝该省主要作物里出现最多的类别（并列时取全国更少见的那一类）；" +
        "点一个作物名可以只看它分布在哪些省。"
      );
    }
  }

  /** 各省主要作物出现次数（用于图例与分带的「几省有它」）。 */
  function cropTally() {
    const tally = new Map();
    provinces.forEach((p) => (p.crops || []).forEach((c) => tally.set(c, (tally.get(c) || 0) + 1)));
    return Array.from(tally.entries())
      .map((pair) => ({ crop: pair[0], n: pair[1] }))
      .sort((a, b) => b.n - a.n || (a.crop < b.crop ? -1 : 1));
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
    const labelEl = document.querySelector(".season-band__label");
    if (!nameEl || !textEl) return;

    // 分带是「这一屏在看什么」的一句话说明，三个图层各说各的：
    // 病害层说当季，地形层说收录了多少处，作物层说覆盖多少种。
    if (layer !== "pest") {
      renderLayerBand(nameEl, textEl, labelEl);
      return;
    }
    if (labelEl) labelEl.textContent = "当季";

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

  /** 地形 / 作物两个图层的分带文案。数字全部当场数出来，不写死 ——
      改了 home_layers.js 的条目或省份数据，这里自动跟着变。 */
  function renderLayerBand(nameEl, textEl, labelEl) {
    if (layer === "terrain") {
      const data = terrainData();
      const shown = data.items.filter(terrainVisible);
      if (labelEl) labelEl.textContent = "地形";
      nameEl.textContent = shown.length + " 处";
      const provs = new Set();
      shown.forEach((it) => (it.provinces || []).forEach((p) => provs.add(p)));
      if (terrainType) {
        textEl.textContent =
          `${terrainTypeName(terrainType)}：${shown.length} 处，分布在 ${provs.size} 个省区市` +
          ` · 颜色＝本省这类地貌的条目数`;
        return;
      }
      const parts = data.types
        .map((t) => `${t.name} ${data.items.filter((i) => i.type === t.key).length}`)
        .join(" · ");
      textEl.textContent = `名山、高原、盆地、平原与江河湖泊 · ${parts} · 颜色＝本省收录条目数`;
      return;
    }

    const tally = cropTally();
    if (labelEl) labelEl.textContent = "作物";
    nameEl.textContent = tally.length + " 种";
    if (cropFilter) {
      const hit = tally.find((x) => x.crop === cropFilter);
      textEl.textContent = `只看${cropFilter}：${hit ? hit.n : 0} 个省区市的主要作物里有它`;
      return;
    }
    textEl.textContent =
      `覆盖 ${provinces.length} 个省区市 · ` +
      tally.slice(0, 3).map((x) => `${x.crop} ${x.n} 省`).join(" · ") +
      ` · 颜色＝主导作物类别`;
  }

  /** 省份上色 + 图例 + 地貌圆点。三者读的是同一份状态（layer / 筛选条件），
      所以不可能出现「图例说一套、沙盘画另一套」。 */
  function applyData() {
    ensureMap().setColors(colorMap());
    renderLegend();
    renderPins();
  }

  /* ------------------------------------------------------------ 地貌圆点 */

  /* 沙盘上那些「某座山在哪」的标记。做法是把经纬度投影成屏幕像素，再用绝对定位的
     按钮贴上去 —— 不是往 three.js 场景里加 sprite：
       · 一来按钮天然可聚焦、可 hover、有 aria-label，键盘也能用；
       · 二来文字标签需要避让，用 DOM 算矩形比在场景里做字体碰撞简单得多。
     代价是每帧要自己跟投影：所以 placePins 只在「视角变了 / 筛选变了 / 窗口变了」
     时跑，而不是放在渲染循环里。 */

  let pinEls = [];            // 与 pinItems 一一对应的按钮
  let pinItems = [];          // 当前显示的条目（已按类型筛选过）
  let pinSizes = [];          // 每个标签的像素尺寸：建完 DOM 量一次，拖动时不再读布局
  let pinBound = false;

  /** 建圆点 DOM。只在条目集合变化时重建，避免每帧销毁重建导致 hover 中断。 */
  function renderPins() {
    const host = document.getElementById("map-pins");
    if (!host) return;
    if (layer !== "terrain") {
      host.innerHTML = "";
      host.dataset.sig = "";
      pinEls = [];
      pinItems = [];
      pinSizes = [];
      return;
    }
    const items = terrainData().items.filter(terrainVisible);
    pinItems = items;                        // 先定数据再建 DOM —— measurePins 里会拿它去投影
    const sig = items.map((it) => it.name).join("|");
    if (host.dataset.sig !== sig) {
      host.dataset.sig = sig;
      host.innerHTML = items
        .map(
          (it, i) =>
            `<button type="button" class="map-pin map-pin--${UI.escapeHtml(it.type)}" data-pin="${i}" ` +
            `aria-label="${UI.escapeHtml(it.name)}（${UI.escapeHtml(terrainTypeName(it.type))}）">` +
            `<span class="map-pin__dot"></span>` +
            `<span class="map-pin__label">${UI.escapeHtml(it.name)}</span>` +
            `</button>`
        )
        .join("");
      pinEls = Array.prototype.slice.call(host.querySelectorAll("[data-pin]"));
      // 标签宽窄差一倍（「秦岭」到「长江中下游平原」），避让必须按各自的真实宽度算，
      // 用一个固定阈值的结果就是「秦岭」把旁边的名字压住。这里量一次，之后拖动只读数组。
      measurePins();
      // 字体晚到会让上面量到的是兜底字体的宽度（中文差 1～2 个字宽），
      // 所以字体就绪后再量一次 —— 只此一次，不进渲染循环。
      if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
        document.fonts.ready.then(measurePins);
      }
      bindPinEvents();
    }
    placePins();
  }

  function measurePins() {
    if (!pinEls.length || pinEls.length !== pinItems.length) return;
    // 先把「只留圆点」摘掉再量：带着这个类量，标签是 display:none，
    // 量到的宽度就是一颗圆点，之后避让全按 16px 算，等于没避让。
    pinEls.forEach((el) => el.classList.remove("map-pin--dotOnly"));
    pinSizes = pinEls.map((el) => ({ w: el.offsetWidth || 64, h: el.offsetHeight || 24 }));
    placePins();
  }

  /** 把每个圆点摆到它的经纬度投影处，并给标签做一次避让：
      和已摆好的标签相碰就这一帧只留圆点（加 --dotOnly）。这样五十多个名字
      不会糊成一片，转动视角时也会自动重新「挑」出一批能看清的。

      矩形一律按「按钮在 CSS 里退了 4px」修正（.map-pin 的 margin 是 -4px），
      否则算的是标签中心点、画出来的是左上角，避让会差半个圆点。 */
  function placePins() {
    if (!map || layer !== "terrain" || !pinEls.length) return;
    const off = canvasOffset();
    // 一次性把坐标算出来：投影接口内部只读一次 canvas 的 rect，
    // 之后再写 style，不会出现「读一次布局 → 写一次样式 → 再读一次布局」的重排循环。
    const pts = map.projectPoints(pinItems.map((it) => ({ lon: it.lon, lat: it.lat })));
    const order = pinEls
      .map((el, i) => i)
      .sort((a, b) => (pinItems[b].hot ? 1 : 0) - (pinItems[a].hot ? 1 : 0) || a - b);
    const placed = [];
    const M = 4;                            // 与 .map-pin 的负外边距对齐

    order.forEach((i) => {
      const el = pinEls[i];
      const pt = pts[i];
      if (!pt) {
        el.hidden = true;
        return;
      }
      const x = Math.round(pt.x + off.x);
      const y = Math.round(pt.y + off.y);
      const size = pinSizes[i] || { w: 64, h: 24 };
      el.hidden = false;
      el.style.transform = `translate3d(${x}px, ${y}px, 0)`;
      const box = { x: x - M, y: y - M, w: size.w, h: size.h };
      const crowded = placed.some(
        (q) => box.x < q.x + q.w && q.x < box.x + box.w &&
               box.y < q.y + q.h && q.y < box.y + box.h
      );
      el.classList.toggle("map-pin--dotOnly", crowded);
      if (!crowded) placed.push(box);
    });
  }

  function pinAnchorOf(btn) {
    const stage = stageEl();
    if (!stage || !btn) return null;
    const r = btn.getBoundingClientRect();
    const s = stage.getBoundingClientRect();
    return { x: r.left - s.left + r.width / 2, y: r.top - s.top + r.height / 2 };
  }

  function bindPinEvents() {
    const host = document.getElementById("map-pins");
    if (!host || pinBound) return;
    pinBound = true;

    const itemOf = (target) => {
      const btn = target && target.closest ? target.closest("[data-pin]") : null;
      return btn ? pinItems[Number(btn.dataset.pin)] : null;
    };

    host.addEventListener("mouseover", (ev) => {
      if (cardPinned) return;               // 停住期间不拿预览去盖掉
      const it = itemOf(ev.target);
      const pt = it ? pinAnchorOf(ev.target.closest("[data-pin]")) : null;
      if (!it || !pt) return;
      cardName = null;
      showCard({ kind: "feature", f: it }, pt.x, pt.y, false);
    });

    host.addEventListener("click", (ev) => {
      const it = itemOf(ev.target);
      const pt = it ? pinAnchorOf(ev.target.closest("[data-pin]")) : null;
      if (!it || !pt) return;
      ev.stopPropagation();
      cardName = null;
      cardAnchor = pt;
      showCard({ kind: "feature", f: it }, pt.x, pt.y, true);
    });

    host.addEventListener("mouseleave", () => {
      if (!cardPinned) closeCard();
    });
  }

  /* ------------------------------------------------------------ 图层切换 */

  let layerBound = false;
  let legendBound = false;

  /** 切图层。三步：改状态 → 换主题（底板/描边/省名）→ 重算数据（省色/图例/圆点）。
      全程不发请求、不重建沙盘，所以是即时的；停住的卡片先收起来 ——
      上一层的内容（病害名 / 作物名）到这一层已经讲不通了。 */
  function setLayer(next) {
    if (!LAYERS.some((l) => l.key === next) || next === layer) return;
    layer = next;
    terrainType = null;
    cropFilter = null;
    closeCard();
    syncLayerButtons();
    applyLayerTheme();
    applyData();
  }

  /** 换层只需要重刷主题与数据，季节属性不必重取 —— 但走 applySeasonTheme 更省事，
      而且它同时把 data-layer 写到舞台上（CSS 靠它换舞台底色）。 */
  function applyLayerTheme() {
    applySeasonTheme();
    renderSeasonBand();
  }

  function syncLayerButtons() {
    document.querySelectorAll("[data-layer-btn]").forEach((b) => {
      const on = b.dataset.layerBtn === layer;
      b.setAttribute("aria-pressed", String(on));
    });
  }

  function bindLayerSwitch() {
    const host = document.getElementById("map-modes");
    if (!host || layerBound) return;
    layerBound = true;
    host.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-layer-btn]");
      if (btn) setLayer(btn.dataset.layerBtn);
    });
  }

  /** 图例里的筛选钮（地貌类型 / 作物名）。事件挂在外层 #legend-items 上做委托 ——
      图例内容每次上色都会重画，逐个绑定等于每画一次就漏一批监听。 */
  function bindLegendEvents() {
    const host = legendHost();
    if (!host || legendBound) return;
    legendBound = true;
    host.addEventListener("click", (ev) => {
      const type = ev.target.closest("[data-terrain-type]");
      if (type) {
        toggleTerrainType(type.dataset.terrainType || null);
        return;
      }
      const crop = ev.target.closest("[data-crop]");
      if (crop) toggleCrop(crop.dataset.crop || null);
    });
  }

  function toggleTerrainType(next) {
    terrainType = next && next !== terrainType ? next : null;
    renderSeasonBand();
    applyData();
    refreshPinnedCard();
  }

  function toggleCrop(next) {
    cropFilter = next && next !== cropFilter ? next : null;
    renderSeasonBand();
    applyData();
    refreshPinnedCard();
  }

  /* ---------------------------------------------------------------- 交互 */

  /** 交互三件事：悬停预览、点击停住、移出收起。
      点击省份**不直接跳转** —— 跳转只发生在卡片里选定某一种病害之后（openLibrary）。
      三个图层共用这套交互，只是卡片内容（renderAnyCard）随图层变。 */
  function bindMapEvents() {
    if (mapBound) return;                   // renderAll 每次换季都会走这里，事件只能挂一次
    mapBound = true;
    const m = ensureMap();

    m.on("hover", (ev) => {
      const el = cardEl();
      if (!el || cardPinned) return;        // 停住期间不理会悬停，免得把钉好的内容改掉
      if (!ev) {
        if (cardTarget) closeCard();
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
      showCard({ kind: "province", p: p }, pt.x, pt.y, false);
    });

    m.on("pick", (ev) => {
      const p = ev.name ? byName.get(ev.name) : null;
      if (!p) {                             // 点到空白（海面、底板）：收起停住的卡片
        if (cardPinned) closeCard();
        return;
      }
      if (layer === "pest" && !(p.top3 || []).length) {
        UI.toast(`${p.name} 在本季节暂无可跳转的病害数据`, "warning");
      }
      const pt = toStage(ev);
      cardName = p.name;
      cardAnchor = pt;
      showCard({ kind: "province", p: p }, pt.x, pt.y, true);
    });

    // 光标整体移出沙盘就收起预览卡；停住的不收
    m.on("leave", () => {
      if (!cardPinned) closeCard();
    });

    // 转动 / 缩放之后把停住的卡片贴回去，否则它会浮到别的省上面；圆点也要重摆
    m.on("view", reanchor);
    m.on("view", placePins);
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
    catRarity = null;                        // 省份数据换了，并列排序的依据要重算
  }

  function renderAll() {
    // 词典没加载上（文件缺失 / 部署时漏拷 home_layers.js）就别把用户丢在一块空沙盘上：
    // 退回「当季病害」—— 那一层只依赖省份快照，没有词典也能完整显示。
    if (layer === "terrain" && !terrainData().items.length) layer = "pest";
    applySeason();
    applySeasonTheme();
    renderSeasonSwitch(source.seasons);
    syncLayerButtons();          // 左上角选项控件的选中态（首屏默认就是「山川地貌」）
    bindLayerSwitch();
    bindLegendEvents();
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

  /* 改窗口会触发沙盘重新取景，圆点的屏幕位置随之变化。map3d 的 fit() 不广播 view
     （那个事件是给拖动与缩放用的），所以这里自己跟一次；用 rAF 合并连发的 resize，
     免得拖窗口时每一像素都重算五十多个投影。 */
  let resizeTick = 0;
  window.addEventListener("resize", () => {
    if (resizeTick) cancelAnimationFrame(resizeTick);
    resizeTick = requestAnimationFrame(() => {
      resizeTick = 0;
      placePins();
      reanchor();
    });
  });

  /* 先写季节与图层属性，再等 DOMContentLoaded。脚本标签就排在这两块容器后面，
     DOM 已经解析到它们了，所以这里能立刻拿到元素；晚到 init 才写的话，
     首屏会先按 CSS 的兜底色（春季绿）画一遍再跳成当季色 —— 十月里就是一闪的绿。
     写属性只碰一个 attribute，不读布局，不算提前执行初始化。 */
  syncSeasonAttr();
  syncLayerAttr();

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
