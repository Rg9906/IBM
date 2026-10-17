"use strict";
// Plain JS, no libraries, no network assets. Server strings are written with textContent only.
const $ = (id) => document.getElementById(id);
const GW = ["G1", "G2", "G3"], BK = ["B1", "B2", "B3", "B4", "B5"];
const H = { gw: {}, seg: {} };                      // histories
const S = { sel: "gateway/G1/*", truth: [], alerts: [], lastTick: null, lastChange: Date.now(), state: null,
            rec: null, paused: false, degraded: false, scn: null, seen: new Set() };
try { $("key").value = sessionStorage.getItem("qskey") || ""; } catch (e) { /* storage may be blocked */ }
$("key").addEventListener("input", () => { try { sessionStorage.setItem("qskey", $("key").value); } catch (e) { /* ignore */ } });

function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
async function api(path, method = "GET", body) {
  const opt = { method, headers: { "X-QS-Key": $("key").value } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || r.status);
  return j;
}
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "-" : Number(v).toFixed(d);

// ---- tabs and controls
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === b));
  $("tab-live").classList.toggle("hidden", b.dataset.tab !== "live");
  $("tab-arena").classList.toggle("hidden", b.dataset.tab !== "arena");
  if (b.dataset.tab === "arena") loadArena();
}));
document.querySelectorAll("[data-scn]").forEach((b) => b.addEventListener("click", async () => {
  try {
    H.gw = {}; H.seg = {}; S.alerts = []; S.truth = []; S.lastTick = null; S.rec = null; S.seen = new Set();
    S.scn = await api("/v1/sim/scenario", "POST", { scenario: b.dataset.scn, seed: +$("seed").value,
      speed: +$("speed").value, auto_operator: $("auto").value === "1" });
    S.lastChange = Date.now();
    setTimeout(async () => { try { S.truth = await api("/v1/sim/truth"); } catch (e) { /* demo only */ } }, 500);
    showBanner("");
  } catch (e) { showBanner("Could not start: " + e.message); }
}));
$("pause").addEventListener("click", async () => {
  try { await api("/v1/sim/control", "POST", { action: S.paused ? "resume" : "pause" }); } catch (e) { showBanner(e.message); }
});
$("stop").addEventListener("click", async () => {
  try { await api("/v1/sim/control", "POST", { action: "stop" }); } catch (e) { showBanner(e.message); }
});
function showBanner(t) { $("banner").textContent = t; $("banner").classList.toggle("hidden", !t); }

// ---- polling state
async function pollState() {
  let st;
  try { st = await (await fetch("/v1/state")).json(); } catch (e) { return; }
  S.state = st; S.paused = !!st.paused;
  $("pause").textContent = S.paused ? "Resume" : "Pause";
  const sc = st.scenario || S.scn;
  $("h-scn").textContent = sc ? `${sc.scenario}  seed ${sc.seed}` : "no scenario";
  $("h-time").textContent = "t = " + fmt((st.sim_t_ms || 0) / 1000, 0) + " s";
  const hs = $("h-state"); hs.textContent = st.state; hs.className = "pill state " + st.state;
  $("h-eps").textContent = (st.events || 0) + " events";
  if (st.tick_ts !== S.lastTick) { S.lastTick = st.tick_ts; S.lastChange = Date.now(); record(st); }
  const stalled = !!sc && !S.paused && st.tick_ts !== null && Date.now() - S.lastChange > 12000;
  S.degraded = st.state === "DEGRADED_MONITORING" || (st.degraded || []).length > 0 || stalled;
  if (st.error) showBanner("Scenario runner failed: " + st.error);
  else if (S.degraded) showBanner("Monitoring degraded: " + ((st.degraded || []).join(", ") || "no new ticks for 12 s") + ". Actions are disabled.");
  else if ($("banner").textContent.startsWith("Monitoring degraded")) showBanner("");
  drawHeat(st); drawMetrics(); drawRace(); drawCascade(); drawVerif(st);
  renderRec();
}
function record(st) {
  for (const [k, v] of Object.entries(st.segments || {})) {
    (H.seg[k] = H.seg[k] || []).push({ t: st.tick_ts, f: v.f, score: v.score });
    if (H.seg[k].length > 70) H.seg[k].shift();
    if (k.startsWith("gateway/")) {
      const g = k.split("/")[1];
      (H.gw[g] = H.gw[g] || []).push({ t: st.tick_ts, p: v.p_stuck || 0 });
      if (H.gw[g].length > 70) H.gw[g].shift();
    }
  }
}

