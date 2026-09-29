/* V2 dashboard render harness: loads the served HTML in jsdom with a fetch
 * stub backed by REAL payloads generated from a mock TraderApp, then visits
 * every page and checks for JS errors + expected content. */
const fs = require("fs");
let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require("jsdom")); }
catch (e) {
  console.error("jsdom is required but not installed.");
  console.error("Run:  npm install jsdom   (in this folder, or anywhere on NODE_PATH)");
  process.exit(2);
}

const html = fs.readFileSync("/tmp/v2h/page.html", "utf8");
const state = JSON.parse(fs.readFileSync("/tmp/v2h/state.json", "utf8"));
const contract = JSON.parse(fs.readFileSync("/tmp/v2h/contract.json", "utf8"));
const journal = JSON.parse(fs.readFileSync("/tmp/v2h/journal.json", "utf8"));
const contractId = fs.readFileSync("/tmp/v2h/contract_id.txt", "utf8").trim();

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", e => { const m = String(e && (e.detail || e)); if (!/Could not load link|Not implemented: HTMLCanvasElement/.test(m)) errors.push("jsdomError: " + m); });
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  url: "http://localhost:8000/",
  virtualConsole: vc,
  pretendToBeVisual: true,
  beforeParse(window) {
    // fetch stub MUST exist before the page script runs (poll() fires at load)
    window.fetch = async (url) => {
      url = String(url);
      let body;
      if (url.startsWith("/api/state")) body = state;
      else if (url.startsWith("/api/contract/")) body = contract;
      else if (url.startsWith("/api/journal")) body = journal;
      else return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => body };
    };
    window.confirm = () => false; // ctrl() safety prompts always abort in the harness
    window.prompt = () => null; // type-to-confirm always aborts in the harness
    window.addEventListener("error", e => errors.push("window error: " + e.message));
  },
});
const { window } = dom;

const sleep = ms => new Promise(r => setTimeout(r, ms));

function check(cond, msg) {
  if (!cond) { errors.push("ASSERT FAIL: " + msg); console.log("  ✗ " + msg); }
  else console.log("  ✓ " + msg);
}
const txt = id => (window.document.getElementById(id).textContent || "").trim();
const htmlOf = id => window.document.getElementById(id).innerHTML || "";

