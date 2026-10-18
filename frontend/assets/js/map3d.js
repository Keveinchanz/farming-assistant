/* ==========================================================================
   任务 5.2 —— 首页：中国山川河流 3D 沙盘（three.js 渲染层）

   为什么把 ECharts 换掉：ECharts 的 map series 画的是**平面填充图**，没有地面、
   没有起伏、没有水体，无论怎么调色都是一张贴纸。这一版要的是「沙盘」——
   省份是一块块有厚度的地块，地面按真实高程起伏，江河贴地流，能转着看。

   分工：map.js 继续管**数据、季节、卡片、图例、遮罩、深浅色**这些逻辑，
   本文件只管「把省份与地形画出来」：

     __CHINA_GEO__     （省界 GeoJSON，34 省 + 南海诸岛）
     china_relief.json （0.5° 网格 DEM，SRTM90m）
     china_water.json  （Natural Earth 50m 河流湖泊）
        ↓ 投影到等距圆柱平面（标准纬线取 36°N，于是 1 个世界单位 ≈ 111 km）
        ↓ 多边形三角化（THREE.ShapeUtils）后递归细分到 ≈ 一个 DEM 格
        ↓ 顶点按 DEM 抬高
        ↓ 合成一个索引化、带顶点色的 Mesh：顶面 = 当季风险色 × 高程明暗

   省界**不合**成一块 —— 省与省是各自独立、留缝的地块，靠描边和厚度区分，
   这样悬停高亮才能只亮一个省，而不是整块地闪一下。

   三条容易踩的线：
   · 颜色一律读 CSS 令牌（--map-{season}-risk-1..5 等），本文件不存任何色值 ——
     四季切换与深浅色因此不需要在这里各写一套。
   · 动效（自转 / 河流流动 / 日照推移）全受 prefers-reduced-motion 管；
     关掉之后 rAF 不再空转，只在交互与换肤时按需重绘。
   · 本文件不碰 DOM 结构、不发请求：数据由 map.js 取好传进来。
   ========================================================================== */