// ---- heat map
function scoreColor(s) {
  if (s === null || s === undefined) return "#2a3340";
  const x = Math.max(0, Math.min(1, s));
  return `hsl(${Math.round(150 - 150 * x)},65%,${28 + 12 * x}%)`;
}
function drawHeat(st) {
  const t = $("heat"); t.replaceChildren();
  const hr = el("tr"); hr.append(el("th", ""));
  BK.forEach((b) => hr.append(el("th", b))); hr.append(el("th", "gateway")); t.append(hr);
  const prim = st.verdict && st.verdict.primary ? st.verdict.primary.join("/") : null;
  const cell = (key, label) => {
    const v = (st.segments || {})[key];
    const td = el("td", v && v.score !== null && v.score !== undefined ? fmt(v.score, 2) : "");
    td.style.background = scoreColor(v ? v.score : null);
    if (key === S.sel) td.classList.add("sel");
    if (key === prim) td.classList.add("vd");
    td.title = label;
    td.addEventListener("click", () => { S.sel = key; drawHeat(st); drawMetrics(); });
    return td;
  };
  GW.forEach((g) => {
    const r = el("tr"); r.append(el("th", g));
    BK.forEach((b) => r.append(cell(`route/${g}/${b}`, `${g} to ${b}`)));
    r.append(cell(`gateway/${g}/*`, g)); t.append(r);
  });
  const br = el("tr"); br.append(el("th", "bank"));
  BK.forEach((b) => br.append(cell(`bank/*/${b}`, b))); br.append(el("td", "")); t.append(br);
  const vd = st.verdict;
  $("verdict").textContent = vd ? `Localizer: ${vd.verdict}${vd.primary ? " at " + vd.primary.join("/") : ""}` : "Localizer: no verdict yet";
}

// ---- canvas helpers
function ctxOf(id) { const c = $(id), x = c.getContext("2d"); x.clearRect(0, 0, c.width, c.height); return [x, c.width, c.height]; }
function line(x, pts, color, w, h, ymax) {
  if (pts.length < 2) return;
  x.strokeStyle = color; x.lineWidth = 1.6; x.beginPath();
  pts.forEach((p, i) => {
    const px = (i / Math.max(pts.length - 1, 1)) * (w - 10) + 5, py = h - 14 - (Math.min(p, ymax) / ymax) * (h - 24);
    if (i) x.lineTo(px, py); else x.moveTo(px, py);
  });
  x.stroke();
}
function drawMetrics() {
  const [x, w, h] = ctxOf("metrics"); $("seg-name").textContent = S.sel;
  const hs = H.seg[S.sel] || [];
  x.fillStyle = "#8b98a8"; x.font = "11px system-ui";
  if (hs.length < 2) { x.fillText("waiting for data", 10, 20); return; }
  const ser = [["timeout_ratio", "#f87171"], ["p95", "#60a5fa"], ["queue_delay_est", "#fbbf24"], ["pending_frac", "#a3e635"]];
  for (const [k, c] of ser) {
    const v = hs.map((p) => (p.f[k] === null || p.f[k] === undefined ? 0 : p.f[k]));
    line(x, v, c, w, h, Math.max(...v, 1e-9));
  }
  x.fillStyle = "#8b98a8"; x.fillText("each series scaled to its own maximum; last ~5 min", 6, 12);
}
function drawRace() {
  const [x, w, h] = ctxOf("race"); const st = S.state; const now = (st && st.sim_t_ms) || 1;
  const lanes = ["D0", "D3"], tmax = Math.max(now, 60000);
  x.font = "11px system-ui"; x.fillStyle = "#8b98a8";
  lanes.forEach((d, i) => {
    const y = 40 + i * 36; x.fillText(d, 4, y + 4); x.strokeStyle = "#2a3340";
    x.beginPath(); x.moveTo(30, y); x.lineTo(w - 6, y); x.stroke();
  });
  const X = (t) => 30 + (t / tmax) * (w - 40);
  const inc = S.truth.filter((t) => t.is_incident);
  inc.forEach((t) => {
    x.strokeStyle = "#fbbf24"; x.beginPath(); x.moveTo(X(t.onset_ms), 14); x.lineTo(X(t.onset_ms), h - 6); x.stroke();
    x.fillStyle = "#fbbf24"; x.fillText("onset", X(t.onset_ms) + 3, 12);
  });
  const first = {};
  S.alerts.forEach((a) => { if (!(a.detector in first)) first[a.detector] = a; });
  Object.values(first).forEach((a) => {
    const i = lanes.indexOf(a.detector.slice(0, 2)); if (i < 0) return;
    const y = 40 + i * 36; x.fillStyle = "#f87171"; x.fillRect(X(a.ts) - 2, y - 8, 4, 16);
    const on = inc.length ? inc[0].onset_ms : null; x.fillStyle = "#e6edf3";
    x.fillText(on !== null && a.ts >= on ? "+" + fmt((a.ts - on) / 1000, 0) + " s" : "alert", X(a.ts) + 5, y - 10);
  });
}
function drawCascade() {
  const [x, w, h] = ctxOf("cascade"); const cols = { G1: "#f87171", G2: "#60a5fa", G3: "#a3e635" };
  x.strokeStyle = "#8b98a8"; x.setLineDash([4, 4]); x.beginPath();
  const y5 = h - 14 - 0.5 * (h - 24); x.moveTo(5, y5); x.lineTo(w - 5, y5); x.stroke(); x.setLineDash([]);
  GW.forEach((g) => line(x, (H.gw[g] || []).map((p) => p.p), cols[g], w, h, 1));
  x.fillStyle = "#8b98a8"; x.font = "11px system-ui"; x.fillText("p_stuck 0 to 1", 6, 12);
}
function drawVerif(st) {
  const p = (st.segments || {})["platform/*/*"]; const f = p ? p.f : {};
  const kv = $("verif"); kv.replaceChildren();
  [["pending payments", fmt(f.pending_count, 0)], ["pending share", fmt(f.pending_frac, 3)], ["status checks / s", fmt(f.status_check_rate, 1)]]
    .forEach(([a, b]) => { const d = el("div"); d.append(el("span", a), el("span", b)); kv.append(d); });
}

