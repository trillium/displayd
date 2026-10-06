"""The control page body markup: one card per section.

Single concept: the controls themselves, top to bottom -- views, playback,
proof, feedback, now showing, notify, policy, and the tap-action list. Every
control drives an existing API endpoint; the wiring lives in the client script
(control_page_script_*).
"""

_BODY = """<h2>Views &mdash; one tap to show</h2>
<div class="card">
  <div id="viewgrid" aria-label="all views, one tap each"></div>
  <div class="btnrow one">
    <button id="clear" class="big ghost">Blank screen</button>
  </div>
  <details>
    <summary>Show with parameters (for views that need options)</summary>
    <label for="renderer">Renderer</label>
    <select id="renderer"></select>
    <div id="rdesc" class="meta"></div>
    <div id="params"></div>
    <button id="show">Show with options</button>
  </details>
</div>

<h2>Playback &mdash; playlist rotation</h2>
<div class="card">
  <div class="meta" id="pl-status">playlist: loading&hellip;</div>
  <div class="btnrow">
    <button id="plpause" class="big ghost">Pause</button>
    <button id="plresume" class="big">Resume</button>
    <button id="plnext" class="big ghost">Next view</button>
  </div>
  <details>
    <summary>Playlist setup (views, bar, timing)</summary>
    <label><input type="checkbox" id="pl-en" style="width:auto"> Playlist enabled (rotates through the views below)</label>
    <label for="pl-place">Bar placement<span class="help">which edge the progress bar sits on (string: top/left/bottom/right)</span></label>
    <select id="pl-place"><option>top</option><option>left</option><option>bottom</option><option>right</option></select>
    <label for="pl-thick">Bar thickness (px)<span class="help">readable at distance without stealing content (number, 2-64)</span></label>
    <input type="number" id="pl-thick" min="2" max="64">
    <label for="pl-dir">Bar direction<span class="help">fill grows empty-to-full, drain shrinks full-to-empty (string)</span></label>
    <select id="pl-dir"><option>fill</option><option>drain</option></select>
    <label for="pl-color">Default bar colour<span class="help">a per-view color or a renderer accent wins over this (string, #rrggbb)</span></label>
    <input type="text" id="pl-color">
    <label for="pl-views">Views (JSON list)<span class="help">each {"renderer": name, "params"?: {}, "dwell"?: seconds 3-3600, "color"?: override}; a manual Show pauses rotation until resumed</span></label>
    <input type="text" id="pl-views">
    <button id="plsave">Save playlist</button>
  </details>
</div>

<h2>Proof &mdash; reload &amp; deploy stamp</h2>
<div class="card">
  <div class="kv"><span>Last deploy</span><span id="dep-when">&ndash;</span></div>
  <div class="kv"><span>SHA</span><code id="dep-sha">&ndash;</code></div>
  <div class="kv"><span>By</span><span id="dep-who">&ndash;</span></div>
  <label for="rl-sha">Reload SHA (full 40-char commit)<span class="help">shows a RELOADED screen with the SHA plus a QR code to the commit page, then returns</span></label>
  <input type="text" id="rl-sha" placeholder="40 hex characters" autocapitalize="off" spellcheck="false">
  <div class="btnrow one">
    <button id="reload" class="big">Reload with proof</button>
  </div>
  <div class="meta" id="reload-result"></div>
</div>

<h2>Feedback &mdash; rate this view</h2>
<div class="card">
  <label for="fb-view">View</label>
  <select id="fb-view"></select>
  <div id="ratebtns" aria-label="rating 1 to 5">
    <button data-rating="1">1</button>
    <button data-rating="2">2</button>
    <button data-rating="3">3</button>
    <button data-rating="4">4</button>
    <button data-rating="5">5</button>
  </div>
  <label for="fb-notes">Notes (optional)</label>
  <input type="text" id="fb-notes" placeholder="readable at distance? colours? layout?">
  <div class="btnrow one">
    <button id="fbsend" class="big">Send rating</button>
  </div>
  <div id="fb-summary" style="margin-top:10px"></div>
</div>

<h2>Now showing</h2>
<div class="card">
  <div class="kv"><span>Renderer</span><code id="cur-renderer">&ndash;</code></div>
  <div class="kv"><span>On screen for</span><span id="cur-age">&ndash;</span></div>
  <div class="kv"><span>Power</span><code id="cur-power">&ndash;</code></div>
  <div class="kv"><span>Backlight</span><span id="cur-bl">&ndash;</span></div>
  <div class="kv"><span>Framebuffer blank</span><code id="cur-blank">&ndash;</code></div>
  <div class="kv"><span>Feeds</span><span id="cur-feeds">&ndash;</span></div>
  <div class="kv"><span>Last switch</span><span id="cur-switch">&ndash;</span></div>
  <div class="kv"><span>Last error</span><span id="cur-err">none</span></div>
  <div class="btnrow two">
    <button id="pon" class="big">Turn on</button>
    <button id="poff" class="big warn">Turn off</button>
  </div>
  <span class="meta">Off darkens the backlight and blanks the framebuffer;
  on restores both and repaints the last frame. Screen power controls.</span>
</div>

<div class="card">
  <div class="meta">Live preview of the panel</div>
  <img id="preview" alt="live preview of the panel">
</div>

<details class="card">
  <summary>Notify (interrupt with a notice)</summary>
  <label for="nt-title">Title<span class="req"> *</span></label>
  <input type="text" id="nt-title">
  <label for="nt-body">Body</label>
  <input type="text" id="nt-body">
  <label for="nt-sev">Severity</label>
  <select id="nt-sev"><option>info</option><option>warn</option><option>critical</option></select>
  <label for="nt-dur">Duration (seconds, blank for policy default)</label>
  <input type="number" id="nt-dur" min="1" max="300">
  <button id="notify">Show notice</button>
  <span class="meta">Interrupts what is showing, then returns.</span>
</details>

<details class="card">
  <summary>Policy (what the screen does on its own)</summary>
  <label><input type="checkbox" id="pol-idle-en" style="width:auto"> Screen off after inactivity</label>
  <label for="pol-idle-after">Idle window (seconds)<span class="help">no mutating API or feed activity for this long blanks the panel; any activity wakes it (number, 5-86400)</span></label>
  <input type="number" id="pol-idle-after" min="5" max="86400">
  <label><input type="checkbox" id="pol-att-en" style="width:auto"> Chat attention: pull panel to chat on new message (off by default)</label>
  <label for="pol-att-view">Attention view<span class="help">renderer a chat event pulls to (string)</span></label>
  <input type="text" id="pol-att-view">
  <label for="pol-att-ret">Stay on chat per message (seconds)<span class="help">re-armed by each new message, then returns (number, 5-600)</span></label>
  <input type="number" id="pol-att-ret" min="5" max="600">
  <label for="pol-notify-dur">Notice duration default (seconds)<span class="help">how long a notification stays up when unset (number, 1-300)</span></label>
  <input type="number" id="pol-notify-dur" min="1" max="300">
  <div class="meta" id="pol-status">policy: loading&hellip;</div>
  <button id="polsave">Save policy</button>
</details>

<h2>Tap actions</h2>
<div class="card">
  <ul class="taplist">
    <li><code>playlist_next</code> <span class="eff">&mdash; advance playlist rotation (POST /playlist/next)</span></li>
    <li><code>playlist_pause</code> <span class="eff">&mdash; hold playlist rotation (POST /playlist/pause)</span></li>
    <li><code>playlist_resume</code> <span class="eff">&mdash; resume playlist rotation (POST /playlist/resume)</span></li>
    <li><code>screen_on</code> <span class="eff">&mdash; drive panel backlight on (POST /screen/on)</span></li>
    <li><code>screen_off</code> <span class="eff">&mdash; drive panel backlight off (POST /screen/off)</span></li>
    <li><code>clear</code> <span class="eff">&mdash; blank the panel (POST /clear)</span></li>
    <li><code>show</code> <span class="eff">&mdash; replace the shown view, renderer named in config (POST /show)</span></li>
    <li><code>select_view</code> <span class="eff">&mdash; reroute the displayed view to the named selection (POST /show)</span></li>
    <li><code>notify</code> <span class="eff">&mdash; interrupt the panel with a transient notice (POST /notify)</span></li>
    <li><code>feedback</code> <span class="eff">&mdash; record a fixed-shape tap-to-rate feedback rating (POST /feedback)</span></li>
    <li><code>reload_confirm</code> <span class="eff">&mdash; confirm the showing reload view via tap (POST /reload/confirm)</span></li>
    <li><code>macbook_mouse</code> <span class="eff">&mdash; move the MacBook cursor to the tapped map point (POST /macbook/mouse)</span></li>
    <li><code>macbook_click</code> <span class="eff">&mdash; click the reviewed point on the magnified image (POST /macbook/click)</span></li>
    <li><code>talon_focus</code> <span class="eff">&mdash; focus the tapped header app chip (POST /talon/focus)</span></li>
    <li><code>macbook_mode</code> <span class="eff">&mdash; pin the merged macbook view to GLANCE or AIM (POST /macbook/mode)</span></li>
    <li><code>talon_tab</code> <span class="eff">&mdash; page the header app strip one window back/forward (POST /talon/tab)</span></li>
  </ul>
  <div class="meta">What a tap on the panel can do (touch bridge allowlist).</div>
</div>

<div id="result"></div>

"""
