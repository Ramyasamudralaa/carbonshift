"""Tests for src/scheduler.py.

Covers spec check #3 (timestamps after the deadline are excluded) and check #4
(the true minimum-carbon slot is chosen), plus the carbon maths, the shape of
the EventBridge request, and the readability of AWS failures.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.carbon_api import ForecastEntry
from src.scheduler import (
    AwsSchedulingError,
    Decision,
    NoSlotBeforeDeadlineError,
    SchedulerConfigError,
    ShiftNotWorthwhileError,
    SchedulerError,
    _schedule_expression,
    baseline_slot,
    create_ecs_schedule,
    emissions_grams,
    estimate_energy_kwh,
    pick_best_slot,
    resolve_zone,
    schedule_job,
    write_run_record,
)

AWS_CONFIG = {
    "cluster_arn": "arn:aws:ecs:eu-central-1:111122223333:cluster/carbonshift-cluster",
    "task_definition_arn": "arn:aws:ecs:eu-central-1:111122223333:task-definition/carbonshift-worker:1",
    "role_arn": "arn:aws:iam::111122223333:role/carbonshift-scheduler-role",
    "subnets": ["subnet-aaa", "subnet-bbb"],
    "security_groups": ["sg-ccc"],
    "assign_public_ip": "ENABLED",
}


class FakeSchedulerClient:
    def __init__(self, raises=None):
        self.requests = []
        self._raises = raises

    def create_schedule(self, **kwargs):
        self.requests.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return {
            "ScheduleArn": "arn:aws:scheduler:eu-central-1:111122223333:schedule/default/"
            + kwargs["Name"]
        }


class FakeClientError(Exception):
    """Mimics botocore's ClientError shape without importing botocore."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


# --- Spec check #3: the deadline is a hard exclusion -----------------------


def test_excludes_slots_after_the_deadline(sample_forecast, base_time):
    """The cleanest hour overall (06:00, 120) sits outside a 4-hour deadline."""
    deadline = base_time + timedelta(hours=4)

    chosen = pick_best_slot(sample_forecast, deadline=deadline)

    assert chosen.timestamp <= deadline
    assert chosen.timestamp == base_time + timedelta(hours=4)
    assert chosen.carbon_intensity == 310.0  # not the global minimum of 120


def test_a_slot_exactly_on_the_deadline_is_allowed(sample_forecast, base_time):
    deadline = base_time + timedelta(hours=6)

    chosen = pick_best_slot(sample_forecast, deadline=deadline)

    assert chosen.timestamp == deadline
    assert chosen.carbon_intensity == 120.0


def test_no_slot_before_deadline_raises_rather_than_scheduling_wrong(
    sample_forecast, base_time
):
    deadline = base_time - timedelta(hours=1)  # entire forecast is after it

    with pytest.raises(NoSlotBeforeDeadlineError) as excinfo:
        pick_best_slot(sample_forecast, deadline=deadline)

    assert "Nothing was scheduled" in str(excinfo.value)


def test_earliest_bound_excludes_slots_already_in_the_past(sample_forecast, base_time):
    """The scheduling path must not pick an hour it cannot still book."""
    chosen = pick_best_slot(
        sample_forecast,
        deadline=base_time + timedelta(hours=11),
        earliest=base_time + timedelta(hours=8),
    )

    assert chosen.timestamp >= base_time + timedelta(hours=8)
    assert chosen.carbon_intensity == 240.0


def test_naive_deadline_is_rejected(sample_forecast):
    with pytest.raises(SchedulerError):
        pick_best_slot(sample_forecast, deadline=datetime(2026, 9, 9, 6, 0))


# --- Spec check #4: the true minimum is found ------------------------------


def test_finds_the_true_minimum_across_the_whole_window(sample_forecast, base_time):
    chosen = pick_best_slot(sample_forecast, deadline=base_time + timedelta(hours=11))

    assert chosen.carbon_intensity == 120.0
    assert chosen.carbon_intensity == min(e.carbon_intensity for e in sample_forecast)
    assert chosen.timestamp == base_time + timedelta(hours=6)


def test_minimum_is_found_regardless_of_input_ordering(sample_forecast, base_time):
    shuffled = list(reversed(sample_forecast))

    chosen = pick_best_slot(shuffled, deadline=base_time + timedelta(hours=11))

    assert chosen.carbon_intensity == 120.0


def test_ties_resolve_to_the_earliest_clean_hour(base_time):
    forecast = [
        ForecastEntry(base_time + timedelta(hours=0), 400.0),
        ForecastEntry(base_time + timedelta(hours=1), 150.0),
        ForecastEntry(base_time + timedelta(hours=2), 150.0),
    ]

    chosen = pick_best_slot(forecast, deadline=base_time + timedelta(hours=5))

    assert chosen.timestamp == base_time + timedelta(hours=1)