(async () => {
  await sleep(1500); // initial poll + render

  console.log("== header ==");
  check(txt("c_mode") === "PAPER", "mode chip PAPER");
  check(/Market/.test(txt("c_market")), "market chip rendered");
  check(txt("c_engine") === "Engine OK", "engine chip OK, got: " + txt("c_engine"));
  check(/build/.test(txt("topclock")), "build version in topbar");

  console.log("== overview ==");
  check(/Today's P/.test(txt("ov_cards")), "overview P&L card");
  check(/OPEN|PRE-OPEN|CLOSED/.test(txt("ov_market")), "market status text");
  check(/Next candle close/.test(txt("ov_engine")), "engine strip");
  check(/Daily loss/.test(txt("ov_risk")), "risk strip");

  console.log("== scanner ==");
  window.showPage("scanner");
  await sleep(150);
  const scanRows = window.document.querySelectorAll("#scanner table tbody tr, #scanner table tr");
  check(scanRows.length >= 20, "scanner shows rows: " + scanRows.length);
  check(/AVWAP since/.test(htmlOf("scanner")), "AVWAP since column present");
  check(/Vol/.test(htmlOf("scanner")) && /OI/.test(htmlOf("scanner")), "Vol + OI columns present");
  const ltpCells = [...window.document.querySelectorAll("#scanner table tr.clickable")].slice(0, 8).map(tr => tr.children[6]);
  check(ltpCells.some(td => /\d/.test(td.textContent)), "LTP column has quoted values (not all \u201c\u2013\u201d)");
  // quick views
  window.document.querySelector('#scan_q button[data-q="OPEN"]').click();
  await sleep(100);
  const openRows = window.document.querySelectorAll("#scanner table tr").length - 1;
  check(openRows >= 1, "OPEN quick view filters to open positions: " + openRows);
  window.document.querySelector('#scan_q button[data-q="PARTIAL"]').click();
  await sleep(100);
  check(/PARTIAL|REBUILD/.test(htmlOf("scanner")), "PARTIAL view + warning line");
  window.document.querySelector('#scan_q button[data-q="ALL"]').click();
  await sleep(100);
  // row click -> contract
  const firstRow = window.document.querySelector("#scanner table tr.clickable");
  check(!!firstRow, "scanner rows are clickable");
  if (firstRow) { firstRow.click(); await sleep(700); }

  console.log("== contract ==");
  check(window.document.getElementById("page_contract").classList.contains("shown"), "contract page shown after row click");
  check(/MOCKA|expiry|d/.test(txt("ct_head")), "contract header rendered: " + txt("ct_head").slice(0, 60));
  check(/AVWAP/.test(txt("ct_cards")), "contract stat cards");
  check(/candles \+ AVWAP line/.test(txt("ct_chartnote")), "chart note rendered (canvas skipped in headless)");
  check(/SCHEDULE AVWAP REBUILD/.test(htmlOf("ct_position")) || /no open position/.test(txt("ct_position")), "contract position area");

  console.log("== positions ==");
  window.showPage("positions");
  await sleep(150);
  check(/Total positions/.test(txt("pos_cards")), "portfolio cards");
  check(/CLOSE/.test(htmlOf("pos_cards_grid")), "position cards with CLOSE button");
  // runtime-compile the CLOSE onclick (prompt->null aborts; must not throw / emit JS error)
  const closeBtn = window.document.querySelector("#pos_cards_grid button.danger");
  if (closeBtn) { closeBtn.click(); await sleep(150); }
  check(!errors.some(e => /Unexpected token|is not defined/.test(String(e))), "CLOSE onclick handler compiles (no JS syntax/Ref error)");

  console.log("== signals ==");
  window.showPage("signals");
  await sleep(150);
  check(/TRUE CROSS BELOW AVWAP|CLOSE ABOVE AVWAP/.test(txt("sig_cards")), "signal rule explainability");
  check(/SIGNAL /.test(txt("sig_cards")) && /→ ORDER/.test(txt("sig_cards")), "signal chain rendered");

  console.log("== orders ==");
  window.showPage("orders");
  await sleep(150);
  check(/HORD1/.test(txt("orders_table")), "order row rendered");
  check(/FILLED/.test(txt("orders_table")), "order status badge");

  console.log("== data health ==");
  window.showPage("datahealth");
  await sleep(150);
  check(/Quote feed \(LTP\)/.test(txt("dh_feeds")), "feed cards");
  check(/Dhan REST API/.test(txt("dh_rest")) || /no live REST/.test(txt("dh_rest")), "REST section (mock: none)");
  check(/PARTIAL history/.test(txt("dh_av_cards")), "AVWAP integrity cards");
  check(/REBUILD \(next start\)/.test(htmlOf("dh_av_partial")), "partial contract with REBUILD button");

  console.log("== system ==");
  window.showPage("system");
  await sleep(150);
  check(/Engine/.test(txt("sys_comp")) && /heartbeat/i.test(txt("sys_beats")), "components + heartbeats");
  check(/psutil|CPU/.test(txt("sys_proc")), "process section");

  console.log("== alerts ==");
  window.showPage("alerts");
  await sleep(300);
  check(/no active alerts/.test(txt("al_now")) || htmlOf("al_now").includes('class="al'), "current alerts rendered");
  check(/Incident history/.test(window.document.body.textContent), "incident history header");

  console.log("== journal ==");
  window.showPage("journal");
  await sleep(400);
  const jRows = window.document.querySelectorAll("#journal_table table tr").length;
  check(jRows >= 2, "journal rows rendered: " + jRows);
  // filter + export wiring
  check(typeof window.exportJournal === "function", "exportJournal function exists");

  console.log("== recovery ==");
  window.showPage("recovery");
  await sleep(150);
  check(/STATUS:/.test(txt("rec_status")), "recovery status: " + txt("rec_status").slice(0, 50));
  check(window.document.querySelectorAll("#rec_timeline table tr").length > 3, "startup timeline rows");

  console.log("== settings ==");
  window.showPage("settings");
  await sleep(150);
  check(/READ-ONLY/.test(txt("page_settings")), "settings read-only notice");
  check(/Strategy/.test(txt("set_tables")) && /Risk/.test(txt("set_tables")), "settings sections");

  console.log("");
  if (errors.length) {
    console.log("ERRORS (" + errors.length + "):");
    errors.forEach(e => console.log("  " + String(e).slice(0, 300)));
    process.exit(1);
  } else {
    console.log("ALL PAGE RENDERS CLEAN - no JS errors");
    process.exit(0);
  }
})().catch(e => { console.log("HARNESS CRASH: " + e.stack); process.exit(1); });
