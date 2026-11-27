"use strict";
/* Alpha AI dashboard: static page, Server-Sent Events in, fetch() out. No framework, no build step. */
const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const TOKEN = (document.querySelector('meta[name="alpha-token"]') || {}).content || "";
const S = { state: "idle", settings: {}, mic: null, tasks: new Map(), pinned: null, conn: "connecting", smooth: 0,
            hist: new Array(72).fill(0), lastPush: 0, auth: null, drag: false, fails: 0, es: null, rgb: [100, 116, 139] };
const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ------------------------------------------------------------------ helpers */
function toast(t) { const el = $("toast"); el.textContent = t; el.classList.add("show"); clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("show"), 2400); }
const fmtDur = (s) => { s = Math.round(s || 0); if (s < 60) return s + "s"; const m = Math.floor(s / 60), h = Math.floor(m / 60); return h ? h + "h " + String(m % 60).padStart(2, "0") + "m" : m + "m"; };
const fmtMs = (ms) => ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : ms + " ms";
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
async function post(msg) {
  try {
    const r = await fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json", "X-Alpha-Token": TOKEN }, body: JSON.stringify(msg) });
    if (r.status === 401) { banner("Session expired - reload this page (the backend was restarted)."); return false; }
    if (!r.ok) { toast("Alpha refused that (" + r.status + ")"); return false; }
    return true;
  } catch (e) { toast("Alpha is offline - retrying…"); return false; }
}
function banner(text) { const b = $("banner"); b.textContent = text || ""; b.classList.toggle("show", !!text); }

/* ---------------------------------------------------------------- connection */
function setConn(mode) {
  S.conn = mode;
  $("connPill").dataset.s = mode;
  $("connTxt").textContent = { connected: "Connected", reconnecting: "Reconnecting…", offline: "Server offline", connecting: "Connecting…" }[mode] || mode;
  if (mode !== "connected") { $("stateName").textContent = mode === "offline" ? "SERVER OFFLINE" : "RECONNECTING"; $("stateSub").textContent = "Alpha's backend isn't answering - this page keeps retrying automatically"; }
  else renderState();
}
async function ping() { try { return (await fetch("/api/ping", { cache: "no-store" })).ok; } catch (e) { return false; } }
let recoveryReload = false;
function connect() {
  if (!TOKEN || TOKEN.indexOf("__ALPHA") === 0) { banner("Open this page through Alpha (python main.py): it needs the session token the server inserts."); return; }
  const es = new EventSource("/events?t=" + encodeURIComponent(TOKEN));
  S.es = es;
  es.onopen = () => { S.fails = 0; setConn("connected"); banner(""); };
  es.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch (e) { return; } dispatch(m); };
  es.onerror = async () => {
    setConn("reconnecting");
    const alive = await ping();
    if (!alive) setConn("offline");
    else {
      // Each Alpha run gets a new dashboard token. Once a restarted backend is
      // reachable, an old page must reload to receive the new token.
      try {
        const r = await fetch("/api/snapshot?t=" + encodeURIComponent(TOKEN), { cache: "no-store" });
        if (r.status === 401 && !recoveryReload) {
          recoveryReload = true;
          banner("Alpha restarted - reconnecting to the new session…");
          window.location.reload();
          return;
        }
      } catch (e) { /* the SSE connection will retry */ }
    }
    if (es.readyState === 2) {                       // the browser gave up (server was down at that moment): retry ourselves
      es.close();
      try { const r = await fetch("/api/snapshot?t=" + encodeURIComponent(TOKEN), { cache: "no-store" }); if (r.status === 401) { banner("Session expired - reload this page (the backend was restarted)."); return; } } catch (e) { /* offline */ }
      setTimeout(connect, Math.min(5000, 500 * Math.pow(2, S.fails++)));
    }                                                // otherwise EventSource reconnects by itself and the server re-sends a snapshot
  };
}

