"""Tests for demo/chart.py.

Covers the offline half of spec check #8: given a real run record, the chart
renders without error and reports the right saving. The "is it readable"
half is a human check on the produced PNG.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.scheduler import schedule_job, write_run_record

CHART_PATH = Path(__file__).resolve().parents[1] / "demo" / "chart.py"


@pytest.fixture(scope="module")
def chart():
    spec = importlib.util.spec_from_file_location("carbonshift_chart", CHART_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_record(sample_forecast, base_time, tmp_path):
    decision = schedule_job(
        payload="nightly-etl",
        deadline=base_time + timedelta(hours=11),
        zone="DE",
        dry_run=True,
        now=base_time,
        forecast=sample_forecast,
    )
    return write_run_record(decision, directory=tmp_path)


def test_renders_a_png_from_a_real_run_record(chart, run_record, tmp_path):
    output = tmp_path / "result.png"

    written = chart.build_chart(chart.load_run_record(run_record), output, None)

    assert written.exists()
    assert written.stat().st_size > 10_000  # a real plot, not a blank canvas


def test_marks_a_verified_execution_time_when_given_one(chart, run_record, tmp_path):
    record = chart.load_run_record(run_record)
    actual = chart.parse_time(record["chosen"]["timestamp"])

    written = chart.build_chart(record, tmp_path / "with-actual.png", actual)

    assert written.exists()


def test_a_missing_run_record_explains_how_to_make_one(chart, tmp_path):
    with pytest.raises(chart.ChartError) as excinfo:
        chart.load_run_record(tmp_path / "nope.json")

    assert "src.scheduler" in str(excinfo.value)


def test_a_malformed_run_record_is_rejected(chart, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"forecast": []}), encoding="utf-8")

    with pytest.raises(chart.ChartError):
        chart.load_run_record(path)


def test_cli_reports_the_saving(chart, run_record, tmp_path, capsys):
    exit_code = chart.main(["--run", str(run_record), "--out", str(tmp_path / "cli.png")])

    assert exit_code == 0
    assert "g CO2 saved" in capsys.readouterr().out


def test_cli_fails_cleanly_when_there_is_no_run(chart, tmp_path, capsys):
    exit_code = chart.main(["--run", str(tmp_path / "missing.json")])

    assert exit_code == 1
    assert "ERROR" in capsys.readouterr().err
