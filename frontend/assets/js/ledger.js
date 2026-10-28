/* ==========================================================================
   用药台账（方案 B）
   
   这一页回答的是「这一季打了什么、哪块地现在能不能收、给合作社交什么表」。
   数据全部来自农事记录里 type=spray 的那些行，没有第二套事实源。
   
   三条必须守住的口径（后端也已按此聚合，前端不重算）：
     · 没填间隔期 = 「不知道」，单独一档，绝不并进「可采收」；
     · 「还在不在间隔期」按**全部**记录判定，不受页面上的区间筛选影响；
     · 不出现推荐剂量，只显示用户自己填的用量原文。
   ========================================================================== */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => UI.escapeHtml(String(s == null ? "" : s));

  const STATUS = {
    locked: { text: "间隔期内", cls: "is-locked" },
    ok: { text: "已过 · 可采收", cls: "is-ok" },
    // 「未记录」不等于「可采收」：旧药期满、新药留空时也给这个状态
    unknown: { text: "间隔期未记录，需核对", cls: "is-unknown" },
    none: { text: "无打药记录", cls: "is-none" },
  };

  const state = {
    view: "field",
    fieldId: "",
    period: "season",
    fields: [],
    data: null,
  };

  /* ------------------------------------------------------------ 启动 */

  function boot() {
    state.fieldId = UI.query("field");
    state.period = UI.query("period") || "season";
    if (!["season", "recent", "all"].includes(state.period)) state.period = "season";
    $("ledger-period").value = state.period;

    const ix = $("ico-export-xlsx");
    if (ix) ix.innerHTML = Icon.render("download", 16);
    const ip = $("ico-export-pdf");
    if (ip) ip.innerHTML = Icon.render("file", 16);

    $("ledger-period").addEventListener("change", () => {
      state.period = $("ledger-period").value;
      syncURL();
      load();
    });
    $("ledger-field").addEventListener("change", () => {
      state.fieldId = $("ledger-field").value;
      syncURL();
      load();
    });
    $("btn-export-xlsx").addEventListener("click", () => exportAs("xlsx", $("btn-export-xlsx")));
    $("btn-export-pdf").addEventListener("click", () => exportAs("pdf", $("btn-export-pdf")));

    document.querySelectorAll(".ledger-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        state.view = tab.dataset.view;
        document.querySelectorAll(".ledger-tab").forEach((t) => {
          const on = t === tab;
          t.classList.toggle("is-on", on);
          t.setAttribute("aria-selected", on ? "true" : "false");
        });
        renderBody();
      });
    });

    loadFields().then(load);
  }

  function syncURL() {
    const p = new URLSearchParams();
    if (state.fieldId) p.set("field", state.fieldId);
    if (state.period && state.period !== "season") p.set("period", state.period);
    const qs = p.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  }

  async function loadFields() {
    try {
      const data = await Api.farmFields();
      state.fields = data.fields || [];
    } catch (e) {
      state.fields = [];
    }
    const sel = $("ledger-field");
    sel.innerHTML = `<option value="">全部地块</option>` +
      state.fields.map((f) =>
        `<option value="${esc(f.id)}">${esc(f.name)}</option>`).join("");
    // 链接里带的田块可能已经被删了；对不上就退回「全部」，不留一个空值选中态
    if (state.fieldId && !state.fields.some((f) => f.id === state.fieldId)) {
      state.fieldId = "";
      syncURL();
    }
    sel.value = state.fieldId;
  }

  /* ------------------------------------------------------------ 取数 */

  async function load() {
    $("ledger-body").innerHTML =
      `<div class="card ledger-loading">` +
      `<span class="spinner" role="status" aria-label="加载中"></span>` +
      `<span class="text-muted">正在汇总施药记录…</span></div>`;
    try {
      const params = { period: state.period };
      if (state.fieldId) params.field_id = state.fieldId;
      const data = await Api.farmLedger(params);
      state.data = data;
      render();
    } catch (err) {
      $("ledger-stats").innerHTML = "";
      $("ledger-alerts").innerHTML = "";
      $("ledger-sub").textContent = "读取失败";
      $("ledger-note").textContent = "";
      $("ledger-body").innerHTML =
        `<div class="card ledger-empty">` +
        `<strong>台账暂时取不到</strong>` +
        `<p class="text-muted">${esc(err.message)}</p>` +
        `<button type="button" class="btn btn--outline btn--sm" id="btn-ledger-retry">重试</button>` +
        `</div>`;
      const btn = $("btn-ledger-retry");
      if (btn) btn.addEventListener("click", load);
    }
  }

  /* ------------------------------------------------------------ 渲染 */

  function render() {
    const d = state.data;
    if (!d) return;

    const badge = $("ledger-period-badge");
    badge.hidden = false;
    badge.textContent = d.period.label || "";

    const one = state.fieldId ? state.fields.find((f) => f.id === state.fieldId) : null;
    $("ledger-sub").textContent = one
      ? `${one.name} · ${one.crop} ${one.area_mu} 亩${one.province ? " · " + one.province + one.city : ""}`
      : `${d.summary.field_count} 块田${state.fields[0] && state.fields[0].province ? " · " + state.fields[0].province + state.fields[0].city : ""}`;

    renderStats(d.summary);
    renderAlerts(d.alerts || []);
    renderBody();
    $("ledger-note").textContent = d.note || "";
  }

  function renderStats(s) {
    const next = s.next_unlock;
    const cards = [
      { k: "本季施药", v: s.spray_count, unit: "次" },
      { k: "涉及药剂", v: s.product_count, unit: "种" },
      { k: "仍在间隔期", v: s.locked_fields, unit: "块", warn: s.locked_fields > 0 },
      s.spray_count === 0
        ? { k: "未记录间隔期", v: s.unknown_fields, unit: "块", muted: true }
        : {
          k: "下一块解禁",
          v: next ? mmdd(next.earliest_date) : "—",
          unit: "",
          sub: next ? `还剩 ${next.days_left} 天` : "都过了",
          muted: !next,
        },
    ];

    $("ledger-stats").innerHTML = cards.map((c) =>
      `<div class="ledger-stat${c.warn ? " ledger-stat--warn" : ""}">` +
      `<div class="ledger-stat__k">${esc(c.k)}</div>` +
      `<div class="ledger-stat__v">${esc(String(c.v))}` +
      (c.unit ? `<small>${esc(c.unit)}</small>` : "") +
      (c.sub ? `<small class="sub">${esc(c.sub)}</small>` : "") +
      `</div></div>`).join("");
  }

  function renderAlerts(alerts) {
    const host = $("ledger-alerts");
    if (!alerts.length) {
      host.innerHTML = "";
      return;
    }
    host.innerHTML = alerts.map((a) => {
      const reg = a.reg_no ? `（${esc(a.reg_no)}）` : "";
      return `<div class="notice notice--danger ledger-alert">` +
        `${Icon.render("alert", 18)}` +
        `<div><strong>${esc(a.name)} 距最早可采收还有 ${a.days_left} 天</strong>` +
        `<p>${esc(a.date)} 施用 ${esc(a.item || "药剂")}${reg}，标签安全间隔期 ` +
        `${a.interval_days} 天。在这之前采收，农残可能不达标。</p></div></div>`;
    }).join("");
  }

  function renderBody() {
    const d = state.data;
    if (!d) return;

    if (!d.summary.spray_count) {
      $("ledger-body").innerHTML =
        `<div class="card ledger-empty">` +
        `<strong>本区间还没有打药记录</strong>` +
        `<p class="text-muted">${esc(d.note)}</p>` +
        `<a class="btn btn--primary btn--sm" href="/farm.html">去记一笔农事</a>` +
        `</div>`;
      return;
    }

    if (state.view === "product") return void ($("ledger-body").innerHTML = tableProduct(d));
    if (state.view === "history") return void ($("ledger-body").innerHTML = historyList(d));
    $("ledger-body").innerHTML = tableField(d);
  }

  function statusHTML(row) {
    const st = STATUS[row.status] || STATUS.none;
    let text = st.text;
    if (row.locked && row.days_left != null) text = `${text} · 还剩 ${row.days_left} 天`;
    // 留空那次施药是哪一天，放在 title 里：表格里塞不下，但核对时必须有据可查
    const hint = row.has_unknown_interval && row.pending_date
      ? `${row.pending_date} 那次施药没有填安全间隔期。${
        row.last_known ? `上次（${row.last_known.date}）${row.last_known.interval_days} 天，已于 ${row.last_known.earliest_date} 期满，不能代表这次。` : ""}`
      : "";
    return `<span class="ledger-pill ${st.cls}"${hint ? ` title="${esc(hint)}"` : ""}>${esc(text)}</span>`;
  }

  function tableField(d) {
    const rows = d.fields.map((r) => {
      const gap = r.interval_days == null ? "未记录" : `${r.interval_days} 天`;
      return `<tr>` +
        `<td>${esc(r.name)}</td>` +
        `<td data-l="作物">${esc(r.crop)}</td>` +
        `<td data-l="最近施药" class="num">${esc(r.last_date || "—")}</td>` +
        `<td data-l="药剂">${esc(r.last_item || "—")}` +
        (r.last_reg_no ? `<span class="reg">${esc(r.last_reg_no)}</span>` : "") + `</td>` +
        `<td data-l="间隔期" class="num">${esc(gap)}</td>` +
        `<td data-l="最早可收" class="num">${esc(r.earliest_date || "—")}</td>` +
        `<td data-l="状态">${statusHTML(r)}</td>` +
        `</tr>`;
    }).join("");

    return `<div class="ledger-table-wrap">` +
      `<table class="ledger-table ledger-table--field">` +
      `<thead><tr><th>地块</th><th>作物</th><th>最近施药</th><th>药剂</th>` +
      `<th>间隔期</th><th>最早可收</th><th>状态</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>`;
  }

  function tableProduct(d) {
    const rows = d.products.map((g) => {
      const tip = g.rotate_warning
        ? `<span class="ledger-pill is-unknown">连续两次同一有效成分，建议换作用机理</span>`
        : `<span class="text-muted">—</span>`;
      return `<tr>` +
        `<td>${esc(g.product)}` +
        (g.registered ? "" : `<span class="reg">登记库未收录</span>`) + `</td>` +
        `<td data-l="登记证号" class="num">${esc(g.reg_no || "手填")}</td>` +
        `<td data-l="毒性">${esc(g.toxicity || "—")}</td>` +
        `<td data-l="本季次数" class="num">${g.count} 次</td>` +
        `<td data-l="用在哪块">${esc((g.field_names || []).join("、"))}</td>` +
        `<td data-l="最近施用" class="num">${esc(g.last_date || "—")}</td>` +
        `<td data-l="提醒">${tip}</td>` +
        `</tr>`;
    }).join("");

    return `<div class="ledger-table-wrap">` +
      `<table class="ledger-table ledger-table--product">` +
      `<thead><tr><th>药剂</th><th>登记证号</th><th>毒性</th><th>本季次数</th>` +
      `<th>用在哪块</th><th>最近施用</th><th>提醒</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>`;
  }

  function historyList(d) {
    const names = {};
    (d.fields || []).forEach((f) => { names[f.field_id] = f.name; });

    const items = (d.history || []).map((lg) => {
      const gap = lg.interval_days == null
        ? `<span class="ledger-pill is-unknown">未记录间隔期</span>`
        : `<span class="ledger-pill ${lg.locked ? "is-locked" : "is-ok"}">` +
          `${lg.locked ? "间隔期内" : "已过"}</span>`;
      const meta = lg.interval_days == null
        ? `${esc(lg.reg_no || "未选登记")} · 没填标签上的间隔期，这块田不参与采收倒计时`
        : `${esc(lg.reg_no || "未选登记")} · 间隔期 ${lg.interval_days} 天 → ` +
          `解禁 ${esc(lg.interval_end || "—")}`;
      return `<li>` +
        `<div class="ledger-tl__row">` +
        `<span class="ledger-tl__date">${esc(lg.date)}</span>` +
        `<span class="ledger-tl__what">${esc(lg.item || "（未填药名）")}` +
        (lg.amount ? ` · ${esc(lg.amount)}` : "") + `</span>` +
        gap + `<span class="ledger-tl__field">${esc(names[lg.field_id] || "")}</span>` +
        `</div>` +
        `<p class="ledger-tl__meta">${meta}</p>` +
        `</li>`;
    }).join("");

    return `<div class="card ledger-history">` +
      `<ul class="ledger-tl">${items}</ul></div>`;
  }

  /* ------------------------------------------------------------ 导出 */

  async function exportAs(fmt, btn) {
    if (!state.data || !state.data.summary.spray_count) {
      UI.toast("本区间没有记录可导出", "warning");
      return;
    }
    const restore = UI.busy(btn, "生成中");
    try {
      const p = new URLSearchParams({ fmt, period: state.period });
      if (state.fieldId) p.set("field_id", state.fieldId);
      const res = await fetch("/api/farm/ledger/export?" + p.toString(),
        { credentials: "same-origin" });
      if (!res.ok) {
        let msg = `导出失败（HTTP ${res.status}）`;
        try {
          const body = await res.json();
          if (body && body.msg) msg = body.msg;
        } catch (e) { /* 非 JSON 就用状态码 */ }
        throw new Error(msg);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `用药台账-${state.data.today || ""}.${fmt}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 立刻 revoke 会让部分浏览器来不及取到文件，给一个延时
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      UI.toast(fmt === "xlsx" ? "Excel 已下载" : "PDF 已下载");
    } catch (err) {
      UI.toast(err.message, "error");
    } finally {
      restore();
    }
  }

  function mmdd(iso) {
    const s = String(iso || "");
    return s.length >= 10 ? s.slice(5) : s;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