/* ------------------------------------------------------------------ dispatch */
const handlers = {
  hello(m) {
    $("feed").replaceChildren(); $("console").replaceChildren(); S.tasks.clear();
    (m.history || []).forEach(addMessage); (m.logs || []).forEach(addLog);
    (m.tasks || []).forEach((t) => S.tasks.set(t.id, t)); renderTasks();
    buildChips(m.quick || []);
    applySettings(m.settings || {}); applyState({ state: m.state }); if (m.mic) applyMic(m.mic);
    if (m.status) applyStatus(m.status); if (m.voice_auth) applyAuth(m.voice_auth);
    if (m.confirm) showConfirm(m.confirm); else hideConfirm();
  },
  message: addMessage, log: addLog, state: applyState, settings: applySettings, mic: applyMic, status: applyStatus,
  task(m) { S.tasks.set(m.id, m); if (m.status === "running" || !S.pinned) S.pinned = m.id; renderTasks(); },
  voice_auth: applyAuth, confirm: showConfirm, confirm_done: hideConfirm,
};
function dispatch(m) { const h = handlers[m.type]; if (h) h(m); }

/* -------------------------------------------------------------------- state */
const STATE = {
  idle: ["IDLE", "Say “Alpha …” or type below"], listening: ["LISTENING", "Speak naturally - start with “Alpha”"], thinking: ["PROCESSING", "Understanding your request"],
  speaking: ["SPEAKING", "Alpha is talking"], executing: ["EXECUTING TASK", "Working on it - watch the steps on the left"],
  calibrating: ["CALIBRATING", "Stay quiet for two seconds"], paused: ["PAUSED", "Listening is paused"],
};
function applyState(m) { S.state = m.state || "idle"; renderState(); }
function renderState() {
  document.body.dataset.state = S.state;
  const [name, sub] = STATE[S.state] || STATE.idle;
  if (S.conn === "connected" || S.conn === "connecting") { $("stateName").textContent = name; $("stateSub").textContent = sub; }
}
function applySettings(s) {
  Object.assign(S.settings, s); const st = S.settings;
  $("brainTxt").textContent = st.provider ? "Brain: " + st.provider : "";
  $("btnListen").setAttribute("aria-pressed", String(!!st.listening)); $("btnListen").querySelector("span").textContent = st.listening ? "Listening" : "Paused"; $("btnListen").disabled = st.has_mic === false;
  $("btnVoice").setAttribute("aria-pressed", String(!st.muted)); $("btnVoice").querySelector("span").textContent = st.muted ? "Voice muted" : "Voice on";
  $("wakeTxt").textContent = st.strict_wake === false ? "follow-ups on" : "say “Alpha …”";
  $("lockTxt").textContent = (st.voice_lock || "").replace(/^Voice lock:\s*/i, "") || "–";
  if (!S.drag) {
    if (st.gate_db != null) { $("gateSlider").value = st.gate_db; $("gateVal").textContent = st.gate_db; }
    if (st.gain != null) { $("gainSlider").value = st.gain; $("gainVal").textContent = Number(st.gain).toFixed(1); }
    if (st.auto_gain != null) { $("autoGain").checked = !!st.auto_gain; $("gainSlider").disabled = !!st.auto_gain; }
  }
}

/* ---------------------------------------------------------------------- feed */
function addMessage(m) {
  const role = ["user", "alpha", "system", "heard", "security"].includes(m.role) ? m.role : "system";
  const feed = $("feed"), stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  const e = el("div", "msg " + role);
  if (role === "user" || role === "alpha") {
    const when = new Date((m.ts || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    e.appendChild(el("span", "m", (role === "user" ? "You" : "Alpha") + (m.via === "voice" ? " · 🎙" : m.via === "quick" ? " · quick" : "") + " · " + when));
  }
  const body = el("span", "", (role === "security" ? "🚫 " : "") + (m.text || "")); body.dir = "auto"; e.appendChild(body);
  feed.appendChild(e);
  while (feed.children.length > 300) feed.firstChild.remove();
  if (stick) feed.scrollTop = feed.scrollHeight;
  if (!m.restored && (role === "user" || role === "alpha" || role === "security")) {   // live caption under the orb
    const c = $("caption"); c.textContent = (role === "user" ? "You: " : role === "alpha" ? "Alpha: " : "🚫 ") + (m.text || ""); c.dir = "auto";
    c.style.color = role === "security" ? "var(--bad)" : "";
  }
}
function addLog(m) {
  const box = $("console"), stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const d = el("div", "ln " + m.level); d.appendChild(el("span", "lv", m.level.slice(0, 4)));
  d.appendChild(el("span", "nm", (m.name || "").replace(/^alpha\./, "") + " ")); d.appendChild(document.createTextNode(m.msg || ""));
  box.appendChild(d); while (box.children.length > 300) box.firstChild.remove();
  if (stick) box.scrollTop = box.scrollHeight;
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === b));
  $("feed").hidden = b.dataset.tab !== "feed"; $("console").hidden = b.dataset.tab !== "console";
}));

