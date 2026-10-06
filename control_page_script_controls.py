"""The control page's client script: policy, playlist, notify, reload, feedback.

Single concept: the page's remaining controls -- the policy and playlist
editors, the notify and reload buttons, tap-to-rate feedback, and the boot
sequence that starts the refresh timers.
"""

_SCRIPT_CONTROLS = """async function refreshPolicy() {
  try {
    const p = await api("/policy");
    const c = p.config;
    document.getElementById("pol-idle-en").checked = !!c.idle.enabled;
    document.getElementById("pol-idle-after").value = c.idle.after_seconds;
    document.getElementById("pol-att-en").checked = !!c.chat_attention.enabled;
    document.getElementById("pol-att-view").value = c.chat_attention.view;
    document.getElementById("pol-att-ret").value = c.chat_attention.return_after;
    document.getElementById("pol-notify-dur").value = c.notifications.default_duration;
    const t = p.transient;
    document.getElementById("pol-status").textContent =
      "policy: idle " + (c.idle.enabled ? ("on after " + c.idle.after_seconds + "s") : "off") +
      " · attention " + (c.chat_attention.enabled ? ("on → " + c.chat_attention.view) : "off") +
      " · transient " + (t.active || "none") +
      (p.idle_off ? " · PANEL IDLE-OFF" : "");
  } catch (err) { say("policy load failed: " + err.message, true); }
}
document.getElementById("polsave").onclick = async () => {
  const num = (id) => { const v = document.getElementById(id).value.trim();
    return v === "" ? undefined : Number(v); };
  const patch = { idle: {}, chat_attention: {}, notifications: {} };
  patch.idle.enabled = document.getElementById("pol-idle-en").checked;
  const ia = num("pol-idle-after"); if (ia !== undefined) patch.idle.after_seconds = ia;
  patch.chat_attention.enabled = document.getElementById("pol-att-en").checked;
  const av = document.getElementById("pol-att-view").value.trim();
  if (av !== "") patch.chat_attention.view = av;
  const ar = num("pol-att-ret"); if (ar !== undefined) patch.chat_attention.return_after = ar;
  const nd = num("pol-notify-dur"); if (nd !== undefined) patch.notifications.default_duration = nd;
  try { await api("/policy", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) });
    say("policy saved"); refreshPolicy(); }
  catch (err) { say("policy save failed: " + err.message, true); }
};
async function refreshPlaylist() {
  try {
    const s = await api("/playlist");
    const p = (await api("/policy")).config.playlist;
    document.getElementById("pl-en").checked = !!p.enabled;
    document.getElementById("pl-place").value = p.placement;
    document.getElementById("pl-thick").value = p.thickness;
    document.getElementById("pl-dir").value = p.direction;
    document.getElementById("pl-color").value = p.color;
    const v = document.getElementById("pl-views");
    if (document.activeElement !== v) v.value = JSON.stringify(p.views);
    const vname = s.view ? s.view.renderer : "(none)";
    document.getElementById("pl-status").textContent =
      "playlist: " + (s.enabled ? ("on \u00b7 " + vname +
        (s.progress == null ? "" : (" \u00b7 " + Math.round(s.progress * 100) + "%"))) : "off") +
      (s.hold && s.hold !== "disabled" && s.hold !== "empty" ? (" \u00b7 held (" + s.hold + ")") : "") +
      (s.last_error ? (" \u00b7 error: " + s.last_error) : "");
  } catch (err) { say("playlist load failed: " + err.message, true); }
}
document.getElementById("plsave").onclick = async () => {
  let views;
  try {
    views = JSON.parse(document.getElementById("pl-views").value || "[]");
  } catch (err) { say("views is not valid JSON", true); return; }
  const patch = { playlist: {
    enabled: document.getElementById("pl-en").checked,
    placement: document.getElementById("pl-place").value,
    direction: document.getElementById("pl-dir").value,
    color: document.getElementById("pl-color").value.trim() || "#FFFFFF",
    views: views } };
  const t = document.getElementById("pl-thick").value.trim();
  if (t !== "") patch.playlist.thickness = Number(t);
  try { await api("/policy", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) });
    say("playlist saved"); refreshPlaylist(); }
  catch (err) { say("playlist save failed: " + err.message, true); }
};
document.getElementById("plpause").onclick = async () => {
  try { await api("/playlist/pause", { method: "POST" }); say("playlist paused"); refreshPlaylist(); }
  catch (err) { say("pause failed: " + err.message, true); }
};
document.getElementById("plresume").onclick = async () => {
  try { await api("/playlist/resume", { method: "POST" }); say("playlist resumed"); refreshPlaylist(); }
  catch (err) { say("resume failed: " + err.message, true); }
};
document.getElementById("plnext").onclick = async () => {
  try { await api("/playlist/next", { method: "POST" }); say("skipped to next view"); refreshPlaylist(); }
  catch (err) { say("skip failed: " + err.message, true); }
};
document.getElementById("notify").onclick = async () => {
  const body = { title: document.getElementById("nt-title").value,
    body: document.getElementById("nt-body").value,
    severity: document.getElementById("nt-sev").value };
  const d = document.getElementById("nt-dur").value.trim();
  if (d !== "") body.duration = Number(d);
  try { await api("/notify", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    say("notice showing"); refreshState(); refreshPreview(); }
  catch (err) { say("notify failed: " + err.message, true); }
};
document.getElementById("reload").onclick = async () => {
  const sha = document.getElementById("rl-sha").value.trim().toLowerCase();
  const box = document.getElementById("reload-result");
  if (!/^[0-9a-f]{40}$/.test(sha)) {
    say("reload needs the full 40-character commit SHA", true);
    return;
  }
  try {
    const out = await api("/reload", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sha: sha }) });
    box.innerHTML = "";
    box.appendChild(document.createTextNode(
      "reloaded \u00b7 returns in " + out.return_in + "s \u00b7 proof: "));
    const a = document.createElement("a");
    a.href = out.commit_url;
    a.textContent = out.commit_url;
    a.target = "_blank";
    a.rel = "noopener";
    box.appendChild(a);
    say("reload proof showing");
    refreshState(); refreshPreview();
  } catch (err) { say("reload failed: " + err.message, true); }
};
let PICKED_RATING = 0;
for (const b of document.querySelectorAll("#ratebtns button")) {
  b.onclick = () => {
    PICKED_RATING = Number(b.dataset.rating);
    for (const x of document.querySelectorAll("#ratebtns button")) {
      x.classList.toggle("picked", x === b);
    }
  };
}
function refreshFeedbackViews(keep) {
  const sel = document.getElementById("fb-view");
  const prev = keep ? sel.value : null;
  sel.innerHTML = "";
  for (const name of Object.keys(SCHEMAS).sort()) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    sel.appendChild(o);
  }
  if (prev && SCHEMAS[prev]) sel.value = prev;
  else if (CURRENT && SCHEMAS[CURRENT]) sel.value = CURRENT;
}
async function refreshFeedbackSummary() {
  const box = document.getElementById("fb-summary");
  try {
    const s = await api("/feedback/summary");
    box.innerHTML = "";
    const head = document.createElement("div");
    head.className = "meta";
    head.textContent = s.total === 0 ? "no ratings yet"
      : (s.total + " rating" + (s.total === 1 ? "" : "s"));
    box.appendChild(head);
    for (const [view, agg] of Object.entries(s.views || {})) {
      const line = document.createElement("div");
      line.className = "fbline";
      const left = document.createElement("span");
      left.textContent = view + " ×" + agg.count;
      const right = document.createElement("span");
      right.textContent = agg.avg_rating == null ? "–" : ("avg " + agg.avg_rating);
      line.appendChild(left);
      line.appendChild(right);
      box.appendChild(line);
    }
  } catch (err) { say("feedback summary failed: " + err.message, true); }
}
document.getElementById("fbsend").onclick = async () => {
  if (!PICKED_RATING) { say("pick a rating 1-5 first", true); return; }
  const body = { view: document.getElementById("fb-view").value,
    rating: PICKED_RATING,
    notes: document.getElementById("fb-notes").value,
    agent: "control-page" };
  try { await api("/feedback", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    say("rating recorded");
    PICKED_RATING = 0;
    for (const x of document.querySelectorAll("#ratebtns button")) {
      x.classList.remove("picked");
    }
    document.getElementById("fb-notes").value = "";
    refreshFeedbackSummary(); }
  catch (err) { say("rating failed: " + err.message, true); }
};
(async function init() {
  try { await refreshRenderers(false); }
  catch (err) { say("could not load renderers: " + err.message, true); }
  await refreshState();
  refreshPreview();
  refreshPolicy();
  refreshPlaylist();
  refreshFeedbackSummary();
  setInterval(refreshState, 2000);
  setInterval(refreshPreview, 2000);
  setInterval(refreshPlaylist, 2000);
  setInterval(async () => {
    try { await refreshRenderers(true); } catch (e) { /* next tick */ }
    refreshFeedbackSummary();
  }, 15000);
})();
</script>
</body>
</html>
"""
