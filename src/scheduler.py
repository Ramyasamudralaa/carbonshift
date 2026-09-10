"""CarbonShift scheduling logic.

Single responsibility: given a job payload, a region and a deadline, pick the
lowest-carbon hour that is still before that deadline, and register a one-time
AWS EventBridge Scheduler rule that fires an ECS RunTask at that moment.

The workload's own code is never touched -- only when it runs changes.

Run it directly:

    python -m src.scheduler --payload "nightly-etl" --deadline-hours 6
    python -m src.scheduler --payload "nightly-etl" --deadline-hours 6 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

try:  # pragma: no cover - import shim so the module works either way
    from src.carbon_api import ForecastEntry, get_carbon_forecast, summarise
except ImportError:  # running as a plain script from inside src/
    from carbon_api import ForecastEntry, get_carbon_forecast, summarise  # type: ignore

# .env must be loaded BEFORE the configuration constants below are evaluated,
# or they capture empty strings and every AWS value looks unset at runtime.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional
    pass


# --- Configuration ---------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")

# Resources provisioned by infra/deploy.py.
ECS_CLUSTER_ARN = os.getenv("ECS_CLUSTER_ARN", "")
WORKER_TASK_DEFINITION_ARN = os.getenv("WORKER_TASK_DEFINITION_ARN", "")
SCHEDULER_ROLE_ARN = os.getenv("SCHEDULER_ROLE_ARN", "")
WORKER_SUBNET_IDS = os.getenv("WORKER_SUBNET_IDS", "")
WORKER_SECURITY_GROUP_IDS = os.getenv("WORKER_SECURITY_GROUP_IDS", "")
WORKER_ASSIGN_PUBLIC_IP = os.getenv("WORKER_ASSIGN_PUBLIC_IP", "ENABLED")

# Scheduling behaviour.
SCHEDULE_NAME_PREFIX = "carbonshift-worker-run"
DEFAULT_DEADLINE_HOURS = 6
FORECAST_HOURS = 24
# EventBridge Scheduler rejects times in the past; keep a small safety margin.
MIN_SCHEDULE_LEAD_SECONDS = 120
# One-time schedules delete themselves after firing, so test runs do not pile up.
DELETE_SCHEDULE_AFTER_COMPLETION = True

# Carbon accounting for the demo worker (see spec section 5.1).
WORKER_VCPU = float(os.getenv("WORKER_VCPU", "2"))
WORKER_RUNTIME_HOURS = float(os.getenv("WORKER_RUNTIME_HOURS", "0.5"))
KWH_PER_VCPU_HOUR = float(os.getenv("KWH_PER_VCPU_HOUR", "0.0074"))

# Where the decision record is written, for demo/chart.py to read back.
RUN_RECORD_DIR = Path(os.getenv("CARBONSHIFT_RUN_DIR", "runs"))

# AWS region -> Electricity Maps zone. Override with CARBON_ZONE when your
# region is not listed, or when you want a different grid boundary.
AWS_REGION_TO_CARBON_ZONE = {
    "us-east-1": "US-MIDA-PJM",
    "us-east-2": "US-MIDW-MISO",
    "us-west-1": "US-CAL-CISO",
    "us-west-2": "US-NW-PACW",
    "ca-central-1": "CA-ON",
    "eu-west-1": "IE",
    "eu-west-2": "GB",
    "eu-west-3": "FR",
    "eu-central-1": "DE",
    "eu-north-1": "SE",
    "ap-south-1": "IN-WE",
    "ap-southeast-2": "AU-NSW",
    "ap-northeast-1": "JP-TK",
    "sa-east-1": "BR-CS",
}


# --- Errors ----------------------------------------------------------------


class SchedulerError(Exception):
    """Base class for scheduling failures."""


class NoSlotBeforeDeadlineError(SchedulerError):
    """No forecast hour falls inside the window ending at the deadline."""


class SchedulerConfigError(SchedulerError):
    """Required AWS configuration is missing."""


class AwsSchedulingError(SchedulerError):
    """AWS refused to create the schedule."""


# --- Carbon maths ----------------------------------------------------------


def estimate_energy_kwh(
    vcpu: float = WORKER_VCPU,
    runtime_hours: float = WORKER_RUNTIME_HOURS,
    kwh_per_vcpu_hour: float = KWH_PER_VCPU_HOUR,
) -> float:
    """Energy the worker is assumed to draw, in kWh."""
    return vcpu * runtime_hours * kwh_per_vcpu_hour


def emissions_grams(carbon_intensity: float, energy_kwh: float) -> float:
    """gCO2 emitted by drawing ``energy_kwh`` at ``carbon_intensity`` gCO2/kWh."""
    return carbon_intensity * energy_kwh


# --- Slot selection --------------------------------------------------------


def pick_best_slot(
    forecast: Sequence[ForecastEntry],
    deadline: datetime,
    earliest: datetime | None = None,
) -> ForecastEntry:
    """Return the lowest-carbon forecast entry that fits inside the window.

    The deadline is a hard exclusion, not a preference: any entry after it is
    dropped before the minimum is taken. ``earliest``, when given, drops entries
    before it too -- the scheduling path uses that to stay in the future.

    Raises:
        NoSlotBeforeDeadlineError: if nothing survives the filter.
    """
    if deadline.tzinfo is None:
        raise SchedulerError("deadline must be timezone-aware")
    if earliest is not None and earliest.tzinfo is None:
        raise SchedulerError("earliest must be timezone-aware")

    eligible = [entry for entry in forecast if entry.timestamp <= deadline]
    if earliest is not None:
        eligible = [entry for entry in eligible if entry.timestamp >= earliest]

    if not eligible:
        window_start = earliest.isoformat() if earliest else "now"
        raise NoSlotBeforeDeadlineError(
            f"No forecast slot available between {window_start} and the deadline "
            f"{deadline.isoformat()}. The deadline is too soon, or the forecast "
            "does not cover this window. Nothing was scheduled."
        )

    # min() keeps the first of any ties, and the list is chronological, so a tie
    # resolves to the earliest clean hour.
    return min(eligible, key=lambda entry: entry.carbon_intensity)


def baseline_slot(forecast: Sequence[ForecastEntry]) -> ForecastEntry:
    """The "run it immediately" comparison point: the earliest forecast hour."""
    if not forecast:
        raise SchedulerError("Cannot establish a baseline from an empty forecast")
    return min(forecast, key=lambda entry: entry.timestamp)


def resolve_zone(region: str, explicit_zone: str | None = None) -> str:
    """Work out which Electricity Maps zone to ask about."""
    zone = (explicit_zone or os.getenv("CARBON_ZONE", "")).strip()
    if zone:
        return zone
    mapped = AWS_REGION_TO_CARBON_ZONE.get(region)
    if not mapped:
        raise SchedulerConfigError(
            f"No carbon zone known for AWS region '{region}'. Set CARBON_ZONE in "
            "your .env to the Electricity Maps zone covering that region."
        )
    return mapped


# --- Decision record -------------------------------------------------------


@dataclass
class Decision:
    """Everything CarbonShift decided, and why. Serialised for the demo chart."""

    job_id: str
    payload: str
    region: str
    zone: str
    submitted_at: datetime
    deadline: datetime
    baseline: ForecastEntry
    chosen: ForecastEntry
    energy_kwh: float
    forecast: list[ForecastEntry]
    schedule_name: str
    schedule_arn: str | None = None
    scheduled: bool = False

    @property
    def baseline_emissions_g(self) -> float:
        return emissions_grams(self.baseline.carbon_intensity, self.energy_kwh)

    @property
    def chosen_emissions_g(self) -> float:
        return emissions_grams(self.chosen.carbon_intensity, self.energy_kwh)

    @property
    def co2_saved_g(self) -> float:
        return self.baseline_emissions_g - self.chosen_emissions_g

    @property
    def co2_saved_percent(self) -> float:
        if self.baseline_emissions_g == 0:
            return 0.0
        return 100.0 * self.co2_saved_g / self.baseline_emissions_g

    @property
    def delay(self) -> timedelta:
        return self.chosen.timestamp - self.baseline.timestamp

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "payload": self.payload,
            "region": self.region,
            "zone": self.zone,
            "submitted_at": self.submitted_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "energy_kwh": self.energy_kwh,
            "worker_vcpu": WORKER_VCPU,
            "worker_runtime_hours": WORKER_RUNTIME_HOURS,
            "baseline": {
                **self.baseline.as_dict(),
                "emissions_g": self.baseline_emissions_g,
            },
            "chosen": {
                **self.chosen.as_dict(),
                "emissions_g": self.chosen_emissions_g,
            },
            "co2_saved_g": self.co2_saved_g,
            "co2_saved_percent": self.co2_saved_percent,
            "delay_hours": self.delay.total_seconds() / 3600.0,
            "schedule_name": self.schedule_name,
            "schedule_arn": self.schedule_arn,
            "scheduled": self.scheduled,
            "forecast": [entry.as_dict() for entry in self.forecast],
        }

    def summary_lines(self) -> list[str]:
        return [
            f"job id            : {self.job_id}",
            f"payload           : {self.payload}",
            f"region / zone     : {self.region} / {self.zone}",
            f"deadline          : {self.deadline.isoformat()}",
            f"run immediately   : {self.baseline.timestamp.isoformat()} "
            f"at {self.baseline.carbon_intensity:.0f} gCO2/kWh "
            f"-> {self.baseline_emissions_g:.2f} g CO2",
            f"CarbonShift picks : {self.chosen.timestamp.isoformat()} "
            f"at {self.chosen.carbon_intensity:.0f} gCO2/kWh "
            f"-> {self.chosen_emissions_g:.2f} g CO2",
            f"delay             : {self.delay.total_seconds() / 3600.0:.1f} h",
            f"CO2 saved         : {self.co2_saved_g:.2f} g "
            f"({self.co2_saved_percent:.1f}%)",
        ]


def write_run_record(decision: Decision, directory: Path = RUN_RECORD_DIR) -> Path:
    """Persist the decision so demo/chart.py can draw it later."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{decision.job_id}.json"
    payload = json.dumps(decision.as_dict(), indent=2)
    path.write_text(payload, encoding="utf-8")
    (directory / "latest.json").write_text(payload, encoding="utf-8")
    return path


