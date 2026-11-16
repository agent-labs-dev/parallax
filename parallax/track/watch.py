"""`parallax watch`: a local page showing live runs, their progress and ETA, from the live feed (`track.live`).

The server holds the Langfuse keys and the page only ever talks to it, so the keys never reach the browser.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from parallax.track.live import RUN_EVENT, LiveSource, progress, runs

logger = logging.getLogger(__name__)

RUN_WINDOW = timedelta(days=2)  # how far back the run list looks
EVENT_SLACK = timedelta(minutes=5)  # events a few seconds before their run's start event still belong to it


def _progress(source: LiveSource, run_id: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    starts = list(source.events(since=now - RUN_WINDOW, session=run_id, name=RUN_EVENT))
    if not starts:
        return {"run_id": run_id, "state": "unknown"}
    first = min(e.at for e in starts) - EVENT_SLACK
    return {"run_id": run_id, **progress(source.events(since=first, session=run_id), now=now)}


def handler(source: LiveSource) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - the http.server hook
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif url.path == "/api/runs":
                    since = datetime.now(UTC) - RUN_WINDOW
                    self._send(200, json.dumps(runs(source.events(since=since, name=RUN_EVENT))).encode(),
                               "application/json")  # fmt: skip
                elif url.path == "/api/progress" and (run := parse_qs(url.query).get("run", [""])[0]):
                    self._send(200, json.dumps(_progress(source, run)).encode(), "application/json")
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as error:  # noqa: BLE001 - shown on the page, the server keeps serving
                logger.warning("watch: %s", error)
                self._send(502, json.dumps({"error": str(error)}).encode(), "application/json")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - quiet
            return

    return Handler


def serve(source: LiveSource, *, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), handler(source))
    print(f"watching live runs at http://{host}:{server.server_port}/ (Ctrl-C stops)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parallax live</title>
<style>
  :root { --bg:#fff; --fg:#1b1d21; --muted:#6b7079; --line:#e4e6ea; --card:#f7f8fa;
          --pass:#1f8a4c; --fail:#c2372d; --invalid:#b7791f; --na:#8a8f98; --bar:#2f6fde; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14161a; --fg:#e8eaed; --muted:#9aa0a8; --line:#2a2e35; --card:#1c1f24;
            --pass:#3fb873; --fail:#ef6a5f; --invalid:#e0a43c; --na:#80868f; --bar:#5b8ff0; } }
  * { box-sizing:border-box }
  body { margin:0; font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--fg) }
  header { display:flex; gap:12px; align-items:center; padding:14px 22px; border-bottom:1px solid var(--line) }
  header h1 { font-size:16px; margin:0; font-weight:600 }
  select { font:inherit; padding:5px 8px; border-radius:6px; border:1px solid var(--line); background:var(--card); color:var(--fg); max-width:60vw }
  #updated { margin-left:auto; color:var(--muted); font-size:12px }
  main { padding:18px 22px; max-width:1200px; margin:0 auto }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:16px }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px }
  .tile .k { color:var(--muted); font-size:12px } .tile .v { font-size:22px; font-weight:600; margin-top:2px; font-variant-numeric:tabular-nums }
  .tile .s { color:var(--muted); font-size:12px; margin-top:2px }
  .progress { height:12px; border-radius:6px; background:var(--line); overflow:hidden; display:flex; margin:4px 0 18px }
  .progress span { height:100% }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:22px 0 8px }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap }
  th { color:var(--muted); font-weight:500; font-size:12px }
  td.reason { white-space:normal; color:var(--muted); max-width:420px }
  .v-pass { color:var(--pass) } .v-fail { color:var(--fail) } .v-invalid { color:var(--invalid) } .v-na { color:var(--na) }
  .empty { color:var(--muted); padding:30px 0 }
  .err { color:var(--fail) }
  .cols { display:grid; grid-template-columns:1fr 1fr; gap:24px } @media (max-width:900px){ .cols { grid-template-columns:1fr } }
  .panel { min-height:240px }
</style>
</head>
<body>
<header><h1>Parallax live</h1><select id="run"></select><span id="updated"></span></header>
<main id="main"><p class="empty">Loading runs…</p></main>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = v => v == null ? "–" : (v * 100).toFixed(1) + "%";
const usd = v => v == null ? "–" : "$" + (v < 0.1 ? v.toFixed(4) : v.toFixed(2));
const dur = s => { if (s == null) return "–"; s = Math.round(s); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${x}s` : `${x}s`; };
const clock = iso => iso ? new Date(iso).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}) : "–";
const COLORS = {pass:"var(--pass)", fail:"var(--fail)", invalid:"var(--invalid)", na:"var(--na)"};
let current = new URLSearchParams(location.search).get("run");

async function loadRuns() {
  const r = await fetch("/api/runs"); const list = await r.json();
  if (!r.ok) throw new Error(list.error);
  const sel = $("#run");
  sel.innerHTML = list.map(x => `<option value="${esc(x.run_id)}">${esc(x.run_id)} · ${esc(x.labels.join(", "))} · ${clock(x.started_at)}</option>`).join("");
  if (!list.length) { $("#main").innerHTML = '<p class="empty">No live runs in the last two days.</p>'; return; }
  if (!current || !list.some(x => x.run_id === current)) current = list[0].run_id;
  sel.value = current;
}

function render(p) {
  const v = p.verdicts || {}, total = p.total || 0, done = p.done || 0;
  const bar = total ? ["pass","fail","invalid","na"].map(k => `<span style="width:${(v[k]||0)/total*100}%;background:${COLORS[k]}" title="${k} ${v[k]||0}"></span>`).join("") : "";
  const eta = p.state === "finished" ? "done" : p.eta_s == null ? "–" : dur(p.eta_s);
  const arms = Object.entries(p.arms || {}).map(([name, a]) => `<tr><td>${esc(name)}</td><td>${a.done}</td>
    <td class="v-pass">${a.verdicts.pass||0}</td><td class="v-fail">${a.verdicts.fail||0}</td><td class="v-invalid">${a.verdicts.invalid||0}</td>
    <td>${pct(a.pass_rate)}</td><td>${dur(a.median_pass_s)}</td><td>${usd(a.mean_cost_usd)}</td></tr>`).join("");
  const running = (p.running || []).map(t => `<tr><td>${esc(t.task)}</td><td>${esc(t.label)}</td><td>#${t.repeat}${t.attempt ? " retry " + t.attempt : ""}</td><td>${dur(t.for_s)}</td></tr>`).join("");
  const recent = (p.recent || []).map(t => `<tr><td>${clock(t.at)}</td><td>${esc(t.task)}</td><td>${esc(t.label)}</td><td>#${t.repeat}</td>
    <td class="v-${esc(t.verdict)}">${esc(t.verdict)}</td><td>${dur(t.wall_s)}</td><td>${usd(t.cost_usd)}</td><td class="reason">${esc(t.reason)}</td></tr>`).join("");
  $("#main").innerHTML = `
    <div class="tiles">
      <div class="tile"><div class="k">Progress</div><div class="v">${done}${total ? " / " + total : ""}</div><div class="s">${total ? pct(done/total) : ""} · ${esc(p.state)}</div></div>
      <div class="tile"><div class="k">ETA</div><div class="v">${eta}</div><div class="s">${p.eta_at ? "around " + clock(p.eta_at) : ""}</div></div>
      <div class="tile"><div class="k">Elapsed</div><div class="v">${dur(p.elapsed_s)}</div><div class="s">started ${clock(p.started_at)}</div></div>
      <div class="tile"><div class="k">Pass rate</div><div class="v">${pct(p.pass_rate)}</div><div class="s">${v.pass||0} pass · ${v.fail||0} fail · ${v.invalid||0} invalid</div></div>
      <div class="tile"><div class="k">Cost so far</div><div class="v">${usd(p.cost_usd)}</div><div class="s">${p.concurrency ? p.concurrency + " at a time" : ""}</div></div>
    </div>
    <div class="progress">${bar}</div>
    <h2>By arm</h2>
    <table><tr><th>Arm</th><th>Done</th><th>Pass</th><th>Fail</th><th>Invalid</th><th>Pass rate</th><th>Median pass time</th><th>Mean cost</th></tr>${arms}</table>
    <div class="cols">
      <div class="panel"><h2>Running (${(p.running||[]).length})</h2>${running ? `<table><tr><th>Task</th><th>Arm</th><th>Run</th><th>For</th></tr>${running}</table>` : '<p class="empty">Nothing running.</p>'}</div>
      <div class="panel"><h2>Latest results</h2>${recent ? `<table><tr><th>At</th><th>Task</th><th>Arm</th><th>Run</th><th>Verdict</th><th>Time</th><th>Cost</th><th>Why</th></tr>${recent}</table>` : '<p class="empty">No trial has finished yet.</p>'}</div>
    </div>`;
}

async function tick() {
  try {
    if (!current) await loadRuns();
    if (current) {
      const r = await fetch("/api/progress?run=" + encodeURIComponent(current)); const p = await r.json();
      if (!r.ok) throw new Error(p.error);
      render(p);
    }
    $("#updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) { $("#updated").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
$("#run").addEventListener("change", e => { current = e.target.value; history.replaceState(null, "", "?run=" + encodeURIComponent(current)); tick(); });
loadRuns().then(tick).catch(e => $("#main").innerHTML = `<p class="err">${esc(e.message)}</p>`);
setInterval(tick, 15000);
setInterval(() => loadRuns().catch(() => {}), 60000);
</script>
</body>
</html>
"""
