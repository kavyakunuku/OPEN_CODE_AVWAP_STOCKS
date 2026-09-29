"""Web dashboard V2 (Flask) - Trading Command Center.

Four zones (spec: V2 roadmap, LIVE TRADING + DATA + OPERATIONS):
  LIVE TRADING : Overview · Scanner · Contract detail (chart) ·
                 Positions (operational) · Signals (with explainability +
                 signal->order->position chain) · Orders
  DATA         : Data Health (feeds, REST, AVWAP integrity, rebuild queue)
  OPERATIONS   : System health · Alerts · Journal (filter+export) ·
                 Restart/Recovery · Settings (READ-ONLY by design)

Read-only state + operational controls (spec §24, §33, §35):
  * controls: disable/enable entries, emergency stop/release, exit-all
    (type-to-confirm), close-single-position, schedule AVWAP rebuild -
    all token-gated. Controls are OPERATIONAL, never strategy logic.
  * trigger-proximity / "exit due" / "actionable" views are CLIENT-SIDE
    DISPLAY ONLY - the strategy engine evaluates signals independently.

Endpoints:
  GET  /                      the page
  GET  /api/state             full state blob (polled by the page)
  GET  /api/contract/<sec>    contract detail (state + candles + signals)
  GET  /api/journal           filtered journal (event, symbol, limit)
  POST /api/control           token-gated operational actions
"""
from __future__ import annotations

import logging
import threading
import time

from flask import Flask, jsonify, request

from common.version import APP_VERSION

log = logging.getLogger("avwap.dashboard")