/* ----------------------------------------------------------------- execution */
const SI = { done: "✓", failed: "✕", skipped: "–", running: "" };
function renderTasks() {
  const list = Array.from(S.tasks.values()).sort((a, b) => (b.updated || 0) - (a.updated || 0));
  const cur = S.tasks.get(S.pinned) || list[0];
  const box = $("exec"); box.replaceChildren();
  if (!cur) { const e = el("div", "empty"); e.append("No task running.", document.createElement("br"), "Try “Alpha, open Task Manager”."); box.appendChild(e); $("execHint").textContent = "idle"; }
  else {
    $("execHint").textContent = cur.status === "running" ? "running…" : cur.status;
    const head = el("div", "tk-head"); head.append(el("span", "tk-title", cur.title), el("span", "badge " + cur.status, cur.status));
    box.appendChild(head);
    const meta = el("div", "hint", cur.agent + " agent · " + fmtMs(cur.ms || 0)); box.appendChild(meta);
    const ol = el("ol", "steps");
    (cur.steps || []).forEach((s) => { const li = el("li", s.status); li.append(el("span", "si " + s.status, SI[s.status] || ""), el("span", "tx", "Step " + s.i + ": " + s.text)); ol.appendChild(li); });
    box.appendChild(ol);
    if (cur.detail && cur.status !== "running") box.appendChild(el("div", "detail", cur.detail));
  }
  const tl = $("taskList"); tl.replaceChildren();
  list.slice(0, 8).forEach((t) => {
    const r = el("div", "row" + (cur && t.id === cur.id ? " sel" : ""));
    r.append(el("span", "t", t.title), el("span", "badge " + t.status, t.status));
    r.addEventListener("click", () => { S.pinned = t.id; renderTasks(); }); tl.appendChild(r);
  });
}
setInterval(() => { const c = S.tasks.get(S.pinned); if (c && c.status === "running") $("execHint").textContent = "running…"; }, 1000);

/* ----------------------------------------------------------- mic / voice lock */
const dbPos = (db) => clamp((db + 80) / 80, 0, 1) * 100;
function applyMic(m) {
  S.mic = m;
  $("micFill").style.width = dbPos(m.level_db) + "%"; $("micThr").style.left = dbPos(m.thr_db) + "%";
  const g = m.gate || "closed", chip = $("gateChip"); chip.className = "gate " + g;
  chip.textContent = { open: "GATE OPEN", closed: "gate closed", blocked: "blocked", calibrating: "calibrating" }[g] || g;
  $("micTxt").textContent = m.level_db.toFixed(0) + " dBFS" + (m.signal ? "" : " · no signal!");
  $("ftTxt").textContent = "false triggers " + m.false_triggers; $("ftTxt").title = "gate-dropped " + m.rejected_gate + " · empty " + m.rejected_empty + " · no wake word " + m.no_wake;
  $("devTxt").textContent = m.device ? "Input: " + m.device + " · " + m.rate + " Hz" : "";
}
function applyAuth(a) {
  S.auth = a; const card = $("authCard"), pill = $("lockPill"); let cls = "", txt;
  if (a.mode === "off") txt = "🔓 Voice lock is off: anyone can command Alpha";
  else if (!a.enrolled) { cls = "warn"; txt = "⚠ Voice lock ON but no voice profile: run  python main.py enroll"; }
  else if (a.last && Date.now() / 1000 - a.last.ts < 20) {
    cls = a.last.ok ? "good" : "bad";
    txt = a.last.ok ? "✓ Your voice was verified (score " + a.last.score + ")" : "🚫 Unknown voice ignored (score " + a.last.score + ", needs ≥ " + a.last.threshold + ")";
  } else { cls = "good"; txt = "🔒 Voice lock active (" + a.backend + ") · waiting for you"; }
  card.className = "auth " + cls; card.textContent = txt + (a.enrolled ? "  ·  accepted " + a.accepted + " / rejected " + a.rejected : "");
  pill.className = "pill " + (a.mode === "off" ? "warn" : !a.enrolled ? "warn" : cls === "bad" ? "bad" : "good");
}
setInterval(() => { if (S.auth) applyAuth(S.auth); }, 5000);

