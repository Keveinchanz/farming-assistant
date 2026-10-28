/* ==========================================================================
   公开只读的问诊单页（/share.html?t=xxx）

   打开它的是**农户**：不登录、不装 App，扫码即看。所以这一页不依赖站点的
   任何脚本（不引 layout.js / api.js），只做三件事 —— 取快照、画成一张纸、能打印。

   两条硬约束：
     · 快照内容全部来自用户输入，**一律当纯文本渲染**（esc 后再拼 HTML）；
     · 链接过期/不存在不解释细节，统一一句「不存在或已过期」—— 别让这一页
       变成可以试探 token 是否存在的接口。
   ========================================================================== */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setState(title, detail) {
    $("share-root").innerHTML =
      `<div class="share-state share-state--empty">` +
      `<div class="share-state__title">${esc(title)}</div>` +
      `<p class="share-state__desc">${esc(detail)}</p>` +
      `</div>`;
  }

  function section(title, body) {
    return `<div class="sheet__sec"><h3>${esc(title)}</h3>${body}</div>`;
  }

  function renderSheet(p) {
    const r = p.result || {};
    const region = p.region || {};
    const sections = p.sections || [];

    const head =
      `<div class="sheet__top">` +
      `<div>` +
      `<div class="sheet__brand">田头农技台</div>` +
      `<div class="sheet__sub">田间问诊单</div>` +
      `</div>` +
      `<div class="sheet__no">` +
      `<div>编号：<b>${esc(p.no || "现场未归档")}</b></div>` +
      `<div>日期：${esc(p.date || "—")}</div>` +
      `</div>` +
      `</div>`;

    const meta =
      `<table class="sheet__meta"><tbody>` +
      `<tr><th>服务对象</th><td>${esc(p.subject || "—")}</td>` +
      `<th>地块</th><td>${esc(p.field || "—")}</td></tr>` +
      `<tr><th>作物</th><td>${esc(p.crop || "—")}</td>` +
      `<th>送检方式</th><td>${esc((p.sources || []).join(" + ") || "—")}</td></tr>` +
      `</tbody></table>`;

    const main = r.healthy ? "未见明显病斑" : (r.name || "未能确定");
    const tail = [];
    if (r.pest) tail.push("虫害");
    if (typeof r.confidence === "number") tail.push(`置信度 ${r.confidence}%`);

    const concl =
      `<p class="sheet__result"><b>${esc(main)}</b>` +
      (tail.length ? `　${esc(tail.join("，"))}` : "") +
      `</p>` +
      (p.summary ? `<p>${esc(p.summary)}</p>` : "") +
      (p.basis ? `<p class="sheet__note">模型候选：${esc(p.basis)}</p>` : "");

    let advice = `<p>本次没有可执行的处置建议。</p>`;
    if (sections.length) {
      const first = sections[0];
      advice =
        `<p class="sheet__first"><b>先做：${esc(first.title || "")}</b></p>` +
        (first.content ? `<p>${esc(first.content)}</p>` : "") +
        sections.slice(1).map((s) =>
          `<p><b class="sheet__sub2">${esc(s.title || "")}</b>　${esc(s.content || "")}</p>`
        ).join("");
    }

    const visit =
      `<p>${p.recheck ? `建议 <b>${esc(p.recheck)}</b> 回到同一块地复查` : "本次未排回访"}` +
      `${r.healthy ? "；同一片叶、同一角度再拍一张对比即可。" : "；拍一张同部位照片，对比用药前后的变化。"}</p>`;

    const regionItems = region.items || [];
    const regionBody = regionItems.length
      ? `<p>${esc(regionItems.join("；"))}</p>` +
        `<p class="sheet__note">来源：全国农技中心《2026 年全国农作物病虫害发生趋势预报》` +
        `+ 各省作物分布推导` +
        (region.demo ? `　<b>［演示数据，非实时测报］</b>` : "") + `</p>`
      : `<p class="sheet__note">未设置所在省份，无法给出本省预警。</p>`;

    // 本地产收提醒：只在快照带 harvest 时出现。unknown 一律不写日期 ——
    // 那张纸上唯一能写日期的地方就是这里，写错等于替农户做了采收决定。
    const hv = p.harvest;
    let harvestSection = "";
    if (hv && hv.status && hv.status !== "none") {
      if (hv.status === "unknown") {
        harvestSection = section("采收提醒",
          `<p>这块田 ${esc(hv.date || "—")} 施过 ${esc(hv.item || "药剂")}，` +
          `但这次记录没有填安全间隔期，<b>无法判断哪天可以采收</b>。</p>` +
          `<p>请按瓶身标签补填间隔期后再看这张单。</p>`);
      } else if (hv.status === "locked") {
        harvestSection = section("采收提醒",
          `<p>这块田 ${esc(hv.date || "—")} 施过 ${esc(hv.item || "药剂")}，` +
          `标签安全间隔期 ${esc(String(hv.interval_days ?? "—"))} 天，` +
          `<b>最早可收 ${esc(hv.earliest_date || "—")}</b>` +
          `${typeof hv.days_left === "number" ? `（距今 ${hv.days_left} 天）` : ""}。` +
          `在这之前不要采收。</p>`);
      } else {
        harvestSection = section("采收提醒",
          `<p>这块田 ${esc(hv.date || "—")} 施过 ${esc(hv.item || "药剂")}，` +
          `间隔期 ${esc(String(hv.interval_days ?? "—"))} 天，` +
          `已于 ${esc(hv.earliest_date || "—")} 期满，可以采收。</p>`);
      }
    }

    $("share-root").innerHTML =
      `<div class="sheet" id="sheet-paper">` +
      head + meta +
      section("一、现场情况", `<p>${esc(p.scene || "（未填写文字或语音，仅提交照片）")}</p>`) +
      section("二、诊断结论", concl) +
      section("三、处置建议", advice) +
      (p.medication
        ? section("四、用药提醒", `<p>${esc(p.medication)}</p>`)
        : "") +
      section("五、回访安排", visit) +
      section("六、本省当前高发（供参考）", regionBody) +
      harvestSection +
      `<div class="sheet__sign">` +
      `<span>农技员签字：____________</span>` +
      `<span>联系方式：____________</span>` +
      `<span>农户确认：____________</span>` +
      `</div>` +
      `<p class="sheet__foot">` +
      `本单由「田头农技台」辅助生成：结论基于${esc((p.sources || []).join("、") || "提供的材料")}，` +
      `不构成全田诊断；用药请以农药登记标签和当地植保部门指导为准。` +
      `</p>` +
      `</div>`;

    $("share-ops").hidden = false;
  }

  function load() {
    const token = new URLSearchParams(location.search).get("t") || "";
    if (!token) {
      setState("链接不完整", "这条链接少了分享码。请让农技员重新生成一次，或直接看他的手机。");
      return;
    }

    fetch("/api/share/" + encodeURIComponent(token), { headers: { Accept: "application/json" } })
      .then((res) => res.json().then((body) => ({ ok: res.ok, body })))
      .then(({ ok, body }) => {
        if (!ok || !body || body.code !== 0) {
          setState("这张单打不开了",
            "链接可能已过期（问诊单默认 30 天有效），或者被农技员重新生成过。" +
            "请他再发一次即可 —— 纸上的结论本身不受影响。");
          return;
        }
        const data = body.data || {};
        renderSheet(data.payload || {});
        const until = String(data.expires_at || "").slice(0, 10);
        if (until) $("share-foot").textContent = `本页为只读快照，${until} 前可查看。`;
      })
      .catch(() => {
        setState("网络不通", "没能连上服务器。换个网络再试，或者让农技员把这张单打印给你。");
      });
  }

  $("btn-share-print").addEventListener("click", () => window.print());
  load();
})();