# Bump on every dashboard change (kept for backwards compatibility; the
# canonical build version is common.version.APP_VERSION).
DASH_VERSION = APP_VERSION

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AVWAP F&O Command Center</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { background:#0d1117; color:#c9d1d9; font:13px/1.45 "SF Mono",Consolas,Menlo,monospace; margin:0; }
h1 { font-size:16px; margin:0; }
h2 { font-size:13px; margin:18px 0 8px; color:#58a6ff; text-transform:uppercase; letter-spacing:1px; }
a { color:#58a6ff; text-decoration:none; cursor:pointer; }
/* ---------- top bar ---------- */
#topbar { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:10px 14px; background:#10161f; border-bottom:1px solid #30363d; position:sticky; top:0; z-index:5; }
.chip { padding:2px 9px; border-radius:11px; font-size:11px; font-weight:bold; border:1px solid #30363d; background:#161b22; }
.chip.ok { color:#3fb950; border-color:#238636; }
.chip.warn { color:#d29922; border-color:#9e6a03; }
.chip.err { color:#f85149; border-color:#da3633; }
.chip.info { color:#79c0ff; border-color:#1f6feb; }
.chip.live { color:#ffa198; border-color:#f85149; animation:pulse 1.6s infinite; }
@keyframes pulse { 0%,100% {opacity:1} 50% {opacity:.5} }
#topclock { color:#8b949e; font-size:12px; }
#controls { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-left:auto; }
button { font:inherit; font-size:12px; background:#21262d; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:4px 10px; cursor:pointer; }
button:hover { border-color:#58a6ff; }
button.danger { background:#3d1418; border-color:#f85149; color:#ffa198; }
button.primary { background:#0d2a43; border-color:#1f6feb; color:#79c0ff; }
button.small { padding:2px 8px; font-size:11px; }
input,select { font:inherit; font-size:12px; background:#21262d; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:4px 8px; }
/* ---------- banner ---------- */
.banner { padding:8px 14px; font-weight:bold; }
.banner.paper { background:#10233a; border-bottom:1px solid #1f6feb; color:#79c0ff; }
.banner.live { background:#3d1418; border-bottom:1px solid #f85149; color:#ffa198; animation:pulse 1.6s infinite; }
.banner.err { background:#3d1418; border-bottom:1px solid #f85149; color:#ffa198; }
.banner.boot { background:#10233a; border-bottom:1px solid #1f6feb; color:#79c0ff; }
/* ---------- layout ---------- */
#layout { display:flex; min-height:calc(100vh - 120px); }
#nav { width:170px; flex:0 0 170px; border-right:1px solid #30363d; padding:12px 8px; background:#0f141b; }
.navgroup { font-size:10px; color:#484f58; letter-spacing:1px; margin:12px 8px 4px; }
.navgroup:first-child { margin-top:0; }
#nav a { display:block; padding:6px 10px; border-radius:6px; color:#c9d1d9; font-size:12.5px; }
#nav a:hover { background:#1c2129; }
#nav a.active { background:#10233a; color:#79c0ff; font-weight:bold; }
#main { flex:1; padding:14px 16px 40px; min-width:0; }
.page { display:none; }
.page.shown { display:block; }
/* ---------- cards / tables ---------- */
.cards { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:10px; }
.card { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:8px 14px; min-width:118px; }
.card .k { font-size:10px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; }
.card .v { font-size:15px; margin-top:2px; }
.card .s { font-size:10px; color:#8b949e; margin-top:1px; }
.ok { color:#3fb950; } .warn { color:#d29922; } .err { color:#f85149; } .dim { color:#8b949e; }
table { border-collapse:collapse; width:100%; background:#161b22; border:1px solid #30363d; border-radius:6px; overflow:hidden; margin-bottom:8px; }
th,td { padding:4px 9px; text-align:right; border-bottom:1px solid #21262d; white-space:nowrap; }
th { background:#1c2129; color:#8b949e; font-size:10.5px; text-transform:uppercase; }
td:first-child, th:first-child, td.l, th.l { text-align:left; }
tbody tr:hover td { background:#1c2129; }
tr.clickable { cursor:pointer; }
.badge { padding:1px 7px; border-radius:10px; font-size:10.5px; font-weight:bold; }
.b-open { background:#10233a; color:#79c0ff; } .b-signal { background:#2d1a35; color:#d2a8ff; }
.b-watch { background:#161b22; color:#8b949e; } .b-pending { background:#2d2410; color:#d29922; }
.b-near { background:#0d2a12; color:#3fb950; } .b-err { background:#3d1418; color:#ffa198; }
.b-filled { background:#0d2a12; color:#3fb950; } .b-rej { background:#3d1418; color:#ffa198; }
.b-part { background:#2d2410; color:#d29922; } .b-canc { background:#161b22; color:#8b949e; }
.small { font-size:11px; color:#8b949e; }
.rowctl { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:8px; }
.rowctl label { font-size:12px; color:#8b949e; }
.qbtns button.on { background:#10233a; border-color:#1f6feb; color:#79c0ff; font-weight:bold; }
pre { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px; overflow-x:auto; font-size:11.5px; }
/* ---------- session bar ---------- */
.sessbar { position:relative; height:14px; background:#21262d; border-radius:7px; margin:6px 0 2px; overflow:hidden; }
.sessbar .open { position:absolute; top:0; bottom:0; background:#10233a; border-left:2px solid #3fb950; border-right:2px solid #3fb950; }
.sessbar .nowm { position:absolute; top:-2px; bottom:-2px; width:2px; background:#f85149; }
/* ---------- pos cards ---------- */
.pgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:10px; margin-bottom:12px; }
.pcard { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:10px 14px; }
.pcard h3 { margin:0 0 6px; font-size:13.5px; }
.pcard .row { display:flex; justify-content:space-between; font-size:12px; margin:2px 0; }
.pcard .row .k { color:#8b949e; }
/* ---------- signal cards ---------- */
.sgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:10px; }
.scard { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:10px 14px; font-size:12px; }
.scard .chain { margin-top:8px; padding-top:8px; border-top:1px dashed #30363d; color:#8b949e; }
.rule-ok { color:#3fb950; font-weight:bold; } .rule-bad { color:#f85149; font-weight:bold; }
/* ---------- alerts ---------- */
.al { display:flex; gap:10px; align-items:baseline; padding:6px 10px; border:1px solid #30363d; border-radius:6px; margin-bottom:6px; background:#161b22; }
.al.CRITICAL { border-color:#da3633; background:#2a1215; }
.al.WARNING { border-color:#9e6a03; background:#241d0c; }
.al .sev { font-weight:bold; min-width:74px; }
.al.CRITICAL .sev { color:#f85149; } .al.WARNING .sev { color:#d29922; } .al.INFO .sev { color:#8b949e; }
/* ---------- misc ---------- */
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
.dot.ok { background:#3fb950; } .dot.warn { background:#d29922; } .dot.err { background:#f85149; }
.tl { font-size:12px; }
.tl td { white-space:normal; }
.kv { display:grid; grid-template-columns:230px 1fr; gap:2px 12px; font-size:12px; margin-bottom:10px; }
.kv .k { color:#8b949e; }
#chart { width:100%; max-width:1080px; height:400px; background:#161b22; border:1px solid #30363d; border-radius:8px; }
.bootprogress { margin:0 0 12px; padding:10px 14px; background:#10233a; border:1px solid #1f6feb; border-radius:6px; }
.bp-label { font-size:12px; color:#79c0ff; margin-bottom:7px; }
.bp-bar { height:10px; background:#0d1117; border-radius:6px; overflow:hidden; }
.bp-fill { height:100%; width:0%; background:linear-gradient(90deg,#1f6feb,#3fb950); transition:width .4s; }
#pollstat { margin-top:16px; color:#d29922; }
@media (max-width:900px) {
  #layout { flex-direction:column; }
  #nav { width:100%; flex:none; display:flex; flex-wrap:wrap; gap:2px; border-right:none; border-bottom:1px solid #30363d; }
  .navgroup { width:100%; margin:6px 4px 2px; }
  #nav a { padding:4px 8px; font-size:11.5px; }
  #controls { margin-left:0; }
}
</style></head><body>
<div id="banner" class="banner boot">LOADING…</div>
<div id="bootprogress" class="bootprogress" style="display:none;">
  <div class="bp-label" id="bp_label">BOOTSTRAPPING…</div>
  <div class="bp-bar"><div class="bp-fill" id="bp_fill"></div></div>
</div>
<div id="topbar">
  <h1>AVWAP Trading Command Center</h1>
  <span class="chip" id="c_mode">…</span>
  <span class="chip" id="c_market">…</span>
  <span class="chip" id="c_engine">…</span>
  <span class="chip" id="c_dhan">…</span>
  <span class="chip" id="c_data">…</span>
  <span id="topclock"></span>
  <div id="controls">
    <button class="primary small" onclick="ctrl('disable_entries')">Pause entries</button>
    <button class="primary small" onclick="ctrl('enable_entries')">Resume entries</button>
    <button class="danger small" onclick="ctrl('emergency_stop')">EMERGENCY STOP</button>
    <button class="primary small" onclick="ctrl('release_stop')">Release stop</button>
    <button class="danger small" onclick="ctrl('exit_all')">EXIT ALL</button>
    <span class="small" id="ctrl_status"></span>
  </div>
</div>
<div id="layout">
<nav id="nav">
  <div class="navgroup">LIVE TRADING</div>
  <a data-page="overview" class="active">Overview</a>
  <a data-page="scanner">Scanner</a>
  <a data-page="contract">Contract</a>
  <a data-page="positions">Positions</a>
  <a data-page="signals">Signals</a>
  <a data-page="orders">Orders</a>
  <div class="navgroup">DATA</div>
  <a data-page="datahealth">Data Health</a>
  <div class="navgroup">OPERATIONS</div>
  <a data-page="system">System</a>
  <a data-page="alerts">Alerts</a>
  <a data-page="journal">Journal</a>
  <a data-page="daypnl">Day P&L</a>
  <a data-page="recovery">Recovery</a>
  <a data-page="settings">Settings</a>
</nav>
<main id="main">

<section id="page_overview" class="page shown">
  <h2>Today</h2>
  <div class="cards" id="ov_cards"></div>
  <h2>Market</h2>
  <div id="ov_market"></div>
  <h2>Engine</h2>
  <div class="cards" id="ov_engine"></div>
  <h2>Risk</h2>
  <div class="cards" id="ov_risk"></div>
  <h2>Alerts <a class="small" onclick="showPage('alerts')" style="float:right;">view all →</a></h2>
  <div id="ov_alerts"></div>
</section>

<section id="page_scanner" class="page">
  <h2>Scanner <span class="small">(click a row for detail; click headers to sort)</span></h2>
  <div class="rowctl qbtns" id="scan_q">
    <button data-q="ALL" class="on">ALL</button>
    <button data-q="ACTIONABLE">ACTIONABLE</button>
    <button data-q="OPEN">OPEN</button>
    <button data-q="EXIT">EXIT CANDIDATES</button>
    <button data-q="PARTIAL">PARTIAL AVWAP</button>
    <button data-q="ERROR">ERROR</button>
  </div>
  <div class="rowctl">
    <div class="qbtns" id="scan_type">
      <button data-t="ALL" class="on">ALL</button><button data-t="CE">CE</button><button data-t="PE">PE</button>
    </div>
    <div class="qbtns" id="scan_side">
      <button data-s="ALL" class="on">ALL</button><button data-s="ABOVE">ABOVE AVWAP</button><button data-s="BELOW">BELOW AVWAP</button>
    </div>
    <input id="filter" placeholder="filter (e.g. RELIANCE, 2500, NEAR)" style="width:220px;">
    <label>“near trigger” ≤ <input id="near_in" type="number" step="0.1" min="0.1" max="50" style="width:60px;"> % of AVWAP</label>
    <button class="primary small" onclick="exportScannerCSV()">Export CSV</button>
  </div>
  <div id="scanner"></div>
</section>

<section id="page_contract" class="page">
  <h2>Contract</h2>
  <div id="ct_head"></div>
  <div class="cards" id="ct_cards" style="margin-top:8px;"></div>
  <canvas id="chart" width="1080" height="400"></canvas>
  <div class="small" id="ct_chartnote" style="margin-top:4px;"></div>
  <h2>Signals for this contract</h2>
  <div id="ct_signals"></div>
  <h2>Position</h2>
  <div id="ct_position"></div>
</section>

<section id="page_positions" class="page">
  <h2>Portfolio</h2>
  <div class="cards" id="pos_cards"></div>
  <h2>Open positions <span class="small">(operational: close a position at market)</span></h2>
  <div class="pgrid" id="pos_cards_grid"></div>
  <div id="pos_table"></div>
</section>

<section id="page_signals" class="page">
  <h2>Signals <span class="small">(each signal keeps its full evidence chain: candle → rule → risk → order → position)</span></h2>
  <div class="sgrid" id="sig_cards"></div>
</section>

<section id="page_orders" class="page">
  <h2>Orders <span class="small">(click a row to see the raw broker response)</span></h2>
  <div id="orders_table"></div>
</section>

<section id="page_datahealth" class="page">
  <h2>Feeds</h2>
  <div class="cards" id="dh_feeds"></div>
  <h2>Dhan REST API</h2>
  <div class="cards" id="dh_rest"></div>
  <h2>AVWAP data integrity</h2>
  <div class="cards" id="dh_av_cards"></div>
  <div id="dh_av_partial"></div>
  <div id="dh_queue"></div>
</section>

<section id="page_system" class="page">
  <h2>Components</h2>
  <div class="cards" id="sys_comp"></div>
  <h2>Heartbeats</h2>
  <div class="cards" id="sys_beats"></div>
  <h2>API by endpoint</h2>
  <div id="sys_by_path"></div>
  <h2>Process</h2>
  <div class="cards" id="sys_proc"></div>
</section>

<section id="page_alerts" class="page">
  <h2>Current alerts</h2>
  <div id="al_now"></div>
  <h2>Incident history <span class="small">(from the journal)</span></h2>
  <div id="al_hist"></div>
</section>

<section id="page_journal" class="page">
  <h2>Journal</h2>
  <div class="rowctl">
    <input id="j_event" placeholder="event (e.g. ORDER_SENT)" style="width:170px;">
    <input id="j_symbol" placeholder="symbol (partial)" style="width:170px;">
    <select id="j_limit">
      <option value="200">last 200</option>
      <option value="500">last 500</option>
      <option value="2000">last 2000</option>
    </select>
    <button class="primary small" onclick="loadJournal(true)">Apply</button>
    <button class="small" onclick="exportJournal('csv')">Export CSV</button>
    <button class="small" onclick="exportJournal('json')">Export JSON</button>
    <span class="small" id="j_status"></span>
  </div>
  <div id="journal_table"></div>
</section>

<section id="page_daypnl" class="page">
  <h2>Day-wise P&L <span class="small">(estimated net after charges)</span></h2>
  <div class="rowctl">
    <button class="primary small" onclick="loadDayPnl()">Refresh</button>
  </div>
  <div class="cards" id="daypnl_summary"></div>
  <div style="margin-top:10px" id="daypnl_table"></div>
</section>

<section id="page_recovery" class="page">
  <h2>This startup</h2>
  <div id="rec_status"></div>
  <div id="rec_timeline"></div>
  <h2>Previous startup <span class="small">(persisted at its completion)</span></h2>
  <div id="rec_prev"></div>
</section>

<section id="page_settings" class="page">
  <h2>Settings <span class="small">— READ-ONLY by design</span></h2>
  <div class="small" style="margin-bottom:10px;">
    Nothing trading-related can be changed from the dashboard. To change any setting:
    edit <b>config\config.json</b> and restart the app. Strategy / risk / execution / mode
    parameters are deliberately outside the UI (safety guardrail).
  </div>
  <div id="set_tables"></div>
</section>

</main>
</div>
<div id="pollstat" class="small" style="padding:0 14px;">engine link: connecting…</div>
<div class="small" style="padding:4px 14px 10px;">dashboard __DASH_VERSION__ — if the banner says "LOADING…" for more than a few seconds, read the "engine link" line and report it.</div>

<script>
console.log("avwap dashboard version __DASH_VERSION__ loaded");
let S = null;
const $ = id => document.getElementById(id);
function fmt(v, d) { if (d === undefined) d = 2; return (v===null||v===undefined||isNaN(v)) ? "–" : Number(v).toFixed(d); }
// LTP cell: explain a missing value instead of a silent "–"
function ltpCell(v) {
  if (v !== null && v !== undefined && !isNaN(v)) return fmt(v);
  return (S && S.market_state === "OPEN")
    ? '<span title="No quote received for this contract yet">–</span>'
    : '<span title="No quote yet: LTP polling only runs during market hours (09:15–15:30 IST, Mon–Fri) and values are lost on restart. It will fill from the first poll after 09:15.">–</span>';
}
function inr(v) { if (v===null||v===undefined||isNaN(v)) return "–"; const n = Number(v);
  return (n<0?"-":"") + "₹" + Math.abs(n).toLocaleString("en-IN", {maximumFractionDigits:2, minimumFractionDigits:2}); }
function ts(v) { if(!v) return "–"; const d=new Date(v*1000); const p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds()); }
function hhmm(v) { if(!v) return "–"; const d=new Date(v*1000); return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0"); }
function ago(v) { if(!v) return "never"; const s=Math.max(0, Math.round(Date.now()/1000 - v));
  if (s<5) return "just now"; if (s<60) return s+"s ago"; if (s<3600) return Math.round(s/60)+"m ago";
  return Math.round(s/3600)+"h "+Math.round((s%3600)/60)+"m ago"; }
function durFmt(s) { if (s===null||s===undefined||isNaN(s)) return "–"; s=Math.max(0,Math.round(s));
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60); return h>0 ? h+"h "+m+"m" : m+"m "+(s%60)+"s"; }
function badge(s) {
  if (s==="OPEN") return '<span class="badge b-open">OPEN</span>';
  if (s==="PENDING") return '<span class="badge b-pending">PENDING</span>';
  if (s==="ENTRY"||s==="EXIT") return '<span class="badge b-signal">'+s+'</span>';
  if (s==="ERROR") return '<span class="badge b-err">ERROR</span>';
  return '<span class="badge b-watch">'+(s||"")+'</span>';
}
function orderBadge(s) {
  if (s==="FILLED") return '<span class="badge b-filled">FILLED</span>';
  if (s==="PENDING") return '<span class="badge b-pending">PENDING</span>';
  if (s==="PARTIAL") return '<span class="badge b-part">PARTIAL</span>';
  if (s==="REJECTED"||s==="FAILED"||s==="CANCELLED") return '<span class="badge b-rej">'+s+'</span>';
  return '<span class="badge b-canc">'+(s||"")+'</span>';
}
let _memStore = {};
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return _memStore[k] ?? null; } }
function lsSet(k, v) { try { localStorage.setItem(k, String(v)); } catch (e) { _memStore[k] = v; } }
// HTML-attribute escaping for inline handlers that embed JSON (quotes)
function escAttr(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;"); }
function nearPct() { const v = parseFloat(lsGet("avwap_near_pct") || "1"); return (isFinite(v) && v > 0) ? v : 1; }
function setNear(v) { lsSet("avwap_near_pct", String(parseFloat(v) || 1)); render(); }

// ---------------- trigger proximity (DISPLAY ONLY) ----------------
function deltaPct(close, av) {
  if (close === null || close === undefined || av === null || av === undefined || av <= 0) return null;
  return (close - av) / av * 100;
}
function triggerInfo(r, d) {
  const open = (r.status === "OPEN" || r.status === "PENDING");
  const T = nearPct();
  if (d === null) return { html: '<span class="small">no AVWAP yet</span>', token: "NOAVWAP", rank: 9999 };
  if (open) {
    if (d >= 0) return { html: '<span class="badge b-signal">EXIT DUE</span>', token: "EXIT DUE", rank: 0.001 + Math.min(d, 100) * 0.0001 };
    const near = d >= -T;
    return near
      ? { html: '<span class="badge b-near">EXIT NEAR ' + d.toFixed(2) + '%</span>', token: "EXIT NEAR", rank: 1 + d }
      : { html: '<span class="badge b-watch">hold · exit on close above</span>', token: "HOLD", rank: 50 - d };
  }
  if (d < 0) return { html: '<span class="badge b-watch">below · re-arm above first</span>', token: "BELOW", rank: 200 - d };
  const near = d <= T;
  return near
    ? { html: '<span class="badge b-near">ENTRY NEAR ' + d.toFixed(2) + '%</span>', token: "ENTRY NEAR", rank: 10 + d }
    : { html: '<span class="badge b-watch">armed @ +' + d.toFixed(2) + '%</span>', token: "ARMED", rank: 10 + d };
}
function dCell(d) {
  if (d === null) return "–";
  const cls = d < 0 ? "err" : (Math.abs(d) <= nearPct() ? "warn" : "ok");
  return '<span class="' + cls + '">' + (d >= 0 ? "+" : "") + d.toFixed(2) + "%</span>";
}
function dteOf(expiryStr) {
  if (!expiryStr) return null;
  const p = String(expiryStr).split("-");
  if (p.length !== 3) return null;
  const e = new Date(+p[0], +p[1] - 1, +p[2]);
  const c = new Date();
  const t = new Date(c.getFullYear(), c.getMonth(), c.getDate());
  return Math.round((e - t) / 86400000);
}
function dteCell(expiryStr) {
  const d = dteOf(expiryStr);
  if (d === null) return "–";
  if (d < 0) return '<span class="small">expired</span>';
  if (d === 0) return '<span class="badge b-pending">TODAY</span>';
  if (d === 1) return '<span class="warn">1d</span>';
  return d + "d";
}
function anchorCell(r) {
  if (!r.avwap_anchor_ts) return '<span class="small">no anchor yet</span>';
  const d = new Date(r.avwap_anchor_ts * 1000); const p = n => String(n).padStart(2,"0");
  const s = p(d.getDate()) + "-" + p(d.getMonth()+1) + "-" + d.getFullYear();
  const warn = 'PARTIAL history: the AVWAP anchor may be LATER than the real first tradable candle. Use Data Health → REBUILD (applies at next start) or tools\\reanchor_avwap.py.';
  return r.hist_partial
    ? '<span class="err" title="' + warn + '">' + s + " &#9888;</span>"
    : '<span class="small" title="AVWAP anchor = first tradable candle of this contract">' + s + '</span>';
}
function lastCloseCell(t) {
  if (!t) return "–";
  const mins = Math.max(0, Math.round((Date.now() / 1000 - t) / 60));
  const cls = mins > 60 ? "err" : "";
  return '<span class="' + cls + '">' + hhmm(t) + ' <span class="small">(' + mins + 'm)</span></span>';
}

// ---------------- sortable tables ----------------
const sortState = {};
function cmp(a, b) {
  const an = (a === null || a === undefined || a === ""), bn = (b === null || b === undefined || b === "");
  if (an && bn) return 0;
  if (an) return 1;
  if (bn) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}
function makeTable(el, headers, cells, keys) {
  if (!cells.length) { el.innerHTML = '<div class="small">— none —</div>'; return; }
  const id = el.id || ("t" + Math.random().toString(36).slice(2));
  el.id = el.id || id;
  let rows = cells.map((c, i) => {
    // a cell may be a plain array OR a row object {c:[...], cls, onclick}
    if (c && typeof c === "object" && !Array.isArray(c) && Array.isArray(c.c))
      return { c: c.c, cls: c.cls, onclick: c.onclick,
               k: keys ? keys[i] : c.c.map(x => String(x).replace(/<[^>]*>/g, "")) };
    return { c, k: keys ? keys[i] : c.map(x => String(x).replace(/<[^>]*>/g, "")) };
  });
  const st = sortState[el.id];
  if (st) rows.sort((a, b) => cmp(a.k[st.col], b.k[st.col]) * st.dir);
  let h = "<table><tr>" + headers.map((x, i) => {
    const arrow = (st && st.col === i) ? (st.dir === 1 ? " ↑" : " ↓") : "";
    return '<th class="' + (x.l ? "l " : "") + 'sortable" data-el="' + el.id + '" data-col="' + i + '">' + x.t + arrow + "</th>";
  }).join("") + "</tr>";
  for (const r of rows) {
    const cls = r.cls ? ' class="' + r.cls + '"' : "";
    // HTML-attribute escaping: handler strings contain JSON (double quotes)
    const onclick = r.onclick ? ' onclick="' + escAttr(r.onclick) + '"' : "";
    h += "<tr" + cls + onclick + ">" + r.c.map((c, i) => '<td class="' + (headers[i].l ? "l" : "") + '">' + c + "</td>").join("") + "</tr>";
  }
  el.innerHTML = h + "</table>";
  el.querySelectorAll("th.sortable").forEach(th => th.onclick = () => {
    const elId = th.getAttribute("data-el"), col = +th.getAttribute("data-col");
    const cur = sortState[elId];
    sortState[elId] = (cur && cur.col === col) ? { col, dir: -cur.dir } : { col, dir: 1 };
    render();
  });
}
function statusOrder(s) { return ({OPEN:0, PENDING:0, EXIT:1, ENTRY:1, ERROR:2, WATCH:3})[s] ?? 9; }
function cardRow(pairs) {
  return pairs.map(c => '<div class="card"><div class="k">' + c[0] + '</div><div class="v ' + (c[2]||"") + '">' + c[1] + '</div>' + (c[3] ? '<div class="s">' + c[3] + '</div>' : "") + '</div>').join("");
}

// ---------------- navigation ----------------
const PAGES = ["overview","scanner","contract","positions","signals","orders",
               "datahealth","system","alerts","journal","daypnl","recovery","settings"];
let CUR = (lsGet("avwap_page") && PAGES.includes(lsGet("avwap_page"))) ? lsGet("avwap_page") : "overview";
let scanQ = "ALL", scanType = "ALL", scanSide = "ALL";
let contractSel = lsGet("avwap_contract") || "";
let contractData = null, contractFetchedAt = 0;
let journalData = null, journalLoadedFor = "";
let dayPnlData = null;

function showPage(id) {
  if (!PAGES.includes(id)) id = "overview";
  CUR = id; lsSet("avwap_page", id);
  PAGES.forEach(p => $(("page_" + p)).classList.toggle("shown", p === id));
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("active", a.dataset.page === id));
  if (id === "journal" || id === "alerts") loadJournal(false);
  if (id === "daypnl") loadDayPnl();
  render();
}
document.querySelectorAll("#nav a").forEach(a => a.onclick = () => showPage(a.dataset.page));
function selectContract(sec) {
  contractSel = sec; lsSet("avwap_contract", sec);
  contractData = null;
  showPage("contract");
  loadContract(true);
}

// ---------------- header / banner ----------------
function renderHeader() {
  if (!S) return;
  const b = $("banner"), up = S.startup || {};
  const booting = up.phase === "bootstrapping";
  const bp = $("bootprogress");
  if (booting) {
    bp.style.display = "block";
    const pct = up.total > 0 ? Math.max(1, Math.round(100 * (up.progress||0) / up.total)) : 0;
    $("bp_fill").style.width = pct + "%";
    $("bp_label").textContent = "BOOTSTRAPPING — " + (up.detail||"initializing") +
      (up.total > 0 ? "   ·   " + (up.progress||0) + "/" + up.total + "  (" + pct + "%)" : "");
    b.className = "banner boot";
    b.textContent = "Starting up — building the market universe & AVWAP history. First start takes several minutes (much faster on restart). Keep the window open.";
    return;
  }
  bp.style.display = "none";
  if (S.mode === "LIVE") { b.className = "banner live"; b.textContent = "⚠ LIVE TRADING ENABLED — REAL ORDERS MAY BE SENT TO DHAN"; }
  else { b.className = "banner paper"; b.textContent = "PAPER TRADING MODE — no live orders are sent (data source: " + (S.source === "mock" ? "MOCK/synthetic" : "Dhan live market data") + ")"; }
  const now = Date.now() / 1000;
  const m = $("c_mode"); m.textContent = S.mode; m.className = "chip " + (S.mode === "LIVE" ? "live" : "info");
  const mk = $("c_market"); mk.textContent = "Market " + S.market_state;
  mk.className = "chip " + (S.market_state === "OPEN" ? "ok" : "warn");
  const eng = S.system && S.system.engine;
  const stale = eng && eng.last_tick && (now - eng.last_tick > 15);
  const ce = $("c_engine"); ce.textContent = "Engine " + (eng && eng.last_error ? "ERROR" : (stale ? "STALE" : "OK"));
  ce.className = "chip " + (eng && eng.last_error ? "err" : (stale ? "warn" : "ok"));
  const cd = $("c_dhan"); cd.textContent = S.dhan === "connected" ? "Dhan ●" : S.dhan;
  cd.className = "chip " + (S.dhan === "connected" ? "ok" : "warn");
  const feeds = S.feeds || {};
  const feedBad = Object.values(feeds).some(f => f.last_fail && f.last_fail > f.last_ok && now - f.last_fail < 300);
  const cdat = $("c_data"); cdat.textContent = "Data " + (feedBad ? "▲" : "●");
  cdat.className = "chip " + (feedBad ? "warn" : "ok");
  $("topclock").textContent = (S.clock || "").replace(/ .*$/, "") + " " + ((S.clock || "").match(/\d\d:\d\d:\d\d/) || [""])[0] +
    "  ·  build " + (S.version || "?") + "  ·  up " + durFmt(S.uptime_s);
}

// ---------------- pages ----------------
function render() {
  renderHeader();
  if (!S) return;
  switch (CUR) {
    case "overview": renderOverview(); break;
    case "scanner": renderScanner(); break;
    case "contract": renderContract(); break;
    case "positions": renderPositions(); break;
    case "signals": renderSignals(); break;
    case "orders": renderOrders(); break;
    case "datahealth": renderDataHealth(); break;
    case "system": renderSystem(); break;
    case "alerts": renderAlerts(); break;
    case "journal": renderJournal(); break;
    case "daypnl": renderDayPnl(); break;
    case "recovery": renderRecovery(); break;
    case "settings": renderSettings(); break;
  }
}

function sessionBar() {
  const now = new Date();
  const mins = now.getHours() * 60 + now.getMinutes();
  const axisS = 8 * 60, axisE = 16 * 60;
  const openS = 9 * 60 + 15, openE = 15 * 60 + 30;
  const pct = v => ((v - axisS) / (axisE - axisS) * 100).toFixed(2);
  const marker = (mins >= axisS && mins <= axisE)
    ? '<div class="nowm" style="left:' + pct(Math.max(axisS, Math.min(axisE, mins))) + '%"></div>' : "";
  return '<div class="sessbar"><div class="open" style="left:' + pct(openS) + '%;width:' +
    (pct(openE) - pct(openS)) + '%"></div>' + marker + '</div>' +
    '<div class="small" style="display:flex;justify-content:space-between;"><span>08:00</span><span>09:15 ─── market open ─── 15:30</span><span>16:00</span></div>';
}

function renderOverview() {
  const sum = S.summary || {}, risk = S.risk || {};
  $("ov_cards").innerHTML = cardRow([
    ["Today's P&L", inr(sum.pnl_today), (sum.pnl_today||0) < 0 ? "err" : "ok", "realized + unrealized"],
    ["Open positions", (S.positions||[]).length + " / " + (risk.max_open_positions ?? "–"), (S.positions||[]).length ? "warn" : "ok"],
    ["Signals today", (S.signals||[]).length, "", "this session's window"],
    ["Orders today", sum.orders_today ?? "–"],
    ["Win / Loss (today)", (sum.wins_today ?? 0) + " / " + (sum.losses_today ?? 0)],
    ["Unrealized P&L", inr(sum.unrealized), (sum.unrealized||0) < 0 ? "err" : "ok"],
    ["Realized (today)", inr(sum.realized_today), (sum.realized_today||0) < 0 ? "err" : "ok"],
    ["Exposure", inr(sum.exposure), "", "notional premium (margin: LIVE only)"],
  ]);
  let idxHtml = "";
  for (const [u, d] of Object.entries(S.indices || {})) {
    let arrow = '<span class="dim">–</span>';
    if (d.prev !== null && d.prev !== undefined && d.prev > 0) {
      const ch = d.spot - d.prev;
      arrow = ch > 0 ? '<span class="ok">▲</span>' : ch < 0 ? '<span class="err">▼</span>' : '<span class="dim">·</span>';
    }
    idxHtml += '<div class="card"><div class="k">' + u + ' spot</div><div class="v">' + fmt(d.spot, 2) + " " + arrow + '</div></div>';
  }
  const stateTxt = S.market_state === "OPEN" ? '<span class="ok">OPEN — closes 15:30</span>'
    : S.market_state === "PRE_OPEN" ? '<span class="warn">PRE-OPEN — opens 09:15</span>'
    : '<span class="dim">CLOSED — reopens 09:15</span>';
  $("ov_market").innerHTML = idxHtml + sessionBar() + '<div style="margin-top:6px;">' + stateTxt + '</div>';
  const sys = S.system || {};
  $("ov_engine").innerHTML = cardRow([
    ["Last candle processed", sys.last_candle_ts ? ts(sys.last_candle_ts) : "–", "", "any monitored contract"],
    ["Next candle close", hhmm(sys.next_candle_close), ""],
    ["Engine heartbeat", sys.engine && sys.engine.last_tick ? ago(sys.engine.last_tick) : "–", sys.engine && sys.engine.last_error ? "err" : "ok", sys.engine && sys.engine.last_error ? String(sys.engine.last_error).slice(0, 60) : ""],
    ["Version", S.version || "–"],
  ]);
  const lossLimit = risk.max_daily_loss ? Math.min(1, Math.abs(risk.daily_pnl || 0) / Math.abs(risk.max_daily_loss)) * (risk.daily_pnl < 0 ? 100 : 0) : 0;
  $("ov_risk").innerHTML = cardRow([
    ["Daily loss used", Math.abs(lossLimit).toFixed(0) + " % of " + inr(risk.max_daily_loss), lossLimit > 75 ? "err" : lossLimit > 50 ? "warn" : "ok", "intraday loss limit"],
    ["Trades today", (risk.trades_today ?? 0) + " / " + (risk.max_trades_per_day ?? "–")],
    ["Entries", risk.disable_new_entries ? '<span class="warn">DISABLED</span>' : '<span class="ok">enabled</span>'],
    ["Emergency stop", risk.emergency_stop ? '<span class="err">ENGAGED</span>' : '<span class="ok">off</span>'],
  ]);
  $("ov_alerts").innerHTML = (S.alerts || []).slice(0, 5).map(alHtml).join("") || '<div class="small">no active alerts</div>';
}

function alHtml(a) {
  return '<div class="al ' + a.severity + '"><span class="sev">' + (a.severity === "CRITICAL" ? "🔴" : a.severity === "WARNING" ? "🟠" : "🟢") + " " + a.severity + '</span><span>' + a.message + '</span><span class="small" style="margin-left:auto;">' + ago(a.ts) + '</span></div>';
}

function renderScanner() {
  const f = ($("filter").value || "").toUpperCase();
  const T = nearPct();
  let rows = (S.scanner || []).map(r => {
    const d = deltaPct(r.close !== undefined ? r.close : r.last_close, r.avwap);
    const trig = triggerInfo(r, d);
    const open = (r.status === "OPEN" || r.status === "PENDING");
    const actionable = open ? (d !== null && d >= -T) : (d !== null && d > 0 && d <= T);
    const exitCand = open && d !== null && d >= 0;
    return { r, d, trig, actionable, exitCand,
      txt: (r.underlying + " " + r.symbol + " " + r.status + " " + r.type + " " + r.expiry + " " + trig.token).toUpperCase() };
  });
  if (scanQ === "ACTIONABLE") rows = rows.filter(x => x.actionable);
  else if (scanQ === "OPEN") rows = rows.filter(x => x.r.status === "OPEN" || x.r.status === "PENDING");
  else if (scanQ === "EXIT") rows = rows.filter(x => x.exitCand);
  else if (scanQ === "PARTIAL") rows = rows.filter(x => x.r.hist_partial);
  else if (scanQ === "ERROR") rows = rows.filter(x => x.r.error);
  if (scanType !== "ALL") rows = rows.filter(x => x.r.type === scanType);
  if (scanSide !== "ALL") rows = rows.filter(x => x.d !== null && (scanSide === "ABOVE" ? x.d >= 0 : x.d < 0));
  if (f) rows = rows.filter(x => x.txt.includes(f));
  rows.sort((a, b) => (a.trig.rank - b.trig.rank) || ((b.r.last_close_ts || 0) - (a.r.last_close_ts || 0)));
  const shown = rows.slice(0, 400);
  makeTable($("scanner"),
    [{t:"Underlying",l:1},{t:"Option",l:1},{t:"Expiry",l:1},{t:"DTE"},{t:"Strike"},{t:"Type",l:1},{t:"LTP"},{t:"Close"},{t:"AVWAP"},{t:"AVWAP since",l:1},{t:"Δ vs AVWAP"},{t:"Trigger",l:1},{t:"Vol"},{t:"OI"},{t:"Last close",l:1},{t:"Status",l:1}],
    shown.map(x => {
      const r = x.r;
      return { c: [r.underlying, r.symbol, r.expiry, dteCell(r.expiry), fmt(r.strike, 0), r.type,
          ltpCell(r.ltp), fmt(r.close !== undefined ? r.close : r.last_close), fmt(r.avwap, 3), anchorCell(r),
          dCell(x.d), x.trig.html, r.vol ?? "–", r.oi ?? "–", lastCloseCell(r.last_close_ts),
          r.error ? '<span class="badge b-err" title="' + String(r.error).slice(0, 140) + '">ERROR</span>' + badge(r.status) : badge(r.status)],
        cls: "clickable", onclick: "selectContract(" + JSON.stringify(r.security_id || "") + ")" };
    }),
    shown.map(x => {
      const r = x.r;
      return [r.underlying, r.symbol, r.expiry, dteOf(r.expiry), r.strike, r.type,
        r.ltp, r.close !== undefined ? r.close : r.last_close, r.avwap, r.avwap_anchor_ts, x.d, x.trig.rank,
        r.vol, r.oi, r.last_close_ts, statusOrder(r.status)];
    }));
  const extra = document.createElement("div");
  if (rows.length > 400) extra.innerHTML += '<div class="small">showing 400 of ' + rows.length + ' rows (use the filters)</div>';
  if (S && S.market_state !== "OPEN" && (S.scanner || []).some(r => r.ltp == null))
    extra.innerHTML += '<div class="small" style="margin-top:4px;">LTP column is empty because LTP polling only runs during market hours (09:15–15:30 IST, Mon–Fri) — values appear from the first poll after 09:15.</div>';
  const nPartial = (S.scanner || []).filter(r => r.hist_partial).length;
  if (nPartial) extra.innerHTML += '<div class="err" style="margin-top:4px;">⚠ ' + nPartial +
    ' contract(s) have PARTIAL AVWAP history (anchor may be too late) — Data Health → REBUILD applies at next start.</div>';
  extra.className = "";
  $("scanner").appendChild(extra);
}

function renderContract() {
  const head = $("ct_head");
  if (!contractSel) {
    head.innerHTML = '<div class="small">Select a contract from the <a onclick="showPage(\'scanner\')">Scanner</a> (click any row).</div>';
    $("ct_cards").innerHTML = ""; $("ct_signals").innerHTML = ""; $("ct_position").innerHTML = "";
    $("ct_chartnote").textContent = "";
    const cv = $("chart");
    if (cv && cv.getContext) { const ctx = cv.getContext("2d"); if (ctx) ctx.clearRect(0, 0, cv.width, cv.height); }
    return;
  }
  const d = contractData;
  if (!d) { head.innerHTML = '<div class="small">loading ' + contractSel + '…</div>'; return; }
  const c = d.contract || {};
  head.innerHTML = "<h3 style='margin:0;'>" + (c.symbol || contractSel) +
    (c.expiry ? ' <span class="small">· expiry ' + c.expiry + " · " + dteCell(c.expiry) + "</span>" : "") +
    (d.position ? " " + badge(d.position.status) : "") + "</h3>";
  const av = d.avwap || {};
  const dlt = deltaPct(d.ltp !== null ? d.ltp : av.last_close, av.last_avwap);
  $("ct_cards").innerHTML = cardRow([
    ["LTP", ltpCell(d.ltp)], ["Last close", fmt(av.last_close)],
    ["AVWAP", fmt(av.last_avwap, 3), "", av.partial ? '<span class="err">⚠ PARTIAL history</span>' : ""],
    ["Δ vs AVWAP", dlt === null ? "–" : (dlt >= 0 ? "+" : "") + dlt.toFixed(2) + "%", dlt < 0 ? "err" : "ok"],
    ["AVWAP since", av.anchor_ts ? new Date(av.anchor_ts * 1000).toLocaleDateString("en-IN") : "–", "",
      av.partial ? "anchor may be late" : "contract birth"],
    ["OI (chain)", d.oi ?? "–", "", S.oi_ts ? "snapshot " + ago(S.oi_ts) : ""],
  ]);
  drawChart($("chart"), d);
  $("ct_chartnote").textContent = "candles + AVWAP line (blue) · ▲▼ = entry/exit signals · dashed = AVWAP anchor. Data: persisted 15-min candles (" + (d.candles || []).length + " shown, last 400 kept).";
  makeTable($("ct_signals"),
    [{t:"Candle",l:1},{t:"Action",l:1},{t:"Price"},{t:"Prev close"},{t:"Prev AVWAP"},{t:"AVWAP"},{t:"Reason",l:1}],
    (d.signals || []).map(s => [ts(s.candle_ts), s.action, fmt(s.signal_price), fmt(s.prev_close), fmt(s.prev_avwap, 3), fmt(s.avwap, 3), s.reason || ""]),
    (d.signals || []).map(s => [s.candle_ts, s.action, s.signal_price, s.prev_close, s.prev_avwap, s.avwap, s.reason]));
  const p = d.position;
  if (p && (p.status === "OPEN" || p.status === "PENDING")) {
    $("ct_position").innerHTML = '<div class="pcard"><h3>' + (p.symbol || p.security_id) + ' — ' + p.quantity + ' @ ' + fmt(p.entry_price) +
      '</h3><div class="row"><span class="k">Entry time</span><span>' + ts(p.entry_time) + '</span></div>' +
      '<div class="row"><span class="k">Entry AVWAP</span><span>' + fmt(p.entry_avwap, 3) + '</span></div>' +
      '<div class="row" style="margin-top:8px;"><span></span><button class="danger small" onclick="ctrl(\'close_position\', {position_id: ' + escAttr(JSON.stringify(p.position_id)) + '})">CLOSE POSITION</button></div></div>';
  } else {
    $("ct_position").innerHTML = '<div class="small">no open position on this contract</div>' +
      (av.partial ? ' <button class="small" onclick="ctrl(\'rebuild_avwap\', {security_id: ' + escAttr(JSON.stringify(contractSel)) + '})">SCHEDULE AVWAP REBUILD (next start)</button>' : "");
  }
  if (av.partial && p) {
    const b = document.createElement("div");
    b.innerHTML = ' <button class="small" onclick="ctrl(\'rebuild_avwap\', {security_id: ' + escAttr(JSON.stringify(contractSel)) + '})">SCHEDULE AVWAP REBUILD (next start)</button>';
    $("ct_position").appendChild(b);
  }
}

// ---------------- contract chart ----------------
function drawChart(cv, d) {
  if (!cv || !cv.getContext) return;
  const ctx = cv.getContext("2d");
  if (!ctx) return; // canvas 2d unavailable (headless environments)
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const cs = d.candles || [];
  if (cs.length < 2) {
    ctx.fillStyle = "#8b949e"; ctx.font = "13px monospace";
    ctx.fillText("Not enough persisted candles yet (candles persist as they close).", 14, 30);
    return;
  }
  const N = Math.min(cs.length, 160);
  const view = cs.slice(-N);
  let lo = Infinity, hi = -Infinity;
  for (const c of view) {
    lo = Math.min(lo, c.low, c.avwap != null ? c.avwap : c.low);
    hi = Math.max(hi, c.high, c.avwap != null ? c.avwap : c.high);
  }
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const volMax = Math.max.apply(null, view.map(c => c.volume || 0).concat([1]));
  const plotW = W - 64, plotH = H - 70, volH = 38;
  const x = i => 10 + i * (plotW / (N - 1));
  const y = p => 8 + (hi - p) * (plotH / (hi - lo));
  const cw = Math.max(2, (plotW / N) * 0.6);
  ctx.font = "10px monospace";
  for (let g = 0; g <= 4; g++) {
    const p = lo + (hi - lo) * g / 4, yy = y(p);
    ctx.strokeStyle = "#21262d"; ctx.beginPath(); ctx.moveTo(10, yy); ctx.lineTo(W - 54, yy); ctx.stroke();
    ctx.fillStyle = "#8b949e"; ctx.fillText(p.toFixed(1), W - 50, yy + 3);
  }
  view.forEach((c, i) => {
    const up = c.close >= c.open;
    const col = up ? "#3fb950" : "#f85149";
    const xx = x(i);
    ctx.strokeStyle = col;
    ctx.beginPath(); ctx.moveTo(xx, y(c.high)); ctx.lineTo(xx, y(c.low)); ctx.stroke();
    const yo = y(c.open), yc = y(c.close);
    ctx.fillStyle = col;
    ctx.fillRect(xx - cw / 2, Math.min(yo, yc), cw, Math.max(1, Math.abs(yo - yc)));
    const vh = (c.volume || 0) / volMax * volH;
    ctx.fillStyle = up ? "rgba(63,185,80,.35)" : "rgba(248,81,73,.35)";
    ctx.fillRect(xx - cw / 2, H - 26 - vh, cw, vh);
  });
  ctx.strokeStyle = "#58a6ff"; ctx.lineWidth = 2; ctx.beginPath();
  let started = false;
  view.forEach((c, i) => {
    if (c.avwap == null) return;
    const xx = x(i), yy = y(c.avwap);
    if (!started) { ctx.moveTo(xx, yy); started = true; } else ctx.lineTo(xx, yy);
  });
  ctx.stroke(); ctx.lineWidth = 1;
  const anchor = d.avwap && d.avwap.anchor_ts;
  if (anchor) {
    const ai = view.findIndex(c => c.ts >= anchor);
    if (ai >= 0) {
      const xx = x(ai);
      ctx.strokeStyle = "#d29922"; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(xx, 6); ctx.lineTo(xx, H - 26); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#d29922"; ctx.font = "10px monospace";
      ctx.fillText("AVWAP START", Math.min(xx + 5, W - 120), 16);
    }
  }
  (d.signals || []).forEach(s => {
    const i = view.findIndex(c => c.ts === s.candle_ts);
    if (i < 0) return;
    const xx = x(i);
    if (s.action === "ENTRY_SELL") {
      ctx.fillStyle = "#f85149";
      ctx.beginPath(); ctx.moveTo(xx, 14); ctx.lineTo(xx - 5, 4); ctx.lineTo(xx + 5, 4); ctx.closePath(); ctx.fill();
    } else if (s.action === "EXIT_BUY") {
      ctx.fillStyle = "#3fb950";
      ctx.beginPath(); ctx.moveTo(xx, 4); ctx.lineTo(xx - 5, 14); ctx.lineTo(xx + 5, 14); ctx.closePath(); ctx.fill();
    }
  });
  ctx.fillStyle = "#8b949e"; ctx.font = "10px monospace";
  const step = Math.max(1, Math.floor(N / 9));
  view.forEach((c, i) => {
    if (i % step) return;
    const dt = new Date(c.ts * 1000);
    ctx.fillText(String(dt.getHours()).padStart(2, "0") + ":" + String(dt.getMinutes()).padStart(2, "0"),
      Math.max(4, Math.min(x(i) - 12, W - 34)), H - 10);
  });
}

function renderPositions() {
  const sum = S.summary || {}, risk = S.risk || {};
  $("pos_cards").innerHTML = cardRow([
    ["Total positions", (S.positions || []).length + " / " + (risk.max_open_positions ?? "–")],
    ["Exposure", inr(sum.exposure), "", "notional premium"],
    ["Unrealized P&L", inr(sum.unrealized), (sum.unrealized || 0) < 0 ? "err" : "ok"],
    ["Realized (today)", inr(sum.realized_today), (sum.realized_today || 0) < 0 ? "err" : "ok"],
    ["Realized (all time)", inr(sum.realized_total), (sum.realized_total || 0) < 0 ? "err" : "ok"],
  ]);
  const pos = S.positions || [];
  $("pos_cards_grid").innerHTML = pos.map(p => {
    const exitTxt = p.exit_due ? '<span class="err">YES — close above AVWAP</span>' : '<span class="dim">no (last close below AVWAP)</span>';
    return '<div class="pcard"><h3>' + p.option + ' <span class="dim">SHORT ' + p.quantity + '</span></h3>' +
      '<div class="row"><span class="k">Entry</span><span>' + fmt(p.entry) + '</span></div>' +
      '<div class="row"><span class="k">Current</span><span>' + fmt(p.current) + '</span></div>' +
      '<div class="row"><span class="k">AVWAP (entry / now)</span><span>' + fmt(p.avwap, 3) + ' / ' + fmt(p.avwap_now, 3) + '</span></div>' +
      '<div class="row"><span class="k">Unrealized</span><span class="' + ((p.unrealized_pnl||0) < 0 ? "err" : "ok") + '">' + inr(p.unrealized_pnl) + '</span></div>' +
      '<div class="row"><span class="k">Duration</span><span>' + durFmt(p.duration_s) + '</span></div>' +
      '<div class="row"><span class="k">AVWAP exit (close > AVWAP)</span><span>' + exitTxt + '</span></div>' +
      '<div class="row"><span class="k">Status</span><span>' + badge(p.status) + '</span></div>' +
      '<div class="row" style="margin-top:8px;"><span></span><button class="danger small" onclick="ctrl(\'close_position\', {position_id: ' + escAttr(JSON.stringify(p.position_id)) + '})">CLOSE</button></div></div>';
  }).join("") || '<div class="small">no open positions</div>';
  makeTable($("pos_table"),
    [{t:"Underlying",l:1},{t:"Option",l:1},{t:"Expiry",l:1},{t:"Strike"},{t:"Type",l:1},{t:"Qty"},{t:"Entry"},{t:"Current"},{t:"AVWAP(entry)"},{t:"uP&L"},{t:"Status",l:1},{t:"Entry time",l:1}],
    pos.map(p => [p.underlying, p.option, p.expiry, fmt(p.strike, 0), p.type, p.quantity, fmt(p.entry), fmt(p.current), fmt(p.avwap),
      '<span class="' + ((p.unrealized_pnl||0) < 0 ? "err" : "ok") + '">' + fmt(p.unrealized_pnl) + '</span>', badge(p.status), ts(p.entry_time)]),
    pos.map(p => [p.underlying, p.option, p.expiry, p.strike, p.type, p.quantity, p.entry, p.current, p.avwap, p.unrealized_pnl, statusOrder(p.status), p.entry_time]));
}

function renderSignals() {
  const sigs = S.signals || [];
  $("sig_cards").innerHTML = sigs.map(s => {
    const entry = s.action === "ENTRY_SELL";
    const prevOk = s.prev_close != null && s.prev_avwap != null && s.prev_close >= s.prev_avwap;
    const curOk = entry
      ? (s.prev_close != null && s.avwap != null && s.prev_close >= s.prev_avwap && s.signal_price < s.avwap)
      : (s.signal_price != null && s.avwap != null && s.signal_price > s.avwap);
    const rule = entry ? "TRUE CROSS BELOW AVWAP" : "CLOSE ABOVE AVWAP";
    const ch = s.chain || {};
    let riskHtml;
    if (!entry) riskHtml = '<span class="dim">n/a (exit)</span>';
    else if (ch.blocked) riskHtml = '<span class="err">BLOCKED: ' + ch.blocked.join(", ") + '</span>';
    else if (ch.order) riskHtml = '<span class="ok">PASSED</span> → ' + orderBadge(ch.order.status);
    else riskHtml = '<span class="dim">no order (no fill needed / pending)</span>';
    const chain = "SIGNAL " + (entry ? "🔴 SELL" : "🟢 BUY-BACK") +
      " → ORDER " + (ch.order ? (ch.order.order_id + " (" + ch.order.status + ")") : "—") +
      " → POSITION " + (ch.position ? ch.position.status + " @ " + fmt(ch.position.entry_price) : "—");
    return '<div class="scard"><b>' + ts(s.candle_ts) + '</b> ' + (entry
        ? '<span class="badge b-rej">SELL SIGNAL</span>' : '<span class="badge b-filled">EXIT SIGNAL</span>') +
      " <a onclick=\"selectContract(" + escAttr(JSON.stringify(s.security_id || "")) + ")\">" + (s.symbol || s.security_id) + "</a>" +
      '<div style="margin-top:8px;">' +
      '<div>Previous candle&nbsp; close ' + fmt(s.prev_close) + ' vs AVWAP ' + fmt(s.prev_avwap, 3) + ' &nbsp;→&nbsp; <span class="' + (prevOk ? "rule-ok" : "rule-bad") + '">' + (prevOk ? "✓ close ≥ AVWAP" : "✗ close < AVWAP") + '</span></div>' +
      '<div>Current candle&nbsp; close ' + fmt(s.signal_price) + ' vs AVWAP ' + fmt(s.avwap, 3) + ' &nbsp;→&nbsp; <span class="' + (curOk ? "rule-ok" : "rule-bad") + '">' + (entry ? (curOk ? "✓ close < AVWAP" : "✗ close ≥ AVWAP") : (curOk ? "✓ close > AVWAP" : "✗ close ≤ AVWAP")) + '</span></div>' +
      '<div>Rule: <b>' + rule + '</b>' + (s.reason ? ' <span class="small">(' + s.reason + ')</span>' : "") + '</div>' +
      '<div>Risk check: ' + riskHtml + '</div></div>' +
      '<div class="chain">' + chain + '</div></div>';
  }).join("") || '<div class="small">no signals yet</div>';
}

let ordersExpanded = null;
function renderOrders() {
  const orders = S.orders || [];
  makeTable($("orders_table"),
    [{t:"Placed",l:1},{t:"Order ID",l:1},{t:"Contract",l:1},{t:"Action",l:1},{t:"Side",l:1},{t:"Qty"},{t:"Status",l:1},{t:"Filled"},{t:"Avg price"},{t:"Broker response",l:1}],
    orders.map(o => {
      const cells = [ts(o.placed_at), o.order_id, o.symbol, o.action, o.side, o.quantity,
        orderBadge(o.status), o.filled_qty ?? "–", fmt(o.avg_price),
        '<span class="small">' + (o.raw_snippet ? o.raw_snippet.slice(0, 60) + (o.raw_snippet.length > 60 ? "…" : "") : "—") + '</span>'];
      if (ordersExpanded === o.order_id) {
        return { c: cells, extra: true };
      }
      return { c: cells, cls: "clickable", onclick: "ordersExpanded = (ordersExpanded === " + escAttr(JSON.stringify(o.order_id)) + ") ? null : " + escAttr(JSON.stringify(o.order_id)) + "; render();" };
    }),
    orders.map(o => [o.placed_at, o.order_id, o.symbol, o.action, o.side, o.quantity, o.status, o.filled_qty, o.avg_price, o.raw_snippet]));
  if (ordersExpanded) {
    const o = orders.find(x => x.order_id === ordersExpanded);
    if (o) {
      const d = document.createElement("pre");
      d.style.marginTop = "-4px";
      d.textContent = o.raw_snippet || "(no raw response stored - paper fills or pre-V2 order)";
      $("orders_table").appendChild(d);
    }
  }
}

function feedState(f, staleAfter) {
  const now = Date.now() / 1000;
  if (f.last_fail && f.last_fail > f.last_ok && now - f.last_fail < 300) return "err";
  if (!f.last_ok) return "warn";
  if (now - f.last_ok > staleAfter) return "warn";
  return "ok";
}
function renderDataHealth() {
  const feeds = S.feeds || {};
  const labels = { ltp: "Quote feed (LTP)", chain: "Option chain", candles: "Candle feed", master: "Instrument master" };
  const stale = { ltp: 90, chain: 3600, candles: 20 * 60, master: 14 * 3600 };
  $("dh_feeds").innerHTML = cardRow(Object.keys(labels).map(k => {
    const f = feeds[k] || {};
    const st = feedState(f, stale[k]);
    return ["<span class='dot " + st + "'></span>" + labels[k],
      st === "ok" ? '<span class="ok">OK</span>' : st === "warn" ? '<span class="warn">STALE</span>' : '<span class="err">ERROR</span>',
      "", "last OK " + ago(f.last_ok) + (f.errors ? " · " + f.errors + " err(s)" : "") + (f.last_latency_ms ? " · " + Math.round(f.last_latency_ms) + " ms" : "")];
  }));
  const rest = (S.system && S.system.rest) || null;
  if (rest) {
    $("dh_rest").innerHTML = cardRow([
      ["Requests", rest.requests],
      ["HTTP 429 (rate limit)", rest.h429, rest.h429 ? "warn" : "ok"],
      ["Errors (final)", rest.errors, rest.errors ? "err" : "ok"],
      ["Avg latency", Math.round(rest.avg_latency_ms) + " ms"],
      ["Last success", ago(rest.last_success_ts)],
      ["Last error", rest.last_error ? '<span class="err">' + String(rest.last_error).slice(0, 40) + '</span>' : '<span class="ok">none</span>', "", ago(rest.last_error_ts)],
    ]);
  } else {
    $("dh_rest").innerHTML = '<div class="small">no live REST session (mock source)</div>';
  }
  const av = S.avwap_health || {};
  $("dh_av_cards").innerHTML = cardRow([
    ["Contracts monitored", av.monitored ?? "–"],
    ["Complete anchors", av.complete ?? "–", "ok"],
    ["PARTIAL history", av.partial ? (av.partial.length) : 0, (av.partial && av.partial.length) ? "warn" : "ok", "anchor may be late"],
    ["Rebuild queue", (av.queue || []).length, (av.queue || []).length ? "warn" : "ok", "applies at next start"],
  ]);
  const partial = av.partial || [];
  if (partial.length) {
    makeTable($("dh_av_partial"),
      [{t:"Contract",l:1},{t:"Expiry",l:1},{t:"Stored anchor",l:1},{t:"Action",l:1}],
      partial.map(p => [p.symbol, p.expiry || "–", p.anchor_ts ? new Date(p.anchor_ts * 1000).toLocaleDateString("en-IN") : "–",
        '<button class="danger small" onclick="ctrl(\'rebuild_avwap\', {security_id: ' + JSON.stringify(p.security_id) + '})">REBUILD (next start)</button>']),
      partial.map(p => [p.symbol, p.expiry, p.anchor_ts, ""]));
  } else {
    $("dh_av_partial").innerHTML = '<div class="small">all monitored anchors are complete 🟢</div>';
  }
  $("dh_queue").innerHTML = (av.queue || []).length
    ? '<div class="small">Rebuild queue (applies at next start): ' + av.queue.join(", ") + '</div>' : "";
}

function renderSystem() {
  const sys = S.system || {}, now = Date.now() / 1000;
  const eng = sys.engine || {};
  const engStale = eng.last_tick && (now - eng.last_tick > 15);
  $("sys_comp").innerHTML = cardRow([
    ["Engine", eng.last_tick ? (engStale ? '<span class="warn">STALE</span>' : '<span class="ok">RUNNING</span>') : "–", eng.last_error ? "err" : ""],
    ["Strategy", (S.scanner || []).length ? '<span class="ok">OK</span>' : "–", "", (S.scanner || []).length + " monitored"],
    ["Execution", S.mode, S.mode === "LIVE" ? "err" : "ok", S.mode === "LIVE" ? "real orders" : "virtual fills"],
    ["Database", sys.db && sys.db.ok ? '<span class="ok">OK</span>' : "–", "", sys.db ? sys.db.size_mb + " MB" : ""],
    ["Dashboard", '<span class="ok">OK</span>', "", "build " + (S.version || "?")],
  ]);
  $("sys_beats").innerHTML = cardRow([
    ["Engine heartbeat", eng.last_tick ? ago(eng.last_tick) : "–", engStale ? "warn" : "ok"],
    ["Last candle processed", sys.last_candle_ts ? ts(sys.last_candle_ts) : "–"],
    ["Next candle close", hhmm(sys.next_candle_close)],
    ["Last LTP poll", ago((S.feeds || {}).ltp && S.feeds.ltp.last_ok)],
    ["Last chain fetch", ago((S.feeds || {}).chain && S.feeds.chain.last_ok)],
    ["Uptime", durFmt(S.uptime_s)],
  ]);
  const rest = sys.rest;
  if (rest && rest.by_path) {
    makeTable($("sys_by_path"),
      [{t:"Endpoint",l:1},{t:"Requests"},{t:"429s"},{t:"Errors"},{t:"Last latency"}],
      Object.entries(rest.by_path).map(([p, v]) => [p, v.requests, v.h429, v.errors, Math.round(v.last_latency_ms) + " ms"]),
      Object.entries(rest.by_path).map(([p, v]) => [p, v.requests, v.h429, v.errors, v.last_latency_ms]));
  } else {
    $("sys_by_path").innerHTML = '<div class="small">no live REST session (mock source)</div>';
  }
  const pr = sys.process;
  $("sys_proc").innerHTML = pr
    ? cardRow([["CPU", fmt(pr.cpu_pct, 0) + " %"], ["RAM", pr.ram_mb + " MB"], ["Disk used", pr.disk_pct + " %"], ["PID", "this process"]])
    : '<div class="small">process metrics unavailable (psutil not installed - pip install psutil)</div>';
}

function incidentSeverity(ev) {
  if (["ORDER_FAILED", "ORDER_REJECTED", "EXIT_ALL_REQUESTED", "APP_CRASH"].includes(ev)) return "CRITICAL";
  if (["ENTRY_BLOCKED", "RECONCILE", "AVWAP_REBUILD_APPLIED", "AVWAP_REBUILD_SCHEDULED", "MANUAL_CLOSE_REQUESTED", "ORDER_FAILED_PAPER"].includes(ev)) return "WARNING";
  return "INFO";
}
function renderAlerts() {
  $("al_now").innerHTML = (S.alerts || []).map(alHtml).join("") || '<div class="small">no active alerts 🟢</div>';
  const rows = (journalData || []).slice(0, 100).map(j => ({ j, sev: incidentSeverity(j.event) }));
  makeTable($("al_hist"),
    [{t:"Time",l:1},{t:"Severity",l:1},{t:"Event",l:1},{t:"Symbol",l:1},{t:"Detail",l:1}],
    rows.map(r => [ts(r.j.ts), r.sev, r.j.event, r.j.symbol || "",
      '<span class="small">' + JSON.stringify(r.j.detail || {}).slice(0, 110) + "</span>"]),
    rows.map(r => [r.j.ts, r.sev, r.j.event, r.j.symbol, ""]));
}

async function loadDayPnl() {
  $("daypnl_summary").innerHTML = '<div class="small">loading...</div>';
  try {
    const res = await fetch("/api/pnl_history");
    if (res.ok) dayPnlData = await res.json();
  } catch (e) {
    console.error("loadDayPnl failed", e);
  }
  if (CUR === "daypnl") renderDayPnl();
}

function renderDayPnl() {
  if (!dayPnlData) return;
  const days = dayPnlData.days || [];
  let totalGross = 0, totalChg = 0, totalNet = 0;
  days.forEach(d => { totalGross += d.gross_pnl; totalChg += d.charges; totalNet += d.net_pnl; });
  $("daypnl_summary").innerHTML = cardRow([
      ["Total Gross P&L", inr(totalGross), totalGross < 0 ? "err" : "ok"],
      ["Total Estimated Charges", inr(totalChg)],
      ["Total Net P&L", inr(totalNet), totalNet < 0 ? "err" : "ok"]
  ]);

  makeTable($("daypnl_table"),
    [{t:"Date",l:1},{t:"Trades",l:1},{t:"Gross P&L"},{t:"Est. Charges"},{t:"Net P&L (approx)"}],
    days.map(d => [d.date, d.trades, inr(d.gross_pnl), inr(d.charges), inr(d.net_pnl)]),
    days.map(d => [d.date, d.trades, d.gross_pnl, d.charges, d.net_pnl])
  );
}

function renderJournal() {
  const rows = journalData || [];
  $("j_status").textContent = rows.length ? rows.length + " rows loaded " + ago(journalLoadedAt) : "loading…";
  makeTable($("journal_table"),
    [{t:"Time",l:1},{t:"Event",l:1},{t:"Symbol",l:1},{t:"Position",l:1},{t:"Detail",l:1}],
    rows.map(j => [ts(j.ts), j.event, j.symbol || "", j.position_id || "",
      '<span class="small">' + JSON.stringify(j.detail || {}).slice(0, 150) + "</span>"]),
    rows.map(j => [j.ts, j.event, j.symbol, j.position_id, ""]));
}
let journalLoadedAt = 0;
async function loadJournal(force) {
  const q = new URLSearchParams();
  const ev = ($("j_event").value || "").trim();
  const sym = ($("j_symbol").value || "").trim();
  const lim = $("j_limit").value || "200";
  if (ev) q.set("event", ev);
  if (sym) q.set("symbol", sym);
  q.set("limit", lim);
  const key = q.toString();
  if (!force && journalLoadedFor === key && (Date.now() - journalLoadedAt) < 8000) return;
  try {
    const r = await fetch("/api/journal?" + key.toString());
    if (r.ok) {
      journalData = await r.json();
      journalLoadedFor = key;
      journalLoadedAt = Date.now();
      render();
    }
  } catch (e) { /* keep previous */ }
}
function exportJournal(kind) {
  const rows = journalData || [];
  let href, name;
  if (kind === "json") {
    href = "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(rows, null, 1));
    name = "avwap_journal_" + Date.now() + ".json";
  } else {
    const esc = v => v === null || v === undefined ? "" : (typeof v === "string" && /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v);
    const lines = ["ts,event,symbol,position_id,security_id,detail"];
    rows.forEach(j => lines.push([new Date(j.ts * 1000).toISOString(), j.event, j.symbol || "", j.position_id || "", j.security_id || "", esc(JSON.stringify(j.detail || {}))].map(esc).join(",")));
    href = "data:text/csv;charset=utf-8," + encodeURIComponent(lines.join("\n"));
    name = "avwap_journal_" + Date.now() + ".csv";
  }
  const a = document.createElement("a");
  a.href = href; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
}

function renderRecovery() {
  const up = S.startup || {};
  $("rec_status").innerHTML = (up.phase === "running")
    ? '<div class="ok" style="margin-bottom:8px;">STATUS: <b>READY</b> — engine running since ' + ts(S.started_at) +
      " · " + (S.universe ? S.universe.monitored_contracts : 0) + " monitored contracts · " +
      (S.positions || []).length + " open positions</div>"
    : '<div class="warn" style="margin-bottom:8px;">STATUS: BOOTSTRAPPING — ' + (up.detail || "") + '</div>';
  const tl = up.timeline || [];
  makeTable($("rec_timeline"),
    [{t:"Time",l:1},{t:"Phase",l:1},{t:"Detail",l:1},{t:"Progress"}],
    tl.map(e => [ts(e.ts), e.phase, e.detail, e.total ? (e.progress + " / " + e.total) : ""]),
    tl.map(e => [e.ts, e.phase, e.detail, e.progress]));
  const prev = up.prev_timeline || [];
  makeTable($("rec_prev"),
    [{t:"Time",l:1},{t:"Phase",l:1},{t:"Detail",l:1},{t:"Progress"}],
    prev.map(e => [ts(e.ts), e.phase, e.detail, e.total ? (e.progress + " / " + e.total) : ""]),
    prev.map(e => [e.ts, e.phase, e.detail, e.progress]));
}

function renderSettings() {
  const st = S.settings || {};
  const sec = (title, pairs) => {
    const rows = pairs.filter(p => p[1] !== undefined && p[1] !== null).map(p =>
      '<div class="k">' + p[0] + '</div><div>' + (typeof p[1] === "object" ? JSON.stringify(p[1]) : p[1]) + "</div>").join("");
    return "<h2>" + title + '</h2><div class="kv">' + (rows || '<div class="dim">—</div>') + "</div>";
  };
  const m = st.market_data || {}, s2 = st.strategy || {}, r = st.risk || {}, p = st.paper || {}, lv = st.live || {}, sd = st.storage || {};
  $("set_tables").innerHTML =
    sec("Strategy", [["candle interval (min)", s2.candle_interval_minutes], ["ITM strikes per side", s2.itm_strikes_per_side], ["expiry switch day", s2.expiry_switch_day]]) +
    sec("Market data", [["source", m.source], ["LTP poll (s)", m.ltp_poll_seconds], ["universe refresh (min)", m.universe_refresh_minutes], ["loop tick (s)", m.loop_tick_seconds], ["universe stocks", m.universe_stocks_count], ["universe indices", (m.universe_indices || []).join(", ")], ["weekly expiries", m.weekly_expiries], ["history start", m.history_start]]) +
    sec("Risk", [["quantity per trade", r.quantity_per_trade], ["max open positions", r.max_open_positions], ["max trades / day", r.max_trades_per_day], ["max daily loss", r.max_daily_loss]]) +
    sec("Paper", [["fill mode", p.fill_mode], ["slippage (bps)", p.slippage_bps], ["next-quote timeout (s)", p.next_quote_timeout_seconds]]) +
    sec("Live", [["product type", lv.product_type], ["order type", lv.order_type], ["limit offset (ticks)", lv.limit_price_offset_ticks], ["order poll (s)", lv.order_poll_seconds]]) +
    sec("System", [["version", st.version], ["trading mode", st.trading_mode], ["db path", sd.db_path]]);
}

// ---------------- contract fetch ----------------
async function loadContract(force) {
  if (!contractSel) return;
  if (!force && contractData && (Date.now() - contractFetchedAt) < 15000) return;
  try {
    const r = await fetch("/api/contract/" + encodeURIComponent(contractSel));
    if (r.ok) { contractData = await r.json(); contractFetchedAt = Date.now(); }
  } catch (e) { /* keep previous */ }
  if (CUR === "contract") render();
}

// ---------------- controls ----------------
function ctrl(action, payload) {
  const token = prompt("Enter dashboard control token (empty if unset):") ?? "";
  if (action === "exit_all") {
    const c = prompt("This closes ALL open positions at market.\nType EXIT ALL to confirm:");
    if (c !== "EXIT ALL") return;
  }
  if (action === "close_position" && !confirm("Close this position now (buy-to-close at market)?")) return;
  if (action === "rebuild_avwap" && !confirm("Schedule AVWAP rebuild for this contract?\n\nIt applies at the NEXT application start (the running engine is never modified live).")) return;
  fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({ action: action, token: token }, payload || {}))
  }).then(r => r.json()).then(d => {
    $("ctrl_status").textContent = (d.ok ? "✓ " : "✗ ") + d.message;
    setTimeout(() => { if ($("ctrl_status")) $("ctrl_status").textContent = ""; }, 8000);
    setTimeout(poll, 600);
  }).catch(e => { $("ctrl_status").textContent = "✗ " + e; });
}

// ---------------- scanner filter wiring ----------------
document.querySelectorAll("#scan_q button").forEach(b => b.onclick = () => {
  scanQ = b.dataset.q;
  document.querySelectorAll("#scan_q button").forEach(x => x.classList.toggle("on", x === b));
  render();
});
document.querySelectorAll("#scan_type button").forEach(b => b.onclick = () => {
  scanType = b.dataset.t;
  document.querySelectorAll("#scan_type button").forEach(x => x.classList.toggle("on", x === b));
  render();
});
document.querySelectorAll("#scan_side button").forEach(b => b.onclick = () => {
  scanSide = b.dataset.s;
  document.querySelectorAll("#scan_side button").forEach(x => x.classList.toggle("on", x === b));
  render();
});
if ($("filter")) $("filter").oninput = render;
if ($("near_in")) { $("near_in").value = nearPct(); $("near_in").onchange = () => setNear($("near_in").value); }
if ($("j_event")) $("j_event").onchange = () => loadJournal(true);
if ($("j_symbol")) $("j_symbol").onchange = () => loadJournal(true);
if ($("j_limit")) $("j_limit").onchange = () => loadJournal(true);

// ---------------- CSV export (scanner) ----------------
function exportScannerCSV() {
  const esc = v => v === null || v === undefined ? "" : (typeof v === "string" && /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v);
  const lines = ["underlying,symbol,expiry,dte,strike,type,ltp,close,avwap,delta_pct_vs_avwap,vol,oi,status"];
  (S.scanner || []).forEach(r => {
    const d = deltaPct(r.close !== undefined ? r.close : r.last_close, r.avwap);
    lines.push([r.underlying, r.symbol, r.expiry, dteOf(r.expiry), r.strike, r.type,
      r.ltp, r.close !== undefined ? r.close : r.last_close, r.avwap,
      d === null ? "" : d.toFixed(4), r.vol, r.oi, r.status].map(esc).join(","));
  });
  const a = document.createElement("a");
  a.href = "data:text/csv;charset=utf-8," + encodeURIComponent(lines.join("\n"));
  a.download = "avwap_scanner_" + new Date().toISOString().slice(0, 16).replace("T", "_") + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
}

// ---------------- polling ----------------
let lastPollOk = false, pollFails = 0, pollCount = 0;
function nowStr() { return new Date().toLocaleTimeString(); }
function setPollstat(msg, ok) {
  const el = $("pollstat");
  if (!el) return;
  el.textContent = "engine link: " + msg;
  el.style.color = ok ? "#3fb950" : "#d29922";
}
async function poll() {
  pollCount++;
  try {
    const ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
    const to = ac ? setTimeout(() => ac.abort(), 15000) : null;
    const r = await fetch("/api/state", ac ? { signal: ac.signal } : undefined);
    if (to) clearTimeout(to);
    S = await r.json();
    lastPollOk = true; pollFails = 0;
    setPollstat("OK — engine responded at " + nowStr() +
      " (engine says: " + ((S && S.startup) ? S.startup.phase : "n/a") +
      (S && S.version ? ", build " + S.version : "") + ")", true);
    if (CUR === "contract" && contractSel) loadContract(false);
    render();
  } catch (e) {
    lastPollOk = false; pollFails++;
    S = S || { startup: { phase: "starting" } };
    const why = (e && e.name === "AbortError") ? "timed out after 15 s (engine not responding)" : String(e);
    setPollstat("FAILED at " + nowStr() + " — " + why + " (will retry)", false);
    $("banner").className = "banner err";
    $("banner").textContent = "dashboard: cannot reach engine /api/state (" + why + ") — retrying…";
  }
  const booting = !!(S && S.startup && S.startup.phase === "bootstrapping");
  setTimeout(poll, booting ? 1000 : 4000);
}
showPage(CUR);
poll();
</script>
</body></html>"""

PAGE_HTML = PAGE.replace("__DASH_VERSION__", DASH_VERSION)


def create_dashboard(app, control_token: str = "") -> Flask:
    flask = Flask(__name__)

    @flask.route("/")
    def index():
        return PAGE_HTML

    @flask.route("/api/state")
    def state():
        t0 = time.time()
        try:
            out = app.state_for_dashboard()
            out["dashboard_version"] = DASH_VERSION
            return jsonify(out)
        except Exception:
            log.exception("api/state failed")
            raise
        finally:
            dt_ms = (time.time() - t0) * 1000
            if dt_ms > 500:
                log.warning("api/state took %.0f ms (slow)", dt_ms)

    @flask.route("/api/contract/<security_id>")
    def contract(security_id: str):
        try:
            return jsonify(app.contract_payload(security_id))
        except Exception:
            log.exception("api/contract failed")
            raise

    @flask.route("/api/journal")
    def journal():
        try:
            return jsonify(app.journal_payload(
                limit=int(request.args.get("limit", 200)),
                event=request.args.get("event") or None,
                symbol=request.args.get("symbol") or None,
                security_id=request.args.get("security_id") or None,
                since_ts=int(request.args.get("since_ts", 0) or 0),
                until_ts=(int(request.args["until_ts"])
                          if request.args.get("until_ts") else None),
            ))
        except Exception:
            log.exception("api/journal failed")
            raise

    @flask.route("/api/pnl_history")
    def pnl_history():
        try:
            return jsonify(app.pnl_history_payload())
        except Exception:
            log.exception("api/pnl_history failed")
            raise

    @flask.route("/api/control", methods=["POST"])
    def control():
        data = request.get_json(silent=True) or {}
        action = data.get("action", "")
        token = data.get("token", "")
        if control_token and token != control_token:
            return jsonify(ok=False, message="bad control token"), 403
        try:
            if action == "disable_entries":
                app.set_disable_entries(True)
                return jsonify(ok=True, message="new entries disabled")
            if action == "enable_entries":
                app.set_disable_entries(False)
                return jsonify(ok=True, message="new entries enabled")
            if action == "emergency_stop":
                app.set_emergency_stop(True)
                return jsonify(ok=True, message="EMERGENCY STOP engaged (exits still work)")
            if action == "release_stop":
                app.set_emergency_stop(False)
                return jsonify(ok=True, message="emergency stop released")
            if action == "exit_all":
                n = app.exit_all_positions("MANUAL_EXIT_ALL")
                return jsonify(ok=True, message=f"exit-all executed for {n} positions")
            if action == "close_position":
                pid = data.get("position_id", "")
                if not pid:
                    return jsonify(ok=False, message="position_id required"), 400
                msg = app.close_position_manual(pid)
                return jsonify(ok=True, message=msg)
            if action == "rebuild_avwap":
                sec = data.get("security_id", "")
                if not sec:
                    return jsonify(ok=False, message="security_id required"), 400
                return jsonify(ok=True, message=app.schedule_rebuild(sec))
            return jsonify(ok=False, message=f"unknown action {action!r}"), 400
        except Exception as e:
            log.exception("control action failed: %s", e)
            return jsonify(ok=False, message=str(e)), 500

    return flask


class DashboardServer:
    def __init__(self, app, host: str, port: int, control_token: str = ""):
        self.app = app
        self.host = host
        self.port = port
        self.flask = create_dashboard(app, control_token)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self.flask.run,
            kwargs={"host": self.host, "port": self.port,
                    "debug": False, "use_reloader": False, "threaded": True},
            daemon=True, name="dashboard",
        )
        self.thread.start()
        log.info("Dashboard listening on http://%s:%d", self.host, self.port)