def test_baseline_is_the_earliest_forecast_hour(sample_forecast, base_time):
    baseline = baseline_slot(sample_forecast)

    assert baseline.timestamp == base_time
    assert baseline.carbon_intensity == 480.0


def test_baseline_of_an_empty_forecast_raises():
    with pytest.raises(SchedulerError):
        baseline_slot([])


# --- Carbon maths ----------------------------------------------------------


def test_energy_matches_the_spec_worked_example():
    """2 vCPU x 0.5 h x 0.0074 kWh/vCPU-h = 0.0074 kWh (spec section 5.1)."""
    assert estimate_energy_kwh(vcpu=2, runtime_hours=0.5) == pytest.approx(0.0074)


def test_emissions_match_the_spec_worked_example():
    energy = estimate_energy_kwh(vcpu=2, runtime_hours=0.5)

    assert emissions_grams(480, energy) == pytest.approx(3.552, abs=0.01)
    assert emissions_grams(190, energy) == pytest.approx(1.406, abs=0.01)


def test_decision_reports_the_saving(sample_forecast, base_time):
    decision = Decision(
        job_id="test",
        payload="demo",
        region="eu-central-1",
        zone="DE",
        submitted_at=base_time,
        deadline=base_time + timedelta(hours=11),
        baseline=sample_forecast[0],  # 480
        chosen=sample_forecast[6],  # 120
        energy_kwh=estimate_energy_kwh(vcpu=2, runtime_hours=0.5),
        forecast=sample_forecast,
        schedule_name="test",
    )

    assert decision.co2_saved_percent == pytest.approx(75.0)
    assert decision.co2_saved_g == pytest.approx(2.664, abs=0.01)
    assert decision.delay == timedelta(hours=6)


# --- Zone resolution -------------------------------------------------------


def test_explicit_zone_wins_over_the_region_mapping():
    assert resolve_zone("eu-central-1", "FR") == "FR"


def test_region_maps_to_a_zone_when_none_is_given(monkeypatch):
    monkeypatch.delenv("CARBON_ZONE", raising=False)
    assert resolve_zone("eu-central-1") == "DE"


def test_env_zone_is_used_when_no_explicit_zone(monkeypatch):
    monkeypatch.setenv("CARBON_ZONE", "IN-WE")
    assert resolve_zone("eu-central-1") == "IN-WE"


def test_unknown_region_without_a_zone_raises(monkeypatch):
    monkeypatch.delenv("CARBON_ZONE", raising=False)
    with pytest.raises(SchedulerConfigError) as excinfo:
        resolve_zone("me-south-1")

    assert "CARBON_ZONE" in str(excinfo.value)


# --- EventBridge request shape ---------------------------------------------


def test_schedule_expression_has_no_offset_suffix():
    run_at = datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc)

    assert _schedule_expression(run_at) == "at(2026-09-09T06:00:00)"


def test_schedule_expression_is_converted_to_utc():
    run_at = datetime(2026, 9, 9, 8, 0, tzinfo=timezone(timedelta(hours=2)))

    assert _schedule_expression(run_at) == "at(2026-09-09T06:00:00)"


def test_create_schedule_builds_a_valid_one_time_ecs_target():
    client = FakeSchedulerClient()
    run_at = datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc)

    arn = create_ecs_schedule(
        "carbonshift-worker-run-abc", run_at, "nightly-etl",
        client=client, config=AWS_CONFIG,
    )

    request = client.requests[0]
    assert request["ScheduleExpression"] == "at(2026-09-09T06:00:00)"
    assert request["ScheduleExpressionTimezone"] == "UTC"
    assert request["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert request["Target"]["Arn"] == "arn:aws:scheduler:::aws-sdk:ecs:runTask"
    assert request["Target"]["RoleArn"] == AWS_CONFIG["role_arn"]

    target_input = json.loads(request["Target"]["Input"])
    assert target_input["LaunchType"] == "FARGATE"
    assert target_input["TaskDefinition"] == AWS_CONFIG["task_definition_arn"]
    assert target_input["Cluster"] == AWS_CONFIG["cluster_arn"]
    # PascalCase is required by the universal target; the lowercase spelling the
    # ECS API uses is rejected with "field is not supported by api 'runTask'".
    assert "awsvpcConfiguration" not in target_input["NetworkConfiguration"]
    assert (
        target_input["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"]
        == AWS_CONFIG["subnets"]
    )
    assert (
        target_input["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"]
        == AWS_CONFIG["security_groups"]
    )
    assert arn.endswith("carbonshift-worker-run-abc")


def test_the_payload_reaches_the_container_as_an_env_override():
    client = FakeSchedulerClient()

    create_ecs_schedule(
        "job-1",
        datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc),
        "nightly-etl",
        client=client,
        config=AWS_CONFIG,
    )

    overrides = json.loads(client.requests[0]["Target"]["Input"])["Overrides"]
    environment = overrides["ContainerOverrides"][0]["Environment"]
    values = {item["Name"]: item["Value"] for item in environment}
    assert values["CARBONSHIFT_JOB_PAYLOAD"] == "nightly-etl"


