"""The control page's layout section: named styles and their slot views.

Single concept: choosing how much of the panel one view owns. The style
list, each slot's geometry and each slot's *applicable* view list all come
from ``GET /layout/presets`` -- this script never names a renderer and never
falls back to the full advertised set, so a slot can only be offered a view
that declared it fits a reduced region. Applying is one ``POST /layout``
with the style name and the chosen slots; "Single view" is ``DELETE
/layout``. Both paths are the daemon's existing shapes: nothing here adds an
endpoint or a geometry rule.
"""

_SCRIPT_LAYOUT = """
// --- layout styles: one tap each, slots list only the views that fit ---
let PRESETS = [];
let LAY_PICK = null;   // the style being edited
let LAY_CHOICE = {};   // slot -> renderer chosen for it
let LAY_LIVE = null;   // GET /layout's live state, or null
let LAY_DRAWN = null;  // what #layslots currently shows
function layoutStyle(name) {
  return PRESETS.find((p) => p.name === name) || null;
}
function layoutSignature() {
  const live = LAY_LIVE
    ? LAY_LIVE.regions.map((r) => r.name + "=" + r.renderer +
        (r.error ? "!" : "")).join(",")
    : "-";
  return [LAY_PICK, live, PRESETS.map((p) => p.name).join(",")].join("|");
}
function renderLayoutStyles() {
  const box = document.getElementById("laystyles");
  box.innerHTML = "";
  for (const p of PRESETS) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = p.label;
    b.title = p.hint || p.name;
    b.dataset.style = p.name;
    b.onclick = () => pickLayoutStyle(b.dataset.style);
    if (p.name === LAY_PICK) b.classList.add("active");
    if (LAY_LIVE && p.name === LAY_LIVE.preset) b.classList.add("live");
    box.appendChild(b);
  }
}
function slotLabel(slot) {
  const geo = Object.keys(slot.geometry || {})
    .map((k) => slot.geometry[k] + " " + k).join(", ");
  const role = slot.role === "navigation" ? "application band"
    : (slot.role === "primary" ? "primary" : "view");
  return slot.name + " \\u2014 " + role + (geo ? " (" + geo + ")" : "");
}
function renderLayoutSlots() {
  const box = document.getElementById("layslots");
  box.innerHTML = "";
  const style = layoutStyle(LAY_PICK);
  if (!style) {
    const tip = document.createElement("div");
    tip.className = "meta";
    tip.textContent = PRESETS.length ? "Pick a style to choose its views."
      : "no layout styles available";
    box.appendChild(tip);
    return;
  }
  for (const slot of style.slots) {
    const lab = document.createElement("label");
    lab.textContent = slotLabel(slot);
    const sel = document.createElement("select");
    sel.dataset.slot = slot.name;
    for (const name of slot.views) {
      const o = document.createElement("option");
      o.value = name;
      o.textContent = name;
      sel.appendChild(o);
    }
    const want = LAY_CHOICE[slot.name] || slot.default;
    if (want && slot.views.includes(want)) sel.value = want;
    sel.onchange = () => { LAY_CHOICE[sel.dataset.slot] = sel.value; };
    box.appendChild(lab);
    box.appendChild(sel);
  }
}
function pickLayoutStyle(name) {
  LAY_PICK = name;
  LAY_CHOICE = {};
  if (LAY_LIVE && LAY_LIVE.preset === name) {
    for (const r of LAY_LIVE.regions) LAY_CHOICE[r.name] = r.renderer;
  }
  renderLayoutStyles();
  renderLayoutSlots();
  LAY_DRAWN = layoutSignature();
}
function layoutStatus() {
  if (!LAY_LIVE) return "layout: one view owns the panel";
  const named = layoutStyle(LAY_LIVE.preset);
  const where = named ? named.label : "custom regions";
  const bad = LAY_LIVE.regions.filter((r) => r.error);
  return "layout: " + where + " \\u00b7 " + LAY_LIVE.regions.length +
    " regions \\u00b7 " +
    LAY_LIVE.regions.map((r) => r.name + "=" + r.renderer).join(" ") +
    (bad.length ? " \\u00b7 errors: " + bad.map((r) => r.name).join(",") : "");
}
async function refreshLayout() {
  try {
    const doc = await api("/layout/presets");
    PRESETS = doc.presets || [];
    LAY_LIVE = (await api("/layout")).layout || null;
    if (!layoutStyle(LAY_PICK) && PRESETS.length) {
      LAY_PICK = (LAY_LIVE && layoutStyle(LAY_LIVE.preset)
        ? LAY_LIVE.preset : PRESETS[0].name);
    }
    const busy = document.getElementById("layslots")
      .contains(document.activeElement);
    if (layoutSignature() !== LAY_DRAWN && !busy) {
      renderLayoutStyles();
      renderLayoutSlots();
      LAY_DRAWN = layoutSignature();
    }
    document.getElementById("lay-status").textContent = layoutStatus();
  } catch (err) { say("layout load failed: " + err.message, true); }
}
document.getElementById("layapply").onclick = async () => {
  const style = layoutStyle(LAY_PICK);
  if (!style) { say("pick a layout style first", true); return; }
  const views = {};
  for (const sel of document.querySelectorAll("#layslots select")) {
    if (sel.value) views[sel.dataset.slot] = sel.value;
  }
  try {
    await api("/layout", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset: style.name, views: views }) });
    say("layout applied: " + style.label);
    LAY_DRAWN = null;
    refreshLayout(); refreshState(); refreshPreview();
  } catch (err) { say("layout failed: " + err.message, true); }
};
document.getElementById("layclear").onclick = async () => {
  try {
    await api("/layout", { method: "DELETE" });
    say("regions cleared: single view again");
    LAY_DRAWN = null;
    refreshLayout(); refreshState(); refreshPreview();
  } catch (err) { say("clear regions failed: " + err.message, true); }
};
"""