/* -------------------------------------------------------------------- system */
function applyStatus(s) {
  if (!s || s.cpu == null) return;
  $("cpuTxt").textContent = s.cpu.toFixed(0) + "%"; $("cpuBar").style.width = clamp(s.cpu, 0, 100) + "%";
  $("ramTxt").textContent = s.ram_used_gb.toFixed(1) + "/" + s.ram_total_gb.toFixed(1) + " GB"; $("ramBar").style.width = clamp(s.ram_percent, 0, 100) + "%";
  $("sysTxt").textContent = "CPU " + s.cpu.toFixed(0) + "% · RAM " + s.ram_percent.toFixed(0) + "% · Alpha " + s.alpha_mb + " MB";
  $("winTxt").textContent = s.window ? s.window.app : "–"; $("winTxt").title = s.window ? s.window.title : "";
  const t = s.screen_time && s.screen_time.today; $("stTxt").textContent = t ? fmtDur(t.total) + (t.apps && t.apps[0] ? " · top: " + t.apps[0][0] : "") : (s.tracking === false ? "tracking off" : "–");
}

/* -------------------------------------------------------------- controls etc */
function buildChips(list) {
  const box = $("chips"); box.replaceChildren();
  list.forEach((q) => { const b = el("button", "qchip", q.icon + " " + q.label); b.type = "button"; b.addEventListener("click", () => post({ type: "quick", id: q.id })); box.appendChild(b); });
}
$("btnListen").addEventListener("click", () => post({ type: "set_listening", value: !S.settings.listening }));
$("btnVoice").addEventListener("click", () => post({ type: "set_muted", value: !S.settings.muted }));
$("btnStop").addEventListener("click", () => post({ type: "stop_speaking" }));
$("btnSettings").addEventListener("click", () => { $("drawer").hidden = !$("drawer").hidden; });
$("dClose").addEventListener("click", () => { $("drawer").hidden = true; });
$("btnCal").addEventListener("click", () => post({ type: "recalibrate" }));
$("btnReset").addEventListener("click", () => post({ type: "reset_counters" }));
$("gateSlider").addEventListener("input", (e) => { S.drag = true; $("gateVal").textContent = e.target.value; });
$("gateSlider").addEventListener("change", (e) => { post({ type: "set_gate_db", value: Number(e.target.value) }); setTimeout(() => (S.drag = false), 700); });
$("gainSlider").addEventListener("input", (e) => { S.drag = true; $("gainVal").textContent = Number(e.target.value).toFixed(1); });
$("gainSlider").addEventListener("change", (e) => { post({ type: "set_gain", auto: false, gain: Number(e.target.value) }); setTimeout(() => (S.drag = false), 700); });
$("autoGain").addEventListener("change", (e) => post({ type: "set_gain", auto: e.target.checked }));
$("cmdForm").addEventListener("submit", (e) => { e.preventDefault(); const v = $("cmdInput").value.trim(); if (v) { post({ type: "command", text: v }); $("cmdInput").value = ""; } });
function showConfirm(m) { $("mText").textContent = m.text || "Are you sure?"; $("modal").hidden = false; $("mNo").focus(); }
function hideConfirm() { $("modal").hidden = true; }
$("mYes").addEventListener("click", () => { post({ type: "confirm", answer: true }); hideConfirm(); });
$("mNo").addEventListener("click", () => { post({ type: "confirm", answer: false }); hideConfirm(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("modal").hidden) $("mNo").click(); });