# --- AWS failures are readable ---------------------------------------------


def test_access_denied_names_the_missing_permission():
    client = FakeSchedulerClient(
        raises=FakeClientError("AccessDeniedException", "not authorized")
    )

    with pytest.raises(AwsSchedulingError) as excinfo:
        create_ecs_schedule(
            "job-1",
            datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc),
            "demo",
            client=client,
            config=AWS_CONFIG,
        )

    message = str(excinfo.value)
    assert "scheduler:CreateSchedule" in message
    assert "iam:PassRole" in message


def test_missing_resource_names_the_env_vars_to_check():
    client = FakeSchedulerClient(
        raises=FakeClientError("ResourceNotFoundException", "no such cluster")
    )

    with pytest.raises(AwsSchedulingError) as excinfo:
        create_ecs_schedule(
            "job-1",
            datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc),
            "demo",
            client=client,
            config=AWS_CONFIG,
        )

    assert "ECS_CLUSTER_ARN" in str(excinfo.value)


# --- End-to-end decision (no AWS, no network) ------------------------------


def test_schedule_job_dry_run_decides_without_touching_aws(sample_forecast, base_time):
    decision = schedule_job(
        payload="nightly-etl",
        deadline=base_time + timedelta(hours=11),
        region="eu-central-1",
        zone="DE",
        dry_run=True,
        now=base_time,
        forecast=sample_forecast,
    )

    assert decision.scheduled is False
    assert decision.schedule_arn is None
    assert decision.chosen.carbon_intensity == 120.0
    assert decision.co2_saved_percent == pytest.approx(75.0)


def test_schedule_job_creates_the_schedule_when_not_a_dry_run(
    sample_forecast, base_time, monkeypatch
):
    monkeypatch.setattr("src.scheduler._require_aws_config", lambda: AWS_CONFIG)
    client = FakeSchedulerClient()

    decision = schedule_job(
        payload="nightly-etl",
        deadline=base_time + timedelta(hours=11),
        zone="DE",
        now=base_time,
        forecast=sample_forecast,
        scheduler_client=client,
    )

    assert decision.scheduled is True
    assert len(client.requests) == 1
    assert client.requests[0]["ScheduleExpression"] == "at(2026-09-09T06:00:00)"


def test_a_deadline_in_the_past_is_refused(sample_forecast, base_time):
    with pytest.raises(NoSlotBeforeDeadlineError):
        schedule_job(
            payload="demo",
            deadline=base_time - timedelta(hours=1),
            zone="DE",
            dry_run=True,
            now=base_time,
            forecast=sample_forecast,
        )


def test_a_deadline_inside_the_lead_time_is_refused(sample_forecast, base_time):
    """One minute is not enough time for EventBridge to book anything."""
    with pytest.raises(NoSlotBeforeDeadlineError):
        schedule_job(
            payload="demo",
            deadline=base_time + timedelta(seconds=60),
            zone="DE",
            dry_run=True,
            now=base_time,
            forecast=sample_forecast,
        )


def test_missing_aws_config_names_every_missing_variable(monkeypatch):
    for name in (
        "ECS_CLUSTER_ARN",
        "WORKER_TASK_DEFINITION_ARN",
        "SCHEDULER_ROLE_ARN",
        "WORKER_SUBNET_IDS",
    ):
        monkeypatch.setattr(f"src.scheduler.{name}", "")

    from src.scheduler import _require_aws_config

    with pytest.raises(SchedulerConfigError) as excinfo:
        _require_aws_config()

    message = str(excinfo.value)
    assert "ECS_CLUSTER_ARN" in message
    assert "SCHEDULER_ROLE_ARN" in message
    assert "infra/deploy.py" in message


# --- Run record ------------------------------------------------------------


def test_run_record_round_trips_for_the_chart(sample_forecast, base_time, tmp_path):
    decision = schedule_job(
        payload="nightly-etl",
        deadline=base_time + timedelta(hours=11),
        zone="DE",
        dry_run=True,
        now=base_time,
        forecast=sample_forecast,
    )

    path = write_run_record(decision, directory=tmp_path)
    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["chosen"]["carbon_intensity"] == 120.0
    assert record["baseline"]["carbon_intensity"] == 480.0
    assert len(record["forecast"]) == len(sample_forecast)
    assert (tmp_path / "latest.json").exists()


