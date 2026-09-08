/* SmileX Memory 面板 — 无构建,vanilla JS fetch,只读 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const LAYERS = ["L0", "L1", "L2", "L3"];
const LAYER_NAMES = { L0: "工作记忆", L1: "短时", L2: "长时", L3: "语义社区" };
const KIND_LABEL = { fragment: "片段", entity: "实体", triple: "三元组" };
const KIND_CLS = { fragment: "dim", entity: "l1", triple: "l2" };
const REFRESH_MS = 15000;

/* ---------- 工具 ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function fmtNum(n) {
  return Number.isFinite(+n) ? (+n).toLocaleString("zh-CN") : "—";
}

function fmtBytes(n) {
  n = +n;
  if (!Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

function fmtDur(s) {
  s = Math.max(0, Math.floor(+s || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d) return `${d} 天 ${h} 时`;
  if (h) return `${h} 时 ${m} 分`;
  if (m) return `${m} 分 ${sec} 秒`;
  return `${sec} 秒`;
}

function relTime(iso) {
  if (!iso) return "—";
  const t = Date.parse(String(iso).replace(" ", "T"));
  if (Number.isNaN(t)) return String(iso);
  const diff = (Date.now() - t) / 1000;
  if (diff < 0) return String(iso);
  if (diff < 45) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  return `${Math.round(diff / 86400)} 天前`;
}

async function getJSON(url, opts) {
  // §15.4 API Key 鉴权: 已保存的 key 以 X-API-Key 附在所有请求上
  const merged = Object.assign({ headers: {} }, opts || {});
  const key = localStorage.getItem("smilex.apikey") || "";
  if (key) merged.headers["X-API-Key"] = key;
  const r = await fetch(url, merged);
  if (!r.ok) {
    const err = new Error(`HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

function tag(text, cls) {
  return `<span class="bdg ${cls || "dim"}">${esc(text)}</span>`;
}

function emptyRow(cols, msg) {
  return `<tr class="empty-row"><td colspan="${cols}">${esc(msg)}</td></tr>`;
}

/* ---------- Tab ---------- */

