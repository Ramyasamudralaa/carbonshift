"""Carbon reduction reports, for the people who have to file them.

A running total on a terminal is not evidence. An organisation reporting under
a carbon disclosure regime needs a document: the period, the jobs, the figures,
the method used to produce them, and an honest statement of what the figures
are and are not.

This builds that from the run records, verified against CloudWatch where
possible.

    python -m src.report                      # the current month
    python -m src.report --month 2026-09
    python -m src.report --year 2026
    python -m src.report --all
    python -m src.report --verify             # confirm each run against AWS
    python -m src.report --csv                # also write the row-level CSV

Figures are estimates derived from forecast carbon intensity, not metered
emissions. The report says so on its face, because a compliance document that
overstates its own certainty is worse than none.
"""

from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import os
import sys
import webbrowser
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

REPO = Path(__file__).resolve().parents[1]
RUN_DIR = Path(os.getenv("CARBONSHIFT_RUN_DIR", REPO / "runs"))
REPORT_DIR = Path(os.getenv("CARBONSHIFT_REPORT_DIR", REPO / "reports"))
ORGANISATION = os.getenv("CARBONSHIFT_ORGANISATION", "")

KWH_PER_VCPU_HOUR = float(os.getenv("KWH_PER_VCPU_HOUR", "0.0074"))

BOLD, DIM, GREEN, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    try:
        import colorama

        colorama.just_fix_windows_console()
    except Exception:
        BOLD = DIM = GREEN = RESET = ""


# --- data ------------------------------------------------------------------


def parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_runs(directory: Path = RUN_DIR) -> list[dict[str, Any]]:
    runs = []
    for path in glob.glob(str(directory / "*.json")):
        if Path(path).name == "latest.json":
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                runs.append(json.load(handle))
        except (json.JSONDecodeError, OSError):
            continue
    runs.sort(key=lambda r: r.get("chosen", {}).get("timestamp", ""))
    return runs


def select_period(runs, month: str | None, year: str | None, everything: bool):
    """Keep only executed runs inside the requested period.

    Previews are always dropped. They were never scheduled, so counting them
    would overstate the saving -- which is the one thing a compliance report
    must not do.
    """
    real = [r for r in runs if r.get("scheduled") and r.get("chosen", {}).get("timestamp")]

    if everything:
        return real, "All recorded activity"
    if year:
        return ([r for r in real if parse(r["chosen"]["timestamp"]).strftime("%Y") == year],
                f"Calendar year {year}")

    target = month or datetime.now(timezone.utc).strftime("%Y-%m")
    selected = [r for r in real
                if parse(r["chosen"]["timestamp"]).strftime("%Y-%m") == target]
    label = datetime.strptime(target, "%Y-%m").strftime("%B %Y")
    return selected, label


def summarise(runs) -> dict[str, Any]:
    baseline_g = sum(float(r.get("baseline", {}).get("emissions_g", 0)) for r in runs)
    actual_g = sum(float(r.get("chosen", {}).get("emissions_g", 0)) for r in runs)
    saved_g = baseline_g - actual_g
    energy = sum(float(r.get("energy_kwh", 0)) for r in runs)
    delay = sum(float(r.get("delay_hours", 0)) for r in runs)

    zones = sorted({r.get("zone", "?") for r in runs})
    regions = sorted({r.get("region", "?") for r in runs})

    return {
        "jobs": len(runs),
        "baseline_g": baseline_g,
        "actual_g": actual_g,
        "saved_g": saved_g,
        "saved_percent": (100 * saved_g / baseline_g) if baseline_g else 0.0,
        "energy_kwh": energy,
        "mean_delay_hours": (delay / len(runs)) if runs else 0.0,
        "zones": zones,
        "regions": regions,
    }


def verify(runs) -> dict[str, str]:
    """Which runs produced a completion log. Absence is reported, not hidden."""
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        return {"_error": "boto3 not installed"}

    log_group = os.getenv("CARBONSHIFT_LOG_GROUP", "/ecs/carbonshift-worker")
    region = os.getenv("AWS_REGION", "eu-central-1")
    found: dict[str, str] = {}
    try:
        client = boto3.client("logs", region_name=region)
        pages = client.get_paginator("filter_log_events").paginate(
            logGroupName=log_group, filterPattern="CARBONSHIFT_JOB_COMPLETE")
        for page in pages:
            for event in page.get("events", []):
                marker = "CARBONSHIFT_JOB_COMPLETE "
                if marker not in event.get("message", ""):
                    continue
                try:
                    payload = json.loads(event["message"].split(marker, 1)[1])
                except (ValueError, IndexError):
                    continue
                found[f"{payload.get('payload')}|{payload.get('scheduled_for')}"] = (
                    payload.get("started_at", ""))
    except NoCredentialsError:
        return {"_error": "no AWS credentials"}
    except (ClientError, BotoCoreError) as exc:
        return {"_error": str(exc)[:80]}
    return found


