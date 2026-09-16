"""Tests for src/scanner.py.

The scanner makes recommendations a person may act on, so the failure that
matters is a false positive: telling someone a latency-sensitive job is safe to
delay. These concentrate on the signals that prevent that.
"""

from __future__ import annotations

import pytest

from src import scanner as S
from src.scanner import Job, classify_heuristic, parse_crontab, runs_per_year


def job(name="thing.sh", schedule="0 2 * * *", command=None, runs=365):
    return Job(name=name, schedule=schedule,
               command=command if command is not None else f"/opt/jobs/{name}",
               runs_per_year=runs)


# --- cron parsing ----------------------------------------------------------


def test_comments_and_environment_lines_are_ignored():
    jobs = parse_crontab(
        "# a comment\n"
        "MAILTO=ops@example.com\n"
        "PATH=/usr/bin\n"
        "\n"
        "0 2 * * * /opt/jobs/backup.sh\n"
    )

    assert len(jobs) == 1
    assert jobs[0].command == "/opt/jobs/backup.sh"


def test_cron_aliases_are_understood():
    jobs = parse_crontab("@daily /opt/jobs/cleanup.sh\n@weekly /opt/jobs/report.py")

    assert [j.runs_per_year for j in jobs] == [365, 52]


def test_a_malformed_line_is_skipped_not_fatal():
    jobs = parse_crontab("this is not a crontab line\n0 2 * * * /opt/ok.sh")

    assert len(jobs) == 1


# --- frequency -------------------------------------------------------------


@pytest.mark.parametrize("schedule,expected", [
    ("0 2 * * *", 365),          # daily
    ("*/5 * * * *", 105120),     # every five minutes
    ("* * * * *", 525600),       # every minute
    ("0 * * * *", 8760),         # hourly
])
def test_run_frequency_is_in_the_right_order_of_magnitude(schedule, expected):
    assert runs_per_year(schedule) == expected


def test_weekly_and_monthly_are_far_rarer_than_daily():
    assert runs_per_year("0 4 * * 0") < runs_per_year("0 2 * * *")
    assert runs_per_year("0 5 1 * *") < runs_per_year("0 4 * * 0")


# --- classification: the dangerous direction -------------------------------


@pytest.mark.parametrize("name", [
    "send_otp.py", "healthcheck.sh", "heartbeat.py", "process_payment.py",
    "send_alert_queue.py", "auth_refresh.sh", "fraud_scan.py",
])
def test_latency_sensitive_jobs_are_never_called_shiftable(name):
    verdict = classify_heuristic(job(name=name, runs=365))

    assert verdict.shiftable is False


def test_urgency_beats_a_shiftable_sounding_word():
    """'backup' in the name must not rescue an alerting job."""
    verdict = classify_heuristic(
        job(name="alert_backup_failure.sh", command="/opt/alert_backup_failure.sh"))

    assert verdict.shiftable is False
    assert "alert" in verdict.reason


def test_running_more_often_than_hourly_is_never_shiftable():
    verdict = classify_heuristic(
        job(name="sync_data.py", schedule="*/2 * * * *", runs=262800))

    assert verdict.shiftable is False
    assert "too often" in verdict.reason


# --- classification: the useful direction ----------------------------------


@pytest.mark.parametrize("name", [
    "backup_database.sh", "etl_warehouse.py", "cleanup_temp.sh",
    "generate_report.py", "retrain_model.py", "reindex_search.py",
])
def test_batch_jobs_running_daily_are_shiftable_with_confidence(name):
    verdict = classify_heuristic(job(name=name, runs=365))

    assert verdict.shiftable is True
    assert verdict.confidence == "high"


def test_an_unrecognised_but_rare_job_is_a_low_confidence_suggestion():
    verdict = classify_heuristic(job(name="do_the_thing.sh", runs=365))

    assert verdict.shiftable is True
    assert verdict.confidence == "low"


def test_an_unrecognised_frequent_job_is_left_alone():
    verdict = classify_heuristic(
        job(name="do_the_thing.sh", schedule="*/5 * * * *", runs=105120))

    assert verdict.shiftable is False


# --- savings ---------------------------------------------------------------


def test_saving_scales_with_how_often_the_job_runs():
    daily = S.estimate_annual_kg(job(runs=365), typical=400, reachable=200)
    weekly = S.estimate_annual_kg(job(runs=52), typical=400, reachable=200)

    assert daily > weekly
    assert daily == pytest.approx(weekly * 365 / 52)


def test_a_flat_grid_yields_no_saving():
    assert S.estimate_annual_kg(job(runs=365), typical=400, reachable=400) == 0


def test_saving_is_never_negative():
    """A reachable hour dirtier than typical means no benefit, not a loss."""
    assert S.estimate_annual_kg(job(runs=365), typical=200, reachable=400) == 0


# --- aws expressions -------------------------------------------------------


@pytest.mark.parametrize("expression,expected", [
    ("rate(1 hour)", 8760),
    ("rate(30 minutes)", 17520),
    ("rate(1 day)", 365),
    ("at(2026-09-11T09:00:00)", 1),
    ("cron(0 2 * * ? *)", 365),
])
def test_aws_schedule_expressions_are_converted_to_a_frequency(expression, expected):
    assert S._runs_from_aws_expression(expression) == expected


# --- claude path -----------------------------------------------------------


def test_claude_is_skipped_without_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    assert S.classify_with_claude([job()]) is None


def test_a_wrong_number_of_verdicts_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"text": '[{"shiftable": true, '
                                         '"confidence": "high", "reason": "x"}]'}]}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse())

    result = S.classify_with_claude([job(name="a"), job(name="b")])

    assert result is None  # two jobs, one verdict - refuse rather than guess
    assert "2 jobs" in capsys.readouterr().out


def test_a_claude_error_falls_back_rather_than_failing(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeResponse:
        status_code = 429
        text = "rate limited"

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse())

    assert S.classify_with_claude([job()]) is None
    assert "429" in capsys.readouterr().out


def test_a_good_claude_reply_is_used(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"text": '[{"shiftable": false, '
                                         '"confidence": "high", '
                                         '"reason": "serves live traffic"}]'}]}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse())

    verdicts = S.classify_with_claude([job()])

    assert len(verdicts) == 1
    assert verdicts[0].shiftable is False
    assert verdicts[0].engine == "claude"
