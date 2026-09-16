"""Tests for demo/dashboard.py.

The dashboard is what gets shown to people, so the things that matter are that
it reports the right numbers, distinguishes a job that has not run yet from one
that has, and does not let a job name break the page.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = REPO / "demo" / "dashboard.py"


@pytest.fixture(scope="module")
def dash():
    spec = importlib.util.spec_from_file_location("carbonshift_dashboard",
                                                  DASHBOARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_run(payload="job", hours_from_now=-2, baseline=400.0, chosen=250.0,
             scheduled=True):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=4)
    forecast = [
        {"timestamp": (start + timedelta(hours=h)).isoformat(),
         "carbon_intensity": float(v)}
        for h, v in enumerate([400, 430, 380, 300, 250, 260, 320, 390])
    ]
    chosen_at = now + timedelta(hours=hours_from_now)
    energy = 0.0074
    saved = (baseline - chosen) * energy
    return {
        "job_id": "test", "payload": payload, "region": "eu-central-1", "zone": "DE",
        "submitted_at": start.isoformat(),
        "deadline": (now + timedelta(hours=6)).isoformat(),
        "energy_kwh": energy,
        "baseline": {"timestamp": start.isoformat(),
                     "carbon_intensity": baseline,
                     "emissions_g": baseline * energy},
        "chosen": {"timestamp": chosen_at.isoformat(),
                   "carbon_intensity": chosen,
                   "emissions_g": chosen * energy},
        "co2_saved_g": saved,
        "co2_saved_percent": 100.0 * (baseline - chosen) / baseline,
        "delay_hours": 4.0,
        "scheduled": scheduled,
        "forecast": forecast,
    }


# --- empty state -----------------------------------------------------------


def test_no_runs_produces_a_usable_page(dash):
    page = dash.build_html([])

    assert "Nothing scheduled" in page
    assert "CarbonShift" in page
    assert "<svg" not in page  # nothing to draw, and it must not crash trying


# --- countdown vs done -----------------------------------------------------


def test_a_future_run_gets_a_live_countdown(dash):
    page = dash.build_html([make_run(hours_from_now=3)])

    assert 'class="countdown" data-target=' in page
    assert "Next job runs in" in page


def test_a_past_run_shows_done_not_a_countdown(dash):
    page = dash.build_html([make_run(hours_from_now=-3)])

    assert "data-target=" not in page
    assert "Last job ran" in page


def test_the_soonest_future_run_is_the_one_counted_down(dash):
    page = dash.build_html([
        make_run(payload="later", hours_from_now=8),
        make_run(payload="sooner", hours_from_now=2),
    ])

    countdown_section = page.split("Next job runs in", 1)[1][:400]
    assert "sooner" in countdown_section
    assert "later" not in countdown_section


def test_a_future_dry_run_does_not_get_a_countdown(dash):
    """A preview was never scheduled, so nothing is going to happen."""
    page = dash.build_html([make_run(hours_from_now=3, scheduled=False)])

    assert "data-target=" not in page


# --- totals ----------------------------------------------------------------


def test_dry_runs_are_excluded_from_the_total(dash):
    runs = [
        make_run(payload="real", baseline=400, chosen=200, scheduled=True),
        make_run(payload="preview", baseline=400, chosen=200, scheduled=False),
    ]

    page = dash.build_html(runs)

    # one real run at (400-200) * 0.0074 = 1.48 g
    assert "1.48 g" in page
    assert "across 1 real run" in page


def test_a_negative_saving_is_shown_as_negative(dash):
    page = dash.build_html([make_run(baseline=400, chosen=500)])

    assert "-0.74 g" in page


# --- the curve -------------------------------------------------------------


def test_the_curve_is_drawn_with_both_points_marked(dash):
    svg = dash.curve_svg(make_run())

    assert "<svg" in svg and "</svg>" in svg
    assert "polyline" in svg          # the carbon curve
    assert "dot-now" in svg           # run-immediately marker
    assert "dot-chosen" in svg        # what CarbonShift picked


def test_a_forecast_too_short_to_plot_degrades_politely(dash):
    record = make_run()
    record["forecast"] = [record["forecast"][0]]

    svg = dash.curve_svg(record)

    assert "<svg" not in svg
    assert "No forecast data" in svg


# --- safety ----------------------------------------------------------------


def test_a_job_name_cannot_inject_markup(dash):
    """Job names are user input and go straight into the page."""
    page = dash.build_html([make_run(payload="<script>alert(1)</script>")])

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