def verification_key(record) -> str:
    return f"{record.get('payload')}|{record.get('chosen', {}).get('timestamp')}"


def split_by_verification(runs, verified):
    """Separate what can be evidenced from what cannot.

    Returns (counted, excluded). When verification did not run, nothing can be
    excluded on that basis, so everything is counted and the report says the
    figures are unchecked.
    """
    if verified is None or verified.get("_error"):
        return list(runs), []
    counted, excluded = [], []
    for record in runs:
        (counted if verification_key(record) in verified else excluded).append(record)
    return counted, excluded


# --- outputs ---------------------------------------------------------------


METHODOLOGY = [
    ("Scope",
     "Operational emissions from the electricity consumed by compute workloads "
     "whose execution time was deferred by CarbonShift. Equivalent to a Scope 2 "
     "(market-based) reduction attributable to load shifting."),
    ("Data source",
     "Hourly grid carbon-intensity forecasts from Electricity Maps, for the grid "
     "zone serving the cloud region in which each workload ran."),
    ("Baseline",
     "The grid carbon intensity of the hour in which the job would have run had "
     "it been executed immediately on submission. This is a counterfactual, not "
     "a measurement."),
    ("Energy model",
     f"energy (kWh) = vCPU x runtime (hours) x {KWH_PER_VCPU_HOUR} kWh per "
     "vCPU-hour. The coefficient is a published approximation for cloud compute "
     "at typical utilisation, not a metered figure for the specific hardware."),
    ("Emissions",
     "emissions (gCO2) = energy (kWh) x carbon intensity (gCO2/kWh). The saving "
     "is the difference between the baseline hour and the hour actually used."),
    ("Verification",
     "Each job is cross-checked against AWS CloudWatch Logs. A run without a "
     "completion log is reported as unverified and excluded from the totals."),
]

LIMITATIONS = [
    "Figures are estimates derived from forecast carbon intensity, not metered "
    "emissions. Actual grid intensity at the moment of execution may have differed "
    "from the forecast.",
    "The energy coefficient is an approximation. It does not account for actual CPU "
    "utilisation, instance family, or data-centre power usage effectiveness.",
    "Only operational electricity is considered. Embodied emissions of the hardware "
    "are out of scope.",
    "The baseline is a counterfactual: the job was never actually run at the earlier "
    "hour, so the avoided emissions were modelled rather than observed.",
]


def write_csv(runs, verified, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "executed_at_utc", "job", "cloud_region", "grid_zone",
            "baseline_gco2_per_kwh", "actual_gco2_per_kwh", "energy_kwh",
            "baseline_emissions_g", "actual_emissions_g", "emissions_avoided_g",
            "reduction_percent", "delay_hours", "verified",
        ])
        for record in runs:
            chosen = record.get("chosen", {})
            baseline = record.get("baseline", {})
            status = ("not checked" if verified is None
                      else "yes" if verification_key(record) in verified
                      else "no")
            writer.writerow([
                chosen.get("timestamp", ""),
                record.get("payload", ""),
                record.get("region", ""),
                record.get("zone", ""),
                f"{baseline.get('carbon_intensity', 0):.1f}",
                f"{chosen.get('carbon_intensity', 0):.1f}",
                f"{record.get('energy_kwh', 0):.6f}",
                f"{baseline.get('emissions_g', 0):.4f}",
                f"{chosen.get('emissions_g', 0):.4f}",
                f"{record.get('co2_saved_g', 0):.4f}",
                f"{record.get('co2_saved_percent', 0):.2f}",
                f"{record.get('delay_hours', 0):.2f}",
                status,
            ])
    return path


