/* ==========================================================================
   阶段 7 / 7.6 —— 3D 田块视图（Three.js）

   为什么用 r160 的 **UMD 单文件**（frontend/assets/vendor/three/three.min.js）：
   与本项目既有的 ECharts 完全同一套约定 —— 普通 <script src> 引入、挂全局 THREE、
   零外网依赖、无构建步骤。r160 之后官方移除了 UMD 构建，所以这是最后一个可用版本。
   （控制台会打印一行 r150+ 的弃用提示，来自 three 自己，不是我们的代码。）

   为什么轨道控制器**自己写**（约 60 行）而不是用 OrbitControls：
   r160 只提供 ES Module 版的 examples/jsm/controls/OrbitControls.js，
   用它就得把本文件改成 <script type="module">，与其余所有页面脚本（IIFE 经典脚本）
   风格割裂，也不能再靠全局变量与页面脚本通信。而 7.6.1 要的只是「旋转 + 缩放」，
   自己写反而更小更可控。

   性能：整块田用**一次** InstancedMesh 画完（一株 = 一个合并好的几何体，
   逐顶点着色），所以不管田里有多少株，draw call 都是 1 个。
   ========================================================================== */

(function () {
  "use strict";

  // 一亩地实际有一两万株，画不了也没必要画 —— 这里按亩数换算出一个**代表性样本**（7.6.3），
  // 它是"看起来像一块田"的密度，不是真实株数。换算的依据是**作物的真实株行距**：
  //
  //   水稻 30×15cm（约 22 穴/㎡）· 玉米 60×30cm（约 5.6 株/㎡）
  //
  // 原来是一个写死的 80 株/亩 —— 折算到 5 米见方的地是 0.5×0.6 米的株行距，
  // 那是果园的行距，种稻麦的地里根本不会这么稀，一眼就"假"。
  //
  // 表里给的是**视觉株行距**，不是真实值：真按 30×15 厘米画，一亩要一万多株，
  // 画不动也没意义。取真实株距的六到九成，既能看出成行的垄，又不会糊成一片绿墙。
  // 玉米/棉花的行距是稻麦的两倍，用同一个密度必然有一种作物假 —— 所以必须分作物。
  const ROW_SPACING = {
    "水稻": 0.28, "小麦": 0.28,                        // 丛生密植
    "大豆": 0.34, "油菜": 0.34, "马铃薯": 0.34,
    "玉米": 0.52, "棉花": 0.52,                        // 宽行距
  };
  const DEFAULT_ROW_SPACING = 0.34;                    // 拿不到作物时兜底

  // 植株铺到离田埂多近（占地块边长的比例）。**0.88 是错的**：那会在田埂内侧
  // 留出一圈 12% 宽的裸土，整块地看着就像"花盆里种了一丛草"，不像一丘田 ——
  // 真实的地，庄稼一直长到田埂根下。0.93 留出约 5 厘米的缝（田埂内壁到第一行），
  // 既不穿帮，也不留白。垄位、株数、地表纹理的相位三处都从它推出来。
  const RIDGE_INSET = 0.93;

  // 叶长 ÷ 株高。**分作物**：玉米三米高、叶片才一米出头（0.38），
  // 用同一个比例的话，玉米会长出一米三的叶子，十四个株挤在半米的株距里
  // 糊成一床"吊兰毯" —— 这正是原来玉米那一眼假的原因。
  const LEAF_LEN = {
    "玉米": 0.38, "棉花": 0.44, "大豆": 0.42, "油菜": 0.42, "马铃薯": 0.46,
  };
  const DEFAULT_LEAF_LEN = 0.55;                       // 稻麦：株高 0.8 米、叶长 0.45 米

  const MAX_PLANTS = 700;
  // 只在亩数未知时兜底用；正常路径走 plantCountFor() 的株行距换算。
  const PLANTS_PER_MU = 180;
  const PLANT_SPACING = 0.34;      // 米（场景单位）

  const TWEEN_MS = 700;            // 成熟度变化的补间时长（7.6.2.6）

  // 俯角限位：不让相机钻到地面以下，也不让它完全垂直（垂直时方位角失去意义，
  // 拖动会感觉「卡住」）
  const PHI_MIN = 0.18;
  const PHI_MAX = 1.45;
  const RADIUS_MIN = 2.5;
  const RADIUS_MAX = 26;

  // 天光/雾色。天空球底部、场景背景、雾用同一个值 —— 三处不一致的话，
  // 地平线上会出现一道看得出来的"接缝"。
  const SKY_FOG = 0xcfe1f0;
  const SKY_TOP = 0x8fbfe8;
  const SKY_HORIZON = 0xe9f0f1;   // 贴地平线那一圈的暖白雾带（见下面天空顶点色）

  // 太阳方向（**单位向量**，从地面指向日头）。光源位置、阴影相机、叶片透光
  // 三处都从它推出来 —— 各写一个坐标的话，改了方位只会有一个地方跟着动。
  const SUN_DIR = { x: 6, y: 10, z: 4 };
  {
    const len = Math.hypot(SUN_DIR.x, SUN_DIR.y, SUN_DIR.z);
    SUN_DIR.x /= len; SUN_DIR.y /= len; SUN_DIR.z /= len;
  }

  /** 确定性伪随机（mulberry32）：同一个种子 → 同一条序列。
   *
   * 地表色斑、地表纹理、散落的土块都用它。用 Math.random 的话，
   * 每次切田块重建地表都会"闪"一下 —— 同一块地看着看着就换了个样子。
   */
  function rng(seed) {
    let t = seed >>> 0;
    return function () {
      t = (t + 0x6d2b79f5) >>> 0;
      let x = t;
      x = Math.imul(x ^ (x >>> 15), x | 1);
      x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
      return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
    };
  }

  /* ---------------------------------------------------------------- 地表噪声 */

  /** 二维整数哈希 → [0,1)。同一个 (x,z) 永远得到同一个值。 */
  function hash2(x, z) {
    let h = Math.imul((Math.round(x) | 0) ^ 0x9e3779b9, 0x85ebca6b);
    h = Math.imul(h ^ (Math.round(z) | 0), 0xc2b2ae35);
    h = Math.imul(h ^ (h >>> 15), 0x27d4eb2f);
    return ((h ^ (h >>> 13)) >>> 0) / 4294967296;
  }

  /** 值噪声：格点上取哈希，格内用 smoothstep 双线性插值 —— 连续、无方向性条纹。 */
  function smoothNoise(x, z) {
    const xi = Math.floor(x), zi = Math.floor(z);
    const xf = x - xi, zf = z - zi;
    const u = xf * xf * (3 - 2 * xf);
    const v = zf * zf * (3 - 2 * zf);
    const a = hash2(xi, zi), b = hash2(xi + 1, zi);
    const c = hash2(xi, zi + 1), d = hash2(xi + 1, zi + 1);
    return (a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v;
  }

  /** 分形噪声：三个倍频叠加 —— 低频给"地势起伏"，高频给"地表颗粒"。 */
  function fbm(x, z) {
    return smoothNoise(x, z) * 0.55 +
           smoothNoise(x * 2.3, z * 2.3) * 0.28 +
           smoothNoise(x * 5.1, z * 5.1) * 0.17;
  }

  /** 垄的排布：垄距与株行距一致，且第 0 行正好落在垄背上。
   *
   * 原来垄距是写死的 `side / 9`，和株行距各算各的 —— 植株踩在垄沟里、
   * 垄背穿过两行之间，"行栽"就看不出来了。这里由行数反推垄距。
   */
  function ridgePlan(side, rows) {
    const usable = side * RIDGE_INSET;
    const n = Math.max(1, rows | 0);
    const every = n > 1 ? usable / (n - 1) : usable;
    return { every: every, z0: -usable / 2 - every * 0.25 };
  }

  /** 地表高度（世界坐标）。**三处共用同一份**。
   *
   * 原来地表的起伏来自 PlaneGeometry 的局部 Y，而散落土块的高度用的是世界 Z，
   * 中间隔着一次 `rotateX(-90°)` —— 它把局部 Y 映射成世界 **−Z**，两者差一个负号。
   * 于是土块在垄背上下沉、在垄沟上悬空，只是小到看不出来。现在只有一个函数，
   * 想错也没有第二次机会。
   */
  function groundYAt(x, z, ridge) {
    const wave = Math.sin(((z - ridge.z0) / ridge.every) * Math.PI * 2);
    // 缓坡：整块地不再是一张绝对平的板。幅度压在 ±5 厘米 —— 是"田面整得不太匀"，
    // 不是"这块地在山坡上"，后者会让所有植株都歪。垄的起伏本身只有 ±3.5 厘米，
    // 两者叠起来在 5 米见方的地上约 10 厘米的高差，是旱地田面该有的样子。
    const slope = (fbm(x * 0.42 + 11.3, z * 0.42 + 7.7) - 0.5) * 0.10;
    return wave * 0.035 + slope;
  }

  /** 地块边长（米）。原来这个公式在 buildGround 与补间里各写了一遍。 */
  function fieldSide(areaMu) {
    return Math.max(2.4, Math.sqrt(Math.max(areaMu, 0.3) * 666.7) * 0.2);
  }

  /** 株数 → 行列。行距略大于株距（真实田就是这样），并保证行列都 ≥ 1。 */
  function gridFor(count) {
    const cols = Math.max(1, Math.round(Math.sqrt(count * 1.15)));
    const rows = Math.max(1, Math.ceil(count / cols));
    return { cols: cols, rows: rows };
  }

  /* 叶片透光的三个 uniform。
   *
   * 放在模块级共用一份：材质在补间期间每帧都重建，每次都新建一组 uniform
   * 只是白造垃圾 —— 值本来就是常量，没有任何一张田需要不一样的透光。
   * 惰性建：THREE 没加载进来时（WebGL 不可用）不该在脚本求值阶段就抛异常。
   */
  let leafUniforms = null;
  function makeLeafUniforms() {
    if (!leafUniforms) {
      leafUniforms = {
        // 从地面指向日头的单位向量，与下面 sun.position 同源（SUN_DIR）
        uSunDir: { value: new THREE.Vector3(SUN_DIR.x, SUN_DIR.y, SUN_DIR.z) },
        uLeafTint: { value: new THREE.Color(0xffe6b0) },   // 透过来的光是暖的
        uLeafTrans: { value: 0.42 },
      };
    }
    return leafUniforms;
  }

  /* 程序化地表纹理 —— 一张 256×256 的**灰度**细节图：土粒噪点 + 一条垄向暗带。
   *
   * 为什么是灰度不是土色：地表颜色已经由顶点色决定（同一块地处处一致），
   * 这张图只负责"颗粒感"，和顶点色相乘，乘完还是那个土色。写死颜色的话，
   * 两处颜色会各乘一遍，地表会明显发暗。
   *
   * 只画一次，模块级缓存，之后所有田块（主视图 + 缩略图）共用同一张 ——
   * 每建一次地表都新建一张纹理，切几次田就把显存耗光了。
   */
  let soilTexCache = null;
  function soilTexture(renderer) {
    if (soilTexCache) return soilTexCache;

    const size = 256;
    const cv = document.createElement("canvas");
    cv.width = size;
    cv.height = size;
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#fbf8f2";                 // 接近白的底色，乘上去几乎不改色
    ctx.fillRect(0, 0, size, size);

    // 一条垄向暗带：纹理沿 v 平铺成垄的条数（repeat 在 soilFor 里按行数设）。
    // 用一行行的半透明横线叠出渐变，而不是 canvas 的 createLinearGradient ——
    // 项目对"渐变"有回归锁，这里干脆连 API 都不碰。
    for (let y = 0; y < size; y++) {
      const t = Math.abs(y / size - 0.5) * 2;          // 0 在中间、1 在上下边
      ctx.fillStyle = "rgba(120,104,80," + (0.30 * (1 - t)).toFixed(3) + ")";
      ctx.fillRect(0, y, size, 1);
    }

    // 土粒：深浅两种小点各撒一批，位置与深浅都来自确定性随机
    const rand = rng(20261010);
    for (let i = 0; i < 2600; i++) {
      const x = rand() * size;
      const y = rand() * size;
      const r = 0.5 + rand() * 1.7;
      const dark = rand() < 0.62;
      const a = (dark ? 0.16 : 0.13) * (0.35 + rand() * 0.65);
      ctx.fillStyle = dark
        ? "rgba(112,96,72," + a.toFixed(3) + ")"
        : "rgba(255,252,244," + a.toFixed(3) + ")";
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    }

    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;      // 颜色贴图走 sRGB（threejs-textures 的建议）
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    if (renderer && renderer.capabilities) {
      tex.anisotropy = renderer.capabilities.getMaxAnisotropy();  // 斜着看地表时更清楚
    }
    tex.needsUpdate = true;
    soilTexCache = tex;
    return tex;
  }

  /* 按垄数取一张地表纹理。
   *
   * 母版是共享的，但 **repeat / offset 是逐张的**：垄距现在跟着株行距走，
   * 每块田的垄数不同，纹理的 v 向平铺次数必须跟着变。直接改母版的 repeat 会让
   * 主视图和 8 格小图互相打架 —— 它们共用一个离屏 renderer 顺序出图，
   * 后写进去的那个值会把前一张的覆盖掉。
   *
   * clone 只复制参数：r152 之后 Texture 的图像数据挂在共享的 .source 上，
   * 副本不会把 256×256 重传一遍。按垄数缓存，行数一共就那么几十种，
   * 用完不销毁也不会一直涨。
   */
  const soilClones = new Map();
  function soilFor(renderer, rows) {
    const key = Math.max(1, rows | 0);
    let tex = soilClones.get(key);
    if (!tex) {
      tex = soilTexture(renderer).clone();
      // 平铺份数：一垄在 v 轴上占 RIDGE_INSET/(行数−1)，要让它正好等于纹理的一个
      // 周期，份数就取它的倒数（不是整数，RepeatWrapping 认）。
      const rep = key > 1 ? (key - 1) / RIDGE_INSET : 1;
      tex.repeat.set(1, rep);
      // 相位：纹理最暗的一线在 v' = 0.5，要把它压在**垄沟**上（与 groundYAt 的
      // sin 波谷对齐）。v = 0.5 − 世界z/边长；波谷在 z = −usable/2 + 每垄距/2，
      // 代进去解得 offset = 1 − rep/2 − (行数−1)/2。
      const off = 1 - rep / 2 - (key - 1) / 2;
      tex.offset.set(0, off - Math.floor(off));
      tex.needsUpdate = true;
      soilClones.set(key, tex);
    }
    return tex;
  }

  function supported() {
    try {
      const canvas = document.createElement("canvas");
      return !!(window.WebGLRenderingContext &&
                (canvas.getContext("webgl") || canvas.getContext("experimental-webgl")));
    } catch (e) {
      return false;
    }
  }

  /* ---------------------------------------------------------------- 几何工具 */

  /** 把若干带颜色的部件合并成一个几何体（逐顶点着色）。
   *
   * 不用 examples/jsm 的 BufferGeometryUtils：那是模块版，UMD 构建里没有。
   * 自己合并反而更简单 —— 每个部件先 toNonIndexed()（免去索引重映射），
   * 然后顺序拼接 position / normal，并给每个顶点刷上它所属部件的颜色。
   */
  function mergeParts(parts) {
    // src 留着是为了之后一起 dispose：toNonIndexed() 会另建一份几何，
    // 只 dispose 结果的话，每个部件的原件会一直挂在 GPU 上。植株在补间期间
    // 每帧重建一次，这点泄漏会被放大成几十倍的垃圾。
    const prepared = parts.map((p) => ({
      geom: p.geometry.index ? p.geometry.toNonIndexed() : p.geometry,
      color: new THREE.Color(p.color),
      src: p.geometry,
    }));

    let total = 0;
    for (const p of prepared) total += p.geom.attributes.position.count;

    const position = new Float32Array(total * 3);
    const normal = new Float32Array(total * 3);
    const color = new Float32Array(total * 3);

    let offset = 0;
    for (const p of prepared) {
      const n = p.geom.attributes.position.count;
      position.set(p.geom.attributes.position.array, offset * 3);
      if (p.geom.attributes.normal) normal.set(p.geom.attributes.normal.array, offset * 3);
      for (let i = 0; i < n; i++) {
        color[(offset + i) * 3] = p.color.r;
        color[(offset + i) * 3 + 1] = p.color.g;
        color[(offset + i) * 3 + 2] = p.color.b;
      }
      offset += n;
      if (p.src !== p.geom) p.src.dispose();
      p.geom.dispose();
    }

    const merged = new THREE.BufferGeometry();
    merged.setAttribute("position", new THREE.BufferAttribute(position, 3));
    merged.setAttribute("normal", new THREE.BufferAttribute(normal, 3));
    merged.setAttribute("color", new THREE.BufferAttribute(color, 3));
    return merged;
  }

  /** 一片带弧度的叶子。
   *
   * 不再是"一块平板"：沿叶长把中脉抬起一点，叶片就有了 V 形截面 ——
   * 光打在正面和背面上颜色不一样，远看才有叶子的样子；叶尖按 droop 弯下去。
   * 位置靠对「叶片切线角」积分得到，所以弯曲是连续的弧，而不是两段折线。
   *
   * droop 越大，起始上扬角越小、总弯曲越大：0 是嫩苗那样斜向上挺着，
   * 1 是完熟油菜那样整片垂下来。
   */
  function bladeGeometry({ length, width, droop, angle, at, color }) {
    const SEG = 5;    // 5 段够弯得顺了；再细分只是给每株多加几十个顶点
    const d = Math.max(0, Math.min(1, droop));
    const start = Math.max(-0.25, 0.70 - 1.4 * d);   // 起始方向（弧度，正 = 向上）
    const end = start - (0.75 + 1.9 * d);            // 叶尖方向
    const tangent = (t) => start + (end - start) * Math.pow(t, 1.35);

    const pos = [];
    const idx = [];
    let x = 0;
    let y = 0;
    for (let i = 0; i <= SEG; i++) {
      if (i > 0) {                       // 沿切线积分一步，得到弯曲的脊线
        const dt = 1 / SEG;
        const a = tangent((i - 0.5) / SEG);
        x += Math.cos(a) * length * dt;
        y += Math.sin(a) * length * dt;
      }
      const t = i / SEG;
      // 叶宽：基部有鞘（较窄）→ 中段最宽 → 叶尖收成尖
      const w = width * Math.sin(Math.PI * (0.12 + 0.86 * t)) * (1 - 0.12 * t);
      pos.push(x, y, -w / 2, x, y + w * 0.55, 0, x, y, w / 2);
    }
    for (let i = 0; i < SEG; i++) {
      const a = i * 3;
      const b = (i + 1) * 3;
      idx.push(a, b, a + 1, a + 1, b, b + 1);            // 左半片
      idx.push(a + 1, b + 1, a + 2, a + 2, b + 1, b + 2); // 右半片
    }

    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    g.rotateY(angle);
    g.translate(0, at, 0);
    return { geometry: g, color };
  }

  /**
   * 按 12 个参数造一株。参数由后端算好（7.3.5），前端只负责画 ——
   * **形态随进度怎么变**那件事在 src/farm/growth.py 里，两边不重复实现；
   * 这里只决定"长成什么模样"，即不同作物各自的轮廓（稻麦丛生、玉米顶穗、
   * 豆荚角果、棉铃……）。所以判据是 m.crop，而所有数值仍来自后端。
   */
  /** 倒伏程度（0~1）。穗越垂、枯叶越多，越像"熟到该收了"。
   *
   * 后端不传这个量，它是纯视觉推导：成熟后期整片朝一个方向歪下去，
   * 比只把颜色调黄更能让人一眼看出"这块地快能割了"。
   */
  function lodgingOf(m) {
    if (!m) return 0;
    const droop = Math.max(0, Math.min(1, m.earDroop || 0));
    const dry = Math.max(0, Math.min(1, m.dryLeafRatio || 0));
    return Math.max(0, Math.min(1, droop * 0.75 + (dry - 0.5) * 0.6));
  }

  function plantGeometry(m) {
    const parts = [];
    const crop = m.crop || "";
    const height = Math.max(0.12, (m.height || 10) / 100);   // cm → 米
    const droop = Math.max(0, Math.min(1, m.leafDroop || 0));
    const dry = Math.max(0, Math.min(1, m.dryLeafRatio || 0));

    // 穗/角果这类顶端结构的小工具：以底面为原点立一个锥，再倾倒、转方位
    const spikeAt = (radius, len, tilt, spin, at, color) => {
      const g = new THREE.ConeGeometry(radius, len, 6);
      g.translate(0, len / 2, 0);
      g.rotateZ(tilt);
      g.rotateY(spin || 0);
      g.translate(0, at, 0);
      parts.push({ geometry: g, color });
    };

    // 茎：下粗上细一点更自然（用两级圆柱近似）
    const stemR = 0.012 + height * 0.012;
    const lower = new THREE.CylinderGeometry(stemR * 0.8, stemR, height * 0.55, 6, 1, true);
    lower.translate(0, height * 0.275, 0);
    parts.push({ geometry: lower, color: m.stemColor });

    const upper = new THREE.CylinderGeometry(stemR * 0.5, stemR * 0.8, height * 0.45, 6, 1, true);
    upper.translate(0, height * 0.775, 0);
    parts.push({ geometry: upper, color: m.stemColor });

    // 分蘖：稻麦是丛生的，一株其实是"一丛"。补两根侧茎，田里立刻不像插了一排木棍。
    if (crop === "水稻" || crop === "小麦") {
      for (let k = 0; k < 2; k++) {
        const spin = 1.7 + k * 2.5;
        const h = height * (0.78 - 0.07 * k);
        const seg = new THREE.CylinderGeometry(stemR * 0.45, stemR * 0.75, h, 5, 1, true);
        seg.translate(0, h / 2, 0);
        seg.rotateZ(-(0.22 + 0.05 * k));
        seg.rotateY(spin);
        parts.push({ geometry: seg, color: m.stemColor });

        for (let j = 0; j < 2; j++) {
          parts.push(bladeGeometry({
            length: height * (0.46 - 0.10 * j),
            width: Math.max(0.016, height * 0.060) * (1 - 0.15 * j),
            droop: Math.min(1, droop + 0.10 + 0.15 * j),
            angle: spin + 0.9 + j * 2.2,
            at: h * (0.55 + 0.22 * j),
            color: dry > 0.4 ? DRY : m.leafColor,
          }));
        }
      }
    }

    // 叶：沿茎自下而上分布，越往上越短（下面的是老叶）
    const leafCount = Math.max(0, Math.min(20, m.leafCount || 0));
    const leafW = Math.max(0.016, height * (crop === "玉米" ? 0.075 : 0.060));
    // 枯叶的颜色：**干草黄**，不是灰褐。原来用的 #a1887f 在暖光下一压就偏灰粉，
    // 玉米地里那片粉紫色的叶子正是它 —— 干透的叶子是稻秆色，不是紫的。
    const DRY = "#bfa25c";
    for (let i = 0; i < leafCount; i++) {
      const t = leafCount === 1 ? 0.5 : i / (leafCount - 1);
      const at = height * (0.12 + 0.78 * t);
      // 枯叶按比例挑掉一部分：同一株里哪几片枯了不重要，整体比例对就行。
      // 用确定性的伪随机（同一个 i 每次结果一样），免得每帧重建时叶子闪来闪去。
      const isDry = ((i * 37) % 100) / 100 < dry;
      parts.push(bladeGeometry({
        length: height * (LEAF_LEN[crop] || DEFAULT_LEAF_LEN) * (1.12 - 0.34 * t),
        width: leafW * (1 - 0.30 * t),
        droop: Math.min(1, droop + 0.22 * t),   // 越靠上的叶越年轻，反而没那么垂
        angle: i * 2.399,                       // 黄金角，叶片不会叠在一起
        at,
        color: isDry ? DRY : m.leafColor,
      }));
    }
    // 枯叶比例很高时（油菜完熟）再补几片掉在茎上的枯叶，避免"光杆"
    if (dry > 0.5) {
      for (let i = 0; i < 3; i++) {
        parts.push(bladeGeometry({
          length: height * 0.30, width: leafW * 0.55,
          droop: 0.95, angle: 1.1 + i * 2.0, at: height * (0.2 + 0.2 * i),
          color: DRY,
        }));
      }
    }

    // 穗 / 角果 / 棉铃：进度到了才有（后端用生育期表判定，与阶段名永远一致）
    if (m.hasEar) {
      const size = Math.max(0, Math.min(1, m.earSize || 0));
      const tip = Math.max(0, Math.min(1, m.earDroop || 0));

      if (crop === "玉米") {
        // 玉米两样都有：顶上散开的雄穗，腰上挂的棒子。只画一个锥不是玉米。
        for (let i = 0; i < 6; i++) {
          spikeAt(0.010 + 0.008 * size, 0.09 + 0.09 * size,
                  0.50 + 0.18 * (i % 3), i * 1.05, height, m.earColor);
        }
        const cobLen = Math.max(0.09, height * 0.24 * (0.5 + 0.5 * size));
        spikeAt(0.030 + 0.020 * size, cobLen, -1.15, 0.7, height * 0.60, m.earColor);
      } else if (crop === "大豆") {
        // 大豆：一簇短短的豆荚，鼓鼓的，斜垂在茎上
        for (let i = 0; i < 5; i++) {
          spikeAt(0.016 + 0.010 * size, 0.07 + 0.06 * size,
                  1.35 + 0.10 * (i % 2), i * 1.257, height * (0.45 + 0.10 * i), m.earColor);
        }
      } else if (crop === "油菜") {
        // 油菜：长角果，细长、斜指向上
        for (let i = 0; i < 5; i++) {
          spikeAt(0.008 + 0.006 * size, 0.10 + 0.10 * size,
                  0.55 + 0.12 * (i % 2), i * 1.257, height * (0.55 + 0.09 * i), m.earColor);
        }
      } else if (crop === "棉花") {
        // 棉花：棉铃是一团一团的，用小球而不是锥
        for (let i = 0; i < 4; i++) {
          const boll = new THREE.IcosahedronGeometry(0.032 + 0.022 * size, 1);
          const a = i * 1.571;
          boll.translate(Math.cos(a) * height * 0.16, height * (0.42 + 0.14 * i),
                         Math.sin(a) * height * 0.16);
          parts.push({ geometry: boll, color: m.earColor });
        }
      } else {
        // 稻穗 / 麦穗：一根带垂头的穗（earDroop 越大垂得越低）
        spikeAt(0.016 + 0.022 * size, Math.max(0.06, height * 0.22 * (0.45 + 0.55 * size)),
                -tip * 1.1, 0, height, m.earColor);
      }
    }

    return mergeParts(parts);
  }

  /* ---------------------------------------------------------------- 精简轨道控制器 */

  /** 按球坐标把相机摆到 state 指定的位置。
   *
   * 抽成独立函数是因为离屏快照（createSnapshotter）也要摆相机，但它**没有交互**，
   * 不能走 attachOrbit。相机公式只留这一处，免得两边各写一遍走岔。
   */
  function updateCamera(camera, state) {
    const { theta } = state;
    const target = state.target;

    // 3D 那一格的高度由右侧面板决定（底边要和「干过啥」齐平），面板比一屏还长时，
    // 这一格就成了**竖长条**。两个后果，都在这里修：
    //
    // ① 机位距离是按"地块边长 × radiusScale"定的，那个距离默认视口是横的。
    //    42° 是**竖直**视场角，水平能看到的宽度 = 2·R·tan(fov/2)·aspect ——
    //    视口一窄，左右就先不够，地的两个角被裁出画面。
    //    该让出多少：地是方的、相机又总站在一个角上看，横里要盛下的是**对角线**
    //    （≈1.41 × 边长），加一成余量 → 1.55 × 边长。拿它除以实际的水平视野，
    //    就是这一刻需要的距离；再除以本来定的距离，得到"要退几倍"。
    //    乘在 state.radius 上而不是取 max，是为了保住滚轮缩放：退得比 fit 远时
    //    用户照样能拉近，只是拉不过"刚好装下整块地"这条线。
    // ② 竖屏里 45° 的俯角会变成"站在田边看一堵植株墙"，看不见这是一块地。
    //    抬高俯角往俯视走，垄和地块的形状才看得出来。
    const aspect = camera.aspect || 1;
    const narrow = Math.max(0, 1 - Math.min(1, aspect));   // 0 = 横屏，越大越竖
    const tanY = Math.tan((camera.fov * Math.PI) / 360) || 0.38;
    const side = state.radius / (state.radiusScale || 1.5);  // 反推地块边长
    const needX = (side * 1.55) / Math.max(0.05, 2 * tanY * aspect);
    const fit = Math.max(1, needX / Math.max(state.radius, RADIUS_MIN));
    const radius = state.radius * fit;
    const phi = Math.min(PHI_MAX, state.phi + narrow * 0.8);

    camera.position.set(
      target.x + radius * Math.sin(phi) * Math.sin(theta),
      target.y + radius * Math.cos(phi),
      target.z + radius * Math.sin(phi) * Math.cos(theta)
    );
    camera.lookAt(target.x, target.y, target.z);
  }

  function attachOrbit(camera, dom, state) {
    let dragging = false;
    let lastX = 0;
    let lastY = 0;

    const apply = () => updateCamera(camera, state);

    function onDown(e) {
      dragging = true;
      lastX = e.clientX;
      lastY = e.clientY;
      dom.setPointerCapture && dom.setPointerCapture(e.pointerId);
      dom.classList.add("is-dragging");
    }

    function onMove(e) {
      if (!dragging) return;
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      state.theta -= dx * 0.006;
      // 夹住俯角：不让相机钻到地下，也不让它正对天顶
      state.phi = Math.min(PHI_MAX, Math.max(PHI_MIN, state.phi - dy * 0.005));
      apply();
      state.render();
      // 拖动时不要选中页面文字
      e.preventDefault();
    }

    function onUp(e) {
      dragging = false;
      dom.classList.remove("is-dragging");
      dom.releasePointerCapture && e.pointerId !== undefined &&
        dom.hasPointerCapture && dom.hasPointerCapture(e.pointerId) &&
        dom.releasePointerCapture(e.pointerId);
    }

    function onWheel(e) {
      e.preventDefault();
      // 按当前距离比例缩放：远处步子大、近处步子小，手感才均匀
      state.radius = Math.min(RADIUS_MAX,
        Math.max(RADIUS_MIN, state.radius * (1 + Math.sign(e.deltaY) * 0.12)));
      apply();
      state.render();
    }

    dom.addEventListener("pointerdown", onDown);
    dom.addEventListener("pointermove", onMove);
    dom.addEventListener("pointerup", onUp);
    dom.addEventListener("pointercancel", onUp);
    dom.addEventListener("pointerleave", onUp);
    // passive: false —— 否则 preventDefault 无效，滚轮会连页面一起滚
    dom.addEventListener("wheel", onWheel, { passive: false });

    // 双指缩放（手机）
    let pinch = null;
    dom.addEventListener("touchstart", (e) => {
      if (e.touches.length === 2) {
        pinch = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY);
      }
    }, { passive: true });
    dom.addEventListener("touchmove", (e) => {
      if (pinch && e.touches.length === 2) {
        const d = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY);
        state.radius = Math.min(RADIUS_MAX, Math.max(RADIUS_MIN, state.radius * (pinch / d)));
        pinch = d;
        apply();
        state.render();
      }
    }, { passive: true });

    apply();
  }

  /* ---------------------------------------------------------------- 场景 */

  /** 建一个 3D 视图。
   *
   * opts（全部可选；不传就是主视图原来的那套行为）：
   *   interactive   默认 true —— false 时不挂轨道控制器（离屏快照用，它没有鼠标）
   *   pixelRatio    默认 min(devicePixelRatio, 2)；离屏快照传 1 省一半像素
   *   antialias     默认 true
   *   shadowMapSize 默认 1024（缩略图 512 足够，主视图按机器给 2048）
   *   camera        { theta, phi, radiusScale } —— 起始机位
   *   areaMu        () => number —— 当前地块亩数（闭包，切田时读得到最新值）
   */
  function create(canvas, opts) {
    opts = opts || {};
    const renderer = new THREE.WebGLRenderer({
      canvas, antialias: opts.antialias !== false, alpha: false,
    });
    renderer.setPixelRatio(opts.pixelRatio || Math.min(window.devicePixelRatio || 1, 2));
    // 软阴影：整块田只有 1 个 InstancedMesh 要投影，代价是一次额外的绘制，
    // 换来的是植株之间互相遮挡的层次 —— 没有影子时几百株会像贴纸一样浮在地面上。
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    // 电影级色调映射：直射光下的高光不再"顶到 255 就一片白"，
    // 而是像胶片一样滚过去，土色、叶色在高光区还能分出层次。
    // 曝光压在 1.05 —— ACES 会把暗部整体抬一点（0.2 的线性亮度约抬到 0.3），
    // 所以下面**同时**把环境光压暗、把直射光加强，明暗比才拉得开；
    // 只开色调映射不动光，画面会变成灰蒙蒙的一层。
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(SKY_FOG);

    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 200);

    // 天空：一个内表面着色的球，用逐顶点颜色做出上深下浅的过渡
    // （CSS 里的渐变被项目规则禁掉了，而这里是 WebGL 的顶点色，不是 CSS 渐变）
    // 低空再压一条**暖白雾带**：地平线附近最浓、往上迅速化开。
    // 地表有 scene.fog（同一个 SKY_FOG）把田边融进天色，若天空这边是干净的三段色，
    // 天地交界就还是"一刀切"；这条雾带把两侧接上。
    const skyGeo = new THREE.SphereGeometry(60, 24, 18);
    const skyColors = new Float32Array(skyGeo.attributes.position.count * 3);
    const top = new THREE.Color(SKY_TOP);
    const bottom = new THREE.Color(0xe8f1f7);
    const haze = new THREE.Color(SKY_HORIZON);
    for (let i = 0; i < skyGeo.attributes.position.count; i++) {
      const y = skyGeo.attributes.position.getY(i);
      const t = Math.min(1, Math.max(0, (y / 60 + 1) / 2));
      const c = bottom.clone().lerp(top, t);
      // 雾带中心压在 t≈0.46（就在地平线上下），半宽 0.24；平方一下边缘收得更快
      const h = Math.max(0, 1 - Math.abs(t - 0.46) / 0.24);
      c.lerp(haze, 0.72 * h * h);
      skyColors[i * 3] = c.r; skyColors[i * 3 + 1] = c.g; skyColors[i * 3 + 2] = c.b;
    }
    skyGeo.setAttribute("color", new THREE.BufferAttribute(skyColors, 3));
    scene.add(new THREE.Mesh(skyGeo, new THREE.MeshBasicMaterial({
      vertexColors: true, side: THREE.BackSide, depthWrite: false,
      fog: false,     // 天空不该被雾本身染一遍，否则地平线以上会一起发灰
    })));

    // 天光偏冷、地面反光偏黄绿：上白下草绿，比单一环境光更有"下地"的感觉。
    // 1.0 → 0.70：环境光就是"影子里的亮度"，它越高明暗比越平、越像塑料。
    // 直射光同时从 1.32 提到 1.95，两者的比值从 1.3:1 拉到 2.8:1 ——
    // 这一步才是"光影不再像塑料"的关键，色调映射只是让高光不糊。
    scene.add(new THREE.HemisphereLight(0xffffff, 0x6b7f5a, 0.70));
    const sun = new THREE.DirectionalLight(0xffedc9, 1.95);
    // 光源距离随地块边长走：地块大到十几米时，光源还停在 12 米处，
    // 阴影相机的近远平面会把地的一角裁掉（那里的植株就没有影子了）。
    let sunDist = 12.3;
    sun.position.set(SUN_DIR.x * sunDist, SUN_DIR.y * sunDist, SUN_DIR.z * sunDist);
    sun.castShadow = true;
    const shadowSize = opts.shadowMapSize || 1024;
    sun.shadow.mapSize.set(shadowSize, shadowSize);
    // 阴影相机视锥按地块大小收紧：原来固定 ±10，小地块（2.4 米见方）下
    // 十米的范围里九成是空气，影子全糊在少数几个 texel 上。
    const sc = sun.shadow.camera;
    const fitShadow = (side) => {
      const r = side * 0.82 + 1.2;
      sc.left = -r; sc.right = r; sc.top = r; sc.bottom = -r;
      sc.near = 1; sc.far = 80;
      sc.updateProjectionMatrix();
      sunDist = Math.max(12.3, side * 1.7);
      sun.position.set(SUN_DIR.x * sunDist, SUN_DIR.y * sunDist, SUN_DIR.z * sunDist);
    };
    fitShadow(5.2);                       // 先按一亩地摆好，铺地时再按实际边长调
    // bias 消自阴影的摩尔纹，normalBias 消薄片叶片上的条纹 ——
    // 只调 bias 的话叶子会因为太薄而漏光，调不出干净的结果。
    sun.shadow.bias = -0.0006;
    sun.shadow.normalBias = 0.02;
    scene.add(sun);

    const cam = opts.camera || {};
    const state = {
      theta: cam.theta != null ? cam.theta : Math.PI * 0.25,
      phi: cam.phi != null ? cam.phi : Math.PI / 4,   // 7.6.1：相机俯视 45°
      radius: 9,
      // 机位距离 = 地块边长 × 这个系数。缩略图比主视图退得远一点，
      // 好让田埂和一条地平线一起入镜，一眼看得出"这是一块地"。
      radiusScale: cam.radiusScale || 1.5,
      targetY: cam.targetY || 0.5,
      target: new THREE.Vector3(0, 0.6, 0),
      render: () => {},
    };

    let plantMesh = null;
    let groundGroup = null;
    let fieldId = "";
    let tween = null;
    // 当前这块地的几何（边长 + 垄位）。**补间时只重建植株、不重铺地**，
    // 所以要有个地方记住当初栽下去用的是哪套垄 —— 否则补间那几帧里，
    // 植株会踩着另一套垄的高度上下跳。
    let fieldGeom = { side: fieldSide(1), ridge: ridgePlan(fieldSide(1), 9) };

    function clearField() {
      if (plantMesh) {
        scene.remove(plantMesh);
        plantMesh.geometry.dispose();
        plantMesh.material.dispose();
        plantMesh = null;
      }
      if (groundGroup) {
        scene.remove(groundGroup);
        groundGroup.traverse((o) => {
          if (o.geometry) o.geometry.dispose();
          if (o.material) o.material.dispose();
        });
        groundGroup = null;
      }
      if (tween) {
        cancelAnimationFrame(tween.raf);
        tween = null;
      }
    }

    /** 按亩数铺一块地。
     *
     * 不再是"一块色板"：地表按垄距做出正弦起伏（垄高沟低），再叠一层**缓坡噪声**，
     * 顶峰可以差出一拃高 —— 一块整得不匀的田本来就不是镜面平的。
     * 垄距与株行距一致（见 ridgePlan），行栽的走向才看得出来。
     * 色斑同样走噪声：低频给出成片的深浅，高频给出细碎的斑驳，
     * 而不是"每个顶点一个随机数"那种均匀的颗粒。
     *
     * 全部**确定性**：同样的坐标每次结果一样 —— 用 Math.random 的话，
     * 每次切田块重建地表都会"闪"一下。
     */
    function buildGround(areaMu, rows) {
      const group = new THREE.Group();
      const side = fieldSide(areaMu);
      const ridge = ridgePlan(side, rows || 9);
      const SEG = 64;                       // 网格分辨率：缓坡要有够多顶点才立得起来

      const plane = new THREE.PlaneGeometry(side, side, SEG, SEG);
      const pos = plane.attributes.position;
      const colors = new Float32Array(pos.count * 3);
      const base = new THREE.Color("#8d7a5e");
      const dark = new THREE.Color("#6b5941");     // 沟
      const light = new THREE.Color("#a38d6b");    // 垄

      for (let i = 0; i < pos.count; i++) {
        // 旋转前的 (x, y) 对应世界 (x, −z)：rotateX(−90°) 把局部 +Y 送到世界 −Z。
        // 先把世界坐标还原出来，再统一交给 groundYAt —— 地表的起伏、土块的落点、
        // 植株的根，从此都在同一个坐标系里，不会再差一个符号。
        const wx = pos.getX(i);
        const wz = -pos.getY(i);
        const wave = Math.sin(((wz - ridge.z0) / ridge.every) * Math.PI * 2);
        pos.setZ(i, groundYAt(wx, wz, ridge));     // 旋转后就是高度

        const patch = fbm(wx * 0.55 + 3.1, wz * 0.55 + 5.9);
        const c = base.clone()
          .lerp(wave > 0 ? light : dark, 0.18 + 0.24 * Math.abs(wave))
          .lerp(base, 0.45 * patch);
        colors[i * 3] = c.r;
        colors[i * 3 + 1] = c.g;
        colors[i * 3 + 2] = c.b;
      }
      plane.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      plane.computeVertexNormals();
      plane.rotateX(-Math.PI / 2);

      // map = 程序化灰度纹理（土粒 + 垄向暗带），与顶点色相乘：
      // 顶点色给出"这块地是什么土色"，纹理给出"土粒有多粗"。
      const soil = new THREE.Mesh(plane, new THREE.MeshLambertMaterial({
        vertexColors: true, map: soilFor(renderer, rows || 9),
      }));
      soil.receiveShadow = true;
      group.add(soil);

      // 田埂：四条细长盒子，比地表高一档，地块边界一眼看得出。
      // 盒体一直埋到田外那层地（SURROUND_Y）以下 —— 不埋的话，田面与外围之间
      // 会露出一圈空隙，从上往下看就是一道"缝"。
      const ridgeMat = new THREE.MeshLambertMaterial({ color: "#6d5c45" });
      const t = 0.07;
      for (const [w, d, x, z] of [
        [side + t * 2, t, 0, side / 2], [side + t * 2, t, 0, -side / 2],
        [t, side + t * 2, side / 2, 0], [t, side + t * 2, -side / 2, 0],
      ]) {
        const ridge2 = new THREE.Mesh(new THREE.BoxGeometry(w, 0.34, d), ridgeMat);
        ridge2.position.set(x, 0.09 - 0.17, z);
        ridge2.receiveShadow = true;
        group.add(ridge2);
      }

      // 散落的土块、断茬与杂草：地表本身只是一张起伏的网，近看还是"平"的
      group.add(buildDebris(side, ridge));

      // 田外的地：没有它，这块田就是浮在白色里的一张褐色菱形
      group.add(buildSurround(side));

      // 阴影视锥跟着地块走（小地收窄、大地放宽），光源也一起退到够远
      fitShadow(side);

      // 雾：**起点必须落在田的远角之外**，否则远处那半块田会先被雾洗白。
      // 相机站在半径 side×1.5 的对角上，田的远角离相机约 side×2.45 ——
      // 所以近平面给到 side×2.6，田里一丝雾都不沾；再往外渐渐融进天色。
      scene.fog = new THREE.Fog(SKY_FOG, side * 2.6, side * 6);

      scene.add(group);
      groundGroup = group;
      fieldGeom = { side: side, ridge: ridge };
      return side;
    }

    /** 田外的地：一片延伸到雾里的、邻近的田。
     *
     * 原来田块是**孤零零浮在白色里的** —— 四周什么都没有，天地之间只有一块褐色
     * 菱形。这是"假"最直接的一条：真实场景里，视线从田里往外总有东西可看。
     *
     * 只做"一圈纯色地板"还不够：相机俯角 45°、视场 42°，**整幅画面里根本
     * 看不到天**，所以背景不能指望雾去糊 —— 糊出来就是一片灰绿的浆糊。
     * 这里给的是**真的有起伏、有色块的邻田**：格状噪声量化成一块一块颜色不同的
     * 方田（深绿还在长的、黄绿快熟的、土黄刚翻过的），格子边界压一条暗线当田埂，
     * 再叠上粗尺度的缓坡。于是画面从近处的土块杂草、到田外的邻田、再到雾里的
     * 地平线，是一层层退过去的，而不是"田 → 白"。
     */
    function buildSurround(side) {
      const R = Math.min(Math.max(side * 4, 24), 58);
      const SEG = 40;
      const plane = new THREE.PlaneGeometry(R * 2, R * 2, SEG, SEG);
      const pos = plane.attributes.position;
      const colors = new Float32Array(pos.count * 3);
      // 邻田的调色板 —— 真实农村从地里望出去，就是一块块颜色不同的方田。
      // 比田里的作物**压暗一档**：它是背景，不该比主角还亮，否则看着像贴在
      // 后面的一块绿布。（田里的光照有 ACES + 曝光 1.05，这里不做任何补偿。）
      const PALETTE = ["#5f753d", "#788742", "#8a7c4a", "#697853", "#546a39"];
      const c = new THREE.Color();
      for (let i = 0; i < pos.count; i++) {
        const x = pos.getX(i);
        const z = -pos.getY(i);
        const d = Math.hypot(x, z);

        // 缓坡：**贴着田的那圈必须是平的**，否则坡会把田埂顶起来、从田面里穿出去。
        // 田的半对角是 side×0.707，所以从 side×0.78 起坡；k² 让坡脚是渐进的，
        // 不会在某个半径上突然折出一道埂。起伏 ±0.85 米 —— 是"远处的地有高有低"，
        // 不是"山"。
        const k = Math.min(1, Math.max(0, (d - side * 0.78) / (side * 0.9)));
        pos.setZ(i, (fbm(x * 0.3 + 5.3, z * 0.3 + 2.7) - 0.5) * 1.7 * k * k);

        // 田块：把噪声量化成"一格一格"，格子边缘压一条暗线当田埂。
        // 边界是有机的（噪声等值线）而不是直线 —— 航拍看下去很像田畴的形状。
        const t = fbm(x * 0.14 + 21.7, z * 0.14 + 4.3) * 4.35;
        const frac = t - Math.floor(t);
        c.set(PALETTE[Math.max(0, Math.min(4, Math.floor(t)))]);
        c.multiplyScalar(1 - 0.30 * Math.max(0, 1 - Math.min(frac, 1 - frac) * 7));
        // 每块田里再有一点细噪声，免得一个格子是一整片纯色
        c.multiplyScalar(0.88 + 0.18 * fbm(x * 0.9 + 31.1, z * 0.9 + 17.3));
        colors[i * 3] = c.r;
        colors[i * 3 + 1] = c.g;
        colors[i * 3 + 2] = c.b;
      }
      plane.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      plane.computeVertexNormals();
      plane.rotateX(-Math.PI / 2);
      const mesh = new THREE.Mesh(plane, new THREE.MeshLambertMaterial({ vertexColors: true }));
      // 压在田面最低点（−8.5cm，见 groundYAt）以下：田面自己再怎么起伏也不会
      // 被它穿出来；田埂的盒子又埋得更深，所以两者之间不留缝。
      mesh.position.y = -0.12;
      mesh.receiveShadow = false;   // 它在阴影相机之外，开接收只是白搭
      return mesh;
    }

    /** 地面上的零碎：土块、断茬、小石子，外加一丛丛杂草。
     *
     * 两层 InstancedMesh —— 土块一层、杂草一层，一共 2 次 draw call。
     * 矩阵都在构建期算好，之后每帧开销是 0；形状各只有一个（一个低模土块、
     * 一丛三片草），靠**逐实例**的缩放、旋转、颜色拉开差别，
     * 看起来是散在地里的杂物，而不是一片复制粘贴。
     *
     * 高度一律走 groundYAt —— 原来这里用的是世界 z 的 sin，而地表用的是旋转前的
     * 局部 y，两者差一个符号，土块因此陷进垄背、浮在垄沟上方。
     *
     * 都不投影（castShadow = false）：几厘米的东西投影看不见，白搭一次 pass。
     */
    function buildDebris(side, ridge) {
      const group = new THREE.Group();
      const rand = rng(0x5eed1 + Math.round(side * 1000) * 7);
      const range = side * 0.5 - 0.12;                // 收进田埂里面一点
      const dummy = new THREE.Object3D();
      const color = new THREE.Color();

      // ---- 第一层：土块 / 断茬 / 石子
      const count = Math.round(Math.min(160, Math.max(36, side * side * 2.2)));
      const clods = new THREE.InstancedMesh(
        new THREE.IcosahedronGeometry(0.05, 0),      // 12 顶点的小土块
        new THREE.MeshLambertMaterial(),
        count
      );
      clods.receiveShadow = true;
      clods.castShadow = false;
      for (let i = 0; i < count; i++) {
        const x = (rand() * 2 - 1) * range;
        const z = (rand() * 2 - 1) * range;
        // 稍微埋进土里：只露出上半截，才不会像撒上去的一把弹珠
        const y = groundYAt(x, z, ridge) + 0.02 * (0.25 + rand() * 0.75);
        const flat = rand() < 0.45;                   // 扁的当石子，细长的当断茬
        const s = 0.55 + rand() * 1.25;
        dummy.position.set(x, y, z);
        dummy.scale.set(s * (flat ? 1.25 : 0.6), s * (flat ? 0.5 : 1.5), s * (flat ? 0.95 : 0.7));
        dummy.rotation.set(rand() * Math.PI, rand() * Math.PI * 2, rand() * Math.PI);
        dummy.updateMatrix();
        clods.setMatrixAt(i, dummy.matrix);
        // 颜色在土色与石灰色之间取，亮度也抖一下，避免同一批土块一个色。
        // 明度压在 0.14~0.30：土块是**从地里翻出来的**，不是撒在地面上的白石子；
        // 原来给到 0.56，朝阳的那些面直接反光成一片白的。
        const k = rand();
        color.setHSL(0.085 - 0.015 * k, 0.10 + 0.06 * k, 0.14 + 0.16 * k);
        clods.setColorAt(i, color);
      }
      clods.instanceMatrix.needsUpdate = true;
      if (clods.instanceColor) clods.instanceColor.needsUpdate = true;
      group.add(clods);

      // ---- 第二层：杂草丛
      // 地里本来就有草，而且总是田边、垄头和漏播的那几处长势最旺。
      // 先挑几处"草窝"再往里撒，比满地均匀撒一遍真得多。
      const weedGeo = mergeParts([
        bladeGeometry({ length: 0.20, width: 0.012, droop: 0.10, angle: 0.0, at: 0, color: "#5f7f3c" }),
        bladeGeometry({ length: 0.15, width: 0.010, droop: 0.35, angle: 2.2, at: 0, color: "#7d9a4e" }),
        bladeGeometry({ length: 0.17, width: 0.011, droop: 0.20, angle: 4.3, at: 0, color: "#6b8b42" }),
      ]);
      const weedCount = Math.round(Math.min(90, Math.max(18, side * side * 1.1)));
      const weeds = new THREE.InstancedMesh(
        weedGeo,
        new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide }),
        weedCount
      );
      weeds.castShadow = false;
      weeds.receiveShadow = true;
      const patches = 3 + (rand() < 0.5 ? 1 : 0);
      const centers = [];
      for (let p = 0; p < patches; p++) {
        const a = rand() * Math.PI * 2;
        // 六成的草窝贴着田边（那里人踩得少、除草也糊弄），其余留在田中间
        const rr = rand() < 0.6 ? range * (0.7 + rand() * 0.28) : range * rand() * 0.8;
        centers.push([Math.cos(a) * rr, Math.sin(a) * rr]);
      }
      for (let i = 0; i < weedCount; i++) {
        let x, z;
        if (rand() < 0.7) {
          const c = centers[(i * 7) % centers.length];
          const a = rand() * Math.PI * 2;
          const rr = rand() * side * 0.10;
          x = c[0] + Math.cos(a) * rr;
          z = c[1] + Math.sin(a) * rr;
        } else {
          x = (rand() * 2 - 1) * range;
          z = (rand() * 2 - 1) * range;
        }
        const s = 0.6 + rand() * 0.9;
        dummy.position.set(x, groundYAt(x, z, ridge) - 0.01, z);
        dummy.scale.set(s * (0.85 + rand() * 0.3), s * (0.7 + rand() * 0.9), s);
        dummy.rotation.set(0, rand() * Math.PI * 2, 0);
        dummy.updateMatrix();
        weeds.setMatrixAt(i, dummy.matrix);
        // 草色也抖：新草偏黄绿、老草偏深，全一色看着像塑料草皮。
        // 整体压暗到 0.68~1.02 —— 杂草比作物矮、又常在半阴里，
        // 亮成一片荧光绿就立刻穿帮。
        const g = 0.68 + rand() * 0.34;
        color.setRGB(g * 0.95, g, g * 0.85);
        weeds.setColorAt(i, color);
      }
      weeds.instanceMatrix.needsUpdate = true;
      if (weeds.instanceColor) weeds.instanceColor.needsUpdate = true;
      group.add(weeds);

      return group;
    }

    function buildPlants(geometry, count, side, lodging, grid, ridge) {
      const lodge = Math.max(0, Math.min(1, lodging || 0));
      // DoubleSide：叶片是两张单面片拼的薄壳，只画正面的话从上方俯视会看到"镂空"，
      // 而俯视恰恰是默认相机的角度。代价是填充率翻倍，但每片叶子只有 24 个顶点。
      const material = new THREE.MeshLambertMaterial({
        vertexColors: true, side: THREE.DoubleSide,
      });
      // 叶片透光：真实叶片很薄，逆光看时整片是亮的。只靠 Lambert 的正面漫反射，
      // 叶子永远是塑料片。这里往片元着色器里塞一小段背光散射项：
      // 法线背对日头的那一面，按背光程度补一点暖色 —— 正是叶隙透光的样子。
      // 用 onBeforeCompile 而不是再加一层 BackSide 材质：整块田仍然只画一遍。
      const uni = makeLeafUniforms();
      material.onBeforeCompile = (shader) => {
        shader.uniforms.uSunDir = uni.uSunDir;
        shader.uniforms.uLeafTint = uni.uLeafTint;
        shader.uniforms.uLeafTrans = uni.uLeafTrans;
        shader.fragmentShader = shader.fragmentShader
          .replace("#include <common>", [
            "#include <common>",
            "uniform vec3 uSunDir;",
            "uniform vec3 uLeafTint;",
            "uniform float uLeafTrans;",
          ].join("\n"))
          .replace("#include <opaque_fragment>", [
            "#include <opaque_fragment>",
            "{",
            "  vec3 sunView = normalize( ( viewMatrix * vec4( uSunDir, 0.0 ) ).xyz );",
            "  float backDot = max( 0.0, -dot( normalize( vNormal ), sunView ) );",
            "  gl_FragColor.rgb += uLeafTint * uLeafTrans * backDot * backDot * diffuseColor.rgb;",
            "}",
          ].join("\n"));
      };
      // 同一个缓存键：这个材质的着色器只有这一种改法，不必让 three 为每次重建
      // 都重新编译一份 program（补间期间每帧都在新建材质）。
      material.customProgramCacheKey = () => "farm3d-leaf";
      const mesh = new THREE.InstancedMesh(geometry, material, count);
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      mesh.castShadow = true;
      mesh.receiveShadow = true;

      const cols = grid.cols;
      const rows = grid.rows;
      const usable = side * RIDGE_INSET;
      // 横向上留出半个株距的余量给"隔行错开"用（见下面的 x），所以除的是 cols
      // 而不是 cols−1 —— 不然错开的那几行会伸出到田埂上，植株像是从埂上长出来的。
      const dx = cols > 1 ? usable / cols : 0;
      const dz = rows > 1 ? usable / (rows - 1) : 0;

      // 逐株的形态抖动走一条**确定性**随机序列（种子只看株数）：补间期间每帧重建，
      // 序列一样 → 形态不会跳；换了田块株数变了才重新洗牌。
      const jit = rng(0x1f2e3d + count);
      const dummy = new THREE.Object3D();
      const tint = new THREE.Color();
      let i = 0;
      for (let r = 0; r < rows && i < count; r++) {
        for (let c = 0; c < cols && i < count; c++) {
          const x = -usable / 2 + dx * (0.5 + (r % 2) * 0.5) + c * dx;  // 隔行错开半株，像插秧
          const z = -usable / 2 + r * dz;                              // 行压在垄背上（见 ridgePlan）
          dummy.position.set(x, groundYAt(x, z, ridge) - 0.02, z);
          // 长势：整块地里有**成片**的高矮差（肥力不均、水头远近），
          // 不是逐株乱抖。同一片里的植株一起高一起矮，才像一块真的田。
          const lush = fbm(x * 0.30 + 41.7, z * 0.30 + 12.3);          // 0~1，斑块尺度约 3 米
          // 每株还独立高矮胖瘦：同一个几何体复制几百遍，形状一样不要紧，
          // "一样高、一样宽、都朝着同一个方向"才是最刺眼的那种假。
          const tall = (0.80 + jit() * 0.42) * (0.86 + 0.28 * lush);   // 株高约 0.69 ~ 1.34
          const wide = 0.88 + jit() * 0.24;      // 株幅 0.88 ~ 1.12
          dummy.scale.set(wide, tall, wide * (0.92 + jit() * 0.16));
          // 微倾：没有两株是笔直站着的。幅度压在 6° 以内，再大就像被风刮倒了。
          const leanX = (jit() - 0.5) * 0.20;
          const leanZ = (jit() - 0.5) * 0.20;
          dummy.rotation.set(leanX, jit() * Math.PI * 2, leanZ);
          if (lodge) {
            // 倒伏：整片朝同一个方向歪（像被风雨压过），每株的幅度再抖一下。
            // 都在建矩阵时算完，不进渲染循环，每帧开销是 0。
            const k = lodge * (0.30 + jit() * 0.13);
            dummy.rotation.set(k * 0.5, dummy.rotation.y, -k);
          }
          dummy.updateMatrix();
          mesh.setMatrixAt(i, dummy.matrix);
          // 颜色深浅：整体明暗抖 0.82 ~ 1.18，红绿各自再偏一点 ——
          // 一色到底的田看着就是草坪。用 instanceColor：它和顶点色相乘，
          // 不需要把几何体按颜色拆成多份，仍然一次 draw call。
          const k = (0.82 + jit() * 0.36) * (0.86 + 0.28 * lush);
          tint.setRGB(k * (0.96 + jit() * 0.08), k, k * (0.90 + jit() * 0.10));
          mesh.setColorAt(i, tint);
          i++;
        }
      }
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      scene.add(mesh);
      plantMesh = mesh;
    }

    /** 株数 = 按**该作物的株行距**铺满这块地能放几行几列。
     *
     * 不是「亩数 × 常数」：那样亩数一变，株行距跟着变，同一种作物在 1 亩地和
     * 5 亩地上的疏密看起来完全两回事。按株行距算，**亩数变大时株行距不变**，
     * 只是行列变多 —— 这正是真实田块的样子。
     */
    function plantCountFor(areaMu, crop) {
      const usable = fieldSide(areaMu) * RIDGE_INSET;
      const spacing = ROW_SPACING[crop] || DEFAULT_ROW_SPACING;
      const n = Math.round(Math.pow(usable / spacing + 1, 2));
      return Math.max(12, Math.min(MAX_PLANTS, n));
    }

    /** 设置/切换模型。
     *
     * 同一块田的进度变了 → 做补间（7.6.2.6「成熟度动画补间」），
     * 换个田块 → 直接重建，不做补间（那是"切了个对象"，不是"长大了"）。
     */
    function setModel(model) {
      if (!model) return;
      const sameField = model.field_id === fieldId && plantMesh;
      const areaMu = (opts.areaMu && opts.areaMu()) || 1;

      if (!sameField) {
        clearField();
        const count = plantCountFor(areaMu, model.crop);
        const grid = gridFor(count);
        // 先由株数定行数，再用行数定垄距 —— 垄和行才是同一套
        const side = buildGround(areaMu, grid.rows);
        buildPlants(plantGeometry(model), count, side, lodgingOf(model), grid, fieldGeom.ridge);
        state.radius = Math.max(RADIUS_MIN, side * state.radiusScale);
        state.target.set(0, Math.max(0.3, (model.height || 10) / 100 * state.targetY), 0);
        // 换田块后必须真的把相机摆过去：只改 radius/target 而不 apply，
        // 机位会一直停在上一块田的距离上（地块大小差得多时框不住）。
        updateCamera(camera, state);
        fieldId = model.field_id;
        state.render();
        return;
      }

      // 同田换进度：把数值参数从旧值补间到新值
      const from = { ...tweenParams };
      const to = numeric(model);
      const t0 = performance.now();
      if (tween) cancelAnimationFrame(tween.raf);

      const step = () => {
        const k = Math.min(1, (performance.now() - t0) / TWEEN_MS);
        const eased = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
        const mixed = { ...model, ...lerpParams(from, to, eased) };
        const count = plantMesh.count;
        const grid = gridFor(count);
        scene.remove(plantMesh);
        plantMesh.geometry.dispose();
        plantMesh.material.dispose();
        // 不重铺地：沿用栽下去时那套边长与垄位，否则补间这几帧里
        // 植株会随着地块"长高长矮"上下跳。
        buildPlants(plantGeometry(mixed), count, fieldGeom.side, lodgingOf(mixed),
                    grid, fieldGeom.ridge);
        state.render();
        if (k < 1) tween = { raf: requestAnimationFrame(step) };
        else { tween = null; tweenParams = to; }
      };
      if (!tweenParams) tweenParams = from;
      tween = { raf: requestAnimationFrame(step) };
    }

    /** 只铺地、不种作物 —— 8 格里「新建田地」那几个空位要的正是这个。
     *
     * 原来的 setModel(null) 直接 return（没有 model 就没有参数），
     * 所以空格子没有任何可画的路径。这里只调 buildGround。 */
    function setBare(areaMu) {
      clearField();
      const side = buildGround(areaMu || 1, 9);
      state.radius = Math.max(RADIUS_MIN, side * state.radiusScale);
      state.target.set(0, 0.3, 0);
      fieldId = "";
      updateCamera(camera, state);
      state.render();
    }

    /** 同步出一张图（dataURL）。
     *
     * **render() 与 toDataURL() 之间绝不能有任何 await / setTimeout / rAF**：
     * WebGL 的绘制缓冲在交出执行权后就被清空，异步取到的会是全白（或全黑）的图。
     * 所以这里直接调 renderer.render，而不是走按需渲染的 state.render()。
     * 同步取值也就**不需要** preserveDrawingBuffer（那个选项有实打实的性能代价）。
     */
    function capture(type, quality) {
      renderer.render(scene, camera);
      try {
        return canvas.toDataURL(type || "image/jpeg", quality == null ? 0.85 : quality);
      } catch (e) {
        return "";      // 取不到图就让调用方走降级，不要抛出去把整批预览搞挂
      }
    }

    // 可补间的数值参数（颜色也补，但那要在植物几何里插值 —— 这里只补尺寸类的，
    // 颜色直接跟随最终值，肉眼看不出差别）
    let tweenParams = null;
    const NUMERIC = ["height", "leafCount", "leafDroop", "earSize", "earDroop", "dryLeafRatio"];

    function numeric(m) {
      const o = {};
      for (const k of NUMERIC) o[k] = Number(m[k]) || 0;
      return o;
    }

    function lerpParams(a, b, t) {
      const o = {};
      for (const k of NUMERIC) o[k] = (a[k] || 0) + ((b[k] || 0) - (a[k] || 0)) * t;
      return o;
    }

    function setSize(width, height) {
      const w = Math.max(1, Math.floor(width));
      const h = Math.max(1, Math.floor(height));
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      state.render();
    }

    let disposed = false;
    let framePending = false;
    state.render = function () {
      // 按需渲染：控件没动、模型没变就不重绘。60fps 不是"一直画"，
      // 而是"需要时能跟上"。
      if (disposed || framePending) return;
      framePending = true;
      requestAnimationFrame(() => {
        framePending = false;
        renderer.render(scene, camera);
      });
    };

    // 离屏快照不挂控制器：它没有指针事件，挂了也只是白占监听；
    // 更要紧的是同一批 8 格里不能有 canvas 抢走按钮的拖动/滚轮。
    if (opts.interactive !== false) attachOrbit(camera, canvas, state);
    else updateCamera(camera, state);
    state.setModel = setModel;
    state.setSize = setSize;
    state.setBare = setBare;
    state.capture = capture;
    state.dispose = () => {
      if (disposed) return;
      disposed = true;
      clearField();
      renderer.dispose();
    };
    // 首次渲染
    state.render();
    return state;
  }

  /** 一批"田块小图"：**一个**离屏 renderer 顺序出图，每次返回一张 dataURL。
   *
   * 为什么不每格一个：一块田一个 WebGLRenderer 就是一整套场景 + 一张阴影贴图，
   * 8 格同时开会把浏览器的 WebGL 上下文用光（低端机上直接黑屏）；而且
   * canvas 放进 <button class="plot-card"> 会抢走拖动与滚轮，格子就点不动了。
   * 这里出的是 <img>，没有任何事件，格子仍然是一个普通按钮。
   *
   * 用法：渲完一批立刻 dispose() —— dispose 会回收地表/植株几何并释放 renderer，
   * 忘了调就会每重渲一次网格泄漏一个上下文。
   */
  function createSnapshotter(opts) {
    opts = opts || {};
    const W = Math.max(64, Math.round(opts.width || 384));
    const H = Math.max(64, Math.round(opts.height || 240));
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;

    let area = 1;
    const view = create(canvas, {
      interactive: false,               // 离屏：没有指针，也不要抢事件
      pixelRatio: opts.pixelRatio || 1,
      antialias: opts.antialias !== false,
      shadowMapSize: opts.shadowMapSize || 512,   // 缩略图用不着 1024
      camera: {
        theta: Math.PI * 0.22,          // 略偏一点，看得到垄的走向
        phi: Math.PI * 0.33,            // 比主视图更俯视：小图看的是"这块地"
        radiusScale: 1.85,              // 退远些，田埂和一条地平线一起入镜
        targetY: 0.6,
      },
      areaMu: () => area,
    });
    view.setSize(W, H);

    function shot(model, o) {
      o = o || {};
      area = o.areaMu || 1;
      view.setSize(o.width || W, o.height || H);
      view.setModel(model);
      return view.capture(o.type || "image/jpeg", o.quality);
    }

    function shotBare(o) {
      o = o || {};
      area = o.areaMu || 1;
      view.setSize(o.width || W, o.height || H);
      view.setBare(area);
      return view.capture(o.type || "image/jpeg", o.quality);
    }

    return { shot, shotBare, dispose: () => view.dispose(), size: { width: W, height: H } };
  }

  /** 不支持 WebGL 时的 2D 兜底（7.6.7）：一条进度条 + 形态文字。 */
  function renderFallback(host, model, growth) {
    const pct = Math.round(Math.min(1, Math.max(0, (model && model.progress) || 0)) * 100);
    host.innerHTML =
      `<div class="farm3d-fallback">` +
      `<div class="notice notice--warning">${Icon.render("warning", 18)}` +
      `<span>当前设备不支持 3D（WebGL 不可用），已切换为 2D 生长示意图。</span></div>` +
      `<div class="farm3d-fallback__bar"><span style="width:${pct}%"></span></div>` +
      `<div class="farm3d-fallback__row">` +
      `<span>全生育期进度 <strong>${pct}%</strong></span>` +
      `<span>${UI.escapeHtml((growth && growth.stage) || "")}</span>` +
      `</div>` +
      `<div class="farm3d-fallback__row text-sm text-muted">` +
      `<span>株高 ${model ? model.height : "—"} cm · ${model ? model.leafCount : "—"} 片叶 · ` +
      `叶色 ${UI.escapeHtml((model && model.leafColorName) || "—")}</span>` +
      `</div>` +
      `</div>`;
  }

  window.Farm3D = {
    supported, create, createSnapshotter, renderFallback, MAX_PLANTS, PLANTS_PER_MU,
  };
})();