(function () {
  "use strict";

  /* three 没加载起来时也要有 window.Map3D —— map.js 会调用它，抛出可读的错误，
     一路走到「地图组件未能启动」那个带重试按钮的出口，而不是白屏 + 控制台报错。 */
  if (typeof THREE === "undefined") {
    window.Map3D = {
      loadAssets: function () {
        return Promise.reject(new Error("three.js 未能加载（assets/vendor/three/three.min.js）"));
      },
      mount: function () {
        throw new Error("three.js 未能加载（assets/vendor/three/three.min.js）");
      },
    };
    return;
  }

  /* ------------------------------------------------------------------ 常量 */

  var LON_MID = 104.0;                       // 投影中心（大致在中国腹地）
  var LAT_MID = 36.0;
  var COS_MID = Math.cos((LAT_MID * Math.PI) / 180);

  /* 垂直夸张：真实比例下珠峰只有 0.08 个世界单位（≈9 km），肉眼看不出来。
     沙盘本来就是夸张的，这里把 6500 m 顶到 6.5 个单位（约 75 倍）。用幂函数
     而不是线性，是为了让 500 m 的平原（≈0.5 单位）和 5000 m 的高原都还分得开。 */
  var MAX_H = 6.5;
  var H_REF = 6500;
  var H_EXP = 0.72;

  var SLAB = 2.2;              // 地块往下的厚度（沙盘的「底座」）
  /* 界线的落位：贴着底板平铺。无名的那份数据是九段线（adcode 100000_JD）——
     一串断续的小岛与界线，不是省级行政区。当地块做就会散成一堆小方块像碎石，
     平铺在海面上才是「划在海里的界」的样子。 */
  var FLAT_Y = -SLAB + 0.02;
  var SUB_EDGE = 0.5;          // 细分目标边长 ≈ DEM 一格；再细就是插值出来的假细节
  var TRI_BUDGET = 160000;     // 细分上限：宁可粗一点，也不能让首屏卡住
  var SNAP = 4;                // 相邻边重合判定的坐标小数位

  var RIVER_W = 0.09;          // 河道宽（≈10 km —— 地图上不夸张就看不见）
  var RIVER_LIFT = 0.06;
  var LAKE_LIFT = 0.06;
  var BORDER_LIFT = 0.055;
  var RIVER_PERIOD = 2.6;      // 水面波光的周期（世界单位）

  /* 取景窗口：比国界放宽一圈。**不用 GeoJSON 的整体包围盒**——那样会被
     南海诸岛一路拉到北纬 3.4°，本土只剩画面上半截。 */
  var OVERVIEW = { lon0: 73, lon1: 136, lat0: 17, lat1: 54.5 };

  var SNOW = new THREE.Color("#dce6ee");
  var WHITE = new THREE.Color(1, 1, 1);
  var AXIS_Y = new THREE.Vector3(0, 1, 0);
  var WATER_LIGHT = "#6fadd8";
  var WATER_DARK = "#2c5f8a";
  var PLATE_MARGIN = 2.4;      // 底板比取景窗每边多出的余量（世界单位 ≈ 2.4° 纬）
  var BORDER_OPACITY = 0.5;
  var LABEL_H = 1.05;          // 标签的世界高度（≈117 km）

  var PITCH_MIN = 0.22;
  var PITCH_MAX = 1.45;
  var ZOOM_MIN = 0.55;
  var ZOOM_MAX = 2.6;

  /* -------------------------------------------------------------- 小工具 */

  /** 经纬 → 沙盘 XZ。北在 -Z 方向。 */
  function project(lon, lat) {
    return { x: (lon - LON_MID) * COS_MID, z: -(lat - LAT_MID) };
  }

  function unprojectX(x) {
    return LON_MID + x / COS_MID;
  }

  function unprojectZ(z) {
    return LAT_MID - z;
  }

  function heightOf(elev) {
    var e = elev > 0 ? elev : 0;
    return MAX_H * Math.pow(e / H_REF, H_EXP);
  }

  /** 线性空间的相对亮度 —— 判断当前配色是不是深色模式。
      （THREE.Color 读十六进制会转到线性工作空间，所以阈值也按线性给。） */
  function luminance(color) {
    return 0.2126 * color.r + 0.7152 * color.g + 0.0722 * color.b;
  }

  var ABBR = /(省|市|自治区|特别行政区|壮族|回族|维吾尔|自治州|地区)/g;
  function shortOf(name) {
    if (!name) return "";
    return name.replace(ABBR, "") || name;
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  /** 折线按最大段长加密 —— 河要贴着地形走，直连的两点之间地形可能高出一座山。 */
  function densify(pts, maxStep) {
    var out = [];
    for (var i = 0; i < pts.length; i++) {
      if (i > 0) {
        var q = pts[i - 1];
        var p = pts[i];
        var dx = (p[0] - q[0]) * COS_MID;
        var dz = p[1] - q[1];
        var n = Math.ceil(Math.hypot(dx, dz) / maxStep);
        for (var k = 1; k < n; k++) {
          out.push([q[0] + ((p[0] - q[0]) * k) / n, q[1] + ((p[1] - q[1]) * k) / n]);
        }
      }
      out.push(pts[i]);
    }
    return out;
  }

  /* ---------------------------------------------------------------- 数据 */

  /** 把两份额外数据取回来。geo 已经在内存里（随首屏脚本同步到位），不用再要。 */
  function loadAssets(base) {
    var root = base || "/assets/geo/";
    function get(name) {
      return fetch(root + name).then(function (r) {
        if (!r.ok) throw new Error(name + " HTTP " + r.status);
        return r.json();
      });
    }
    return Promise.all([get("china_relief.json"), get("china_water.json")]).then(function (out) {
      return { relief: out[0], water: out[1] };
    });
  }

  /** 双线性采样。数据行优先、第 0 行在最北（dlat 为负），纬度代进去直接得行号。 */
  function makeElevation(relief) {
    var lon0 = relief.lon0;
    var lat0 = relief.lat0;
    var dlon = relief.dlon || 0.5;
    var dlat = relief.dlat || -0.5;
    var cols = relief.cols;
    var rows = relief.rows;
    var elev = relief.elev;
    var lastC = cols - 1;
    var lastR = rows - 1;

    return function elevAt(lon, lat) {
      /* 格网之外一律当作海面 0 米，不夹到边缘格。
         那份 DEM 只到 15°N（再往南是南海），而海南省的岛礁有到 3.8°N 的 ——
         夹边缘等于把 15°N 那条线上陆地的海拔糊到几千公里外的礁石上，
         礁石会莫名其妙地立起来。没有数据就是没有数据，海面最诚实。 */
      if (lon < lon0 || lat > lat0 || lon > lon0 + lastC * dlon || lat < lat0 + lastR * dlat) {
        return 0;
      }
      var fc = clamp((lon - lon0) / dlon, 0, lastC);
      var fr = clamp((lat - lat0) / dlat, 0, lastR);
      var c0 = Math.floor(fc);
      var r0 = Math.floor(fr);
      var c1 = c0 < lastC ? c0 + 1 : lastC;
      var r1 = r0 < lastR ? r0 + 1 : lastR;
      var tc = fc - c0;
      var tr = fr - r0;
      var a = elev[r0 * cols + c0];
      var b = elev[r0 * cols + c1];
      var d = elev[r1 * cols + c0];
      var e = elev[r1 * cols + c1];
      return a * (1 - tc) * (1 - tr) + b * tc * (1 - tr) + d * (1 - tc) * tr + e * tc * tr;
    };
  }

  /* ------------------------------------------------------------ 多边形工具 */

  /**
   * 每个 GeoJSON 环 → 投影后的 Vector2 列表（只留 x/z 两轴）。
   * 顺手去掉重合点：重复点会喂给三角化退化的三角形，也让我下面的边配对失灵。
   */
  function toRing(coords) {
    var out = [];
    for (var i = 0; i < coords.length; i++) {
      var p = project(coords[i][0], coords[i][1]);
      var last = out[out.length - 1];
      if (last && Math.abs(last.x - p.x) < 1e-7 && Math.abs(last.y - p.z) < 1e-7) continue;
      out.push(new THREE.Vector2(p.x, p.z));
    }
    /* 有的环首尾同点。ShapeUtils 自己也会去尾，但它是在**我这份数组上**去 ——
       去掉之后顶点数和我拿来建索引的那份就对不上了，所以这里先自己清干净。 */
    if (out.length > 1 && out[0].distanceTo(out[out.length - 1]) < 1e-7) out.pop();
    return out;
  }

  /**
   * 抽样几个三角，求它们在投影平面上的有向面积之和（正 = 逆时针）。
   * 只用来判绕向，值本身没有意义。跳掉退化三角，免得拿一组共线的点定方向。
   */
  function facesArea2(verts, faces, sample) {
    var acc = 0;
    var used = 0;
    var want = sample || 8;
    for (var i = 0; i < faces.length && used < want; i++) {
      var a = verts[faces[i][0]];
      var b = verts[faces[i][1]];
      var c = verts[faces[i][2]];
      if (!a || !b || !c) continue;
      var s = (b.x - a.x) * (c.y - a.y) - (c.x - a.x) * (b.y - a.y);
      if (Math.abs(s) > 1e-9) { acc += s; used++; }
    }
    return acc;
  }

  /** GeoJSON 的 Polygon / MultiPolygon 统一成「外环 + 内环」的列表。 */
  function polygonsOf(feature) {
    var g = feature.geometry;
    if (!g || !g.coordinates) return [];
    var polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
    var out = [];
    polys.forEach(function (poly) {
      var outer = toRing(poly[0]);
      if (outer.length < 3) return;
      out.push({ outer: outer, holes: poly.slice(1).map(toRing).filter(function (h) { return h.length >= 3; }) });
    });
    return out;
  }

  /**
   * 递归细分到目标边长。中点用 cache 共享，于是几何是索引化的、法线才是平滑的 ——
   * 非索引几何上 computeVertexNormals 只能给面法线，地形会变成一块块的多面体。
   * 切最长边（切出的两个三角形绕序不变）。
   */
  function subdivide(verts, faces, maxEdge, budget) {
    var cache = new Map();
    var out = [];
    var stack = faces.slice();
    var guard = 0;

    function midpoint(a, b) {
      var k = a < b ? a + "|" + b : b + "|" + a;
      var m = cache.get(k);
      if (m !== undefined) return m;
      m = verts.length;
      verts.push(new THREE.Vector2((verts[a].x + verts[b].x) / 2, (verts[a].y + verts[b].y) / 2));
      cache.set(k, m);
      return m;
    }

    while (stack.length) {
      var t = stack.pop();
      if (out.length + stack.length >= budget || guard > budget * 8) {
        out.push(t);
        continue;
      }
      guard++;
      var e01 = verts[t[0]].distanceTo(verts[t[1]]);
      var e12 = verts[t[1]].distanceTo(verts[t[2]]);
      var e20 = verts[t[2]].distanceTo(verts[t[0]]);
      var longest = Math.max(e01, e12, e20);
      if (longest <= maxEdge) {
        out.push(t);
        continue;
      }
      var a, b, c;
      if (longest === e01) { a = t[0]; b = t[1]; c = t[2]; }
      else if (longest === e12) { a = t[1]; b = t[2]; c = t[0]; }
      else { a = t[2]; b = t[0]; c = t[1]; }
      var m = midpoint(a, b);
      stack.push([a, m, c], [m, b, c]);
    }
    return out;
  }

  /* --------------------------------------------------------------- 建几何 */

  /**
   * 所有省级地块合成一个大 Mesh，并记下每省占用的顶点/索引区间 ——
   * 悬停高亮与射线拾取都得按区间找回去。
   *
   * 侧壁只给**国界**做：省与省共用的那条边会出现两次（方向相反），
   * 只有国界那圈是一次性的。省界要是也砌墙，两块共面竖墙就会互相 z-fighting，
   * 从斜上方看是一道闪的缝。
   */
  function buildProvinces(geo, elevAt) {
    var pos = [];
    var col = [];
    var idx = [];
    var provs = [];
    var borderPts = [];          // [x, y, z, x, y, z, ...] 描边线段
    var edgeCount = new Map();
    var ringBag = [];            // [{name, rings:[[Vector2,...]]}]
    var vertBase = 0;
    var idxBase = 0;
    var triBase = 0;

    /* 整块模型只画在「底板」这个矩形里，落在板外的多边形整块丢掉。
       不丢的话，海南那份数据里 132 个南海岛礁（最南到 3.8°N，底板只到 14.6°N）
       与九段线南段会悬在板子外面、飘在页面底色上，像撒了一把灰 —— 而且它们
       离相机比本土近得多，取景时会把相机顶得老远，地图只好缩小。
       板外的部分本来也没有「沙盘」可言，就没有东西托着它。 */
    var keep = (function () {
      var nw = project(OVERVIEW.lon0, OVERVIEW.lat1);
      var se = project(OVERVIEW.lon1, OVERVIEW.lat0);
      return {
        x0: Math.min(nw.x, se.x) - PLATE_MARGIN, x1: Math.max(nw.x, se.x) + PLATE_MARGIN,
        z0: Math.min(nw.z, se.z) - PLATE_MARGIN, z1: Math.max(nw.z, se.z) + PLATE_MARGIN,
      };
    })();

    function onPlate(ring) {
      var x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
      for (var i = 0; i < ring.length; i++) {
        var p = ring[i];
        if (p.x < x0) x0 = p.x;
        if (p.x > x1) x1 = p.x;
        if (p.y < z0) z0 = p.y;
        if (p.y > z1) z1 = p.y;
      }
      return x1 >= keep.x0 && x0 <= keep.x1 && z1 >= keep.z0 && z0 <= keep.z1;
    }

    (geo.features || []).forEach(function (feature) {
      var props = feature.properties || {};
      var name = props.name || "";
      var polys = polygonsOf(feature).filter(function (poly) {
        return onPlate(poly.outer);
      });
      if (!polys.length) return;

      var vStart = vertBase;
      var iStart = idxBase;
      var tStart = triBase;
      var local = [];
      var localIdx = [];
      var rings = [];

      polys.forEach(function (poly) {
        var base = local.length;
        var ringVerts = poly.outer.concat(poly.holes);
        local = local.concat(poly.outer, poly.holes);
        var faces = THREE.ShapeUtils.triangulateShape(poly.outer, poly.holes);
        if (!faces.length) return;
        /* 顶面必须是朝上的（法线 +y）。投影把纬度折向南，平面坐标是地理坐标的
           镜像，三角化出来的绕向到三维里正好是反的 —— 不翻的话法线朝下：正面剔除
           之后整块地从头看不到（只瞧见一圈侧壁，像蒙了层毛），射线也打不中，
           悬停、点击全失灵。这里不假设三角化器的绕向，直接量三角本身：
           平面有向面积为正（逆时针）时，三维里叉积的 y 分量是负的，即朝下。
           孔洞跟着一起翻，同一块地绕向得一致。 */
        if (facesArea2(ringVerts, faces) > 0) {
          faces = faces.map(function (f) { return [f[0], f[2], f[1]]; });
        }
        if (base) {
          faces = faces.map(function (f) { return [f[0] + base, f[1] + base, f[2] + base]; });
        }
        faces = subdivide(local, faces, SUB_EDGE, TRI_BUDGET);
        for (var i = 0; i < faces.length; i++) localIdx.push(faces[i][0], faces[i][1], faces[i][2]);
        rings.push(poly.outer);
        poly.holes.forEach(function (h) { rings.push(h); });
      });

      if (!local.length) return;

      var flat = !name;               // 九段线：平铺在底板上，不随地形起伏
      var shade = new Float32Array(local.length);
      for (var v = 0; v < local.length; v++) {
        var q = local[v];
        var h = flat ? FLAT_Y : heightOf(elevAt(unprojectX(q.x), unprojectZ(q.y)));
        pos.push(q.x, h, q.y);
        col.push(0, 0, 0);
        shade[v] = h;
      }
      for (var j = 0; j < localIdx.length; j++) idx.push(localIdx[j] + vertBase);

      var centroid = props.centroid || props.center || null;
      provs.push({
        name: name,
        label: shortOf(name),
        vStart: vStart,
        vCount: local.length,
        iStart: iStart,
        iCount: localIdx.length,
        triStart: tStart,
        triCount: localIdx.length / 3,
        shade: shade,
        centroid: centroid,
        flat: flat,
        sprite: null,
      });
      ringBag.push({ name: name, rings: rings });

      vertBase += local.length;
      idxBase += localIdx.length;
      triBase += localIdx.length / 3;
    });

    // 第一遍数边（用于认出省界），第二遍描边 + 砌墙
    ringBag.forEach(function (bag) {
      bag.rings.forEach(function (rr) {
        for (var i = 0; i < rr.length; i++) {
          var a = rr[i];
          var b = rr[(i + 1) % rr.length];
          edgeCount.set(edgeKey(a, b), (edgeCount.get(edgeKey(a, b)) || 0) + 1);
        }
      });
    });

    var wallPos = [];
    var wallRange = [];
    var wallIdx = [];
    ringBag.forEach(function (bag) {
      var wStart = wallPos.length / 3;
      /* 无名的界线不砌侧壁（它平铺在底板上，见 FLAT_Y）。wallRange 仍然照推，
         因为 paint() 是按省份下标去取它的 —— 空区间就是这一省不上色。 */
      (bag.name ? bag.rings : []).forEach(function (rr) {
        /* 沿折线逐边砌墙，但**共用**上下两条沿的顶点（索引化）。
           逐边各推 6 个独立顶点的话，computedVertexNormals 只会给出面法线，
           海岸线这种高频折线上相邻面片朝向乱跳，整条边就糊成一排毛。
           共用顶点之后法线是平滑的，崖面才是一面崖。 */
        var dep = skirtDepth(rr);   // 这块地自己的底座厚度，见 skirtDepth
        var tv = new Array(rr.length);
        var bv = new Array(rr.length);
        function topAt(i) {
          if (tv[i] === undefined) {
            var q = rr[i];
            tv[i] = wallPos.length / 3;
            wallPos.push(q.x, heightOf(elevAt(unprojectX(q.x), unprojectZ(q.y))), q.y);
          }
          return tv[i];
        }
        function botAt(i) {
          if (bv[i] === undefined) {
            bv[i] = wallPos.length / 3;
            wallPos.push(rr[i].x, -dep, rr[i].y);
          }
          return bv[i];
        }
        for (var i = 0; i < rr.length; i++) {
          var k = (i + 1) % rr.length;
          var a = rr[i];
          var b = rr[k];
          var ha = heightOf(elevAt(unprojectX(a.x), unprojectZ(a.y)));
          var hb = heightOf(elevAt(unprojectX(b.x), unprojectZ(b.y)));
          borderPts.push(a.x, ha + BORDER_LIFT, a.y, b.x, hb + BORDER_LIFT, b.y);
          if (edgeCount.get(edgeKey(b, a))) continue;   // 有反向孪生边 = 省界
          var t0 = topAt(i);
          var t1 = topAt(k);
          var b0 = botAt(i);
          var b1 = botAt(k);
          wallIdx.push(t0, t1, b1, t0, b1, b0);
        }
      });
      wallRange.push({ vStart: wStart, vCount: wallPos.length / 3 - wStart });
    });

    return {
      position: new Float32Array(pos),
      color: new Float32Array(col),
      index: new Uint32Array(idx),
      wallPosition: new Float32Array(wallPos),
      wallColor: new Float32Array(wallPos.length),
      wallIndex: new Uint32Array(wallIdx),
      border: new Float32Array(borderPts),
      provs: provs,
      wallRange: wallRange,
    };
  }

  /**
   * 侧壁（底座）的厚度：随地块大小收。
   *
   * 海南省那份数据里有 132 个南海岛礁，每个只有几公里宽（0.05 世界单位，屏幕上一像素都不到）。
   * 一律按 SLAB 拉 2.2 个单位，就等于把每块礁石都立成一根 39 像素高的柱子 ——
   * 屏幕上散一片，比海南岛本体还高，看着就是碎屑。
   * 阈值取 1.5 个单位（≈165 km）：省级地块都在 2 个单位以上，厚度一分不差；
   * 只有真正的岛礁会被收薄。用线性而不是幂律，是为了让阈值附近不出现台阶。
   */
  function skirtDepth(ring) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (var i = 0; i < ring.length; i++) {
      var p = ring[i];
      if (p.x < x0) x0 = p.x;
      if (p.x > x1) x1 = p.x;
      if (p.y < y0) y0 = p.y;
      if (p.y > y1) y1 = p.y;
    }
    var diag = Math.sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0));
    return SLAB * Math.min(1, diag / 1.5);
  }

  /**
   * 侧壁的法线：取景中心指向该点的水平方向。见 wallGeo 那段的注释。
   */
  function wallNormals(wallPos) {
    var c = project((OVERVIEW.lon0 + OVERVIEW.lon1) / 2,
                    (OVERVIEW.lat0 + OVERVIEW.lat1) / 2);
    var out = new Float32Array(wallPos.length);
    for (var i = 0; i < wallPos.length; i += 3) {
      var dx = wallPos[i] - c.x;
      var dz = wallPos[i + 2] - c.z;
      var len = Math.sqrt(dx * dx + dz * dz) || 1;
      out[i] = dx / len;
      out[i + 1] = 0;
      out[i + 2] = dz / len;
    }
    return out;
  }

  function edgeKey(a, b) {
    return a.x.toFixed(SNAP) + "," + a.y.toFixed(SNAP) + ">" + b.x.toFixed(SNAP) + "," + b.y.toFixed(SNAP);
  }

  /** 河：贴地的带子。UV 沿流向累积，靠一张条纹贴图滚动做出水流。 */
  function buildRivers(rivers, elevAt) {
    var pos = [];
    var uv = [];
    (rivers || []).forEach(function (rv) {
      var pts = densify(rv.pts || [], 0.26);
      if (pts.length < 2) return;
      var P = [];
      for (var i = 0; i < pts.length; i++) {
        var q = project(pts[i][0], pts[i][1]);
        P.push(new THREE.Vector3(q.x, heightOf(elevAt(pts[i][0], pts[i][1])) + RIVER_LIFT, q.z));
      }
      var cum = [0];
      for (var k = 1; k < P.length; k++) cum.push(cum[k - 1] + P[k].distanceTo(P[k - 1]));

      for (var s = 0; s < P.length - 1; s++) {
        var p0 = P[s];
        var p1 = P[s + 1];
        var n0 = sideNormal(P[Math.max(0, s - 1)], p0, P[Math.min(P.length - 1, s + 1)]);
        var n1 = sideNormal(p0, p1, P[Math.min(P.length - 1, s + 2)]);
        var w = RIVER_W / 2;
        var u0 = cum[s] / RIVER_PERIOD;
        var u1 = cum[s + 1] / RIVER_PERIOD;
        pos.push(
          p0.x + n0.x * w, p0.y, p0.z + n0.z * w,
          p1.x + n1.x * w, p1.y, p1.z + n1.z * w,
          p1.x - n1.x * w, p1.y, p1.z - n1.z * w,

          p0.x + n0.x * w, p0.y, p0.z + n0.z * w,
          p1.x - n1.x * w, p1.y, p1.z - n1.z * w,
          p0.x - n0.x * w, p0.y, p0.z - n0.z * w
        );
        uv.push(0, u0, 1, u1, 1, u1, 0, u0, 1, u1, 0, u1);
      }
    });
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    geo.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
    return geo;
  }

  /** 河道某点的侧向单位向量（用前后邻点求方向，拐弯处才不会有缺口）。 */
  function sideNormal(prev, cur, next) {
    var dx = next.x - prev.x;
    var dz = next.z - prev.z;
    var len = Math.hypot(dx, dz) || 1;
    return { x: -dz / len, z: dx / len };
  }

  /** 湖：整片压平的水面。水面不跟地形起伏 —— 湖本来就该是平的。 */
  function buildLakes(lakes, elevAt) {
    var pos = [];
    (lakes || []).forEach(function (lk) {
      var pts = lk.pts;
      if (!pts || pts.length < 3) return;
      var ring2 = toRing(pts);
      if (ring2.length < 3) return;
      var cx = 0, cz = 0;
      ring2.forEach(function (p) { cx += p.x; cz += p.y; });
      cx /= ring2.length;
      cz /= ring2.length;
      var y = heightOf(elevAt(unprojectX(cx), unprojectZ(cz))) + LAKE_LIFT;
      var faces = THREE.ShapeUtils.triangulateShape(ring2, []);
      faces.forEach(function (f) {
        [f[0], f[1], f[2]].forEach(function (i) {
          pos.push(ring2[i].x, y, ring2[i].y);
        });
      });
    });
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    return geo;
  }

  /** 水面波光贴图：一条灰度条纹。滚 offset.x 就有"流动"的感觉。 */
  function makeRippleTexture() {
    var c = document.createElement("canvas");
    c.width = 64;
    c.height = 1;
    var ctx = c.getContext("2d");
    for (var i = 0; i < 64; i++) {
      var v = 0.72 + 0.28 * (0.5 + 0.5 * Math.cos((i / 64) * Math.PI * 2));
      var g = Math.round(255 * v);
      ctx.fillStyle = "rgb(" + g + "," + g + "," + g + ")";
      ctx.fillRect(i, 0, 1, 1);
    }
    var tex = new THREE.CanvasTexture(c);
    tex.wrapS = THREE.RepeatWrapping;
    tex.minFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    return tex;
  }

  function makeLabelTexture(text, fill, halo) {
    var FS = 40;
    var PAD = 12;
    var font = "600 " + FS + "px system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif";
    var c = document.createElement("canvas");
    var ctx = c.getContext("2d");
    ctx.font = font;
    var w = Math.ceil(ctx.measureText(text).width) + PAD * 2;
    var h = FS + PAD * 2;
    c.width = w;
    c.height = h;
    ctx = c.getContext("2d");        // 改画布尺寸会重置状态，得重新取上下文与字体
    ctx.font = font;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.lineWidth = 7;
    ctx.strokeStyle = halo;
    ctx.strokeText(text, w / 2, h / 2);
    ctx.fillStyle = fill;
    ctx.fillText(text, w / 2, h / 2);
    var tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    return { tex: tex, ratio: w / h };
  }

  /* ------------------------------------------------------------------ 实例 */

  function mount(container, data) {
    var geo = data.geo;
    var relief = data.relief;
    var water = data.water || { rivers: [], lakes: [] };
    if (!geo || !geo.features) throw new Error("缺少省界底图（__CHINA_GEO__）");
    if (!relief || !relief.elev) throw new Error("缺少高程数据（china_relief.json）");

    var elevAt = makeElevation(relief);
    var reduced =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    var theme = {
      areaColor: "#eaf5ec",
      borderColor: "#ffffff",
      labelColor: "#3d5a49",
      noData: "#e4f1e7",
    };
    var dark = false;
    var colors = {};             // 省名 → 颜色
    var hovered = null;
    var listeners = {};

    function emit(name, payload) {
      (listeners[name] || []).forEach(function (fn) {
        try { fn(payload); } catch (e) { /* 一个监听器出错不该拖垮渲染循环 */ }
      });
    }

    /* ---- 渲染器 ---- */
    var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setClearAlpha(0);       // 透明：舞台底色仍归 CSS 的季节规则管
    var canvas = renderer.domElement;
    canvas.style.display = "block";
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.cursor = "grab";
    canvas.style.touchAction = "none";
    canvas.setAttribute("aria-hidden", "true");
    container.appendChild(canvas);

    var scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
    scene.add(new THREE.HemisphereLight(0xf2f7ff, 0x6d7a63, 0.42));
    var sun = new THREE.DirectionalLight(0xffffff, 1.05);
    sun.position.set(-34, 48, 30);
    scene.add(sun);
    var sunBase = sun.position.clone();

    var camera = new THREE.PerspectiveCamera(40, 1, 1, 4000);
    var target = new THREE.Vector3(0, 1.6, 0);
    var YAW0 = 0.42;             // 初始方位角：斜着看，画面用得上对角线
    /* 方位角只由用户拖着改（view.yaw）。首屏**不做任何自动转视角** ——
       原先那段「空闲小幅摆动」已按用户要求撤掉；绕圈自转更不行，转到一半地图就是倒的。
       保留的只有天光推移与水面波光，见下面的帧循环。 */
    var view = { yaw: YAW0, pitch: 0.88, zoom: 1, base: 80 };

    /* ---- 地块 ---- */
    var built = buildProvinces(geo, elevAt);
    var topGeo = new THREE.BufferGeometry();
    topGeo.setAttribute("position", new THREE.BufferAttribute(built.position, 3));
    topGeo.setAttribute("color", new THREE.BufferAttribute(built.color, 3));
    topGeo.setIndex(new THREE.BufferAttribute(built.index, 1));
    topGeo.computeVertexNormals();
    var topMesh = new THREE.Mesh(
      topGeo,
      new THREE.MeshLambertMaterial({ vertexColors: true })
    );
    topMesh.frustumCulled = false;
    scene.add(topMesh);

    var wallGeo = new THREE.BufferGeometry();
    wallGeo.setAttribute("position", new THREE.BufferAttribute(built.wallPosition, 3));
    wallGeo.setAttribute("color", new THREE.BufferAttribute(built.wallColor, 3));
    wallGeo.setIndex(new THREE.BufferAttribute(built.wallIndex, 1));
    /* 崖面的法线一律取「从地块中心朝外」的水平方向，而不是逐面算。
       简化过的海岸线本来就左右锯齿，相邻面片的朝向能差到接近 180°，逐面法线
       会明暗交替，整条崖看上去像一排毛。径向法线沿边连续，崖才是一整面崖；
       而且南北两壁受光不同，也正好是转一个立体模型该有的样子。 */
    wallGeo.setAttribute("normal", new THREE.BufferAttribute(wallNormals(built.wallPosition), 3));
    var wallMesh = new THREE.Mesh(
      wallGeo,
      new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide })
    );
    wallMesh.frustumCulled = false;
    scene.add(wallMesh);

    /* ---- 省界描边 ---- */
    var borderGeo = new THREE.BufferGeometry();
    borderGeo.setAttribute("position", new THREE.BufferAttribute(built.border, 3));
    var borderMat = new THREE.LineBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: BORDER_OPACITY,
    });
    var borderLines = new THREE.LineSegments(borderGeo, borderMat);
    borderLines.frustumCulled = false;
    borderLines.renderOrder = 3;
    scene.add(borderLines);

    /* ---- 底板：让整块国土像坐在一张沙盘板上，而不是浮在空气里 ---- */
    var p0 = project(OVERVIEW.lon0, OVERVIEW.lat1);
    var p1 = project(OVERVIEW.lon1, OVERVIEW.lat0);
    var plateW = p1.x - p0.x + PLATE_MARGIN * 2;
    var plateD = p1.z - p0.z + PLATE_MARGIN * 2;
    var plate = new THREE.Mesh(
      new THREE.PlaneGeometry(plateW, plateD),
      new THREE.MeshBasicMaterial({ color: new THREE.Color("#eaf5ec") })
    );
    plate.rotation.x = -Math.PI / 2;
    plate.position.set((p0.x + p1.x) / 2, -SLAB, (p0.z + p1.z) / 2);
    scene.add(plate);

    /* ---- 河湖 ---- */
    var ripple = makeRippleTexture();
    var riverGeo = buildRivers(water.rivers, elevAt);
    var riverMesh = new THREE.Mesh(
      riverGeo,
      new THREE.MeshBasicMaterial({
        map: ripple,
        color: new THREE.Color(WATER_LIGHT),
        transparent: true,
        opacity: 0.95,
        depthWrite: false,
      })
    );
    riverMesh.renderOrder = 2;
    scene.add(riverMesh);

    var lakeGeo = buildLakes(water.lakes, elevAt);
    var lakeMat = new THREE.MeshBasicMaterial({
      color: new THREE.Color(WATER_LIGHT),
      transparent: true,
      opacity: 0.85,
      depthWrite: false,
    });
    var lakeMesh = new THREE.Mesh(lakeGeo, lakeMat);
    lakeMesh.renderOrder = 2;
    scene.add(lakeMesh);

    /* ---- 省名标签 ---- */
    var labelGroup = new THREE.Group();
    scene.add(labelGroup);
    built.provs.forEach(function (p) {
      if (!p.label || !p.centroid) return;
      var lon = p.centroid[0];
      var lat = p.centroid[1];
      var q = project(lon, lat);
      var y = heightOf(elevAt(lon, lat)) + LABEL_H * 1.25;
      var sprite = new THREE.Sprite(
        new THREE.SpriteMaterial({ transparent: true, depthTest: false, depthWrite: false })
      );
      sprite.position.set(q.x, y, q.z);
      sprite.renderOrder = 12;
      labelGroup.add(sprite);
      p.sprite = sprite;
    });

    function paintLabels() {
      built.provs.forEach(function (p) {
        if (!p.sprite) return;
        var made = makeLabelTexture(
          p.label,
          theme.labelColor,
          dark ? "rgba(6,12,9,0.85)" : "rgba(255,255,255,0.92)"
        );
        var old = p.sprite.material.map;
        p.sprite.material.map = made.tex;
        p.sprite.material.needsUpdate = true;
        if (old) old.dispose();
        p.sprite.scale.set(LABEL_H * made.ratio, LABEL_H, 1);
      });
    }

    /* ---- 上色 ---- */
    var tmp = new THREE.Color();

    function paint() {
      var carr = topGeo.attributes.color.array;
      built.provs.forEach(function (p) {
        var hex = colors[p.name] || theme.noData;
        tmp.set(hex);
        var dim = hovered && hovered !== p.name ? 0.76 : 1;
        var hi = hovered === p.name;
        for (var v = 0; v < p.vCount; v++) {
          var h = p.shade[v];
          var lift = dark ? 0.95 : 0.88 + 0.14 * Math.min(1, h / MAX_H);
          var o = (p.vStart + v) * 3;
          var r = tmp.r * lift * dim;
          var g = tmp.g * lift * dim;
          var b = tmp.b * lift * dim;
          var snow = clamp((h - 4.1) / 2.2, 0, 1) * (dark ? 0.3 : 0.55);
          if (snow > 0) {
            r += (SNOW.r - r) * snow;
            g += (SNOW.g - g) * snow;
            b += (SNOW.b - b) * snow;
          }
          if (hi) {
            r += (WHITE.r - r) * 0.3;
            g += (WHITE.g - g) * 0.3;
            b += (WHITE.b - b) * 0.3;
          }
          carr[o] = r;
          carr[o + 1] = g;
          carr[o + 2] = b;
        }
      });
      topGeo.attributes.color.needsUpdate = true;

      var warr = wallGeo.attributes.color.array;
      var wallK = dark ? 0.78 : 0.62;
      built.provs.forEach(function (p, i) {
        tmp.set(colors[p.name] || theme.noData);
        var dim = hovered && hovered !== p.name ? 0.82 : 1;
        var range = built.wallRange[i];
        for (var v = range.vStart; v < range.vStart + range.vCount; v++) {
          var o = v * 3;
          warr[o] = tmp.r * wallK * dim;
          warr[o + 1] = tmp.g * wallK * dim;
          warr[o + 2] = tmp.b * wallK * dim;
        }
      });
      wallGeo.attributes.color.needsUpdate = true;
      dirty = true;
    }

    /** 换肤：深浅色与四季都走这里，颜色本身仍由调用方从 CSS 令牌读出传进来。 */
    function applyTheme(t) {
      if (!t) return;
      theme = {
        areaColor: t.areaColor || theme.areaColor,
        borderColor: t.borderColor || theme.borderColor,
        labelColor: t.labelColor || theme.labelColor,
        noData: t.noData || theme.noData,
      };
      dark = luminance(new THREE.Color(theme.areaColor)) < 0.18;

      plate.material.color.set(theme.areaColor);
      borderMat.color.set(theme.borderColor);
      var waterColor = new THREE.Color(dark ? WATER_DARK : WATER_LIGHT);
      riverMesh.material.color.copy(waterColor);
      lakeMat.color.copy(waterColor);
      paintLabels();
      paint();
    }

    /* ---- 相机 ---- */
    function updateCamera() {
      var pitch = clamp(view.pitch, PITCH_MIN, PITCH_MAX);
      var cp = Math.cos(pitch);
      var sp = Math.sin(pitch);
      var d = view.base * view.zoom;
      camera.position.set(
        target.x + d * cp * Math.sin(view.yaw),
        target.y + d * sp,
        target.z + d * cp * Math.cos(view.yaw)
      );
      camera.lookAt(target);
    }

    /* ---- 取景用的样本点 ----
       取景窗内 13×13 个格点、各自带上真实高程 —— 取景就是让这层「地形曲面」整个进画面。
       别拿底板的角去取景：底板是取景窗再往外扩 2.4 个单位的矩形，而取景窗的四个角
       全在国外（西北是中亚、东北是俄远东、南北两头是海），把它们抬到最高峰去算，
       相机要多退四成，地图在画面里缩成一小块。板子探出画面外反而像一张铺开的沙盘台，
       比四周空一圈好看。 */
    var fitPts = [];
    (function () {
      var N = 13;
      for (var i = 0; i < N; i++) {
        for (var k = 0; k < N; k++) {
          var lon = OVERVIEW.lon0 + (OVERVIEW.lon1 - OVERVIEW.lon0) * (i / (N - 1));
          var lat = OVERVIEW.lat1 + (OVERVIEW.lat0 - OVERVIEW.lat1) * (k / (N - 1));
          var q = project(lon, lat);
          fitPts.push(new THREE.Vector3(q.x, heightOf(elevAt(lon, lat)), q.z));
        }
      }
      /* 再补取景窗四角压到底板底面的点。角上那格的地面未必矮，而板子的底边
         是整块模型最低的地方，不带上它，南边的板沿会切在画面外。 */
      [[p0.x, p0.z], [p1.x, p0.z], [p0.x, p1.z], [p1.x, p1.z]].forEach(function (c) {
        fitPts.push(new THREE.Vector3(c[0], -SLAB, c[1]));
      });
    })();

    var LIMIT = 0.94;                   // 取景时画面四周各留 6% 余量

    var probe = new THREE.Vector3();
    /** 把相机放到距离 d、量模型在画面里占多少：越界倍数 over 与投影中心 (cx, cy)。 */
    function sampleAt(d, out) {
      view.base = d;
      updateCamera();
      camera.updateMatrixWorld(true);
      var over = 0, x0 = 2, x1 = -2, y0 = 2, y1 = -2;
      for (var i = 0; i < fitPts.length; i++) {
        probe.copy(fitPts[i]).project(camera);
        over = Math.max(over, Math.abs(probe.x), Math.abs(probe.y));
        if (probe.x < x0) x0 = probe.x;
        if (probe.x > x1) x1 = probe.x;
        if (probe.y < y0) y0 = probe.y;
        if (probe.y > y1) y1 = probe.y;
      }
      if (out) { out.cx = (x0 + x1) / 2; out.cy = (y0 + y1) / 2; }
      return over;
    }

    /* 取景 = 让整块国土刚好填满画面。
       两件事，反复几轮就收敛（互相影响，但每轮都单调靠近）：

       1) 距离：从太近出发往外挪找上界，再二分收回最小能装下的距离。
          别用「半宽 / 半深」推解析解 —— 斜俯视下南边那条边离相机比中心近得多，
          同样的世界进深在画面里占的高度大一截，按中心算出来的距离会把海南岛、
          台湾切在画外。
       2) 对中：把相机要看的点挪到「模型投在画面上的几何中心」。
          不挪的话，近端那一半被透视放大，整个模型在画面里偏下、只占住中间
          一条，四周一圈空 —— 高度方向的余量白白浪费掉。挪过去之后同样的
          画面能多装两成。 */
    function fit() {
      var w = container.clientWidth || 1;
      var h = container.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();

      var tanY = Math.tan((camera.fov * Math.PI) / 360);
      var tanX = tanY * camera.aspect;
      var out = { cx: 0, cy: 0 };

      for (var pass = 0; pass < 3; pass++) {
        var near = 8;
        var far = near;
        for (var it = 0; it < 40 && sampleAt(far, null) > LIMIT; it++) {
          near = far;
          far *= 1.5;
        }
        for (var k = 0; k < 24; k++) {
          var mid = (near + far) / 2;
          if (sampleAt(mid, null) > LIMIT) near = mid; else far = mid;
        }
        sampleAt(far, out);
        if (pass === 2) { view.base = far; break; }
        if (Math.abs(out.cx) < 0.005 && Math.abs(out.cy) < 0.005) continue;

        /* 相机是绕着 target 摆的：target 往哪挪，相机跟着挪，画面里的模型就往
           反方向平移。所以把 target 沿相机的右/上方向各推「中心偏了多少」的量，
           模型就回到正中；距离按当前视距折算成世界单位。 */
        camera.updateMatrixWorld(true);
        var right = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0);
        var upv = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 1);
        target.addScaledVector(right, out.cx * far * tanX);
        target.addScaledVector(upv, out.cy * far * tanY);
      }
      updateCamera();
      dirty = true;
    }

    /* ---- 交互 ---- */
    var raycaster = new THREE.Raycaster();
    var ndc = new THREE.Vector2();
    var drag = null;
    var lastPointer = { x: 0, y: 0 };
    var dirty = true;

    function provinceAt(clientX, clientY) {
      var rect = canvas.getBoundingClientRect();
      ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, camera);
      var hits = raycaster.intersectObject(topMesh, false);
      if (!hits.length) return null;
      var f = hits[0].faceIndex;
      var lo = 0;
      var hi = built.provs.length - 1;
      while (lo <= hi) {
        var mid = (lo + hi) >> 1;
        var p = built.provs[mid];
        if (f < p.triStart) hi = mid - 1;
        else if (f >= p.triStart + p.triCount) lo = mid + 1;
        else return p;
      }
      return null;
    }

    function localPoint(ev) {
      var rect = canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    }

    function setHover(name, pt) {
      if (hovered === name) {
        if (name && pt) emit("hover", { name: name, x: pt.x, y: pt.y, again: true });
        return;
      }
      hovered = name;
      paint();
      emit("hover", name ? { name: name, x: pt ? pt.x : 0, y: pt ? pt.y : 0 } : null);
    }

    function onPointerDown(ev) {
      if (ev.button !== 0 && ev.pointerType === "mouse") return;
      drag = { x: ev.clientX, y: ev.clientY, yaw: view.yaw, pitch: view.pitch, t: Date.now(), moved: 0 };
      canvas.style.cursor = "grabbing";
      if (canvas.setPointerCapture) canvas.setPointerCapture(ev.pointerId);
    }

    function onPointerMove(ev) {
      var pt = localPoint(ev);
      lastPointer = pt;
      if (drag) {
        var dx = ev.clientX - drag.x;
        var dy = ev.clientY - drag.y;
        drag.moved = Math.max(drag.moved, Math.hypot(dx, dy));
        view.yaw = drag.yaw - dx * 0.006;
        view.pitch = clamp(drag.pitch + dy * 0.005, PITCH_MIN, PITCH_MAX);
        /* 拖的同时也重新取景：转到别的方位、压平或立起来，需要的视距都不一样，
           不重取的话一转就出画。zoom 是乘在 base 上的，用户自己拉近的那一份不丢。 */
        fit();
        dirty = true;
        if (drag.moved > 6) setHover(null);
        emit("view", null);            // 停住的卡片要跟着地块走，不能浮在别省头上
        return;
      }
      var p = provinceAt(ev.clientX, ev.clientY);
      setHover(p ? p.name : null, pt);
    }

    function onPointerUp(ev) {
      canvas.style.cursor = "grab";
      if (!drag) return;
      var wasClick = drag.moved <= 6 && Date.now() - drag.t < 600;
      drag = null;
      if (!wasClick) return;
      var p = provinceAt(ev.clientX, ev.clientY);
      emit("pick", { name: p ? p.name : null, x: lastPointer.x, y: lastPointer.y });
    }

    function onPointerLeave() {
      if (drag) return;
      setHover(null);
      emit("leave", null);
    }

    function onWheel(ev) {
      ev.preventDefault();
      var k = Math.exp(clamp(ev.deltaY, -120, 120) * 0.0016);
      view.zoom = clamp(view.zoom * k, ZOOM_MIN, ZOOM_MAX);
      /* 不再有「每帧一次」的取景兜底了，缩放进出必须在这里自己重取一次，
         否则拉近拉远之后模型会出画。fit() 内部会 updateCamera 并置脏。 */
      fit();
      emit("view", null);
    }

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener("pointerleave", onPointerLeave);
    canvas.addEventListener("wheel", onWheel, { passive: false });

    /* ---- 循环 ---- */
    var raf = 0;
    var last = 0;
    function frame(now) {
      raf = requestAnimationFrame(frame);
      var dt = last ? Math.min(0.05, (now - last) / 1000) : 0;
      last = now;
      /* 减少动效时不空转：只在「脏」的时候画一帧。交互与换肤都会把它置脏。 */
      if (reduced) {
        if (!dirty) return;
        dirty = false;
      } else {
        /* 视角不再自动变化，所以这里既不重取景也不广播 —— 拖动/滚轮各自会 fit()
           并 emit("view")。留下的两样是首屏的「活的」感觉：天光缓慢推移、
           水面波光在走（用户只要求停掉自动摆动，这两样保留）。 */
        sun.position.copy(sunBase).applyAxisAngle(AXIS_Y, now * 0.000045);
        ripple.offset.x -= dt * 0.05;  // 水面的波光在走
      }
      renderer.render(scene, camera);
    }

    fit();
    paintLabels();
    paint();
    raf = requestAnimationFrame(frame);

    var onResize = function () { fit(); };
    window.addEventListener("resize", onResize);
    var ro = null;
    if (window.ResizeObserver) {
      ro = new ResizeObserver(onResize);
      ro.observe(container);
    }

    return {
      canvas: canvas,

      on: function (name, fn) {
        (listeners[name] = listeners[name] || []).push(fn);
        return this;
      },
      setColors: function (map) {
        colors = map || {};
        paint();
      },
      setTheme: applyTheme,
      setHover: function (name) { setHover(name, null); },
      anchor: function (name) {
        var found = null;
        built.provs.forEach(function (p) { if (p.name === name) found = p; });
        if (!found || !found.centroid) return null;
        var q = project(found.centroid[0], found.centroid[1]);
        var y = heightOf(elevAt(found.centroid[0], found.centroid[1]));
        var v = new THREE.Vector3(q.x, y, q.z).project(camera);
        if (v.z > 1) return null;
        var rect = canvas.getBoundingClientRect();
        return { x: ((v.x + 1) / 2) * rect.width, y: ((1 - v.y) / 2) * rect.height };
      },
      resize: function () { fit(); },
      dispose: function () {
        cancelAnimationFrame(raf);
        window.removeEventListener("resize", onResize);
        if (ro) ro.disconnect();
        canvas.removeEventListener("pointerdown", onPointerDown);
        canvas.removeEventListener("pointermove", onPointerMove);
        canvas.removeEventListener("pointerup", onPointerUp);
        canvas.removeEventListener("pointercancel", onPointerUp);
        canvas.removeEventListener("pointerleave", onPointerLeave);
        canvas.removeEventListener("wheel", onWheel);
        built.provs.forEach(function (p) {
          if (p.sprite) {
            if (p.sprite.material.map) p.sprite.material.map.dispose();
            p.sprite.material.dispose();
          }
        });
        [topGeo, wallGeo, borderGeo, riverGeo, lakeGeo, plate.geometry].forEach(function (g) { g.dispose(); });
        [topMesh.material, wallMesh.material, borderMat, riverMesh.material, lakeMat, plate.material]
          .forEach(function (m) { m.dispose(); });
        ripple.dispose();
        renderer.dispose();
        if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
      },
    };
  }

  window.Map3D = { loadAssets: loadAssets, mount: mount };
})();