# --- AWS -------------------------------------------------------------------


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _require_aws_config() -> dict[str, Any]:
    """Collect and validate everything needed to create the schedule."""
    missing = [
        name
        for name, value in (
            ("ECS_CLUSTER_ARN", ECS_CLUSTER_ARN),
            ("WORKER_TASK_DEFINITION_ARN", WORKER_TASK_DEFINITION_ARN),
            ("SCHEDULER_ROLE_ARN", SCHEDULER_ROLE_ARN),
            ("WORKER_SUBNET_IDS", WORKER_SUBNET_IDS),
        )
        if not value
    ]
    if missing:
        raise SchedulerConfigError(
            "Missing AWS configuration: "
            + ", ".join(missing)
            + ". Run `python infra/deploy.py` and paste the values it prints "
            "into your .env, or use --dry-run to test the decision logic only."
        )
    return {
        "cluster_arn": ECS_CLUSTER_ARN,
        "task_definition_arn": WORKER_TASK_DEFINITION_ARN,
        "role_arn": SCHEDULER_ROLE_ARN,
        "subnets": _split_csv(WORKER_SUBNET_IDS),
        "security_groups": _split_csv(WORKER_SECURITY_GROUP_IDS),
        "assign_public_ip": WORKER_ASSIGN_PUBLIC_IP,
    }