def build_html(runs, verified, label: str, totals: dict[str, Any]) -> str:
    generated = datetime.now(timezone.utc)
    org = ORGANISATION or "[organisation not set]"

    rows = []
    for record in runs:
        chosen = record.get("chosen", {})
        baseline = record.get("baseline", {})
        if verified is None:
            status, css = "not checked", "unknown"
        elif verified.get("_error"):
            status, css = "not checked", "unknown"
        elif verification_key(record) in verified:
            status, css = "verified", "yes"
        else:
            status, css = "unverified", "no"
        rows.append(
            "<tr>"
            f"<td>{parse(chosen['timestamp']).strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{html.escape(str(record.get('payload', '')))}</td>"
            f"<td>{html.escape(str(record.get('zone', '')))}</td>"
            f"<td class='n'>{baseline.get('carbon_intensity', 0):.0f}</td>"
            f"<td class='n'>{chosen.get('carbon_intensity', 0):.0f}</td>"
            f"<td class='n'>{record.get('energy_kwh', 0):.4f}</td>"
            f"<td class='n'>{record.get('co2_saved_g', 0):+.3f}</td>"
            f"<td class='n'>{record.get('co2_saved_percent', 0):+.1f}%</td>"
            f"<td class='s {css}'>{status}</td>"
            "</tr>"
        )
    table = "".join(rows) or "<tr><td colspan='9'>No qualifying activity in this period.</td></tr>"

    method = "".join(
        f"<dt>{html.escape(term)}</dt><dd>{html.escape(text)}</dd>"
        for term, text in METHODOLOGY
    )
    limits = "".join(f"<li>{html.escape(item)}</li>" for item in LIMITATIONS)

    _, excluded = split_by_verification(runs, verified)
    if excluded:
        avoided = sum(float(r.get("co2_saved_g", 0)) for r in excluded)
        caveat = (
            f"<p class='warn'><strong>{len(excluded)} job(s) could not be verified</strong> "
            "against execution logs. They are listed below for completeness but are "
            f"<strong>excluded from every figure in this report</strong> "
            f"({avoided:+.3f} g CO&#8322;e not claimed).</p>"
        )
    elif verified is None or verified.get("_error"):
        caveat = (
            "<p class='warn'>Execution logs were <strong>not checked</strong> for this "
            "report. Figures rest on scheduling records alone. Re-run with "
            "<code>--verify</code> before disclosing them.</p>"
        )
    else:
        caveat = ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Carbon Reduction Report &#183; {html.escape(label)}</title>
