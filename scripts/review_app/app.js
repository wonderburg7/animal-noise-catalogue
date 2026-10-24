"use strict";
// Sample review: one source recording at a time, slices marked, one key per decision.

const LABELS = {
  accept: "Accept (keep)", decline: "Decline", maybe: "Maybe pile",
  edit: "Longer clip: drag edges", prev: "Previous slice", next: "Next slice, no decision",
  next_file: "Skip to next sound file", replay: "Replay slice",
  play_file: "Play / stop whole file", cancel: "Cancel edit / stop",
};
const KEY_GLYPHS = { ArrowLeft: "←", ArrowRight: "→", ArrowUp: "↑", ArrowDown: "↓", Escape: "Esc" };
const MIN_CLIP_S = 0.03;
const LANE_H = 14, LANE_GAP = 4;

const $ = (id) => document.getElementById(id);
const tok = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

const S = {
  queue: [], fi: 0, file: null, buf: null, pyr: null, slices: [], cur: 0,
  edit: null, keys: {}, defaults: {}, dry: false, ctx: null, src: null, play: null,
  lastHead: null, view: { a: 0, b: 1 }, sliceBufs: new Map(), busy: false,
  started: false, drag: null, listening: null, dirty: true,
};

// ---------- server ----------
async function getJSON(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
async function fetchAudio(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`could not load ${url}`);
  return audioCtx().decodeAudioData(await r.arrayBuffer());
}

// ---------- audio ----------
function audioCtx() {
  if (!S.ctx) S.ctx = new AudioContext();
  return S.ctx;
}
function stop() {
  if (S.play) S.lastHead = playhead();
  if (S.src) { S.src.onended = null; try { S.src.stop(); } catch (_) { /* already stopped */ } }
  S.src = null; S.play = null;
}
// Play `buffer` from `offset` for `dur` seconds; `from` is where that offset sits on the file timeline.
function playBuffer(buffer, offset, dur, from, whole = false) {
  stop();
  const c = audioCtx();
  const src = c.createBufferSource();
  src.buffer = buffer;
  src.connect(c.destination);
  src.start(0, offset, dur);
  S.src = src;
  S.play = { t0: c.currentTime, from, to: from + dur, whole };
  src.onended = () => { if (S.src === src) { S.lastHead = S.play.to; S.src = null; S.play = null; } };
}
function playhead() {
  if (!S.play) return S.lastHead;
  return Math.min(S.play.from + (audioCtx().currentTime - S.play.t0), S.play.to);
}
const cur = () => S.slices[S.cur];

async function playCurrent() {
  const s = cur();
  if (!s || !S.started) return;
  if (S.edit) return playBuffer(S.buf, S.edit.start, S.edit.end - S.edit.start, S.edit.start);
  if (S.dry && s.edited === "True") {  // dry run never rewrites the slice file
    return playBuffer(S.buf, +s.start_s, s.end_s - s.start_s, +s.start_s);
  }
  const key = `${s.slice_id}@${s.start_s}-${s.end_s}`;
  let b = S.sliceBufs.get(key);
  if (!b) {
    try { b = await fetchAudio(`/slice/${s.slice_id}.wav?v=${encodeURIComponent(key)}`); }
    catch (e) { return toast(e.message); }
    S.sliceBufs.set(key, b);
  }
  if (cur() !== s || S.edit) return;  // moved on while loading
  playBuffer(b, 0, b.duration, +s.start_s);
}
function playFile(from) {
  if (S.play && S.play.whole && from === undefined) { stop(); return; }
  let t = from ?? S.lastHead ?? (cur() ? +cur().start_s : 0);
  if (t >= S.file.duration_s - 0.01) t = 0;
  playBuffer(S.buf, t, S.buf.duration - t, t, true);
}