function switchTab(name) {
  document.querySelectorAll(".tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + name));
  localStorage.setItem("smilex.tab", name);
}

document.querySelectorAll(".tab").forEach(b =>
  b.addEventListener("click", () => switchTab(b.dataset.tab)));

/* ---------- 健康状态 ---------- */

async function loadHealth() {
  const pill = $("#health-pill");
  try {
    const d = await getJSON("/api/health");
    if (d.auth === "required") {
      // 服务启用了 API Key 且当前 key 缺失/无效: health 只回裁剪载荷
      pill.classList.add("bad");
      pill.innerHTML = `<span class="dot"></span>未授权`;
      pill.title = "服务已启用 API Key 鉴权,请在右上角输入框填入 key 后回车";
      return;
    }
    const c = d.config || {};
    pill.classList.remove("bad");
    pill.innerHTML = `<span class="dot"></span>运行中 · ${esc(fmtDur(d.uptime_s))}`;
    pill.title =
      `db ${c.db_path ?? "?"}\n` +
      `embedder ${c.embedder ?? "?"} · reranker ${c.reranker ?? "?"} · ` +
      `抽取 ${c.fact_extractor ?? "?"}\n` +
      `调度器 ${c.enable_scheduler ? "开" : "关"} · token 预算 ${c.token_budget ?? "?"}`;
  } catch (e) {
    pill.classList.add("bad");
    if (e.status === 401) {
      pill.innerHTML = `<span class="dot"></span>未授权`;
      pill.title = "API Key 缺失或错误,请在右上角输入框填入后回车";
    } else {
      pill.innerHTML = `<span class="dot"></span>连接失败`;
      pill.title = String(e);
    }
  }
}

/* ---------- 概览:统计 / 分层叠条 / scope / 任务 ---------- */

async function loadStats() {
  const box = $("#stats-cards");
  try {
    const d = await getJSON("/api/stats");
    const cards = [
      ["实体", d.entities], ["三元组", d.triples],
      ["时序片段", d.temporal_fragments], ["向量链接", d.vector_links],
      ["L0 会话", d.l0_snapshots], ["因果链", d.causal_chains],
    ];
    box.classList.remove("loading");
    box.innerHTML = cards.map(([label, v]) =>
      `<div class="card${v ? "" : " zero"}"><span class="card-num">${fmtNum(v)}</span>` +
      `<span class="card-label">${esc(label)}</span></div>`).join("");
    $("#stats-meta").textContent = `库体积 ${fmtBytes(d.db_size_bytes)}`;
    renderStrata(d.fragments_by_layer || {});
    renderScopes(d.entity_scopes || {});
  } catch (e) {
    box.classList.add("loading");
    box.textContent = `统计加载失败:${e}`;
  }
}

function renderStrata(byLayer) {
  const box = $("#strata");
  const entries = LAYERS.map(l => [l, +byLayer[l] || 0]);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  if (!total) {
    box.innerHTML =
      `<div class="strata-empty">库还是空的 — 通过 MCP 工具 memory_write 写入第一批记忆</div>`;
    return;
  }
  const segs = entries.filter(([, v]) => v > 0).map(([l, v]) => {
    const pct = (v / total * 100).toFixed(2);
    return `<div class="strata-seg ${l.toLowerCase()}" style="width:${pct}%"` +
      ` title="${l} ${LAYER_NAMES[l]} · ${fmtNum(v)} 条(${(v / total * 100).toFixed(1)}%)"></div>`;
  }).join("");
  const legend = entries.map(([l, v]) =>
    `<span class="strata-item"><i class="strata-dot ${l.toLowerCase()}"></i>` +
    `${l} ${LAYER_NAMES[l]} <b>${fmtNum(v)}</b>` +
    `<span class="pct">${(v / total * 100).toFixed(1)}%</span></span>`).join("");
  box.innerHTML =
    `<div class="strata-bar">${segs}</div><div class="strata-legend">${legend}</div>`;
}

function renderScopes(scopes) {
  const box = $("#scopes");
  const entries = Object.entries(scopes);
  if (!entries.length) {
    box.innerHTML = `<span class="scope-empty">暂无实体 — 写入后按 scope 汇总在这里</span>`;
    return;
  }
  box.innerHTML = entries.map(([s, c]) =>
    `<button type="button" class="scope-chip" data-scope="${esc(s)}">` +
    `${esc(s)}<b>${fmtNum(c)}</b></button>`).join("");
  box.querySelectorAll(".scope-chip").forEach(chip =>
    chip.addEventListener("click", () => jumpToScope(chip.dataset.scope)));
}

function jumpToScope(scope) {
  switchTab("browse");
  const f = $("#browse-form");
  f.scope.value = scope;
  f.requestSubmit();
}

async function loadTasks() {
  const tbody = $("#tasks-table tbody");
  try {
    const rows = await getJSON("/api/tasks");
    tbody.innerHTML = rows.length ? rows.map(r => {
      const pct = Math.round((+r.progress || 0) * 100);
      return `<tr><td class="mono">${esc(r.task_id)}</td>` +
        `<td><div class="progresscell"><div class="progress" title="${pct}%">` +
        `<i style="width:${pct}%"></i></div>` +
        `<span class="mono pct">${pct}%</span></div></td>` +
        `<td>${esc(r.step ?? "—")}</td>` +
        `<td title="${esc(r.updated_at)}">${esc(relTime(r.updated_at))}</td></tr>`;
    }).join("") : emptyRow(4, "暂无任务运行记录");
  } catch (e) {
    tbody.innerHTML = emptyRow(4, `任务加载失败:${e}`);
  }
}

/* ---------- 记忆浏览 ---------- */

async function browse(ev) {
  ev.preventDefault();
  const f = ev.target;
  const params = new URLSearchParams();
  if (f.q.value) params.set("q", f.q.value.trim());
  if (f.kind.value) params.set("kind", f.kind.value);
  if (f.layer.value) params.set("layer", f.layer.value);
  if (f.scope.value) params.set("scope", f.scope.value.trim());
  params.set("limit", f.limit.value);
  const tbody = $("#memories-table tbody");
  tbody.innerHTML = emptyRow(6, "加载中…");
  try {
    const rows = await getJSON("/api/memories?" + params);
    tbody.innerHTML = rows.length ? rows.map(r => {
      const imp = r.importance == null ? `<span class="mono">—</span>`
        : `<div class="progress" title="${(+r.importance).toFixed(2)}">` +
          `<i style="width:${Math.min(100, +r.importance * 100).toFixed(0)}%"></i></div>`;
      return `<tr tabindex="0" data-id="${esc(r.id)}">` +
        `<td>${tag(KIND_LABEL[r.kind] || r.kind, KIND_CLS[r.kind])}</td>` +
        `<td class="content">${esc(r.content)}</td>` +
        `<td class="mono">${esc(r.scope)}</td>` +
        `<td>${r.layer ? tag(r.layer, String(r.layer).toLowerCase()) : `<span class="mono">—</span>`}</td>` +
        `<td>${imp}</td>` +
        `<td title="${esc(r.updated_at)}">${esc(relTime(r.updated_at))}</td></tr>`;
    }).join("") : emptyRow(6, "没有匹配的记忆 — 换个关键词,或清空筛选条件");
  } catch (e) {
    tbody.innerHTML = emptyRow(6, `浏览加载失败:${e}`);
  }
}

$("#memories-table tbody").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDetail(tr.dataset.id);
});
$("#memories-table tbody").addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDetail(tr.dataset.id);
});