# --- Regression: .env must load before the config constants ----------------


def test_env_file_is_loaded_before_the_config_constants():
    """scheduler.py captures its AWS config into module constants at import.

    If load_dotenv() runs after that -- as it did when it lived inside main()
    -- every constant captures an empty string, and a correctly populated .env
    still fails with 'Missing AWS configuration'. The ordering in the source is
    the thing that makes it work, so assert on the ordering directly.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "scheduler.py"
    ).read_text(encoding="utf-8")

    load_at = source.index("load_dotenv()")
    first_constant_at = source.index("ECS_CLUSTER_ARN = os.getenv")

    assert load_at < first_constant_at, (
        "load_dotenv() must run before the configuration constants are "
        "evaluated, or values from .env are captured as empty strings."
    )


def test_config_constants_are_read_from_the_environment(monkeypatch):
    """A fresh import picks up whatever the environment holds at that moment."""
    import importlib
    import sys

    monkeypatch.setenv(
        "ECS_CLUSTER_ARN", "arn:aws:ecs:eu-central-1:111122223333:cluster/from-env"
    )
    sys.modules.pop("src.scheduler", None)
    try:
        module = importlib.import_module("src.scheduler")
        assert module.ECS_CLUSTER_ARN.endswith("cluster/from-env")
    finally:
        sys.modules.pop("src.scheduler", None)
        importlib.import_module("src.scheduler")


# --- Guard: never shift a job into a dirtier hour --------------------------


def _rising_forecast(base_time):
    """Cleanest hour is now; every later hour is worse. The 1h-deadline case."""
    return [
        ForecastEntry(base_time + timedelta(hours=0), 462.0),
        ForecastEntry(base_time + timedelta(hours=1), 497.0),
        ForecastEntry(base_time + timedelta(hours=2), 513.0),
    ]


def test_refuses_to_shift_into_a_dirtier_hour(base_time):
    with pytest.raises(ShiftNotWorthwhileError) as excinfo:
        schedule_job(
            payload="demo",
            deadline=base_time + timedelta(hours=2),
            zone="DE",
            dry_run=True,
            now=base_time,
            forecast=_rising_forecast(base_time),
        )

    message = str(excinfo.value)
    assert "497" in message and "462" in message  # names both sides
    assert "Nothing" in message and "was scheduled" in message


def test_refuses_when_the_best_slot_merely_ties_the_baseline(base_time):
    """A shift with no carbon benefit is still a delay for nothing."""
    forecast = [
        ForecastEntry(base_time + timedelta(hours=0), 400.0),
        ForecastEntry(base_time + timedelta(hours=1), 400.0),
    ]

    with pytest.raises(ShiftNotWorthwhileError):
        schedule_job(
            payload="demo",
            deadline=base_time + timedelta(hours=1),
            zone="DE",
            dry_run=True,
            now=base_time,
            forecast=forecast,
        )


def test_no_aws_call_is_made_when_the_shift_is_refused(base_time, monkeypatch):
    monkeypatch.setattr("src.scheduler._require_aws_config", lambda: AWS_CONFIG)
    client = FakeSchedulerClient()

    with pytest.raises(ShiftNotWorthwhileError):
        schedule_job(
            payload="demo",
            deadline=base_time + timedelta(hours=2),
            zone="DE",
            now=base_time,
            forecast=_rising_forecast(base_time),
            scheduler_client=client,
        )

    assert client.requests == []


def test_a_genuinely_cleaner_hour_still_schedules(sample_forecast, base_time):
    decision = schedule_job(
        payload="demo",
        deadline=base_time + timedelta(hours=11),
        zone="DE",
        dry_run=True,
        now=base_time,
        forecast=sample_forecast,
    )

    assert decision.chosen.carbon_intensity < decision.baseline.carbon_intensity
    assert decision.co2_saved_g > 0


def test_the_guard_can_be_turned_off_deliberately(base_time):
    decision = schedule_job(
        payload="demo",
        deadline=base_time + timedelta(hours=2),
        zone="DE",
        dry_run=True,
        now=base_time,
        forecast=_rising_forecast(base_time),
        require_improvement=False,
    )

    assert decision.co2_saved_g < 0  # the old behaviour, now opt-in


def test_the_cli_treats_a_refused_shift_as_success_not_an_error(base_time, capsys, monkeypatch):
    """Exit 0: 'run it now' is a correct answer, not a malfunction."""
    import src.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "get_carbon_forecast",
        lambda zone, hours=24: _rising_forecast(
            datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        ),
    )

    exit_code = scheduler_module.main(
        ["--payload", "demo", "--deadline-hours", "2", "--dry-run"]
    )

    assert exit_code == 0
    assert "NO SHIFT SCHEDULED" in capsys.readouterr().out
