/* ==========================================================================
   阶段 7 / 7.0.7 —— 账号页逻辑：登录 / 注册 / 个人中心 / 管理员
   四个页面共用这一份脚本，靠 <body data-page="..."> 分派。

   跳转规则（7.0.7）：
     未登录访问 farm.html → login.html?redirect=/farm.html，登录后回跳
     已登录访问 login.html / register.html → 直接去 farm.html
   ========================================================================== */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  /** 登录成功后的去处。只接受站内相对路径 ——
      把 redirect 直接丢给 location.replace 会让 ?redirect=https://别人的站
      变成一个开放重定向（钓鱼常用手法）。 */
  function redirectTarget() {
    const raw = UI.query("redirect") || "/farm.html";
    return /^\/[^/\\]/.test(raw) ? raw : "/farm.html";
  }

  function gotoLogin() {
    const back = encodeURIComponent(location.pathname + location.search);
    location.replace("/login.html?redirect=" + back);
  }

  /** 表单错误：显示在字段下方，而不是弹个 toast 就没了。 */
  function showError(el, message) {
    if (!el) return;
    el.textContent = message || "";
    el.hidden = !message;
  }

  function fieldError(name) {
    return document.querySelector(`[data-error="${name}"]`);
  }

  // ---------------------------------------------------------------- 登录

  async function bootLogin() {
    // 已登录就不该停在登录页
    try {
      const status = await Api.authStatus();
      if (status.logged_in) location.replace(redirectTarget());
    } catch (e) {
      /* 拿不到状态照常显示表单：后端没起时用户至少能看到页面 */
    }

    const form = $("login-form");
    if (!form) return;

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      showError($("login-error"), "");
      const username = $("login-username").value.trim();
      const password = $("login-password").value;

      if (!username || !password) {
        showError($("login-error"), "请填写用户名与密码");
        return;
      }

      const restore = UI.busy($("login-submit"), "登录中");
      try {
        const user = await Api.login(username, password);
        UI.toast(`欢迎回来，${user.nickname || user.username}`);
        location.replace(redirectTarget());
      } catch (err) {
        restore();
        showError($("login-error"), err.message);
      }
    });
  }

  // ---------------------------------------------------------------- 注册

  async function bootRegister() {
    try {
      const status = await Api.authStatus();
      if (status.logged_in) location.replace("/farm.html");
    } catch (e) {
      /* 同上 */
    }

    const form = $("register-form");
    if (!form) return;

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      showError($("register-error"), "");

      const username = $("register-username").value.trim();
      const password = $("register-password").value;
      const confirm = $("register-confirm").value;

      // 两次密码一致这一条在前端先挡一道：它是纯粹的输入笔误，
      // 没必要为它跑一趟服务端
      if (password !== confirm) {
        showError($("register-error"), "两次输入的密码不一致");
        $("register-confirm").focus();
        return;
      }

      const restore = UI.busy($("register-submit"), "注册中");
      try {
        // 邮箱留空（7.0.7 的注册页没有这一项），后端补 @agri.local 占位
        const user = await Api.register({
          username, password, nickname: $("register-nickname").value.trim(),
        });
        UI.toast(`注册成功，欢迎 ${user.nickname || user.username}`);
        // 注册接口本身就会下发会话，直接进农田页 —— 那里已经有三条示例田等着
        location.replace("/farm.html");
      } catch (err) {
        restore();
        showError($("register-error"), err.message);
      }
    });
  }

  // ---------------------------------------------------------------- 个人中心

  async function bootProfile() {
    let user;
    try {
      user = await Api.me();
    } catch (err) {
      // 401（未登录或会话过期）→ 去登录页；其他错误照常提示
      if (err.status === 401) return gotoLogin();
      UI.toast("读取账号信息失败：" + err.message, "error");
      return;
    }
    renderProfile(user);

    // 改昵称 / 头像
    $("profile-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      showError($("profile-error"), "");
      const restore = UI.busy($("profile-submit"), "保存中");
      try {
        const updated = await Api.updateMe({
          nickname: $("profile-nickname").value.trim(),
          avatar: $("profile-avatar").value.trim(),
        });
        renderProfile(updated);
        restore();
        UI.toast("已保存");
      } catch (err) {
        restore();
        showError($("profile-error"), err.message);
      }
    });

    // 改密码：改完全端会话失效，必须重新登录
    $("password-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      showError($("password-error"), "");
      const oldPwd = $("password-old").value;
      const newPwd = $("password-new").value;
      const confirm = $("password-confirm").value;

      if (newPwd !== confirm) {
        showError($("password-error"), "两次输入的新密码不一致");
        return;
      }
      const restore = UI.busy($("password-submit"), "提交中");
      try {
        await Api.changePassword(oldPwd, newPwd);
        UI.toast("密码已修改，请用新密码重新登录");
        location.replace("/login.html");
      } catch (err) {
        restore();
        showError($("password-error"), err.message);
      }
    });

    // 注销：要输密码 + 二次确认。不可逆操作不给「手滑」留路。
    async function deleteAccount() {
      showError($("delete-error"), "");
      const pwd = $("delete-password").value;
      if (!pwd) {
        showError($("delete-error"), "请输入当前密码");
        return;
      }
      if (!window.confirm(
        "确定要注销账号吗？\n\n田块、农事记录、诊断记录都会被永久删除，无法恢复。")) return;

      const restore = UI.busy($("delete-account"), "注销中");
      try {
        await Api.deleteMe(pwd);
        window.alert("账号已注销。");
        location.replace("/index.html");
      } catch (err) {
        restore();
        showError($("delete-error"), err.message);
      }
    }

    $("delete-account").addEventListener("click", deleteAccount);
    // 密码框现在包在 <form id="delete-form"> 里（为消除浏览器可访问性告警），
    // 回车会触发 submit —— 拦下来走同一条注销路径，别让页面被默认行为刷掉。
    $("delete-form").addEventListener("submit", (e) => {
      e.preventDefault();
      deleteAccount();
    });
  }

  function renderProfile(user) {
    const name = user.nickname || user.username;
    UI.setText($("profile-name"), name);
    UI.setText($("profile-username"), "@" + user.username);
    // 占位邮箱不冒充真实邮箱
    const synthetic = (user.email || "").endsWith("@agri.local");
    UI.setText($("profile-email"), synthetic ? "未填写邮箱" : user.email);
    UI.setText($("profile-created"), (user.created_at || "").slice(0, 10) || "—");
    UI.setText($("profile-last-login"), (user.last_login_at || "").slice(0, 16).replace("T", " ") || "—");

    const avatar = $("profile-avatar-preview");
    if (avatar) avatar.textContent = (name || "?").trim().charAt(0).toUpperCase();

    // 两个密码表单各带一个隐藏的用户名字段（浏览器可访问性要求），回填当前用户名
    const uname = $("password-username");
    if (uname) uname.value = user.username || "";
    const dname = $("delete-username");
    if (dname) dname.value = user.username || "";

    $("profile-nickname").value = user.nickname || "";
    $("profile-avatar").value = user.avatar || "";
  }

  // ---------------------------------------------------------------- 管理员

  async function bootAdmin() {
    let status;
    try {
      status = await Api.authStatus();
    } catch (e) {
      status = null;
    }
    if (!status || !status.logged_in) return gotoLogin();
    if (!status.is_admin) {
      // 非管理员：不给他「你没权限」这种确认接口存在的提示，直接当页面不存在
      location.replace("/index.html");
      return;
    }

    async function loadHealth() {
      const host = $("admin-health");
      host.innerHTML = `<span class="spinner" role="status" aria-label="加载中"></span>`;
      try {
        const h = await Api.health();
        host.innerHTML = renderHealth(h);
      } catch (err) {
        host.innerHTML =
          `<div class="notice notice--danger">${Icon.render("alert", 18)}` +
          `<span>读取服务状态失败：${UI.escapeHtml(err.message)}</span></div>`;
      }
    }

    function renderHealth(h) {
      const rows = [];
      const push = (label, ok, detail) =>
        rows.push(
          `<tr><td>${UI.escapeHtml(label)}</td>` +
          `<td><span class="badge ${ok ? "badge--primary" : "badge--warning"}">` +
          `${ok ? "正常" : "降级"}</span></td>` +
          `<td>${UI.escapeHtml(detail)}</td></tr>`);

      const model = h.model || {};
      push("图像识别模型", !!model.available,
        model.available ? `${model.backend} · ${model.num_classes} 类` : model.error || "未就绪");
      const pest = h.pest_detector || {};
      push("虫害检测模型", !!pest.available,
        pest.available ? `${pest.num_classes} 类 · conf ${pest.confidence_threshold}` : pest.hint || "未就绪");
      const voice = h.voice || {};
      push("语音服务", voice.provider !== "mock", voice.provider || voice.error || "未知");
      const llm = h.llm || {};
      push("大模型", llm.provider !== "mock", llm.provider || llm.error || "未知");
      const farm = h.farm || {};
      push("农田数据库", !!farm.available,
        farm.available ? `v${farm.schema_version} · ${farm.fields} 块田 · ` +
          `收获样本 ${farm.harvest_samples}/${farm.harvest_samples_needed}` : farm.hint || farm.error || "不可用");
      const weather = h.weather || {};
      push("天气服务", !!weather.reachable, weather.detail || "未知");

      return (
        `<table class="admin-table"><thead><tr><th>能力</th><th>状态</th><th>说明</th></tr></thead>` +
        `<tbody>${rows.join("")}</tbody></table>`
      );
    }

    $("admin-reload").addEventListener("click", async () => {
      const restore = UI.busy($("admin-reload"), "重载中");
      try {
        await Api.post("/api/admin/reload", {});
        restore();
        UI.toast("缓存已清空，下次请求将重新加载");
        loadHealth();
      } catch (err) {
        restore();
        UI.toast("重载失败：" + err.message, "error");
      }
    });

    loadHealth();
  }

  // ---------------------------------------------------------------- 分派

  const PAGES = {
    login: bootLogin,
    register: bootRegister,
    profile: bootProfile,
    admin: bootAdmin,
  };

  function boot() {
    const page = document.body.dataset.page;
    const fn = PAGES[page];
    if (fn) fn();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
