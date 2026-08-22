/* SmileX Memory 面板 — 无构建,vanilla JS fetch */
"use strict";

function switchTab(name) {
  document.querySelectorAll(".tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + name));
}

document.querySelectorAll(".tab").forEach(b =>
  b.addEventListener("click", () => switchTab(b.dataset.tab)));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

async function loadStats() {
  const box = document.getElementById("stats-cards");
  try {
    const r = await fetch("/api/stats");
    const d = await r.json();
    const layers = Object.entries(d.fragments_by_layer || {})
      .map(([k, v]) => `${k}: ${v}`).join(" / ");
    box.innerHTML = [
      ["实体", d.entities], ["三元组", d.triples],
      ["时序片段", d.temporal_fragments], ["向量链接", d.vector_links],
      ["片段分层", layers || "-"],
    ].map(([k, v]) => `<div class="card"><div class="num">${esc(v)}</div><div>${esc(k)}</div></div>`).join("");
  } catch (e) {
    box.textContent = "加载失败: " + e;
  }
}

async function loadTasks() {
  const tbody = document.querySelector("#tasks-table tbody");
  try {
    const rows = await (await fetch("/api/tasks")).json();
    tbody.innerHTML = rows.map(r =>
      `<tr><td>${esc(r.task_id)}</td><td>${Math.round((r.progress || 0) * 100)}%</td>` +
      `<td>${esc(r.step)}</td><td>${esc(r.updated_at)}</td></tr>`).join("")
      || '<tr><td colspan="4">暂无任务运行记录</td></tr>';
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4">加载失败: ${esc(e)}</td></tr>`;
  }
}

async function browse(ev) {
  ev.preventDefault();
  const f = ev.target;
  const params = new URLSearchParams();
  if (f.q.value) params.set("q", f.q.value);
  if (f.layer.value) params.set("layer", f.layer.value);
  if (f.scope.value) params.set("scope", f.scope.value);
  const tbody = document.querySelector("#memories-table tbody");
  tbody.innerHTML = "<tr><td colspan='6'>加载中…</td></tr>";
  const rows = await (await fetch("/api/memories?" + params)).json();
  tbody.innerHTML = rows.map(r =>
    `<tr><td>${esc(r.kind)}</td><td class="content">${esc(r.content)}</td>` +
    `<td>${esc(r.scope)}</td><td>${esc(r.layer)}</td>` +
    `<td>${r.importance ?? "-"}</td><td>${esc(r.updated_at)}</td></tr>`).join("")
    || '<tr><td colspan="6">无匹配记忆</td></tr>';
}

async function recallTest(ev) {
  ev.preventDefault();
  const f = ev.target;
  const resp = await (await fetch("/api/recall-test", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({query: f.query.value, top_k: Number(f.top_k.value)}),
  })).json();
  document.getElementById("recall-meta").textContent =
    `耗时 ${resp.elapsed_ms}ms | tokens ${resp.token_count} | 命中层 ${(resp.layers_used || []).join(", ") || "-"}`;
  document.getElementById("recall-context").textContent = resp.context || "(无召回内容)";
  document.querySelector("#recall-sources tbody").innerHTML =
    (resp.sources || []).map(s =>
      `<tr><td>${esc(s.memory_id)}</td><td>${esc(s.layer)}</td><td>${esc(s.score)}</td></tr>`
    ).join("");
}

document.getElementById("browse-form").addEventListener("submit", browse);
document.getElementById("recall-form").addEventListener("submit", recallTest);
loadStats();
loadTasks();
browse({preventDefault() {}, target: document.getElementById("browse-form")});
