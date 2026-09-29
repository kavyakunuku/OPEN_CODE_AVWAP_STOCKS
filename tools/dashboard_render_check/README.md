# Dashboard render check (dev tool)

Loads the exact HTML the dashboard serves, feeds it the **real payload
shapes** (generated from a mock `TraderApp`), visits all 12 pages in a
headless DOM and fails on any JavaScript error or missing expected content.

Requires Node.js + jsdom (`npm install jsdom`). This is a development check
only — the running system itself has no Node.js dependency.

Usage (repo root or anywhere):

    python tools/dashboard_render_check/gen_payload.py OUT_DIR
    node tools/dashboard_render_check/render_check.js OUT_DIR

`OUT_DIR` defaults to the system temp dir + `v2h` (wiped and rebuilt each run).
