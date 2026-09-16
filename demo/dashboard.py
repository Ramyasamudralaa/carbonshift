"""A live results view for CarbonShift, for showing to people.

Builds one self-contained HTML file from the run records and opens it. No
server, no internet, no dependencies -- so it still works on conference wifi,
which is to say on no wifi at all.

    python demo/dashboard.py              # build and open it
    python demo/dashboard.py --no-open    # just build it
    python demo/dashboard.py --watch      # rebuild every 30s as new runs land

Shows the carbon curve behind the most recent decision, a live countdown to a
job that has not fired yet, and the running total saved across every real run.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

RUN_DIR = Path(os.getenv("CARBONSHIFT_RUN_DIR", REPO / "runs"))
OUTPUT = REPO / "demo" / "dashboard.html"


def load_runs() -> list[dict[str, Any]]:
    runs = []
    for path in glob.glob(str(RUN_DIR / "*.json")):
        if Path(path).name == "latest.json":
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                runs.append(json.load(handle))
        except (json.JSONDecodeError, OSError):
            continue
    runs.sort(key=lambda r: r.get("chosen", {}).get("timestamp", ""))
    return runs


def parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# --- the carbon curve, drawn as inline SVG ---------------------------------


def curve_svg(record: dict[str, Any], width: int = 980, height: int = 300) -> str:
    forecast = record.get("forecast") or []
    if len(forecast) < 2:
        return '<p class="muted">No forecast data in this run record.</p>'

    pad_l, pad_r, pad_t, pad_b = 52, 20, 24, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    times = [parse(p["timestamp"]) for p in forecast]
    values = [float(p["carbon_intensity"]) for p in forecast]
    t0, t1 = times[0].timestamp(), times[-1].timestamp()
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    top = hi + span * 0.18
    bottom = max(0.0, lo - span * 0.12)
    vspan = (top - bottom) or 1.0

    def x_of(dt: datetime) -> float:
        frac = (dt.timestamp() - t0) / ((t1 - t0) or 1)
        return pad_l + frac * plot_w

    def y_of(value: float) -> float:
        frac = (value - bottom) / vspan
        return pad_t + plot_h - frac * plot_h

    points = " ".join(f"{x_of(t):.1f},{y_of(v):.1f}" for t, v in zip(times, values))
    area = (f"{pad_l},{pad_t + plot_h} " + points +
            f" {pad_l + plot_w},{pad_t + plot_h}")

    parts = [f'<svg viewBox="0 0 {width} {height}" class="curve" '
             f'preserveAspectRatio="xMidYMid meet" role="img" '
             f'aria-label="Grid carbon intensity over the forecast window">']

    # horizontal gridlines
    for i in range(4):
        value = bottom + vspan * i / 3
        y = y_of(value)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" '
                     f'y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="axis" '
                     f'text-anchor="end">{value:.0f}</text>')

    parts.append(f'<polygon points="{area}" class="area"/>')
    parts.append(f'<polyline points="{points}" class="line"/>')

    # time ticks
    step = max(1, len(times) // 6)
    for i in range(0, len(times), step):
        x = x_of(times[i])
        parts.append(f'<text x="{x:.1f}" y="{height - 10}" class="axis" '
                     f'text-anchor="middle">{times[i].strftime("%H:%M")}</text>')

    deadline_raw = record.get("deadline")
    if deadline_raw:
        deadline = parse(deadline_raw)
        if times[0] <= deadline <= times[-1]:
            x = x_of(deadline)
            parts.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" '
                         f'y2="{pad_t + plot_h}" class="deadline"/>')
            parts.append(f'<text x="{x + 6:.1f}" y="{pad_t + 12}" '
                         f'class="tag deadline-tag">deadline</text>')

    baseline = record.get("baseline", {})
    chosen = record.get("chosen", {})

    if baseline.get("timestamp"):
        bx, by = x_of(parse(baseline["timestamp"])), y_of(
            float(baseline["carbon_intensity"]))
        parts.append(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="7" class="dot-now"/>')
        parts.append(f'<text x="{bx + 12:.1f}" y="{by - 10:.1f}" class="tag now-tag">'
                     f'run now &#183; {baseline["carbon_intensity"]:.0f}</text>')

    if chosen.get("timestamp"):
        cx, cy = x_of(parse(chosen["timestamp"])), y_of(
            float(chosen["carbon_intensity"]))
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="11" class="dot-halo"/>')
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" class="dot-chosen"/>')
        anchor = "end" if cx > width * 0.7 else "start"
        offset = -12 if anchor == "end" else 12
        parts.append(f'<text x="{cx + offset:.1f}" y="{cy + 26:.1f}" '
                     f'class="tag chosen-tag" text-anchor="{anchor}">'
                     f'CarbonShift &#183; {chosen["carbon_intensity"]:.0f}</text>')

    parts.append("</svg>")
    return "".join(parts)


# --- page ------------------------------------------------------------------


def build_html(runs: list[dict[str, Any]]) -> str:
    real = [r for r in runs if r.get("scheduled")]
    total_saved = sum(float(r.get("co2_saved_g", 0)) for r in real)
    total_baseline = sum(float(r.get("baseline", {}).get("emissions_g", 0)) for r in real)
    percent = (100 * total_saved / total_baseline) if total_baseline else 0.0

    latest = runs[-1] if runs else None
    now = datetime.now(timezone.utc)

    upcoming = [
        r for r in real
        if r.get("chosen", {}).get("timestamp") and parse(r["chosen"]["timestamp"]) > now
    ]
    upcoming.sort(key=lambda r: parse(r["chosen"]["timestamp"]))
    next_run = upcoming[0] if upcoming else None

    if next_run:
        target = parse(next_run["chosen"]["timestamp"])
        countdown = (
            f'<div class="label">Next job runs in</div>'
            f'<div class="countdown" data-target="{target.isoformat()}">--:--:--</div>'
            f'<div class="sub">{html.escape(next_run.get("payload", ""))} '
            f'&#183; {target.strftime("%H:%M UTC on %d %b")} '
            f'&#183; {next_run["chosen"]["carbon_intensity"]:.0f} gCO2/kWh</div>'
        )
        state = "scheduled"
    elif real:
        last = real[-1]
        when = parse(last["chosen"]["timestamp"])
        countdown = (
            f'<div class="label">Last job ran</div>'
            f'<div class="countdown done">DONE</div>'
            f'<div class="sub">{html.escape(last.get("payload", ""))} '
            f'&#183; {when.strftime("%H:%M UTC on %d %b")} '
            f'&#183; saved {last["co2_saved_g"]:.2f} g</div>'
        )
        state = "idle"
    else:
        countdown = ('<div class="label">Nothing scheduled</div>'
                     '<div class="countdown done">&#8212;</div>'
                     '<div class="sub">Schedule a job to see it here</div>')
        state = "empty"

    rows = []
    for record in reversed(runs[-9:]):
        chosen = record.get("chosen", {})
        saved = float(record.get("co2_saved_g", 0))
        is_real = bool(record.get("scheduled"))
        rows.append(
            f'<tr class="{"real" if is_real else "dry"}">'
            f'<td>{parse(chosen["timestamp"]).strftime("%d %b %H:%M") if chosen.get("timestamp") else "-"}</td>'
            f'<td class="name">{html.escape(str(record.get("payload", "?"))[:26])}</td>'
            f'<td>{"scheduled" if is_real else "preview"}</td>'
            f'<td class="num">{record.get("baseline", {}).get("carbon_intensity", 0):.0f}</td>'
            f'<td class="num">{chosen.get("carbon_intensity", 0):.0f}</td>'
            f'<td class="num {"pos" if saved > 0 else "neg"}">{saved:+.2f} g</td>'
            f"</tr>"
        )
    table_rows = "".join(rows) or '<tr><td colspan="6" class="muted">No runs yet</td></tr>'

    if latest:
        headline_pct = float(latest.get("co2_saved_percent", 0))
        headline_sub = (
            f'{html.escape(str(latest.get("payload", "")))} &#183; '
            f'{latest.get("zone", "?")} grid &#183; '
            f'{float(latest.get("delay_hours", 0)):.0f} h delay'
        )
        chart = curve_svg(latest)
    else:
        headline_pct = 0.0
        headline_sub = "no runs recorded yet"
        chart = '<p class="muted">Run a job to see the carbon curve.</p>'

    generated = now.strftime("%H:%M:%S UTC on %d %B %Y")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CarbonShift</title>
<style>
  :root {{
    --bg:#0d1512; --panel:#152220; --edge:#22352f; --ink:#e8f2ec;
    --muted:#7f9a8e; --green:#4ade80; --amber:#fbbf24; --red:#f87171;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
    padding:26px 30px 34px;
  }}
  header {{ display:flex; align-items:baseline; gap:14px; margin-bottom:22px; }}
  h1 {{ font-size:23px; margin:0; letter-spacing:-.3px; }}
  .tagline {{ color:var(--muted); font-size:13.5px; }}
  .dot {{
    width:9px; height:9px; border-radius:50%; display:inline-block;
    margin-right:7px; vertical-align:middle;
    background:{'var(--green)' if state == 'scheduled' else 'var(--muted)'};
    {'animation:pulse 2s infinite;' if state == 'scheduled' else ''}
  }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.35}} }}

  .top {{ display:grid; grid-template-columns:1.15fr 1fr 1fr; gap:16px; margin-bottom:16px; }}
  .card {{ background:var(--panel); border:1px solid var(--edge); border-radius:13px; padding:20px 22px; }}
  .label {{ color:var(--muted); font-size:11.5px; text-transform:uppercase;
            letter-spacing:.09em; margin-bottom:9px; }}
  .big {{ font-size:46px; font-weight:700; letter-spacing:-1.5px; line-height:1; }}
  .big.green {{ color:var(--green); }}
  .sub {{ color:var(--muted); font-size:12.5px; margin-top:9px; }}
  .countdown {{ font-size:40px; font-weight:700; letter-spacing:-1px;
                font-variant-numeric:tabular-nums; color:var(--amber); line-height:1; }}
  .countdown.done {{ color:var(--muted); font-size:34px; }}

  .panel-title {{ font-size:12.5px; color:var(--muted); text-transform:uppercase;
                  letter-spacing:.09em; margin:0 0 14px; }}
  .curve {{ width:100%; height:auto; display:block; }}
  .grid {{ stroke:#223a32; stroke-width:1; }}
  .axis {{ fill:var(--muted); font-size:10.5px; }}
  .area {{ fill:#4ade80; opacity:.10; }}
  .line {{ fill:none; stroke:var(--green); stroke-width:2.4;
           stroke-linejoin:round; stroke-linecap:round; }}
  .deadline {{ stroke:var(--muted); stroke-width:1.4; stroke-dasharray:5 4; }}
  .tag {{ font-size:11.5px; font-weight:600; }}
  .deadline-tag {{ fill:var(--muted); }}
  .now-tag {{ fill:var(--red); }}
  .chosen-tag {{ fill:var(--green); }}
  .dot-now {{ fill:var(--red); stroke:var(--panel); stroke-width:2.5; }}
  .dot-chosen {{ fill:var(--green); stroke:var(--panel); stroke-width:2.5; }}
  .dot-halo {{ fill:var(--green); opacity:.25; }}

  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:var(--muted); font-weight:600; font-size:11px;
        text-transform:uppercase; letter-spacing:.07em;
        padding:0 10px 9px; border-bottom:1px solid var(--edge); }}
  td {{ padding:9px 10px; border-bottom:1px solid #1a2a25; }}
  tr.dry td {{ color:var(--muted); }}
  .name {{ font-weight:600; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .pos {{ color:var(--green); }} .neg {{ color:var(--red); }}
  .muted {{ color:var(--muted); }}
  footer {{ margin-top:20px; color:var(--muted); font-size:11.5px;
            display:flex; justify-content:space-between; }}
  @media (max-width:900px) {{ .top {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>

<header>
  <h1><span class="dot"></span>CarbonShift</h1>
  <span class="tagline">Cloud jobs, run when the grid is cleanest</span>
</header>

<div class="top">
  <div class="card">
    <div class="label">Latest decision</div>
    <div class="big green">{headline_pct:.1f}%</div>
    <div class="sub">less CO2 &#183; {headline_sub}</div>
  </div>
  <div class="card">{countdown}</div>
  <div class="card">
    <div class="label">Total saved so far</div>
    <div class="big">{total_saved:.2f} g</div>
    <div class="sub">across {len(real)} real run{'s' if len(real) != 1 else ''}
      &#183; {percent:.1f}% of {total_baseline:.2f} g</div>
  </div>
</div>

<div class="card" style="margin-bottom:16px">
  <p class="panel-title">Grid carbon intensity &#183; the window behind the latest decision</p>
  {chart}
</div>

<div class="card">
  <p class="panel-title">Recent runs</p>
  <table>
    <thead><tr>
      <th>When it ran</th><th>Job</th><th>Type</th>
      <th class="num">Before</th><th class="num">After</th><th class="num">Saved</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>

<footer>
  <span>Previews are excluded from the total &#8212; they never executed.</span>
  <span>Generated {generated}</span>
</footer>

<script>
  function tick() {{
    var el = document.querySelector('.countdown[data-target]');
    if (!el) return;
    var diff = new Date(el.dataset.target) - new Date();
    if (diff <= 0) {{
      el.textContent = 'RUNNING';
      el.classList.add('done');
      return;
    }}
    var s = Math.floor(diff / 1000);
    var h = String(Math.floor(s / 3600)).padStart(2, '0');
    var m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    var sec = String(s % 60).padStart(2, '0');
    el.textContent = h + ':' + m + ':' + sec;
  }}
  tick();
  setInterval(tick, 1000);
</script>

</body>
</html>"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift-dashboard",
        description="Build a live results view from the run records.",
    )
    parser.add_argument("--no-open", action="store_true",
                        help="Build the file without opening a browser.")
    parser.add_argument("--watch", action="store_true",
                        help="Rebuild every 30 seconds, for leaving on a screen.")
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    def build_once():
        runs = load_runs()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(build_html(runs), encoding="utf-8")
        return len(runs)

    count = build_once()
    print(f"  Built {args.out} from {count} run record{'s' if count != 1 else ''}")

    if not args.no_open:
        webbrowser.open(args.out.resolve().as_uri())
        print("  Opened in your browser.")

    if args.watch:
        print("  Watching for new runs - Ctrl+C to stop.")
        try:
            while True:
                time.sleep(30)
                count = build_once()
                print(f"  Rebuilt at {datetime.now().strftime('%H:%M:%S')} "
                      f"({count} runs) - refresh the page to see it")
        except KeyboardInterrupt:
            print("\n  Stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