<style>
  @page {{ size:A4; margin:18mm; }}
  body {{ font:12px/1.55 Georgia,"Times New Roman",serif; color:#14201b;
         max-width:820px; margin:28px auto; padding:0 24px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:14px; margin:30px 0 10px; padding-bottom:5px;
        border-bottom:1.5px solid #2f6f4e; color:#2f6f4e; }}
  .meta {{ color:#5c6b64; font-size:11.5px; margin-bottom:26px; }}
  .headline {{ background:#f2f8f4; border:1px solid #cfe2d7; border-radius:7px;
               padding:18px 22px; margin:18px 0 8px; }}
  .headline .big {{ font-size:34px; font-weight:bold; color:#2f6f4e; line-height:1.1; }}
  .headline .sub {{ color:#5c6b64; font-size:11.5px; margin-top:6px; }}
  table {{ width:100%; border-collapse:collapse; font-size:10.5px; margin-top:8px; }}
  th {{ text-align:left; background:#f2f8f4; border-bottom:1.5px solid #2f6f4e;
        padding:7px 6px; font-size:9.5px; text-transform:uppercase;
        letter-spacing:.05em; }}
  td {{ padding:6px; border-bottom:1px solid #e6ece9; }}
  .n {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .s {{ font-size:9.5px; }}
  .s.yes {{ color:#2f6f4e; }} .s.no {{ color:#b23b2a; font-weight:bold; }}
  .s.unknown {{ color:#8a8a8a; }}
  dl {{ margin:0; }} dt {{ font-weight:bold; margin-top:11px; }}
  dd {{ margin:3px 0 0; color:#33443c; }}
  ul {{ margin:6px 0 0; padding-left:19px; }} li {{ margin-bottom:6px; }}
  .warn {{ background:#fdf4e7; border-left:3px solid #b4721f; padding:9px 13px;
           font-size:11px; }}
  footer {{ margin-top:34px; padding-top:12px; border-top:1px solid #e6ece9;
            color:#5c6b64; font-size:10px; }}
  @media print {{ body {{ margin:0; }} }}
</style></head><body>

<h1>Carbon Reduction Report</h1>
<div class="meta">
  {html.escape(org)}<br>
  Reporting period: <strong>{html.escape(label)}</strong><br>
  Generated {generated.strftime('%d %B %Y, %H:%M UTC')} by CarbonShift
</div>

<div class="headline">
  <div class="big">{totals['saved_g'] / 1000:.4f} kg CO&#8322;e avoided</div>
  <div class="sub">
    {totals['jobs']} workload{'s' if totals['jobs'] != 1 else ''} shifted &#183;
    {totals['saved_percent']:.1f}% reduction against baseline &#183;
    mean deferral {totals['mean_delay_hours']:.1f} h
  </div>
</div>

<h2>Summary</h2>
<table>
  <tr><td>Workloads shifted</td><td class="n">{totals['jobs']}</td></tr>
  <tr><td>Electricity consumed</td><td class="n">{totals['energy_kwh']:.4f} kWh</td></tr>
  <tr><td>Baseline emissions (run on submission)</td>
      <td class="n">{totals['baseline_g']:.3f} g CO&#8322;e</td></tr>
  <tr><td>Actual emissions (run when shifted)</td>
      <td class="n">{totals['actual_g']:.3f} g CO&#8322;e</td></tr>
  <tr><td><strong>Emissions avoided</strong></td>
      <td class="n"><strong>{totals['saved_g']:.3f} g CO&#8322;e
      ({totals['saved_percent']:.1f}%)</strong></td></tr>
  <tr><td>Grid zones</td><td class="n">{html.escape(', '.join(totals['zones']) or '-')}</td></tr>
  <tr><td>Cloud regions</td><td class="n">{html.escape(', '.join(totals['regions']) or '-')}</td></tr>
</table>

<h2>Itemised activity</h2>
{caveat}
<table>
  <thead><tr>
    <th>Executed (UTC)</th><th>Workload</th><th>Zone</th>
    <th class="n">Baseline<br>gCO&#8322;/kWh</th>
    <th class="n">Actual<br>gCO&#8322;/kWh</th>
    <th class="n">Energy<br>kWh</th>
    <th class="n">Avoided<br>g CO&#8322;e</th>
    <th class="n">Reduction</th><th>Verification</th>
  </tr></thead>
  <tbody>{table}</tbody>
</table>

<h2>Methodology</h2>
<dl>{method}</dl>

<h2>Limitations and basis of preparation</h2>
<ul>{limits}</ul>

<footer>
  Produced by CarbonShift &#183; github.com/Ramyasamudralaa/carbonshift<br>
  Figures are modelled estimates, not metered emissions. This report is not an
  assurance statement and has not been independently verified.
</footer>
</body></html>"""


# --- terminal --------------------------------------------------------------


def print_summary(label, totals, runs, counted, excluded, verified) -> None:
    print()
    print(f"{BOLD}  Carbon Reduction Report{RESET}  {DIM}{label}{RESET}")
    print("  " + "-" * 58)
    print(f"    Workloads shifted        {totals['jobs']:>12}")
    print(f"    Electricity consumed     {totals['energy_kwh']:>12.4f} kWh")
    print(f"    Baseline emissions       {totals['baseline_g']:>12.3f} g")
    print(f"    Actual emissions         {totals['actual_g']:>12.3f} g")
    print(f"  {BOLD}{GREEN}  Emissions avoided        {totals['saved_g']:>12.3f} g "
          f"({totals['saved_percent']:.1f}%){RESET}")
    if verified is not None and not verified.get("_error"):
        print(f"    Verified against logs    {len(counted):>12} of {len(runs)}")
        if excluded:
            withheld = sum(float(r.get("co2_saved_g", 0)) for r in excluded)
            print(f"  {DIM}  Excluded, unverifiable   {len(excluded):>12} "
                  f"({withheld:+.3f} g not claimed){RESET}")
    else:
        print(f"  {DIM}  Logs not checked - run with --verify before disclosing{RESET}")
    print("  " + "-" * 58)


# --- main ------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift-report",
        description="Build a carbon reduction report from recorded activity.",
    )
    period = parser.add_mutually_exclusive_group()
    period.add_argument("--month", help="Report on one month, e.g. 2026-09.")
    period.add_argument("--year", help="Report on one calendar year, e.g. 2026.")
    period.add_argument("--all", action="store_true", dest="everything",
                        help="Report on all recorded activity.")
    parser.add_argument("--verify", action="store_true",
                        help="Cross-check each job against CloudWatch Logs.")
    parser.add_argument("--csv", action="store_true",
                        help="Also write the row-level CSV.")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not open the report in a browser.")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        runs, label = select_period(load_runs(), args.month, args.year, args.everything)
    except ValueError:
        print(f"Could not read --month {args.month!r}. Use YYYY-MM, e.g. 2026-09.",
              file=sys.stderr)
        return 1

    verified = verify(runs) if args.verify else None
    counted, excluded = split_by_verification(runs, verified)
    totals = summarise(counted)

    print_summary(label, totals, runs, counted, excluded, verified)

    slug = label.lower().replace(" ", "-")
    out = args.out or (REPORT_DIR / f"carbon-report-{slug}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(runs, verified, label, totals), encoding="utf-8")
    print(f"    Report   {out}")

    if args.csv:
        csv_path = out.with_suffix(".csv")
        write_csv(runs, verified, csv_path)
        print(f"    CSV      {csv_path}")

    if verified is not None and verified.get("_error"):
        print(f"    {DIM}Verification skipped: {verified['_error']}{RESET}")

    print()
    if not runs:
        print(f"  {DIM}No executed jobs in this period. Previews are never "
              f"counted.{RESET}")
        print()

    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
        print(f"  {DIM}Opened in your browser. Print to PDF to file it.{RESET}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
