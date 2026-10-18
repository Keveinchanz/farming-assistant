/* ==========================================================================
   任务 5.5 —— 惠农政策页
   · 纵向条式列表，每条一行，行内分「政策主体区」与「行末出处区」
   · 政策标题就是原文链接：点开直达农业农村部官网该文，摘要与要点可逐字核对
   · 「要点（原文摘录）」是照抄的原文句子（含金额、比例、期限），不是改写摘要

   本页原有一个筛选栏（时间 / 类型 / 关键词），按需求已整条移除：
   政策总量不大，一屏能看完，筛选栏占的地方比它省下的滚动还多。
   后端 /api/policy/list 仍保留筛选参数（任务 4.7 的接口契约），
   这里只是不再传参，改为一次性取全量。
   ========================================================================== */

(function () {
  "use strict";

  const state = { items: [] };
  const $ = (id) => document.getElementById(id);

  /* ---------------------------------------------------------------- 渲染 */

  // 要点：原文里逐字摘出来的句子。条目名（如「耕地地力保护补贴」）单独加粗，
  // 冒号后面跟的是正文原话，这样读者既知道说的是哪一条，看到的又确实是原文。
  function pointsHtml(points) {
    if (!points.length) return "";
    const rows = points
      .map((pt) => {
        const name = pt.title
          ? `<span class="policy-point__name">${UI.escapeHtml(pt.title)}</span>：`
          : "";
        return `<li class="policy-point">${name}${UI.escapeHtml(pt.text)}</li>`;
      })
      .join("");
    return (
      `<div class="policy-row__points">` +
      `<div class="policy-row__points-label">要点（原文摘录）</div>` +
      `<ul class="policy-points">${rows}</ul>` +
      `</div>`
    );
  }

  // 标题即外链。href 来自数据里的 source_url（官方原文地址）；
  // 万一某条没有 url（抓取失败的占位条目），退回成纯文本标题，不留死链。
  // title= 是有意加的：外链看起来和正文一样时，鼠标悬停至少能看到要去哪。
  function titleHtml(p) {
    const url = (p.source_url || "").trim();
    if (!url || !/^https?:\/\//i.test(url)) {
      return `<h2 class="policy-row__title">${UI.escapeHtml(p.title)}</h2>`;
    }
    return (
      `<h2 class="policy-row__title">` +
      `<a class="policy-row__link" href="${UI.escapeHtml(url)}" ` +
      `title="在新窗口打开官方原文：${UI.escapeHtml(url)}" ` +
      `target="_blank" rel="noopener noreferrer">` +
      `${UI.escapeHtml(p.title)}` +
      `<span class="policy-row__link-mark">${Icon.render("external", 15)}原文</span>` +
      `</a></h2>`
    );
  }

  // ------------------------------------------------------ 条目清单（已不再渲染）
  //
  // 这里曾经还有一块「其余 N 项」的条目列表 + 一个展开按钮：把要点没点过名的条目
  // 摘出来罗列。但页面上的信息密度并不靠它 —— 「要点（原文摘录）」本来就逐条给出
  // 条目名与原话，卡片右侧还有「发布单位 / 发布日期」，再往下堆一串只有标题的
  // 序号列表，读起来是重复的。按约定整块删掉：一页只留要点，要看全文点标题去原文。
  // （旧版还有过"预览与展开各渲染一遍、同名重出"的 bug，删掉之后连这个隐患一起没了。）

  function rowHtml(p) {
    const fetchedBadge = p.fetched
      ? ""
      : `<span class="badge badge--warning">摘要待补充</span>`;

    return (
      `<article class="policy-row">` +
      `<div class="policy-row__main">` +
      titleHtml(p) +
      `<div class="policy-row__meta">` +
      Icon.render("clock", 14) +
      `<span>${UI.escapeHtml(p.publish_date)}</span>` +
      `<span class="badge badge--primary">${UI.escapeHtml(p.type_name)}</span>` +
      (p.item_count ? `<span class="badge">${p.item_count} 项</span>` : "") +
      fetchedBadge +
      `</div>` +
      `<p class="policy-row__summary">${UI.escapeHtml(p.summary)}</p>` +
      pointsHtml(p.points || []) +
      `</div>` +
      // 行末出处区：发布单位
      `<aside class="policy-row__source">` +
      `<span class="policy-row__unit-label">发布单位</span>` +
      `<span class="policy-row__unit">${UI.escapeHtml(p.publish_unit)}</span>` +
      `<span class="policy-row__date">发布日期：${UI.escapeHtml(p.publish_date_iso || p.publish_date)}</span>` +
      `</aside>` +
      `</article>`
    );
  }

  function renderList(items) {
    const host = $("policy-list");
    if (!items.length) {
      host.innerHTML =
        `<div class="notice notice--warning">${Icon.render("info", 18)}` +
        `<span>暂无可显示的政策。请确认已抓取数据（<code>python scripts/05_fetch_policies.py</code>，需联网）。</span></div>`;
      return;
    }

    host.innerHTML = items.map(rowHtml).join("");
  }

  /* ---------------------------------------------------------------- 数据 */

  function load() {
    $("policy-list").innerHTML =
      `<div class="policy-row"><div class="policy-row__main">` +
      `<span class="skeleton" style="display:block;height:20px;width:40%;margin-bottom:12px"></span>` +
      `<span class="skeleton" style="display:block;height:14px;margin-bottom:8px"></span>` +
      `<span class="skeleton" style="display:block;height:14px;width:80%"></span>` +
      `</div><aside class="policy-row__source"><span class="skeleton" style="display:block;height:14px;width:70%"></span></aside></div>`;

    return Api.policies({})
      .then((d) => {
        state.items = d.items || [];
        renderList(state.items);
      })
      .catch((err) => {
        $("policy-list").innerHTML =
          `<div class="notice notice--danger">${Icon.render("alert", 18)}` +
          `<span>政策数据加载失败：${UI.escapeHtml(err.message)}<br>` +
          `请确认已抓取数据（<code>python scripts/05_fetch_policies.py</code>，需联网）。</span></div>`;
      });
  }

  /* ---------------------------------------------------------------- 启动 */

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