// ---------- navigation ----------
async function loadFile(fi, pick = "first") {
  stop();
  S.fi = fi; S.edit = null;
  if (fi >= S.queue.length) { $("done").hidden = false; return; }
  $("done").hidden = true;
  const q = S.queue[fi];
  S.busy = true;
  $("speciesEn").textContent = q.species_en || q.species_sci;
  $("speciesSci").textContent = q.species_sci;
  $("meta").textContent = "Preparing audio…";
  try {
    S.file = await getJSON(`/api/file/${q.source_id}`);
    S.buf = await fetchAudio(`/audio/${q.source_id}.wav`);
  } catch (e) {
    S.busy = false;
    $("meta").textContent = `Could not load this file: ${e.message}`;
    return;
  }
  S.pyr = buildPyramid(S.buf.getChannelData(0));
  S.slices = S.file.slices;
  S.sliceBufs.clear();
  S.lastHead = null;
  let i = pick === "last" ? S.slices.length - 1 : S.slices.findIndex((s) => s.status === "pending");
  if (i < 0) i = 0;
  S.busy = false;
  renderMeta();
  select(i);
  const next = S.queue[fi + 1];
  if (next) fetch(`/api/file/${next.source_id}`).catch(() => {});  // warm the server cache
}
function select(i, autoplay = true) {
  stop();
  S.cur = clamp(i, 0, S.slices.length - 1);
  S.edit = null;
  S.lastHead = null;
  focusView();
  render();
  if (autoplay) playCurrent();
}
function next() {
  if (S.cur < S.slices.length - 1) select(S.cur + 1);
  else loadFile(S.fi + 1);
}
function prev() {
  if (S.cur > 0) select(S.cur - 1);
  else if (S.fi > 0) loadFile(S.fi - 1, "last");
}
async function decide(status) {
  const s = cur();
  if (!s || S.busy) return;
  const body = { slice_id: s.slice_id, status };
  if (S.edit) { body.start_s = +S.edit.start.toFixed(4); body.end_s = +S.edit.end.toFixed(4); }
  S.busy = true;
  if (S.edit) toast("Saving clip…");
  try {
    Object.assign(s, await postJSON("/api/decide", body));
  } catch (e) {
    toast(e.message);
    return;
  } finally {
    S.busy = false;
  }
  S.edit = null;
  next();
}
function toggleEdit() {
  const s = cur();
  if (!s) return;
  if (S.edit) { S.edit = null; focusView(); render(); return; }
  S.edit = { start: +s.start_s, end: +s.end_s };
  focusView(3);
  render();
}
function cancel() {
  if (S.edit) { S.edit = null; focusView(); render(); }
  else stop();
}

const ACTIONS = {
  accept: () => decide("keep"), decline: () => decide("reject"), maybe: () => decide("maybe"),
  edit: toggleEdit, prev, next, next_file: () => loadFile(S.fi + 1),
  replay: playCurrent, play_file: () => playFile(), cancel,
};

// ---------- view ----------
function focusView(pad = 2) {
  const s = cur();
  if (!s || !S.file) return;
  const dur = S.file.duration_s;
  const a0 = S.edit ? S.edit.start : +s.start_s, b0 = S.edit ? S.edit.end : +s.end_s;
  const w = Math.min(dur, Math.max(1.5, (b0 - a0) * (1 + 2 * pad)));
  let a = (a0 + b0) / 2 - w / 2;
  a = clamp(a, 0, Math.max(0, dur - w));
  S.view = { a, b: a + w };
}

