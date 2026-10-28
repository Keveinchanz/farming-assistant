/* ==========================================================================
   任务 5.1 —— 全局布局：顶部固定横向导航栏 / 底部信息栏 / 全局降级提示
   各页面只留占位元素，导航与页脚由本文件渲染，保证四个页面完全一致。
   ========================================================================== */

(function () {
  "use strict";

  /* 六个导航入口只留文字，不再各配一个图标（地图/扫描/书本/文件/技能）。
     挨在一起时那几个图标是噪声，入口名本身已经说清了是哪个入口；
     顶栏只保留品牌区那一个标记（见 brand__mark），左侧才有一处视觉锚点。 */
  const NAV = [
    { key: "home", label: "首页", href: "/index.html" },
    { key: "diagnose", label: "智能诊断", href: "/diagnose.html" },
    { key: "library", label: "知识图书库", href: "/library.html" },
    { key: "policy", label: "惠农政策", href: "/policy.html" },
    { key: "farm", label: "我的农田", href: "/farm.html" },
    { key: "skills", label: "技能", href: "/skills.html" },
  ];

  function activeKey() {
    let path = location.pathname.replace(/\/+$/, "");
    if (path === "" || path === "/index.html" || path === "/") return "home";
    const last = path.split("/").pop() || "";
    const found = NAV.find((n) => n.href.endsWith(last));
    return found ? found.key : "";
  }

  function renderHeader(host) {
    const active = activeKey();
    const links = NAV.map((n) => {
      const current = n.key === active ? ' aria-current="page"' : "";
      return (
        `<a class="site-nav__link" href="${n.href}"${current}>` +
        `<span>${n.label}</span></a>`
      );
    }).join("");

    host.innerHTML =
      `<a class="brand" href="/index.html" aria-label="返回首页">` +
      `<span class="brand__mark">${Icon.render("leaf", 22)}</span>` +
      `<span class="brand__text">` +
      // 只留站名，不挂副标：名字本身已经说清「田头」是谁的地盘、
      // 「农技台」是干什么的，再补一句解释只会把顶栏挤满。
      `<span class="brand__name">田头农技台</span>` +
      `</span></a>` +
      // 「切换回快速版」紧挨品牌名，而不是塞进导航条：它是「同一个平台换一种界面」，
      // 与首页/诊断那六个功能入口不是同一层级。带上 from，快速版里点「切换回标准版」
      // 才能回到用户原来那一页（而不是一律回首页）。
      `<a class="site-header__quick" ` +
      `href="/chat.html?from=${encodeURIComponent(location.pathname + location.search)}" ` +
      `title="切到仿微信的聊天框：拍张照片或说一句话就能问病情">` +
      `<span class="site-header__quickFull">切换回快速版</span>` +
      `<span class="site-header__quickShort">快速版</span></a>` +
      `<nav class="site-nav" aria-label="主导航">${links}</nav>` +
      `<div class="dialect-picker">` +
      `<label class="dialect-picker__label" for="dialect-select">方言</label>` +
      `<select class="select" id="dialect-select" data-dialect-picker ` +
      `aria-label="切换方言" title="切换方言（影响语音识别与方言播报）"></select>` +
      `</div>` +
      // 用户名菜单：先放一个占位，登录状态是异步取的（见 loadUserMenu）。
      // 不在这里同步渲染，是因为 renderHeader 跑在 Layout.boot() 里、
      // 那时还没有任何账号信息；同步去等一个网络请求会把首屏顶栏卡住。
      `<div class="user-menu" data-user-menu></div>`;

    // 顶栏模板写完之后再去问登录状态，拿到什么就画什么
    loadUserMenu(host.querySelector("[data-user-menu]"));
  }

  function renderFooter(host) {
    host.innerHTML =
      `<div class="site-footer__inner">` +
      `<div class="site-footer__group">` +
      `<strong>田头农技台</strong>` +
      `<span>多方言病虫害诊断、问诊单与回访队列、农田数字孪生、知识图书库与惠农政策</span>` +
      `</div>` +
      `<div class="site-footer__group">` +
      `<strong>数据来源</strong>` +
      `<div class="site-footer__tags">` +
      `<span class="badge">PDD271 植物病害样本</span>` +
      `<span class="badge">IP102 虫害检测数据集</span>` +
      `<span class="badge">全国农技中心趋势预报</span>` +
      `<span class="badge">农业农村部政策原文</span>` +
      `<span class="badge">中国农药信息网登记数据</span>` +
      `<span class="badge">Open-Meteo 气象数据</span>` +
      `<span class="badge">科大讯飞方言语音</span>` +
      `<span class="badge">中国地图 GeoJSON</span>` +
      `</div>` +
      `</div>` +
      `</div>`;
  }

  /* ---------------------------------------------------------------- 用户名菜单
     刻意写在 renderFooter **之后**：单元测试用「renderHeader 到 renderFooter 之间只许
     出现 1 处 Icon.render」来锁住「导航入口不再各带图标」这件事。菜单放到这个切片外面，
     两条规则就互不干扰 —— 既不用为了塞一个下拉箭头把锁拆掉，也不会让锁变得没用。

     三种状态：
       未登录   → [登录] [注册]
       已登录   → 头像（昵称首字）+ 用户名 + 下拉（个人中心 / 管理员 / 退出登录）
       取不到   → 什么都不画（后端没起时不该在顶栏显示一句报错，页面主体有提示）

     头像用**首字母**而不是图片：项目里没有任何头像图片资源，而给每个用户生成一张图
     是另一件事。首字母圆圈是零资源、零请求、任何昵称都能用的做法。 */

  function renderUserMenu(root, status) {
    const user = status && status.user;

    if (!status || !status.logged_in || !user) {
      const redirect = encodeURIComponent(location.pathname + location.search);
      root.innerHTML =
        `<a class="btn btn--sm btn--outline" href="/login.html?redirect=${redirect}">登录</a>` +
        `<a class="btn btn--sm btn--primary" href="/register.html">注册</a>`;
      return;
    }

    const name = user.nickname || user.username;
    // 首字：中文取一个字，英文取首字母大写
    const initial = (name || "?").trim().charAt(0).toUpperCase();
    const admin = status.is_admin
      ? `<a class="user-menu__item" role="menuitem" href="/admin.html">管理员</a>`
      : "";

    root.innerHTML =
      `<button type="button" class="user-menu__trigger" aria-expanded="false" ` +
      `aria-haspopup="true" aria-label="账号菜单">` +
      `<span class="user-menu__avatar" aria-hidden="true">${UI.escapeHtml(initial)}</span>` +
      `<span class="user-menu__name">${UI.escapeHtml(name)}</span>` +
      Icon.render("chevronDown", 16) +
      `</button>` +
      `<div class="user-menu__list" role="menu" hidden>` +
      `<div class="user-menu__head"><strong>${UI.escapeHtml(name)}</strong>` +
      `<span>@${UI.escapeHtml(user.username)}</span></div>` +
      `<a class="user-menu__item" role="menuitem" href="/profile.html">个人中心</a>` +
      admin +
      `<button type="button" class="user-menu__item" role="menuitem" data-logout>退出登录</button>` +
      `</div>`;

    const trigger = root.querySelector(".user-menu__trigger");
    const list = root.querySelector(".user-menu__list");

    const close = () => {
      list.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
    };
    const open = () => {
      list.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
    };

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      list.hidden ? open() : close();
    });

    // 点别处 / 按 ESC 关掉。挂在 document 上而不是用焦点失焦：
    // 失焦在点击下拉项的那一瞬间就触发，菜单会先关掉、点击落空。
    document.addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !list.hidden) {
        close();
        trigger.focus();
      }
    });

    const logoutBtn = root.querySelector("[data-logout]");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", async () => {
        const restore = UI.busy(logoutBtn, "退出中");
        try {
          await Api.logout();
          UI.toast("已退出登录");
          close();
          renderUserMenu(root, { logged_in: false });
        } catch (err) {
          restore();
          UI.toast("退出失败：" + err.message, "error");
        }
      });
    }
  }

  async function loadUserMenu(root) {
    if (!root) return;
    try {
      renderUserMenu(root, await Api.authStatus());
    } catch (e) {
      // 拿不到登录状态就留空：顶栏不该成为后端没起时的报错出口
      // （页面主体那条「无法获取服务状态」才是说这件事的地方）
      root.innerHTML = "";
    }
  }

  /* ---------------------------------------------------------------- 降级提示 */

  function renderDegradedBar(host, health) {
    const notes = [];
    const voice = health.voice || {};
    const llm = health.llm || {};
    const rag = health.rag || {};
    const model = health.model || {};

    if (!model.available) notes.push({ text: "图像识别模型未就绪", icon: "alert" });
    if (voice.provider === "mock") notes.push({ text: "语音服务未配置", icon: "mic" });
    if (llm.provider === "mock") notes.push({ text: "大模型未配置", icon: "info" });
    if (rag.enabled === false) notes.push({ text: "知识库检索未启用", icon: "book" });

    if (!notes.length) {
      host.hidden = true;
      return;
    }

    host.className = "degraded-bar";
    host.innerHTML =
      Icon.render("warning", 20) +
      `<span>部分能力尚未配置：在项目根目录的 <code>.env</code> 中填入对应 Key 后` +
      `重启服务即可启用。</span>` +
      `<span class="degraded-bar__tags">` +
      notes.map((n) => `<span class="badge badge--warning">${Icon.render(n.icon, 14)}${n.text}</span>`).join("") +
      `</span>`;
    host.hidden = false;
  }

  /* ---------------------------------------------------------------- 启动 */

  function boot() {
    const headerHost = document.querySelector("[data-layout-header]");
    if (headerHost) renderHeader(headerHost);

    const footerHost = document.querySelector("[data-layout-footer]");
    if (footerHost) renderFooter(footerHost);

    if (!document.getElementById("toast-host")) {
      const host = document.createElement("div");
      host.id = "toast-host";
      document.body.appendChild(host);
    }

    Dialect.init();

    const barHost = document.querySelector("[data-degraded-bar]");
    if (barHost) {
      Api.health()
        .then((h) => renderDegradedBar(barHost, h))
        .catch(() => {
          barHost.className = "degraded-bar";
          barHost.innerHTML =
            Icon.render("alert", 20) +
            "<span>无法获取服务状态，请确认后端服务已启动（python scripts/20_run_backend.py）。</span>";
          // 这句不能少：base.css 的全局 [hidden] { display: none !important } 会让
          // 带着 hidden 的元素真的消失。以前这里靠 .degraded-bar 的 display:flex 压过
          // [hidden] 才显示得出来 —— 正是那个空框 bug 的另一面，现在必须显式置回。
          barHost.hidden = false;
        });
    }

    // 键盘可达性：跳过导航链接
    const skip = document.querySelector("[data-skip-link]");
    if (skip) skip.addEventListener("click", (e) => {
      const target = document.querySelector(skip.getAttribute("href"));
      if (target) {
        e.preventDefault();
        target.setAttribute("tabindex", "-1");
        target.focus();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.Layout = { boot, renderDegradedBar };
})();
