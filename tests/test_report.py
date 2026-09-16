"""Tests for src/report.py.

A compliance report is only worth anything if its numbers are defensible, so
these concentrate on the ways it could overstate a saving: counting previews
that never ran, counting executions it cannot evidence, or claiming more
certainty than the method supports.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from src import report as R


def make_run(payload="job", when="2026-09-11T09:00:00+00:00",
             baseline=497.0, chosen=251.0, scheduled=True, energy=0.0074):
    saved = (baseline - chosen) * energy
    return {
        "payload": payload, "region": "eu-central-1", "zone": "DE",
        "energy_kwh": energy,
        "deadline": when,
        "baseline": {"timestamp": "2026-09-10T17:00:00+00:00",
                     "carbon_intensity": baseline,
                     "emissions_g": baseline * energy},
        "chosen": {"timestamp": when, "carbon_intensity": chosen,
                   "emissions_g": chosen * energy},
        "co2_saved_g": saved,
        "co2_saved_percent": 100.0 * (baseline - chosen) / baseline,
        "delay_hours": 16.0,
        "scheduled": scheduled,
        "forecast": [],
    }


def verified_for(*runs):
    return {R.verification_key(r): "started" for r in runs}


# --- period selection ------------------------------------------------------


def test_previews_are_never_included():
    runs = [make_run(payload="real", scheduled=True),
            make_run(payload="preview", scheduled=False)]

    selected, _ = R.select_period(runs, None, None, everything=True)

    assert [r["payload"] for r in selected] == ["real"]


def test_month_selection_keeps_only_that_month():
    runs = [make_run(when="2026-09-11T09:00:00+00:00", payload="sept"),
            make_run(when="2026-10-02T09:00:00+00:00", payload="oct")]

    selected, label = R.select_period(runs, "2026-09", None, False)

    assert [r["payload"] for r in selected] == ["sept"]
    assert label == "September 2026"


def test_year_selection_keeps_the_whole_year():
    runs = [make_run(when="2026-03-11T09:00:00+00:00"),
            make_run(when="2026-10-02T09:00:00+00:00"),
            make_run(when="2025-10-02T09:00:00+00:00")]

    selected, label = R.select_period(runs, None, "2026", False)

    assert len(selected) == 2
    assert label == "Calendar year 2026"


def test_an_empty_period_is_not_an_error():
    selected, label = R.select_period([make_run()], "2026-01", None, False)

    assert selected == []
    assert label == "January 2026"


# --- verification splitting ------------------------------------------------


def test_unverified_runs_are_separated_when_logs_were_checked():
    good, bad = make_run(payload="ran"), make_run(payload="never-ran")

    counted, excluded = R.split_by_verification([good, bad], verified_for(good))

    assert [r["payload"] for r in counted] == ["ran"]
    assert [r["payload"] for r in excluded] == ["never-ran"]


def test_nothing_is_excluded_when_verification_did_not_run():
    runs = [make_run(), make_run()]

    counted, excluded = R.split_by_verification(runs, None)

    assert len(counted) == 2
    assert excluded == []


def test_nothing_is_excluded_when_verification_failed():
    runs = [make_run()]

    counted, excluded = R.split_by_verification(runs, {"_error": "no credentials"})

    assert len(counted) == 1
    assert excluded == []


# --- arithmetic ------------------------------------------------------------


def test_totals_match_the_underlying_records():
    runs = [make_run(baseline=400, chosen=200, energy=0.01),
            make_run(baseline=300, chosen=100, energy=0.01)]

    totals = R.summarise(runs)

    assert totals["jobs"] == 2
    assert totals["baseline_g"] == pytest.approx(7.0)   # 4.0 + 3.0
    assert totals["actual_g"] == pytest.approx(3.0)     # 2.0 + 1.0
    assert totals["saved_g"] == pytest.approx(4.0)
    assert totals["saved_percent"] == pytest.approx(400 / 7)


def test_an_empty_period_totals_to_zero_without_dividing_by_zero():
    totals = R.summarise([])

    assert totals["jobs"] == 0
    assert totals["saved_g"] == 0
    assert totals["saved_percent"] == 0


def test_a_negative_saving_reduces_the_total():
    runs = [make_run(baseline=400, chosen=200, energy=0.01),
            make_run(baseline=400, chosen=500, energy=0.01)]

    assert R.summarise(runs)["saved_g"] == pytest.approx(1.0)  # 2.0 - 1.0


# --- csv -------------------------------------------------------------------


def test_csv_has_a_row_per_job_and_marks_verification(tmp_path):
    good, bad = make_run(payload="ran"), make_run(payload="never-ran")
    path = R.write_csv([good, bad], verified_for(good), tmp_path / "out.csv")

    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 2
    assert rows[0]["job"] == "ran" and rows[0]["verified"] == "yes"
    assert rows[1]["job"] == "never-ran" and rows[1]["verified"] == "no"


def test_csv_says_not_checked_when_verification_was_skipped(tmp_path):
    path = R.write_csv([make_run()], None, tmp_path / "out.csv")

    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert rows[0]["verified"] == "not checked"


# --- the document ----------------------------------------------------------


def test_the_report_states_its_method_and_its_limits():
    runs = [make_run()]
    page = R.build_html(runs, verified_for(*runs), "September 2026", R.summarise(runs))

    assert "Methodology" in page
    assert "Limitations and basis of preparation" in page
    assert "counterfactual" in page
    assert "not metered emissions" in page


def test_excluded_jobs_are_declared_and_not_claimed():
    good, bad = make_run(payload="ran"), make_run(payload="never-ran")
    counted, _ = R.split_by_verification([good, bad], verified_for(good))
    page = R.build_html([good, bad], verified_for(good), "September 2026",
                        R.summarise(counted))

    assert "could not be verified" in page
    assert "excluded from every figure" in page


def test_an_unchecked_report_warns_before_disclosure():
    runs = [make_run()]
    page = R.build_html(runs, None, "September 2026", R.summarise(runs))

    assert "not checked" in page
    assert "--verify" in page


def test_a_job_name_cannot_inject_markup():
    runs = [make_run(payload="<img src=x onerror=alert(1)>")]
    page = R.build_html(runs, None, "September 2026", R.summarise(runs))

    assert "<img src=x" not in page
    assert "&lt;img" in page


def test_the_headline_is_reported_in_kilograms():
    runs = [make_run(baseline=400, chosen=200, energy=1.0)]  # 200 g saved
    page = R.build_html(runs, None, "September 2026", R.summarise(runs))

    assert "0.2000 kg" in page
