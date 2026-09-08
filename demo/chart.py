"""Render the before/after carbon comparison for one CarbonShift run.

Reads the run record written by ``src/scheduler.py`` and produces a single
static PNG: the carbon-intensity curve for the demo window, the point where the
job would have run if it had been triggered immediately, the point where
CarbonShift actually ran it, and the resulting CO2 saving.

This is deliberately a static image, not an interactive dashboard.

    python demo/chart.py
    python demo/chart.py --run runs/latest.json --out demo/result.png
    python demo/chart.py --actual-run 2026-09-09T03:00:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # no display needed; write straight to a file

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

# --- Configuration ---------------------------------------------------------

DEFAULT_RUN_RECORD = Path("runs/latest.json")
DEFAULT_OUTPUT = Path("demo/result.png")

COLOUR_CURVE = "#2f6f4e"
COLOUR_FILL = "#cfe6d8"
COLOUR_BASELINE = "#c1442e"
COLOUR_CHOSEN = "#1b7f3b"
COLOUR_DEADLINE = "#8a8a8a"

FIGURE_SIZE = (11, 6)
FIGURE_DPI = 150


class ChartError(Exception):
    """The run record is missing or unusable."""


def load_run_record(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ChartError(
            f"No run record at {path}. Submit a job first:\n"
            '  python -m src.scheduler --payload "demo" --deadline-hours 6'
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ChartError(f"Run record at {path} is not valid JSON: {exc}") from exc

    for key in ("forecast", "baseline", "chosen", "deadline"):
        if key not in record:
            raise ChartError(f"Run record at {path} has no '{key}' field.")
    if not record["forecast"]:
        raise ChartError(f"Run record at {path} has an empty forecast.")
    return record


def parse_time(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_chart(record: dict[str, Any], output: Path, actual_run: datetime | None) -> Path:
    times = [parse_time(point["timestamp"]) for point in record["forecast"]]
    intensities = [float(point["carbon_intensity"]) for point in record["forecast"]]

    baseline_time = parse_time(record["baseline"]["timestamp"])
    baseline_intensity = float(record["baseline"]["carbon_intensity"])
    chosen_time = parse_time(record["chosen"]["timestamp"])
    chosen_intensity = float(record["chosen"]["carbon_intensity"])
    deadline = parse_time(record["deadline"])

    saved_g = float(record.get("co2_saved_g", 0.0))
    saved_percent = float(record.get("co2_saved_percent", 0.0))
    baseline_g = float(record["baseline"].get("emissions_g", 0.0))
    chosen_g = float(record["chosen"].get("emissions_g", 0.0))

    figure, axes = plt.subplots(figsize=FIGURE_SIZE, dpi=FIGURE_DPI)

    axes.plot(times, intensities, color=COLOUR_CURVE, linewidth=2, zorder=3)
    axes.fill_between(times, intensities, color=COLOUR_FILL, alpha=0.6, zorder=1)

    if deadline >= times[0]:
        axes.axvline(
            deadline,
            color=COLOUR_DEADLINE,
            linestyle="--",
            linewidth=1.4,
            zorder=2,
        )
        axes.annotate(
            "deadline",
            xy=(deadline, max(intensities)),
            xytext=(4, -12),
            textcoords="offset points",
            color=COLOUR_DEADLINE,
            fontsize=9,
        )

    axes.scatter(
        [baseline_time],
        [baseline_intensity],
        s=150,
        color=COLOUR_BASELINE,
        zorder=5,
        edgecolor="white",
        linewidth=1.5,
    )
    axes.annotate(
        f"run immediately\n{baseline_intensity:.0f} gCO2/kWh -> {baseline_g:.2f} g CO2",
        xy=(baseline_time, baseline_intensity),
        xytext=(10, 18),
        textcoords="offset points",
        color=COLOUR_BASELINE,
        fontsize=10,
        fontweight="bold",
    )

    axes.scatter(
        [chosen_time],
        [chosen_intensity],
        s=180,
        marker="*",
        color=COLOUR_CHOSEN,
        zorder=5,
        edgecolor="white",
        linewidth=1.2,
    )
    axes.annotate(
        f"CarbonShift ran here\n{chosen_intensity:.0f} gCO2/kWh -> {chosen_g:.2f} g CO2",
        xy=(chosen_time, chosen_intensity),
        xytext=(10, -34),
        textcoords="offset points",
        color=COLOUR_CHOSEN,
        fontsize=10,
        fontweight="bold",
    )

    if actual_run is not None:
        axes.axvline(actual_run, color=COLOUR_CHOSEN, linestyle=":", linewidth=1.4)
        axes.annotate(
            f"verified execution\n{actual_run.strftime('%H:%M UTC')}",
            xy=(actual_run, min(intensities)),
            xytext=(6, 10),
            textcoords="offset points",
            color=COLOUR_CHOSEN,
            fontsize=9,
        )

    delay_hours = (chosen_time - baseline_time).total_seconds() / 3600.0
    axes.set_title(
        f"CarbonShift: {saved_percent:.0f}% less CO2 for the same job\n"
        f"{saved_g:.2f} g CO2 saved by delaying execution {delay_hours:.1f} h "
        f"({record.get('zone', 'unknown zone')})",
        fontsize=14,
        fontweight="bold",
        pad=16,
    )
    axes.set_xlabel("Time (UTC)")
    axes.set_ylabel("Grid carbon intensity (gCO2/kWh)")
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))
    axes.grid(True, alpha=0.25, zorder=0)
    axes.set_ylim(bottom=0)
    axes.margins(x=0.02)

    figure.text(
        0.5,
        0.015,
        f"job: {record.get('payload', 'n/a')}   |   "
        f"energy: {record.get('energy_kwh', 0):.4f} kWh   |   "
        f"{record.get('worker_vcpu', '?')} vCPU x "
        f"{record.get('worker_runtime_hours', '?')} h",
        ha="center",
        fontsize=9,
        color="#555555",
    )

    figure.tight_layout(rect=(0, 0.04, 1, 1))
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, bbox_inches="tight")
    plt.close(figure)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift-chart",
        description="Render the before/after carbon chart for a CarbonShift run.",
    )
    parser.add_argument(
        "--run",
        type=Path,
        default=DEFAULT_RUN_RECORD,
        help=f"Run record JSON written by the scheduler (default: {DEFAULT_RUN_RECORD}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the PNG (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--actual-run",
        default=None,
        help=(
            "ISO 8601 timestamp of the verified execution, taken from CloudWatch "
            "Logs. Drawn as a dotted line so the chart shows the real run, not "
            "just the intended one."
        ),
    )
    args = parser.parse_args(argv)

    try:
        record = load_run_record(args.run)
        actual_run = parse_time(args.actual_run) if args.actual_run else None
        output = build_chart(record, args.out, actual_run)
    except ChartError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: could not parse a timestamp: {exc}", file=sys.stderr)
        return 1

    print(f"Chart written to {output}")
    print(
        f"  {record.get('co2_saved_g', 0):.2f} g CO2 saved "
        f"({record.get('co2_saved_percent', 0):.1f}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
