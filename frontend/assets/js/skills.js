/* ==========================================================================
   技能介绍页的增强脚本
   --------------------------------------------------------------------------
   页面主体是静态教程，后端不可用时也必须完整可读。本文件只做三件增强：
     1. 读取 /api/skills，把可用性与版本写成普通小字（不用徽章）
     2. 读取每个技能的 input_schema，填进可折叠的 schema 块
     3. 为代码块提供复制按钮
   ========================================================================== */

(function () {
  "use strict";

  function setStatus(card, text) {
    const el = card.querySelector("[data-skill-status]");
    if (el) el.textContent = text;
  }

  function setVersion(card, version) {
    const el = card.querySelector("[data-skill-version]");
    if (el && version) el.textContent = version;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        if (document.execCommand("copy")) resolve();
        else reject(new Error("复制失败"));
      } catch (e) {
        reject(e);
      } finally {
        area.remove();
      }
    });
  }

  function bindCopyButtons() {
    document.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const wrap = btn.closest(".skill-code-wrap");
        const code = wrap && wrap.querySelector("pre code");
        if (!code) return;
        copyText(code.textContent || "")
          .then(() => {
            const old = btn.textContent;
            btn.textContent = "已复制";
            window.setTimeout(() => { btn.textContent = old; }, 1400);
          })
          .catch(() => {
            try { UI.toast("复制失败，请手动选择代码"); } catch (_) { /* 忽略 */ }
          });
      });
    });
  }

  async function loadSkillDetail(card, name) {
    try {
      const detail = await Api.get("/api/skills/" + encodeURIComponent(name));
      const schema = card.querySelector("[data-skill-schema]");
      const body = card.querySelector("[data-schema-body]");
      if (schema && body && detail.input_schema) {
        body.textContent = JSON.stringify(detail.input_schema, null, 2);
        schema.hidden = false;
      }
      setVersion(card, detail.version);
    } catch (_) {
      /* 详情失败不影响静态教程 */
    }
  }

  async function init() {
    bindCopyButtons();

    let data;
    try {
      data = await Api.get("/api/skills");
    } catch (_) {
      const offline = document.querySelector("[data-skills-offline]");
      if (offline) offline.hidden = false;
      document.querySelectorAll("[data-skill-status]").forEach((el) => {
        el.textContent = "后端未启动";
      });
      return;
    }

    const skills = new Map(((data && data.skills) || []).map((s) => [s.name, s]));
    document.querySelectorAll("[data-skill]").forEach((card) => {
      const name = card.getAttribute("data-skill");
      const skill = skills.get(name);
      if (!skill) {
        setStatus(card, "清单中未找到该技能");
        return;
      }
      setVersion(card, skill.version);
      const deps = (skill.requires || []).join(" / ");
      setStatus(card, (skill.available ? "当前可用" : "当前不可用") +
        (deps ? " · 依赖：" + deps : ""));
      loadSkillDetail(card, name);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
