"""The control page shell: doctype, head, styles, and the sticky top bar.

Single concept: the document frame the body markup and the client script are
dropped into. Assembled verbatim (see control_page.py) so the served page is
byte-identical to the one embedded here before the split.
"""

_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>displayd control</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { background: #111; color: #eee; font-family: system-ui, sans-serif;
         max-width: 520px; margin: 0 auto; padding: 0 12px 32px; }
  h1 { font-size: 1.2em; margin: 12px 0 4px; }
  h2 { font-size: 1.05em; margin: 20px 0 8px; border-bottom: 1px solid #333;
       padding-bottom: 4px; }
  #topbar { position: sticky; top: 0; z-index: 10; background: #161616;
            border-bottom: 1px solid #333; margin: 0 -12px; padding: 8px 12px;
            font-size: 0.85em; display: flex; gap: 10px; align-items: center;
            flex-wrap: wrap; }
  #topbar .now { font-weight: bold; }
  #topbar .dep { color: #aaa; }
  .card { background: #1c1c1c; border: 1px solid #333; border-radius: 8px;
          padding: 12px; margin-bottom: 12px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
         background: #666; margin-right: 6px; vertical-align: baseline; }
  .dot.ok { background: #3d3; } .dot.bad { background: #f44; }
  #preview { width: 100%; aspect-ratio: 16/9; background: #000; object-fit: contain;
             border: 1px solid #333; border-radius: 8px; }
  /* one-tap view grid: two fat thumb columns */
  #viewgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  #viewgrid button { min-height: 56px; font-size: 1rem; margin: 0;
                     background: #2b2b2b; color: #eee; border: 2px solid #444;
                     border-radius: 10px; font-weight: bold; cursor: pointer;
                     overflow: hidden; text-overflow: ellipsis; }
  #viewgrid button:active { background: #3a3a3a; }
  #viewgrid button.active { border-color: #2a5; background: #17351f;
                            box-shadow: 0 0 0 1px #2a5; }
  #viewgrid button:disabled { opacity: 0.45; }
  .btnrow { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px;
            margin-top: 10px; }
  .btnrow.two { grid-template-columns: 1fr 1fr; }
  .btnrow.one { grid-template-columns: 1fr; }
  button.big { min-height: 52px; font-size: 1rem; margin: 0; cursor: pointer;
               background: #2a5; color: #061; border: 0; border-radius: 10px;
               font-weight: bold; }
  button.big.ghost { background: #333; color: #eee; }
  button.big.warn { background: #a53; color: #fff; }
  button.big:active { filter: brightness(1.2); }
  button { min-height: 48px; padding: 8px 14px; margin: 8px 8px 0 0;
           cursor: pointer; background: #2a5; color: #061; border: 0;
           border-radius: 8px; font-weight: bold; font-size: 0.95rem; }
  button.warn { background: #a53; color: #fff; }
  button.ghost { background: #333; color: #eee; }
  #result { margin-top: 12px; min-height: 1.4em; font-size: 0.9em; color: #9cf; }
  .meta { color: #aaa; font-size: 0.9em; }
  .kv { display: flex; justify-content: space-between; gap: 8px;
        padding: 2px 0; font-size: 0.92em; }
  .kv > span:first-child { color: #aaa; }
  code { background: #222; padding: 1px 5px; border-radius: 3px;
         word-break: break-all; }
  label { display: block; margin: 8px 0 2px; font-size: 0.9em; }
  label .req { color: #f88; }
  label .help { color: #999; font-size: 0.85em; display: block; }
  input[type=text], input[type=number], select {
    width: 100%; box-sizing: border-box; padding: 10px 8px; font-size: 1rem;
    background: #222; color: #eee; border: 1px solid #444; border-radius: 8px; }
  details { margin-top: 10px; }
  details > summary { min-height: 48px; display: flex; align-items: center;
                      cursor: pointer; color: #9cf; font-size: 0.95em; }
  /* tap-to-rate stars */
  #ratebtns { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px;
              margin-top: 8px; }
  #ratebtns button { min-height: 56px; font-size: 1.3rem; margin: 0;
                     background: #2b2b2b; color: #eee; border: 2px solid #444;
                     border-radius: 10px; cursor: pointer; }
  #ratebtns button.picked { border-color: #fc3; background: #3a2f10; }
  .taplist { list-style: none; margin: 0; padding: 0; font-size: 0.9em; }
  .taplist li { padding: 6px 0; border-bottom: 1px solid #2a2a2a; }
  .taplist li:last-child { border-bottom: 0; }
  .taplist .eff { color: #aaa; }
  #fb-summary .fbline { display: flex; justify-content: space-between;
                        padding: 3px 0; font-size: 0.92em; }
  #reload-result a { color: #9cf; word-break: break-all; }
</style>
</head>
<body>
<header id="topbar">
  <span><span id="health" class="dot"></span><span id="healthtext">connecting&hellip;</span></span>
  <span class="now">Now: <span id="tb-view">&ndash;</span></span>
  <span class="dep">Deploy: <span id="tb-dep">&ndash;</span></span>
</header>
<h1>displayd control</h1>

"""