// ---- recommendation card
async function pollRec() {
  try {
    const recs = await (await fetch("/v1/recommendations?status=pending")).json();
    S.rec = recs.length ? recs[recs.length - 1] : null;
  } catch (e) { /* keep last */ }
  renderRec();
}
function renderRec() {
  const box = $("rec"), r = S.rec;
  const typing = document.activeElement && document.activeElement.id === "note";
  if (typing && r) return;                      // do not rebuild the card while the operator types a note
  box.replaceChildren();
  if (!r) { box.textContent = "No pending recommendation."; box.className = "muted"; return; }
  box.className = "";
  const head = el("div");
  head.append(el("b", r.severity, "sev-" + r.severity),
    el("span", `  ${r.verdict} at ${r.segment.join("/")}  confidence ${fmt(r.confidence, 2)}` +
      (r.p_stuck != null ? `  p_stuck ${fmt(r.p_stuck, 2)}` : "") +
      (r.countdown_s_estimate != null ? `  about ${fmt(r.countdown_s_estimate, 0)} s to lock-in` : "")));
  box.append(head);
  const ul = el("ul"); (r.reasons || []).forEach((t) => ul.append(el("li", t))); box.append(ul);
  const boxes = [];
  r.actions.forEach((a) => {
    const d = el("div", "", "act" + (a.eligible ? "" : " blocked"));
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.disabled = !a.eligible; cb.checked = a.eligible;
    boxes.push([a, cb]);
    d.append(cb, el("b", " " + a.id), el("span", " " + JSON.stringify(a.params || {})));
    if (!a.eligible) d.append(el("div", "blocked: " + (a.blocked_by || "not eligible"), "muted"));
    box.append(d);
  });
  const off = S.degraded || !$("key").value;
  const mk = (label, kind) => { const b = el("button", label); b.disabled = off; b.addEventListener("click", () => decide(r, kind, boxes)); return b; };
  const note = document.createElement("input"); note.id = "note"; note.placeholder = "note"; note.size = 30;
  box.append(mk("Approve all", "approve_all"), mk("Approve selected", "approve_selected"), mk("Reject", "reject"), note);
  if (off) box.append(el("div", S.degraded ? "Actions disabled while monitoring is degraded." : "Enter the API key to decide.", "muted"));
}
async function decide(r, kind, boxes) {
  try {
    const sel = boxes.filter(([a, cb]) => cb.checked && a.eligible).map(([a]) => a.id);
    await api(`/v1/recommendations/${r.rec_id}/decision`, "POST", {
      decision: kind, operator: $("op").value || "operator1", note: ($("note") || {}).value || "",
      actions_selected: kind === "approve_selected" ? sel : [] });
    S.rec = null; pollRec(); pollLog();
  } catch (e) { showBanner("Decision failed: " + e.message); }
}
async function pollLog() {
  try {
    const rows = await (await fetch("/v1/decisions")).json(); const tb = $("dlog").tBodies[0]; tb.replaceChildren();
    rows.forEach((d) => {
      const tr = el("tr");
      [fmt(d.ts / 1000, 0) + " s", d.rec_id, d.operator, d.decision, (d.actions || []).join(", "), d.note].forEach((c) => tr.append(el("td", c)));
      tb.append(tr);
    });
  } catch (e) { /* keep last */ }
}
async function pollRollbacks() {
  try {
    const rows = await (await fetch("/v1/rollbacks")).json(); const box = $("rb"); box.replaceChildren();
    if (!rows.length) { box.textContent = "None."; return; }
    rows.forEach((r) => {
      const d = el("div", "", "act"); d.append(el("b", r.action), el("span", "  from " + r.rec_id + "  "));
      const b = el("button", "Approve rollback"); b.disabled = S.degraded || !$("key").value;
      b.addEventListener("click", async () => {
        try { await api("/v1/rollbacks/" + encodeURIComponent(r.key) + "/approve", "POST", { operator: $("op").value || "operator1" }); pollRollbacks(); }
        catch (e) { showBanner("Rollback failed: " + e.message); }
      });
      d.append(b); box.append(d);
    });
  } catch (e) { /* keep last */ }
}
async function pollPerf() {
  try {
    const m = await (await fetch("/v1/metrics")).json(); const kv = $("perf"); kv.replaceChildren();
    const rows = [["detector mode", m.detector_mode], ["detector model", (m.models || {}).detector],
      ["forecaster model", (m.models || {}).forecaster], ["buffered events", m.pending_in_buffer]];
    Object.entries(m.ingest || {}).forEach(([k, v]) => rows.push(["ingest " + k, v]));
    rows.forEach(([a, b]) => { const d = el("div"); d.append(el("span", a), el("span", String(b))); kv.append(d); });
  } catch (e) { /* keep last */ }
}

