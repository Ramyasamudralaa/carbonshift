"""Tests for src/doctor.py.

The doctor's value is that it reports accurately, so these pin the reporting
behaviour: statuses are counted correctly, a missing prerequisite produces a
FAIL with an actionable fix, and the exit code distinguishes "broken" from
"works, with caveats".
"""

from __future__ import annotations

import pytest

from src import doctor
from src.doctor import FAIL, OK, SKIP, WARN, Report


@pytest.fixture(autouse=True)
def no_colour(monkeypatch):
    """Strip ANSI codes so assertions read plainly."""
    for name in ("BOLD", "DIM", "GREEN", "RED", "YELLOW", "RESET"):
        monkeypatch.setattr(doctor, name, "")
    monkeypatch.setattr(doctor, "MARK",
                        {OK: "OK", WARN: "WARN", FAIL: "FAIL", SKIP: "--"})


# --- reporting -------------------------------------------------------------


def test_report_counts_each_status():
    report = Report()
    report.add(OK, "a")
    report.add(FAIL, "b")
    report.add(FAIL, "c")
    report.add(WARN, "d")

    assert report.count(OK) == 1
    assert report.count(FAIL) == 2
    assert report.count(WARN) == 1
    assert report.count(SKIP) == 0


def test_a_fix_is_printed_for_failures(capsys):
    report = Report()
    report.add(FAIL, "Worker image", "not in ECR", "docker push my-image:latest")

    out = capsys.readouterr().out
    assert "Worker image" in out
    assert "docker push my-image:latest" in out


def test_no_fix_is_printed_for_passing_checks(capsys):
    report = Report()
    report.add(OK, "Docker daemon", "running", "this should not appear")

    assert "this should not appear" not in capsys.readouterr().out


def test_multiline_fixes_print_each_step(capsys):
    report = Report()
    report.add(FAIL, "Worker image", "missing", "docker build .\ndocker push x")

    out = capsys.readouterr().out
    assert "docker build ." in out
    assert "docker push x" in out


# --- helpers ---------------------------------------------------------------


def test_role_name_is_extracted_from_an_arn():
    arn = "arn:aws:iam::111122223333:role/carbonshift-scheduler-role"
    assert doctor._role_name(arn) == "carbonshift-scheduler-role"


def test_role_name_of_empty_string_is_empty():
    assert doctor._role_name("") == ""


# --- carbon checks ---------------------------------------------------------


def test_missing_api_key_fails_and_points_at_setup(monkeypatch, capsys):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "")
    report = Report()

    doctor.check_carbon(report, quick=True)

    assert report.count(FAIL) == 1
    out = capsys.readouterr().out
    assert "python setup.py" in out


def test_placeholder_api_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "your-electricity-maps-token")
    report = Report()

    doctor.check_carbon(report, quick=True)

    assert report.count(FAIL) == 1


def test_a_key_without_network_checks_is_reported_but_not_tested(monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "a-real-looking-key")
    monkeypatch.setenv("CARBON_ZONE", "DE")
    report = Report()

    doctor.check_carbon(report, quick=True)

    assert report.count(FAIL) == 0
    assert report.count(SKIP) == 1  # live forecast skipped


# --- aws checks ------------------------------------------------------------


def test_missing_arns_fail_and_name_every_one(monkeypatch, capsys):
    for name in ("ECS_CLUSTER_ARN", "WORKER_TASK_DEFINITION_ARN",
                 "SCHEDULER_ROLE_ARN", "WORKER_SUBNET_IDS"):
        monkeypatch.setattr(f"src.scheduler.{name}", "")
    report = Report()

    doctor.check_aws(report, has_boto3=True, quick=True)

    out = capsys.readouterr().out
    assert "ECS_CLUSTER_ARN" in out
    assert "SCHEDULER_ROLE_ARN" in out
    assert "python infra/deploy.py" in out
    assert report.count(FAIL) >= 1


def test_aws_checks_are_skipped_without_boto3(monkeypatch):
    for name in ("ECS_CLUSTER_ARN", "WORKER_TASK_DEFINITION_ARN",
                 "SCHEDULER_ROLE_ARN", "WORKER_SUBNET_IDS"):
        monkeypatch.setattr(f"src.scheduler.{name}", "arn:aws:fake")
    report = Report()

    doctor.check_aws(report, has_boto3=False, quick=False)

    assert report.count(SKIP) >= 1


# --- history checks --------------------------------------------------------


def test_no_recorded_runs_warns_with_a_starter_command(monkeypatch, capsys):
    monkeypatch.setattr("src.history.load_runs", lambda *a, **k: [])
    report = Report()

    doctor.check_history(report, has_boto3=False, quick=True)

    assert report.count(WARN) == 1
    assert "--dry-run" in capsys.readouterr().out


def test_recorded_runs_separate_real_from_dry(monkeypatch, capsys):
    monkeypatch.setattr("src.history.load_runs", lambda *a, **k: [
        {"scheduled": True, "payload": "a", "chosen": {"timestamp": "t"}},
        {"scheduled": False, "payload": "b", "chosen": {"timestamp": "t"}},
        {"scheduled": False, "payload": "c", "chosen": {"timestamp": "t"}},
    ])
    report = Report()

    doctor.check_history(report, has_boto3=False, quick=True)

    assert "3 total, 1 actually scheduled" in capsys.readouterr().out


# --- exit codes ------------------------------------------------------------


def test_exit_code_is_zero_when_only_warnings(monkeypatch):
    monkeypatch.setattr(doctor, "check_machine", lambda r: True)
    monkeypatch.setattr(doctor, "check_carbon", lambda r, q: r.add(WARN, "x"))
    monkeypatch.setattr(doctor, "check_aws", lambda r, b, q: None)
    monkeypatch.setattr(doctor, "check_docker", lambda r: None)
    monkeypatch.setattr(doctor, "check_history", lambda r, b, q: None)

    assert doctor.main(["--quick"]) == 0


def test_exit_code_is_one_when_anything_failed(monkeypatch):
    monkeypatch.setattr(doctor, "check_machine", lambda r: True)
    monkeypatch.setattr(doctor, "check_carbon", lambda r, q: r.add(FAIL, "x"))
    monkeypatch.setattr(doctor, "check_aws", lambda r, b, q: None)
    monkeypatch.setattr(doctor, "check_docker", lambda r: None)
    monkeypatch.setattr(doctor, "check_history", lambda r, b, q: None)

    assert doctor.main(["--quick"]) == 1


def test_a_clean_bill_of_health_says_so(monkeypatch, capsys):
    for name in ("check_carbon", "check_aws", "check_docker", "check_history"):
        monkeypatch.setattr(doctor, name, lambda *a, **k: None)
    monkeypatch.setattr(doctor, "check_machine", lambda r: True)

    assert doctor.main(["--quick"]) == 0
    assert "Everything works" in capsys.readouterr().out
