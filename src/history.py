"""What CarbonShift has actually done, and what it saved.

A one-time AWS schedule deletes itself the moment it fires, so after a job runs
there is nothing left in the console to look at. Every decision is recorded to
runs/ when it is made, and this reads those records back.

    python -m src.history                 # everything, with the running total
    python -m src.history --real-only     # skip dry runs
    python -m src.history --verify        # confirm against CloudWatch Logs
    python -m src.history --chart         # cumulative savings image
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

RUN_DIR = Path(os.getenv("CARBONSHIFT_RUN_DIR", "runs"))
LOG_GROUP = os.getenv("CARBONSHIFT_LOG_GROUP", "/ecs/carbonshift-worker")
AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m",
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    try:
        import colorama

        colorama.just_fix_windows_console()
    except Exception:
        BOLD = DIM = GREEN = RED = YELLOW = RESET = ""


def load_runs(directory: Path = RUN_DIR) -> list[dict[str, Any]]:
    """Every recorded decision, oldest first. latest.json is a duplicate."""
    if not directory.exists():
        return []

    runs = []
    for path in glob.glob(str(directory / "*.json")):
        if Path(path).name == "latest.json":
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (json.JSONDecodeError, OSError):
            continue
        record["_file"] = Path(path).name
        runs.append(record)

    runs.sort(key=lambda r: r.get("chosen", {}).get("timestamp", ""))
    return runs


def verify_against_cloudwatch(runs: list[dict[str, Any]]) -> dict[str, str]:
    """Ask CloudWatch which payloads actually produced a completion log."""
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        return {"_error": "boto3 is not installed, so CloudWatch cannot be checked."}

    client = boto3.client("logs", region_name=AWS_REGION)
    found: dict[str, str] = {}
    try:
        paginator = client.get_paginator("filter_log_events")
        pages = paginator.paginate(
            logGroupName=LOG_GROUP, filterPattern="CARBONSHIFT_JOB_COMPLETE"
        )
        for page in pages:
            for event in page.get("events", []):
                message = event.get("message", "")
                marker = "CARBONSHIFT_JOB_COMPLETE "
                if marker not in message:
                    continue
                try:
                    payload = json.loads(message.split(marker, 1)[1])
                except (ValueError, IndexError):
                    continue
                key = f"{payload.get('payload')}|{payload.get('scheduled_for')}"
                found[key] = payload.get("started_at", "")
    except NoCredentialsError:
        return {"_error": "No AWS credentials found, so CloudWatch cannot be checked."}
    except (ClientError, BotoCoreError) as exc:
        return {"_error": f"CloudWatch could not be read: {exc}"}

    return found


def _short(timestamp: str) -> str:
    return timestamp[:16].replace("T", " ") if timestamp else "-"


def render(runs, verified=None, real_only=False) -> int:
    if not runs:
        print()
        print("  No runs recorded yet.")
        print()
        print("  Make one:")
        print('    python -m src.scheduler --payload "my-job" '
              "--deadline-hours 12 --dry-run")
        print()
        return 1

    shown = [r for r in runs if r.get("scheduled")] if real_only else runs

    print()
    print(f"{BOLD}  CarbonShift run history{RESET}")
    print(f"  {DIM}{RUN_DIR.resolve()}{RESET}")
    print()
    header = f"  {'WHEN IT RAN':<17} {'JOB':<22} {'TYPE':<10} {'BEFORE':>7} {'AFTER':>7} {'SAVED':>9}"
    if verified is not None:
        header += "  STATUS"
    print(f"{BOLD}{header}{RESET}")
    print("  " + "-" * (len(header) + 4))

    total_saved = 0.0
    total_baseline = 0.0
    real_count = 0

    for record in shown:
        chosen = record.get("chosen", {})
        baseline = record.get("baseline", {})
        saved = float(record.get("co2_saved_g", 0.0))
        is_real = bool(record.get("scheduled"))

        kind = f"{GREEN}scheduled{RESET}" if is_real else f"{DIM}dry-run{RESET}"
        kind_pad = "scheduled" if is_real else "dry-run"

        colour = GREEN if saved > 0 else (RED if saved < 0 else "")
        line = (
            f"  {_short(chosen.get('timestamp', '')):<17} "
            f"{record.get('payload', '?')[:22]:<22} "
            f"{kind}{' ' * (10 - len(kind_pad))} "
            f"{baseline.get('carbon_intensity', 0):>7.0f} "
            f"{chosen.get('carbon_intensity', 0):>7.0f} "
            f"{colour}{saved:>+7.2f} g{RESET}"
        )

        if verified is not None:
            key = f"{record.get('payload')}|{chosen.get('timestamp')}"
            if not is_real:
                line += f"  {DIM}n/a{RESET}"
            elif verified.get("_error"):
                line += f"  {YELLOW}?{RESET}"
            elif key in verified:
                line += f"  {GREEN}confirmed ran{RESET}"
            else:
                line += f"  {YELLOW}no log found{RESET}"

        print(line)

        if is_real:
            total_saved += saved
            total_baseline += float(baseline.get("emissions_g", 0.0))
            real_count += 1

    print("  " + "-" * (len(header) + 4))
    print()

    dry_count = len(runs) - sum(1 for r in runs if r.get("scheduled"))
    percent = (100.0 * total_saved / total_baseline) if total_baseline else 0.0

    print(f"{BOLD}  Totals across {real_count} real scheduled run"
          f"{'s' if real_count != 1 else ''}{RESET}")
    print(f"    CO2 that would have been emitted : {total_baseline:7.2f} g")
    print(f"    CO2 actually emitted             : {total_baseline - total_saved:7.2f} g")
    print(f"    {BOLD}{GREEN}CO2 saved                        : {total_saved:7.2f} g "
          f"({percent:.1f}%){RESET}")
    if dry_count:
        print()
        print(f"  {DIM}{dry_count} dry run{'s' if dry_count != 1 else ''} excluded from "
              f"totals - they never executed.{RESET}")

    if verified is not None and verified.get("_error"):
        print()
        print(f"  {YELLOW}Could not verify against CloudWatch:{RESET} {verified['_error']}")

    print()
    return 0


def build_chart(runs, output: Path) -> Path:
    """Cumulative saving over time, counting only real scheduled runs."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    real = [r for r in runs if r.get("scheduled")]
    if not real:
        raise SystemExit(
            "No real scheduled runs yet - nothing to chart. "
            "Dry runs are excluded because they never executed."
        )

    times, cumulative, running = [], [], 0.0
    for record in real:
        stamp = record["chosen"]["timestamp"].replace("Z", "+00:00")
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        running += float(record.get("co2_saved_g", 0.0))
        times.append(parsed)
        cumulative.append(running)

    figure, axes = plt.subplots(figsize=(10, 5), dpi=150)
    axes.step(times, cumulative, where="post", color="#2f6f4e", linewidth=2.4)
    axes.fill_between(times, cumulative, step="post", color="#cfe6d8", alpha=0.65)
    axes.scatter(times, cumulative, s=55, color="#2f6f4e", zorder=5,
                 edgecolor="white", linewidth=1.2)

    for t, c, record in zip(times, cumulative, real):
        axes.annotate(f"{record['payload'][:18]}\n{record['co2_saved_g']:+.2f} g",
                      xy=(t, c), xytext=(0, 12), textcoords="offset points",
                      ha="center", fontsize=7.5, color="#1a2b23")

    axes.set_title(
        f"CarbonShift: {running:.2f} g CO2 saved across {len(real)} scheduled runs",
        fontsize=13, fontweight="bold", pad=14,
    )
    axes.set_ylabel("Cumulative CO2 saved (grams)")
    axes.set_xlabel("When the job ran (UTC)")
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M", tz=timezone.utc))
    axes.grid(True, alpha=0.25)
    axes.set_axisbelow(True)
    figure.autofmt_xdate()
    figure.tight_layout()

    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, bbox_inches="tight")
    plt.close(figure)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift-history",
        description="Show every CarbonShift decision and what it saved.",
    )
    parser.add_argument("--real-only", action="store_true",
                        help="Hide dry runs, which never executed.")
    parser.add_argument("--verify", action="store_true",
                        help="Check CloudWatch Logs to confirm each run really executed.")
    parser.add_argument("--chart", action="store_true",
                        help="Also write a cumulative savings chart.")
    parser.add_argument("--out", type=Path, default=Path("demo/history.png"),
                        help="Where to write the chart (default: demo/history.png).")
    args = parser.parse_args(argv)

    runs = load_runs()
    verified = verify_against_cloudwatch(runs) if args.verify else None
    exit_code = render(runs, verified=verified, real_only=args.real_only)

    if args.chart and runs:
        path = build_chart(runs, args.out)
        print(f"  Chart written to {path}")
        print()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