/* ---------- 详情抽屉 ---------- */

async function openDetail(id) {
  const drawer = $("#drawer");
  const scrim = $("#scrim");
  $("#drawer-title").textContent = id;
  $("#drawer-body").innerHTML = `<div class="kv"><dt>状态</dt><dd>加载中…</dd></div>`;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  scrim.hidden = false;
  requestAnimationFrame(() => scrim.classList.add("show"));
  $("#drawer-close").focus();
  try {
    const d = await getJSON(`/api/memory/${encodeURIComponent(id)}`);
    const body = $("#drawer-body");
    if (d.error) {
      body.innerHTML = `<div class="kv"><dt>错误</dt><dd>${esc(d.error)}</dd></div>`;
      return;
    }
    $("#drawer-title").textContent = `${d.table} · ${id}`;
    body.innerHTML = Object.entries(d.row || {}).map(([k, v]) => {
      const shown = v == null ? "—" : String(v);
      return `<div class="kv"><dt>${esc(k)}</dt><dd>${esc(shown)}</dd></div>`;
    }).join("");
  } catch (e) {
    $("#drawer-body").innerHTML =
      `<div class="kv"><dt>错误</dt><dd>详情加载失败:${esc(String(e))}</dd></div>`;
  }
}

function closeDetail() {
  const drawer = $("#drawer");
  const scrim = $("#scrim");
  if (!drawer.classList.contains("open")) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  scrim.classList.remove("show");
  setTimeout(() => { scrim.hidden = true; }, 220);
}

$("#drawer-close").addEventListener("click", closeDetail);
$("#scrim").addEventListener("click", closeDetail);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDetail(); });

/* ---------- 召回测试 ---------- */