// ---- stream
function connect() {
  const es = new EventSource("/v1/stream");
  es.onopen = () => { $("h-conn").textContent = "stream live"; $("h-conn").className = "pill good"; };
  es.onerror = () => { $("h-conn").textContent = "stream offline"; $("h-conn").className = "pill bad"; };
  es.addEventListener("alert", (e) => {
    if (e.lastEventId && S.seen.has(e.lastEventId)) return;       // a reconnect replays the journal
    if (e.lastEventId) S.seen.add(e.lastEventId);
    S.alerts.push(JSON.parse(e.data)); if (S.alerts.length > 300) S.alerts.shift();
  });
  ["recommendation", "decision", "incident"].forEach((k) => es.addEventListener(k, () => { pollRec(); pollLog(); }));
}

// ---- arena tab
const median = (a) => {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
async function loadArena() {
  const root = $("arena"); root.replaceChildren();
  let runs = [];
  try { runs = await (await fetch("/v1/arena")).json(); } catch (e) { /* handled below */ }
  if (!runs.length) { root.textContent = "results/arena.json not found."; return; }
  const metrics = [["peak_queue", "peak queue", false], ["excess_retries", "excess retries", false], ["recovery_s", "recovery s", false],
    ["unique_success", "unique success", true], ["duplicate_commits", "duplicate commits", false]];
  [...new Set(runs.map((r) => r.scenario))].sort().forEach((sc) => {
    root.append(el("h3", "Scenario " + sc));
    const t = el("table", "", "arena-t"); const hr = el("tr");
    hr.append(el("th", "universe"), el("th", "runs"), el("th", "not recovered"));
    metrics.forEach(([, l]) => hr.append(el("th", l))); t.append(hr);
    const us = [...new Set(runs.filter((r) => r.scenario === sc).map((r) => r.universe))].sort();
    const med = {};
    us.forEach((u) => {
      const rs = runs.filter((r) => r.scenario === sc && r.universe === u);
      med[u] = { n: rs.length, nr: rs.filter((r) => !r.recovered).length };
      metrics.forEach(([k]) => { med[u][k] = median(rs.map((r) => r[k]).filter((v) => v !== null && v !== undefined)); });
    });
    us.forEach((u) => {
      const tr = el("tr"); tr.append(el("td", u), el("td", String(med[u].n)), el("td", String(med[u].nr)));
      metrics.forEach(([k, , hi]) => {
        const vals = us.map((x) => med[x][k]).filter((v) => v !== null);
        const best = hi ? Math.max(...vals) : Math.min(...vals);
        tr.append(el("td", med[u][k] === null ? "-" : Math.round(med[u][k]).toString(), med[u][k] === best ? "best" : ""));
      });
      t.append(tr);
    });
    root.append(t);
  });
  root.append(el("p", "Recovery medians exclude runs that never recovered (see the not-recovered column). Best value per column in green.", "muted"));
}

pollState(); pollRec(); pollLog(); pollPerf(); pollRollbacks(); connect();
setInterval(pollState, 1000); setInterval(pollRec, 2500); setInterval(pollLog, 3000); setInterval(pollRollbacks, 3000); setInterval(pollPerf, 5000);
