"""The control page's client script: fetch helper, view grid, and readouts.

Single concept: the page's read model -- the api/say helpers, the one-tap view
grid, the live state readout, the parameter editor, the preview, and the
show/blank/power controls that act on them.
"""

_SCRIPT_READ = """<script>
async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.error) || ("HTTP " + r.status));
  return body;
}
function say(msg, isErr) {
  const el = document.getElementById("result");
  el.textContent = msg; el.style.color = isErr ? "#f88" : "#9cf";
}
let SCHEMAS = {};
let CURRENT = null;
// The big four stay pinned at the top of the one-tap grid so they need
// no hunt; every other renderer follows alphabetically. Broken entries
// sink to the end, disabled.
const PINNED = ["clock", "chat", "row", "stream"];
function orderedNames() {
  const names = Object.keys(SCHEMAS);
  const ok = names.filter((n) => !(SCHEMAS[n] && SCHEMAS[n].broken));
  const broken = names.filter((n) => SCHEMAS[n] && SCHEMAS[n].broken);
  const pinned = PINNED.filter((n) => ok.includes(n));
  const rest = ok.filter((n) => !PINNED.includes(n)).sort();
  return pinned.concat(rest, broken.sort());
}
function buildViewGrid() {
  const grid = document.getElementById("viewgrid");
  grid.innerHTML = "";
  for (const name of orderedNames()) {
    const r = SCHEMAS[name];
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = name;
    b.dataset.renderer = name;
    if (r && r.broken) {
      b.disabled = true;
      b.title = "broken: " + r.broken;
    } else {
      b.onclick = () => oneTapShow(name);
    }
    if (name === CURRENT) b.classList.add("active");
    grid.appendChild(b);
  }
}
function markCurrent() {
  for (const b of document.getElementById("viewgrid").children) {
    b.classList.toggle("active", b.dataset.renderer === CURRENT);
  }
  document.getElementById("tb-view").textContent = CURRENT || "(blank)";
}
async function oneTapShow(name) {
  try {
    await api("/show", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ renderer: name, params: {} }) });
    say("showing " + name);
    refreshState(); refreshPreview();
  } catch (err) { say("show failed: " + err.message, true); }
}
async function refreshState() {
  try {
    await api("/health");
    const s = await api("/state");
    document.getElementById("health").className = "dot ok";
    document.getElementById("healthtext").textContent =
      "healthy \u00b7 " + s.display.width + "x" + s.display.height +
      " \u00b7 " + Object.keys(SCHEMAS).length + " renderers";
    CURRENT = s.renderer || null;
    markCurrent();
    document.getElementById("cur-renderer").textContent = s.renderer || "(blank)";
    document.getElementById("cur-age").textContent =
      s.age_seconds == null ? "\u2013" : Math.round(s.age_seconds) + "s";
    document.getElementById("cur-power").textContent = s.screen.power;
    const bl = s.screen.backlight;
    document.getElementById("cur-bl").textContent = bl.available
      ? (bl.value + " / " + bl.max) : "no backlight device";
    document.getElementById("cur-blank").textContent = s.screen.fb_blank;
    const feeds = s.feeds || {};
    const bits = [];
    for (const [r, inputs] of Object.entries(feeds)) {
      for (const [i, f] of Object.entries(inputs)) {
        bits.push(r + "." + i + ":" + f.health + "(" + f.count + ")");
      }
    }
    document.getElementById("cur-feeds").textContent = bits.length ? bits.join(" ") : "none";
    const sw = s.switch || {};
    document.getElementById("cur-switch").textContent =
      (sw.first_pixel_ms == null ? "\u2013" : (sw.first_pixel_ms + " ms to pixel")) +
      (sw.fresh_frame_ms == null ? "" : (" / " + sw.fresh_frame_ms + " ms fresh"));
    const e = document.getElementById("cur-err");
    e.textContent = s.last_error || "none";
    e.style.color = s.last_error ? "#f88" : "";
    const d = s.deploy || {};
    const when = d.deployed ? (d.date || "unknown date") : "never recorded";
    document.getElementById("dep-when").textContent = when;
    document.getElementById("dep-sha").textContent =
      d.deployed ? (d.sha || "?") : "\u2013";
    document.getElementById("dep-who").textContent =
      d.deployed ? (d.deployer || "?") : "\u2013";
    document.getElementById("tb-dep").textContent =
      d.deployed ? ((d.sha || "?").slice(0, 7) + " \u00b7 " + when) : "never recorded";
  } catch (err) {
    document.getElementById("health").className = "dot bad";
    document.getElementById("healthtext").textContent = "unreachable: " + err.message;
  }
}
function buildParams(name) {
  const box = document.getElementById("params");
  box.innerHTML = "";
  const schema = (SCHEMAS[name] && SCHEMAS[name].params) || {};
  for (const [key, spec] of Object.entries(schema)) {
    const t = (spec && spec.type) || "string";
    const lab = document.createElement("label");
    lab.htmlFor = "p_" + key;
    lab.appendChild(document.createTextNode(key));
    if (spec && spec.required) {
      const r = document.createElement("span");
      r.className = "req"; r.textContent = " *";
      lab.appendChild(r);
    }
    if (spec && spec.help) {
      const h = document.createElement("span");
      h.className = "help"; h.textContent = spec.help + " (" + t + ")";
      lab.appendChild(h);
    }
    box.appendChild(lab);
    let inp;
    if (t === "boolean") {
      inp = document.createElement("input");
      inp.type = "checkbox";
    } else if (t === "integer" || t === "number") {
      inp = document.createElement("input");
      inp.type = "number";
      if (t === "number") inp.step = "any";
    } else {
      inp = document.createElement("input");
      inp.type = "text";
    }
    inp.id = "p_" + key;
    inp.dataset.pname = key;
    box.appendChild(inp);
  }
}
async function refreshRenderers(keep) {
  const data = await api("/renderers");
  const sel = document.getElementById("renderer");
  const prev = keep ? sel.value : null;
  sel.innerHTML = "";
  SCHEMAS = {};
  for (const r of data.renderers) {
    SCHEMAS[r.name] = r;
    const o = document.createElement("option");
    o.value = r.name;
    o.textContent = r.broken ? (r.name + " (broken: " + r.broken + ")")
                             : (r.name + (r.description ? (" \u2014 " + r.description) : ""));
    if (r.broken) o.disabled = true;
    sel.appendChild(o);
  }
  if (prev && SCHEMAS[prev]) sel.value = prev;
  const showDesc = () => {
    const r = SCHEMAS[sel.value];
    document.getElementById("rdesc").textContent = r
      ? ((r.static ? "static" : "animated") + (r.description ? (" \u00b7 " + r.description) : ""))
      : "";
    buildParams(sel.value);
  };
  sel.onchange = showDesc;
  showDesc();
  buildViewGrid();
  refreshFeedbackViews(keep);
}
function collectParams(name) {
  const schema = (SCHEMAS[name] && SCHEMAS[name].params) || {};
  const out = {};
  for (const [key, spec] of Object.entries(schema)) {
    const el = document.getElementById("p_" + key);
    if (!el) continue;
    const t = (spec && spec.type) || "string";
    if (t === "boolean") { out[key] = el.checked; continue; }
    const v = el.value.trim();
    if (v === "") continue;
    if (t === "integer") { const n = parseInt(v, 10); if (!Number.isNaN(n)) out[key] = n; }
    else if (t === "number") { const n = parseFloat(v); if (!Number.isNaN(n)) out[key] = n; }
    else out[key] = v;
  }
  return out;
}
async function refreshPreview() {
  const img = document.getElementById("preview");
  try {
    const r = await fetch("/snapshot?t=" + Date.now());
    if (!r.ok) return;
    const blob = await r.blob();
    const old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
  } catch (e) { /* preview is best-effort; state poll reports health */ }
}
document.getElementById("show").onclick = async () => {
  const name = document.getElementById("renderer").value;
  try {
    await api("/show", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ renderer: name, params: collectParams(name) }) });
    say("showing " + name);
    refreshState(); refreshPreview();
  } catch (err) { say("show failed: " + err.message, true); }
};
document.getElementById("clear").onclick = async () => {
  try { await api("/clear", { method: "POST" }); say("screen blanked");
    refreshState(); refreshPreview(); }
  catch (err) { say("blank failed: " + err.message, true); }
};
document.getElementById("pon").onclick = async () => {
  try { await api("/screen/on", { method: "POST" }); say("screen on"); refreshState(); }
  catch (err) { say("power on failed: " + err.message, true); }
};
document.getElementById("poff").onclick = async () => {
  try { await api("/screen/off", { method: "POST" }); say("screen off"); refreshState(); }
  catch (err) { say("power off failed: " + err.message, true); }
};
"""