/* ---------------------------------------------------------------- orb canvas */
const PAL = { idle: [100, 116, 139], paused: [100, 116, 139], listening: [45, 212, 191], thinking: [251, 191, 36], speaking: [192, 132, 252], executing: [96, 165, 250], calibrating: [56, 189, 248] };
const PAL2 = { idle: [71, 85, 105], paused: [71, 85, 105], listening: [56, 189, 248], thinking: [251, 146, 60], speaking: [244, 114, 182], executing: [129, 140, 248], calibrating: [45, 212, 191] };
const cv = $("orb"), ctx = cv.getContext("2d"); let rgb2 = [71, 85, 105];
function micLevel() {
  const m = S.mic; if (!m || m.gated || m.calibrating) return 0.04;
  let v = Math.pow(clamp((m.gained_db + 62) / 47, 0, 1), 0.85); if (m.gate === "open") v = Math.max(v, 0.3); return v;
}
function frame(t) {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  const st = S.conn === "connected" ? S.state : "idle", tg = PAL[st] || PAL.idle, tg2 = PAL2[st] || PAL2.idle;
  for (let i = 0; i < 3; i++) { S.rgb[i] += (tg[i] - S.rgb[i]) * 0.07; rgb2[i] += (tg2[i] - rgb2[i]) * 0.07; }
  const c0 = S.rgb.map(Math.round).join(","), c1 = rgb2.map(Math.round).join(",");
  let target;
  if (S.conn !== "connected") target = 0.04; else switch (st) {
    case "listening": target = micLevel(); break;
    case "speaking": target = 0.38 + 0.26 * Math.abs(Math.sin(t / 210)) * (0.65 + 0.35 * Math.sin(t / 83 + 1.3)) + 0.08 * Math.sin(t / 47); break;
    case "thinking": target = 0.16 + 0.05 * Math.sin(t / 170); break;
    case "executing": target = 0.2 + 0.06 * Math.sin(t / 140); break;
    case "calibrating": target = 0.12 + 0.04 * Math.sin(t / 260); break;
    default: target = 0.05;
  }
  if (reduce) target *= 0.5;
  S.smooth += (target - S.smooth) * (target > S.smooth ? 0.35 : 0.12);
  if (t - S.lastPush > 55) { S.hist.push(S.smooth); S.hist.shift(); S.lastPush = t; }
  const cx = w / 2, cy = h * 0.46, r = Math.min(52, h * 0.17) + S.smooth * Math.min(52, h * 0.19);
  let g = ctx.createRadialGradient(cx, cy, r * 0.4, cx, cy, r * 3.6);
  g.addColorStop(0, "rgba(" + c0 + "," + (0.32 + S.smooth * 0.35) + ")"); g.addColorStop(1, "rgba(" + c0 + ",0)"); ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  for (let i = 0; i < 4; i++) {                                         // pulse rings follow the mic level
    const pulse = reduce ? 0 : (Math.sin(t / 560 + i * 1.6) + 1) * 3;
    ctx.beginPath(); ctx.arc(cx, cy, r + 13 + i * 16 + pulse + S.smooth * 20 * (i + 1) * 0.5, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(" + c1 + "," + (0.34 - i * 0.07) + ")"; ctx.lineWidth = 1.5; ctx.stroke();
  }
  if (st === "thinking" || st === "calibrating") {                      // spinner arc
    const a = reduce ? 0 : t / 380; ctx.beginPath(); ctx.arc(cx, cy, r + 11, a, a + Math.PI * 1.15);
    ctx.strokeStyle = "rgba(" + c0 + ",.95)"; ctx.lineWidth = 3.5; ctx.lineCap = "round"; ctx.stroke();
  }
  if (st === "executing") for (let i = 0; i < 5; i++) {                 // orbiting task dots
    const a = (reduce ? 0 : t / 700) + i * (Math.PI * 2 / 5), rr = r + 24;
    ctx.beginPath(); ctx.arc(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr, 3.4, 0, Math.PI * 2); ctx.fillStyle = "rgba(" + c0 + ",.95)"; ctx.fill();
  }
  g = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.1, cx, cy, r);
  g.addColorStop(0, "rgba(255,255,255,.95)"); g.addColorStop(0.35, "rgba(" + c0 + ",.95)"); g.addColorStop(1, "rgba(" + c1 + ",.88)");
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fillStyle = g; ctx.shadowColor = "rgba(" + c0 + ",.85)"; ctx.shadowBlur = 30 + S.smooth * 34; ctx.fill(); ctx.shadowBlur = 0;
  const n = S.hist.length, left = 24, usable = w - 48, slot = usable / n, bw = Math.max(2, slot * 0.6), base = h * 0.9, maxH = h * 0.1;
  const lg = ctx.createLinearGradient(left, 0, left + usable, 0); lg.addColorStop(0, "rgba(" + c1 + ",.2)"); lg.addColorStop(1, "rgba(" + c0 + ",1)"); ctx.fillStyle = lg;
  for (let i = 0; i < n; i++) {                                         // rolling waveform, newest on the right
    const bh = 2 + S.hist[i] * maxH, x = left + i * slot + (slot - bw) / 2;
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(x, base - bh, bw, bh * 2, bw / 2); else ctx.rect(x, base - bh, bw, bh * 2); ctx.fill();
  }
}
function loop(t) { frame(t); requestAnimationFrame(loop); }
renderTasks(); setConn("connecting"); requestAnimationFrame(loop); connect();