async function recallTest(ev) {
  ev.preventDefault();
  const f = ev.target;
  const btn = $("#recall-submit");
  const body = {
    query: f.query.value.trim(),
    top_k: Number(f.top_k.value) || 10,
    session_id: f.session_id.value.trim() || "panel-debug",
  };
  const budget = Number(f.token_budget.value);
  if (budget > 0) body.token_budget = Math.floor(budget);
  btn.disabled = true;
  btn.textContent = "召回中…";
  $("#recall-meta").textContent = "";
  try {
    const resp = await getJSON("/api/recall-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // layers_used 是去重层级列表,按来源实际条数计数才有意义
    const counts = {};
    (resp.sources || []).forEach(s => {
      const l = s.layer || "—";
      counts[l] = (counts[l] || 0) + 1;
    });
    const badges = Object.entries(counts)
      .map(([l, n]) => tag(`${l} ×${n}`, String(l).toLowerCase())).join("");
    $("#recall-meta").innerHTML =
      `耗时 ${fmtNum(resp.elapsed_ms)} ms · tokens ${fmtNum(resp.token_count)}` +
      ` · 来源 ${(resp.sources || []).length} 条 ${badges}`;
    $("#recall-result").hidden = false;
    $("#recall-context").textContent = resp.context || "(无召回内容)";
    $("#recall-sources tbody").innerHTML = (resp.sources || []).length
      ? resp.sources.map(s => {
          const score = +s.score || 0;
          return `<tr tabindex="0" data-id="${esc(s.id)}">` +
            `<td>${tag(s.layer || "—", String(s.layer || "").toLowerCase())}</td>` +
            `<td class="mono">${esc(s.id)}</td>` +
            `<td class="mono">${esc(s.scope)}</td>` +
            `<td><div class="scorecell"><div class="scorebar">` +
            `<i style="width:${(score * 100).toFixed(1)}%"></i></div>` +
            `<span>${score.toFixed(3)}</span></div></td>` +
            `<td class="snippet">${esc(s.snippet ?? "")}</td></tr>`;
        }).join("")
      : emptyRow(5, "没有召回来源 — 试试更具体的关键词,或提高 top_k");
  } catch (e) {
    $("#recall-meta").textContent = `召回失败:${e}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "召回";
  }
}

$("#recall-sources tbody").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDetail(tr.dataset.id);
});
$("#recall-sources tbody").addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDetail(tr.dataset.id);
});

$("#copy-context").addEventListener("click", async () => {
  const btn = $("#copy-context");
  try {
    await navigator.clipboard.writeText($("#recall-context").textContent);
    btn.textContent = "已复制";
  } catch {
    btn.textContent = "复制失败";
  }
  setTimeout(() => { btn.textContent = "复制"; }, 1500);
});

/* ---------- 自动刷新 ---------- */

let refreshTimer = null;
const refreshBtn = $("#auto-refresh");

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    if (document.hidden) return;
    loadHealth();
    loadStats();
    loadTasks();
  }, REFRESH_MS);
}

function stopRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
}

function setAutoRefresh(on) {
  refreshBtn.setAttribute("aria-pressed", String(on));
  localStorage.setItem("smilex.autorefresh", on ? "1" : "0");
  if (on) startRefresh(); else stopRefresh();
}

refreshBtn.addEventListener("click", () =>
  setAutoRefresh(refreshBtn.getAttribute("aria-pressed") !== "true"));

/* ---------- API Key 输入(§15.4) ---------- */

const apiKeyInput = $("#api-key-input");
apiKeyInput.value = localStorage.getItem("smilex.apikey") || "";
apiKeyInput.addEventListener("change", () => {
  localStorage.setItem("smilex.apikey", apiKeyInput.value.trim());
  loadHealth();  // 保存后立即用新 key 重试
});

/* ---------- 初始化 ---------- */

$("#browse-form").addEventListener("submit", browse);
$("#recall-form").addEventListener("submit", recallTest);

const savedTab = localStorage.getItem("smilex.tab");
if (savedTab && document.getElementById("tab-" + savedTab)) switchTab(savedTab);
if (localStorage.getItem("smilex.autorefresh") === "0") {
  refreshBtn.setAttribute("aria-pressed", "false");
} else {
  startRefresh();
}

loadHealth();
loadStats();
loadTasks();
browse({ preventDefault() {}, target: $("#browse-form") });