def _schedule_expression(run_at: datetime) -> str:
    """EventBridge Scheduler one-time expression, in UTC, no offset suffix."""
    utc = run_at.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None)
    return f"at({utc.isoformat()})"


def create_ecs_schedule(
    schedule_name: str,
    run_at: datetime,
    job_payload: str,
    *,
    client: Any = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Register the one-time EventBridge Scheduler rule. Returns its ARN."""
    config = config or _require_aws_config()

    if client is None:
        import boto3  # imported lazily so the pure logic needs no AWS SDK

        client = boto3.client("scheduler", region_name=AWS_REGION)

    # The universal target (aws-sdk:ecs:runTask) takes the AWS SDK JSON shape,
    # which is PascalCase throughout -- including AwsvpcConfiguration. The
    # lowercase 'awsvpcConfiguration' that the ECS API itself uses is rejected
    # here with "field is not supported by api 'runTask'".
    network_configuration = {
        "AwsvpcConfiguration": {
            "Subnets": config["subnets"],
            "AssignPublicIp": config["assign_public_ip"],
        }
    }
    if config["security_groups"]:
        network_configuration["AwsvpcConfiguration"]["SecurityGroups"] = config[
            "security_groups"
        ]

    target_input = {
        "TaskDefinition": config["task_definition_arn"],
        "Cluster": config["cluster_arn"],
        "LaunchType": "FARGATE",
        "Count": 1,
        "NetworkConfiguration": network_configuration,
        "Overrides": {
            "ContainerOverrides": [
                {
                    "Name": "carbonshift-worker",
                    "Environment": [
                        {"Name": "CARBONSHIFT_JOB_PAYLOAD", "Value": job_payload},
                        {
                            "Name": "CARBONSHIFT_SCHEDULED_FOR",
                            "Value": run_at.astimezone(timezone.utc).isoformat(),
                        },
                    ],
                }
            ]
        },
    }

    request = {
        "Name": schedule_name,
        "ScheduleExpression": _schedule_expression(run_at),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Description": f"CarbonShift one-time run for job payload: {job_payload}",
        "Target": {
            "Arn": "arn:aws:scheduler:::aws-sdk:ecs:runTask",
            "RoleArn": config["role_arn"],
            "Input": json.dumps(target_input),
        },
    }
    if DELETE_SCHEDULE_AFTER_COMPLETION:
        request["ActionAfterCompletion"] = "DELETE"

    try:
        response = client.create_schedule(**request)
    except Exception as exc:  # narrowed below via botocore error shape
        raise AwsSchedulingError(_describe_aws_failure(exc, schedule_name)) from exc

    return response.get("ScheduleArn", "")


def _describe_aws_failure(exc: Exception, schedule_name: str) -> str:
    """Turn a boto3 error into a message that names the missing permission."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return f"Failed to create schedule '{schedule_name}': {exc}"

    error = response.get("Error", {})
    code = error.get("Code", "Unknown")
    message = error.get("Message", str(exc))

    hints = {
        "AccessDeniedException": (
            "Your AWS identity is missing scheduler:CreateSchedule, or "
            "iam:PassRole on SCHEDULER_ROLE_ARN."
        ),
        "ValidationException": (
            "AWS rejected the request shape. The most common causes are a "
            "schedule time in the past, or a malformed role/task-definition ARN."
        ),
        "ConflictException": (
            f"A schedule named '{schedule_name}' already exists. Delete it, or "
            "submit the job again to get a fresh name."
        ),
        "ResourceNotFoundException": (
            "One of ECS_CLUSTER_ARN, WORKER_TASK_DEFINITION_ARN or "
            "SCHEDULER_ROLE_ARN points at something that does not exist in "
            f"region {AWS_REGION}."
        ),
    }
    hint = hints.get(code, "")
    return (
        f"Failed to create schedule '{schedule_name}' in {AWS_REGION}. "
        f"AWS said [{code}]: {message}" + (f" -- {hint}" if hint else "")
    )


# --- Orchestration ---------------------------------------------------------


def schedule_job(
    payload: str,
    deadline: datetime,
    *,
    region: str = AWS_REGION,
    zone: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    forecast: Sequence[ForecastEntry] | None = None,
    scheduler_client: Any = None,
) -> Decision:
    """Fetch the forecast, choose the cleanest legal hour, and register the run.

    Args:
        payload: Identifier/description of the job being scheduled.
        deadline: Hard latest start time, timezone-aware.
        region: AWS region the job will run in.
        zone: Electricity Maps zone override; derived from ``region`` if omitted.
        dry_run: Decide and report, but do not touch AWS.
        now: Injectable clock, for tests.
        forecast: Injectable forecast, for tests.
        scheduler_client: Injectable boto3 scheduler client, for tests.
    """
    now = now or datetime.now(timezone.utc)
    if deadline.tzinfo is None:
        raise SchedulerError("deadline must be timezone-aware")
    if deadline <= now:
        raise NoSlotBeforeDeadlineError(
            f"Deadline {deadline.isoformat()} is not in the future "
            f"(now is {now.isoformat()}). Nothing was scheduled."
        )

    resolved_zone = resolve_zone(region, zone)
    entries = (
        list(forecast)
        if forecast is not None
        else get_carbon_forecast(resolved_zone, hours=FORECAST_HOURS)
    )

    earliest = now + timedelta(seconds=MIN_SCHEDULE_LEAD_SECONDS)
    chosen = pick_best_slot(entries, deadline=deadline, earliest=earliest)
    baseline = baseline_slot(entries)

    job_id = f"{SCHEDULE_NAME_PREFIX}-{uuid.uuid4().hex[:10]}"
    decision = Decision(
        job_id=job_id,
        payload=payload,
        region=region,
        zone=resolved_zone,
        submitted_at=now,
        deadline=deadline,
        baseline=baseline,
        chosen=chosen,
        energy_kwh=estimate_energy_kwh(),
        forecast=entries,
        schedule_name=job_id,
    )

    if not dry_run:
        decision.schedule_arn = create_ecs_schedule(
            decision.schedule_name,
            chosen.timestamp,
            payload,
            client=scheduler_client,
        )
        decision.scheduled = True

    return decision


# --- CLI -------------------------------------------------------------------


def _parse_deadline(args: argparse.Namespace, now: datetime) -> datetime:
    if args.deadline:
        text = args.deadline.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise SystemExit(
                f"Could not parse --deadline {args.deadline!r}. Use an ISO 8601 "
                "timestamp, e.g. 2026-09-09T18:00:00Z"
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return now + timedelta(hours=args.deadline_hours)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift",
        description=(
            "Schedule one flexible job to run at the lowest-carbon hour before "
            "its deadline."
        ),
    )
    parser.add_argument(
        "--payload",
        required=True,
        help="Identifier or description of the job being scheduled.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--deadline-hours",
        type=float,
        default=DEFAULT_DEADLINE_HOURS,
        help=f"Hours from now until the job must have started (default: {DEFAULT_DEADLINE_HOURS}).",
    )
    group.add_argument(
        "--deadline",
        help="Absolute ISO 8601 deadline, e.g. 2026-09-09T18:00:00Z.",
    )
    parser.add_argument("--region", default=AWS_REGION, help="AWS region.")
    parser.add_argument("--zone", default=None, help="Electricity Maps zone override.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Decide and report without creating the AWS schedule.",
    )
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    deadline = _parse_deadline(args, now)

    try:
        decision = schedule_job(
            payload=args.payload,
            deadline=deadline,
            region=args.region,
            zone=args.zone,
            dry_run=args.dry_run,
            now=now,
        )
    except SchedulerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # carbon API errors and anything else
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"forecast          : {summarise(decision.forecast)}")
    for line in decision.summary_lines():
        print(line)

    record_path = write_run_record(decision)
    print(f"run record        : {record_path}")

    if decision.scheduled:
        print(f"schedule created  : {decision.schedule_arn}")
    else:
        print("schedule created  : (dry run -- nothing was sent to AWS)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