// min/max per block of 256 samples so long files draw quickly at any zoom
function buildPyramid(data) {
  const B = 256, n = Math.ceil(data.length / B);
  const mn = new Float32Array(n), mx = new Float32Array(n);
  for (let k = 0; k < n; k++) {
    let lo = 1, hi = -1;
    const end = Math.min(data.length, (k + 1) * B);
    for (let i = k * B; i < end; i++) { const v = data[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    mn[k] = lo; mx[k] = hi;
  }
  return { B, mn, mx, data };
}
function colMinMax(t0, t1) {
  const sr = S.buf.sampleRate, P = S.pyr, n = P.data.length;
  const i0 = clamp(Math.floor(t0 * sr), 0, n - 1), i1 = clamp(Math.ceil(t1 * sr), i0 + 1, n);
  let lo = 1, hi = -1;
  if (i1 - i0 >= P.B * 2) {
    for (let k = Math.floor(i0 / P.B); k < Math.ceil(i1 / P.B); k++) {
      if (P.mn[k] < lo) lo = P.mn[k]; if (P.mx[k] > hi) hi = P.mx[k];
    }
  } else {
    for (let i = i0; i < i1; i++) { const v = P.data[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
  }
  return [lo, hi];
}

const canvases = {
  overview: { el: $("overview"), off: document.createElement("canvas"), h: 150,
    range: () => ({ a: 0, b: S.file.duration_s }) },
  detail: { el: $("detail"), off: document.createElement("canvas"), h: 190, range: () => S.view },
};
function geom(c) {
  const w = c.el.clientWidth, h = c.h;
  const waveH = h - 2 * LANE_H - 2 * LANE_GAP;
  return { w, h, waveH, laneY: [waveH + LANE_GAP, waveH + 2 * LANE_GAP + LANE_H] };
}
function sizeCanvases() {
  const dpr = window.devicePixelRatio || 1;
  for (const c of Object.values(canvases)) {
    const { w, h } = geom(c);
    for (const el of [c.el, c.off]) { el.width = Math.round(w * dpr); el.height = Math.round(h * dpr); }
    c.el.style.height = `${h}px`;
  }
  S.dirty = true;
}
const statusColor = (st) => tok(`--${["keep", "reject", "maybe"].includes(st) ? st : "pending"}`);

function drawStatic(c) {
  const dpr = window.devicePixelRatio || 1;
  const g = c.off.getContext("2d");
  const { w, h, waveH, laneY } = geom(c);
  const { a, b } = c.range();
  const tx = (t) => ((t - a) / (b - a)) * w;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const s = cur();

  // current slice band (or edit span) behind the waveform
  const span = S.edit ? [S.edit.start, S.edit.end] : s ? [+s.start_s, +s.end_s] : null;
  if (span) {
    g.globalAlpha = 0.16;
    g.fillStyle = S.edit ? tok("--edit") : statusColor(s.status);
    g.fillRect(tx(span[0]), 0, Math.max(2, tx(span[1]) - tx(span[0])), waveH);
    g.globalAlpha = 1;
  }

  // waveform
  const mid = waveH / 2, amp = waveH / 2 - 2;
  g.fillStyle = tok("--wave");
  for (let x = 0; x < w; x++) {
    const [lo, hi] = colMinMax(a + ((b - a) * x) / w, a + ((b - a) * (x + 1)) / w);
    const y0 = mid - hi * amp, y1 = mid - lo * amp;
    g.fillRect(x, y0, 1, Math.max(1, y1 - y0));
  }

  // slice lanes: short on top, long phrases below
  g.fillStyle = tok("--line");
  for (const y of laneY) g.fillRect(0, y + LANE_H / 2, w, 1);
  S.slices.forEach((sl, i) => {
    const x0 = tx(+sl.start_s), x1 = Math.max(x0 + 2, tx(+sl.end_s));
    if (x1 < 0 || x0 > w) return;
    const y = laneY[sl.kind === "long" ? 1 : 0];
    g.fillStyle = statusColor(sl.status);
    g.fillRect(x0, y, x1 - x0, LANE_H);
    if (i === S.cur) {
      g.strokeStyle = tok("--select"); g.lineWidth = 2;
      g.strokeRect(x0 - 1, y - 1, x1 - x0 + 2, LANE_H + 2);
    }
  });

  // edit handles
  if (S.edit) {
    g.fillStyle = tok("--edit");
    for (const t of [S.edit.start, S.edit.end]) {
      const x = tx(t);
      g.fillRect(x - 1, 0, 2, waveH);
      g.fillRect(x - 5, 0, 10, 12);
      g.fillRect(x - 5, waveH - 12, 10, 12);
    }
  }

  // detail window outline on the overview
  if (c === canvases.overview) {
    g.strokeStyle = tok("--muted"); g.lineWidth = 1; g.setLineDash([4, 3]);
    g.strokeRect(tx(S.view.a) + 0.5, 0.5, Math.max(3, tx(S.view.b) - tx(S.view.a)) - 1, waveH - 1);
    g.setLineDash([]);
  }
}
function frame() {
  try { draw(); } finally { requestAnimationFrame(frame); }
}
function draw() {
  if (canvases.detail.off.width !== Math.round(canvases.detail.el.clientWidth * (window.devicePixelRatio || 1))) {
    sizeCanvases();
  }
  if (S.file && S.buf && canvases.detail.off.width > 0) {
    if (S.dirty) { for (const c of Object.values(canvases)) drawStatic(c); S.dirty = false; }
    const head = playhead();
    for (const c of Object.values(canvases)) {
      const g = c.el.getContext("2d");
      const dpr = window.devicePixelRatio || 1;
      const { w, h } = geom(c);
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.clearRect(0, 0, c.el.width, c.el.height);
      g.drawImage(c.off, 0, 0);
      if (head != null) {
        const { a, b } = c.range();
        const x = ((head - a) / (b - a)) * w;
        if (x >= 0 && x <= w) {
          g.setTransform(dpr, 0, 0, dpr, 0, 0);
          g.fillStyle = tok("--playhead");
          g.fillRect(x - 1, 0, 2, h);
        }
      }
    }
    $("time").textContent = `${(head ?? 0).toFixed(2)} / ${S.file.duration_s.toFixed(2)} s`;
  }
}

// ---------- text ----------
function renderMeta() {
  const m = S.file.meta;
  $("speciesEn").textContent = m.species_en || m.species_sci;
  $("speciesSci").textContent = m.species_sci;
  const meta = $("meta");
  meta.textContent = "";
  const bits = [m.call_type, m.grp, `rec. ${m.recordist}`];
  meta.append(bits.filter(Boolean).join(" · ") + " · ");
  const lic = document.createElement("a");
  lic.href = m.licence_url; lic.target = "_blank"; lic.rel = "noopener";
  lic.textContent = `CC ${m.licence.toUpperCase()}`;
  const src = document.createElement("a");
  src.href = m.source_url; src.target = "_blank"; src.rel = "noopener";
  src.textContent = `XC${m.source_id}`;
  meta.append(lic, " · ", src, ` · tier ${m.tier} · quality ${m.quality}`);
  if (m.ultrasonic === "True") meta.append(" · ultrasonic, slowed to 44.1 kHz playback");
  meta.append(` · ${m.raw_file.split("/").pop()}`);
}
function render() {
  S.dirty = true;
  const s = cur();
  const shorts = S.slices.filter((x) => x.kind !== "long").length;
  $("progress").textContent = S.file
    ? `File ${S.fi + 1} of ${S.queue.length} · slice ${S.cur + 1} of ${S.slices.length}` +
      ` (${shorts} short, ${S.slices.length - shorts} phrases)` : "";
  if (s) {
    let txt = `${s.species_sci} · ${s.slice_id} · ${s.kind === "long" ? "phrase" : "short"} · ` +
      `${(+s.dur_s).toFixed(3)} s · ${s.status}` + (s.role ? ` · ${s.role}` : "") +
      (s.edited === "True" ? " · edited" : "");
    if (S.edit) txt += ` · new edges ${S.edit.start.toFixed(3)}–${S.edit.end.toFixed(3)} s ` +
      `(${(S.edit.end - S.edit.start).toFixed(3)} s)`;
    $("sliceInfo").textContent = txt;
  } else {
    $("sliceInfo").textContent = "No slices in this file.";
  }
  $("editNote").hidden = !S.edit;
}
function keyLabel(k) { return KEY_GLYPHS[k] || (k.length === 1 ? k.toUpperCase() : k); }
function renderKeymap() {
  const box = $("keymap");
  box.textContent = "";
  for (const [act, label] of Object.entries(LABELS)) {
    const d = document.createElement("div");
    const k = document.createElement("kbd");
    k.textContent = keyLabel(S.keys[act] || "?");
    d.append(k, label);
    box.append(d);
  }
}
let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

// ---------- mouse ----------
function hit(c, ev) {
  const r = c.el.getBoundingClientRect();
  const x = ev.clientX - r.left, y = ev.clientY - r.top;
  const { w, waveH, laneY } = geom(c);
  const { a, b } = c.range();
  const t = a + ((b - a) * x) / w;
  const tx = (tt) => ((tt - a) / (b - a)) * w;
  if (S.edit && c === canvases.detail && y < waveH) {
    if (Math.abs(x - tx(S.edit.start)) < 8) return { type: "handle", which: "start", t };
    if (Math.abs(x - tx(S.edit.end)) < 8) return { type: "handle", which: "end", t };
  }
  for (const [lane, ly] of laneY.entries()) {
    if (y < ly - 2 || y > ly + LANE_H + 2) continue;
    let best = -1, bestD = 4;
    S.slices.forEach((sl, i) => {
      if ((sl.kind === "long" ? 1 : 0) !== lane) return;
      const x0 = tx(+sl.start_s), x1 = Math.max(x0 + 2, tx(+sl.end_s));
      const d = x < x0 ? x0 - x : x > x1 ? x - x1 : 0;
      if (d < bestD) { best = i; bestD = d; }
    });
    if (best >= 0) return { type: "slice", index: best, t };
  }
  return { type: "wave", t, x };
}
for (const c of Object.values(canvases)) {
  c.el.addEventListener("mousedown", (ev) => {
    if (!S.file) return;
    const h = hit(c, ev);
    S.drag = { c, h, x0: ev.clientX, view0: { ...S.view }, moved: false };
  });
  c.el.addEventListener("mousemove", (ev) => {
    if (!S.file || S.drag) return;
    const h = hit(c, ev);
    c.el.style.cursor = h.type === "handle" ? "ew-resize" : h.type === "slice" ? "pointer" : "crosshair";
    const tip = $("tooltip");
    if (h.type === "slice") {
      const s = S.slices[h.index];
      tip.textContent = `${s.species_en || s.species_sci} (${s.species_sci}) · ${s.slice_id} · ` +
        `${s.kind === "long" ? "phrase" : "short"} · ${(+s.dur_s).toFixed(3)} s · ${s.status}`;
      tip.style.left = `${ev.clientX + 12}px`; tip.style.top = `${ev.clientY + 14}px`;
      tip.hidden = false;
    } else tip.hidden = true;
  });
  c.el.addEventListener("mouseleave", () => { $("tooltip").hidden = true; });
  c.el.addEventListener("wheel", (ev) => {
    if (c !== canvases.detail || !S.file) return;
    ev.preventDefault();
    const { t } = hit(c, ev);
    const f = ev.deltaY > 0 ? 1.25 : 0.8, dur = S.file.duration_s;
    const w = clamp((S.view.b - S.view.a) * f, 0.05, dur);
    const a = clamp(t - (t - S.view.a) * (w / (S.view.b - S.view.a)), 0, Math.max(0, dur - w));
    S.view = { a, b: a + w };
    S.dirty = true;
  }, { passive: false });
}
window.addEventListener("mousemove", (ev) => {
  const d = S.drag;
  if (!d) return;
  if (Math.abs(ev.clientX - d.x0) > 3) d.moved = true;
  if (!d.moved) return;
  const { w } = geom(d.c);
  if (d.h.type === "handle") {
    const t = clamp(hit(d.c, ev).t, 0, S.file.duration_s);
    if (d.h.which === "start") S.edit.start = Math.min(t, S.edit.end - MIN_CLIP_S);
    else S.edit.end = Math.max(t, S.edit.start + MIN_CLIP_S);
    S.edit.start = Math.max(0, S.edit.start);
    S.edit.end = Math.min(S.file.duration_s, S.edit.end);
    render();
  } else if (d.c === canvases.detail) {  // pan
    const span = d.view0.b - d.view0.a;
    const a = clamp(d.view0.a - ((ev.clientX - d.x0) / w) * span, 0, Math.max(0, S.file.duration_s - span));
    S.view = { a, b: a + span };
    S.dirty = true;
  }
});
window.addEventListener("mouseup", () => {
  const d = S.drag;
  S.drag = null;
  if (!d || d.moved) { if (d && d.h.type === "handle") playCurrent(); return; }
  start();
  if (d.h.type === "slice") select(d.h.index);
  else if (d.h.type === "wave") playFile(clamp(d.h.t, 0, S.file.duration_s));
});

// ---------- keys ----------
function keyName(e) {
  if (e.key === " ") return "Space";
  return e.key.length === 1 ? e.key.toLowerCase() : e.key;
}
function start() {
  if (S.started) return false;
  S.started = true;
  $("start").hidden = true;
  audioCtx().resume();
  playCurrent();
  return true;
}
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if ($("settings").open) {
    if (S.listening) { e.preventDefault(); bindKey(S.listening, keyName(e)); }
    return;
  }
  if (start()) { e.preventDefault(); return; }
  const name = keyName(e);
  const act = Object.keys(S.keys).find((k) => S.keys[k] === name);
  if (!act || !S.file) return;
  e.preventDefault();
  if (e.repeat && !["prev", "next"].includes(act)) return;
  ACTIONS[act]();
});
$("start").addEventListener("click", start);

// ---------- settings ----------
function renderBindings() {
  const box = $("bindings");
  box.textContent = "";
  for (const [act, label] of Object.entries(LABELS)) {
    const l = document.createElement("span");
    l.textContent = label;
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = S.listening === act ? "press a key…" : keyLabel(S.keys[act] || "?");
    b.className = S.listening === act ? "listening" : "";
    b.addEventListener("click", () => { S.listening = act; $("bindMsg").textContent = ""; renderBindings(); });
    box.append(l, b);
  }
}
async function saveKeys(keys) {
  try {
    S.keys = (await postJSON("/api/keys", keys)).keys;
  } catch (e) {
    $("bindMsg").textContent = e.message;
  }
  renderBindings();
  renderKeymap();
}
function bindKey(act, name) {
  S.listening = null;
  const other = Object.keys(S.keys).find((k) => k !== act && S.keys[k] === name);
  if (other) {
    $("bindMsg").textContent = `${keyLabel(name)} is already used by "${LABELS[other]}".`;
    renderBindings();
    return;
  }
  saveKeys({ ...S.keys, [act]: name });
}
$("settingsBtn").addEventListener("click", () => { S.listening = null; renderBindings(); $("settings").showModal(); });
$("finishBtn").addEventListener("click", async () => {
  stop();
  try { await postJSON("/api/quit", {}); } catch (_) { /* server already gone */ }
  $("doneTitle").textContent = "Review finished";
  $("doneText").textContent = "Decisions are saved in data/manifest.csv. You can close this tab.";
  $("done").hidden = false;
});
$("resetKeys").addEventListener("click", () => saveKeys({ ...S.defaults }));
$("settings").addEventListener("close", () => { S.listening = null; });

// ---------- boot ----------
(async function boot() {
  const [q, k] = await Promise.all([getJSON("/api/queue"), getJSON("/api/keys")]);
  S.queue = q.queue; S.dry = q.dry_run; S.keys = k.keys; S.defaults = k.defaults;
  $("dryrun").hidden = !S.dry;
  renderKeymap();
  sizeCanvases();
  requestAnimationFrame(frame);
  if (!S.queue.length) {
    $("start").hidden = true;
    $("speciesEn").textContent = "Nothing to review";
    $("meta").textContent = "No slices match the current --filter.";
    return;
  }
  loadFile(0);
})();
