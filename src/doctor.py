"""Check that CarbonShift is actually able to do its job.

Scheduling is asynchronous: the job fires hours later, on a machine you are not
watching. So the failures that matter most are the silent ones -- a missing
container image, an ARN pointing at a resource somebody deleted. Nothing
surfaces those at the moment you schedule, and by the time the job fails to run
the schedule has already deleted itself.

This checks all of it up front, in one pass, and prints the command that fixes
each problem.

    python -m src.doctor            # everything
    python -m src.doctor --quick    # skip the live API and AWS calls
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m",
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    try:
        import colorama

        colorama.just_fix_windows_console()
    except Exception:
        BOLD = DIM = GREEN = RED = YELLOW = RESET = ""

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

MARK = {
    OK: f"{GREEN}OK  {RESET}",
    WARN: f"{YELLOW}WARN{RESET}",
    FAIL: f"{RED}FAIL{RESET}",
    SKIP: f"{DIM}--  {RESET}",
}

REPO = Path(__file__).resolve().parents[1]


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self.section: str | None = None

    def heading(self, title: str) -> None:
        print()
        print(f"{BOLD}  {title}{RESET}")

    def add(self, status: str, label: str, detail: str = "", fix: str = "") -> str:
        self.rows.append((status, label, detail, fix))
        line = f"  {MARK[status]}  {label}"
        if detail:
            line += f"  {DIM}{detail}{RESET}"
        print(line)
        if fix and status in (FAIL, WARN):
            for step in fix.split("\n"):
                print(f"           {BOLD}->{RESET} {step}")
        return status

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row[0] == status)


# --- machine ---------------------------------------------------------------


def check_machine(report: Report) -> bool:
    report.heading("Your machine")

    version = sys.version_info
    if version >= (3, 10):
        report.add(OK, "Python version", f"{version.major}.{version.minor}.{version.micro}")
    else:
        report.add(FAIL, "Python version",
                   f"{version.major}.{version.minor} found, need 3.10+",
                   "Install Python 3.10 or newer from python.org")

    missing = []
    for module, package in [("requests", "requests"), ("dotenv", "python-dotenv"),
                            ("matplotlib", "matplotlib")]:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        report.add(FAIL, "Core dependencies", f"missing: {', '.join(missing)}",
                   "pip install -r requirements.txt")
    else:
        report.add(OK, "Core dependencies", "requests, python-dotenv, matplotlib")

    try:
        import boto3  # noqa: F401

        has_boto3 = True
        report.add(OK, "boto3", "installed")
    except ImportError:
        has_boto3 = False
        report.add(WARN, "boto3", "not installed",
                   "Only needed to schedule real jobs.\n"
                   "pip install -r requirements.txt")

    env_path = REPO / ".env"
    if env_path.exists():
        report.add(OK, ".env file", str(env_path.name))
    else:
        report.add(FAIL, ".env file", "not found", "python setup.py")

    return has_boto3


# --- carbon data -----------------------------------------------------------


def check_carbon(report: Report, quick: bool) -> None:
    report.heading("Carbon data")

    key = os.getenv("ELECTRICITY_MAPS_API_KEY", "").strip()
    if not key or key == "your-electricity-maps-token":
        report.add(FAIL, "API key", "not set", "python setup.py")
        report.add(SKIP, "Live forecast", "needs an API key")
        return
    report.add(OK, "API key", f"set ({len(key)} characters)")

    try:
        from src.scheduler import resolve_zone

        zone = resolve_zone(os.getenv("AWS_REGION", "eu-central-1"))
        report.add(OK, "Carbon zone", zone)
    except Exception as exc:
        report.add(FAIL, "Carbon zone", str(exc)[:70],
                   "Set CARBON_ZONE in .env, or re-run python setup.py")
        return

    if quick:
        report.add(SKIP, "Live forecast", "skipped (--quick)")
        return

    try:
        from src.carbon_api import CarbonApiError, get_carbon_forecast

        forecast = get_carbon_forecast(zone, hours=24)
        values = [entry.carbon_intensity for entry in forecast]
        report.add(OK, "Live forecast",
                   f"{len(forecast)} points, {min(values):.0f}-{max(values):.0f} "
                   f"gCO2/kWh ({max(values) / min(values):.1f}x swing)")
    except CarbonApiError as exc:
        report.add(FAIL, "Live forecast", str(exc)[:80],
                   "Check your key and zone, or re-run python setup.py")
    except Exception as exc:  # pragma: no cover - defensive
        report.add(FAIL, "Live forecast", f"{type(exc).__name__}: {exc}"[:80])


# --- aws -------------------------------------------------------------------


def _role_name(arn: str) -> str:
    return arn.rsplit("/", 1)[-1] if arn else ""


def check_aws(report: Report, has_boto3: bool, quick: bool) -> None:
    report.heading("AWS")

    from src import scheduler as S

    configured = {
        "ECS_CLUSTER_ARN": S.ECS_CLUSTER_ARN,
        "WORKER_TASK_DEFINITION_ARN": S.WORKER_TASK_DEFINITION_ARN,
        "SCHEDULER_ROLE_ARN": S.SCHEDULER_ROLE_ARN,
        "WORKER_SUBNET_IDS": S.WORKER_SUBNET_IDS,
    }
    missing = [name for name, value in configured.items() if not value]

    if missing:
        report.add(FAIL, "Configuration", f"missing: {', '.join(missing)}",
                   "python infra/deploy.py\n"
                   "then paste the values it prints into .env")
    else:
        report.add(OK, "Configuration", "all ARNs present in .env")

    if not has_boto3:
        report.add(SKIP, "Credentials", "boto3 not installed")
        return
    if quick:
        report.add(SKIP, "Credentials", "skipped (--quick)")
        return

    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    region = os.getenv("AWS_REGION", "eu-central-1")
    try:
        account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        report.add(OK, "Credentials", f"account {account}, region {region}")
    except (NoCredentialsError, ClientError, BotoCoreError):
        report.add(WARN, "Credentials", "not found",
                   "aws configure\n"
                   "Without this you can still use --dry-run.")
        return

    def aws_check(label: str, fn, fix: str = "") -> None:
        try:
            detail = fn()
            report.add(OK, label, detail)
        except (ClientError, BotoCoreError) as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            report.add(FAIL, label, f"{code or type(exc).__name__}", fix)
        except Exception as exc:  # pragma: no cover - defensive
            report.add(FAIL, label, str(exc)[:70], fix)

    deploy_fix = "python infra/deploy.py"

    if configured["ECS_CLUSTER_ARN"]:
        ecs = boto3.client("ecs", region_name=region)

        def cluster():
            result = ecs.describe_clusters(clusters=[configured["ECS_CLUSTER_ARN"]])
            clusters = result.get("clusters", [])
            if not clusters:
                raise RuntimeError("cluster not found - was it deleted?")
            return f"{clusters[0]['clusterName']} ({clusters[0]['status']})"

        aws_check("ECS cluster", cluster, deploy_fix)

        def task_def():
            result = ecs.describe_task_definition(
                taskDefinition=configured["WORKER_TASK_DEFINITION_ARN"])
            td = result["taskDefinition"]
            return f"{td['family']}:{td['revision']} ({td['status']})"

        if configured["WORKER_TASK_DEFINITION_ARN"]:
            aws_check("Task definition", task_def, deploy_fix)

    if configured["SCHEDULER_ROLE_ARN"]:
        iam = boto3.client("iam")

        def role():
            name = _role_name(configured["SCHEDULER_ROLE_ARN"])
            iam.get_role(RoleName=name)
            return name

        aws_check("Scheduler IAM role", role, deploy_fix)

    # The important one: a missing image fails silently, hours later.
    image_uri = os.getenv("WORKER_IMAGE_URI", "").strip()
    if not image_uri:
        report.add(WARN, "Worker image", "WORKER_IMAGE_URI not set in .env", deploy_fix)
    else:
        ecr = boto3.client("ecr", region_name=region)
        repo_and_tag = image_uri.split("/", 1)[-1]
        repo_name, _, tag = repo_and_tag.partition(":")
        tag = tag or "latest"

        def image():
            ecr.describe_images(repositoryName=repo_name,
                                imageIds=[{"imageTag": tag}])
            return f"{repo_name}:{tag} present in ECR"

        aws_check(
            "Worker image", image,
            "The image is missing, so a scheduled job would fail silently.\n"
            f"docker build -t carbonshift-worker src/worker\n"
            f"docker tag carbonshift-worker {image_uri}\n"
            f"docker push {image_uri}",
        )


# --- docker ----------------------------------------------------------------


def check_docker(report: Report) -> None:
    report.heading("Docker")

    binary = shutil.which("docker")
    if not binary:
        report.add(WARN, "Docker CLI", "not on PATH",
                   "Only needed to build the worker image.\n"
                   "Install Docker Desktop, then open a new terminal.")
        return
    report.add(OK, "Docker CLI", "found")

    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        report.add(WARN, "Docker daemon", "did not respond",
                   "Start Docker Desktop and wait for 'Engine running'.")
        return

    if result.returncode == 0 and result.stdout.strip():
        report.add(OK, "Docker daemon", f"running, version {result.stdout.strip()}")
    else:
        report.add(WARN, "Docker daemon", "not running",
                   "Start Docker Desktop and wait for 'Engine running'.")


# --- history ---------------------------------------------------------------


def check_history(report: Report, has_boto3: bool, quick: bool) -> None:
    report.heading("Run history")

    try:
        from src.history import load_runs, verify_against_cloudwatch
    except ImportError:  # pragma: no cover
        report.add(SKIP, "Recorded runs", "history module unavailable")
        return

    runs = load_runs()
    if not runs:
        report.add(WARN, "Recorded runs", "none yet",
                   'python -m src.scheduler --payload "test" '
                   "--deadline-hours 12 --dry-run")
        return

    real = [r for r in runs if r.get("scheduled")]
    report.add(OK, "Recorded runs",
               f"{len(runs)} total, {len(real)} actually scheduled")

    if not real:
        report.add(SKIP, "Execution confirmed", "no real runs to confirm")
        return
    if quick or not has_boto3:
        report.add(SKIP, "Execution confirmed",
                   "skipped (--quick)" if quick else "boto3 not installed")
        return

    verified = verify_against_cloudwatch(runs)
    if verified.get("_error"):
        report.add(WARN, "Execution confirmed", verified["_error"][:70])
        return

    confirmed = 0
    for record in real:
        key = f"{record.get('payload')}|{record.get('chosen', {}).get('timestamp')}"
        if key in verified:
            confirmed += 1

    unconfirmed = len(real) - confirmed
    if unconfirmed == 0:
        report.add(OK, "Execution confirmed",
                   f"all {confirmed} scheduled runs found in CloudWatch")
    else:
        report.add(WARN, "Execution confirmed",
                   f"{confirmed} of {len(real)} confirmed, {unconfirmed} with no log",
                   "python -m src.history --verify   to see which\n"
                   "A run with no log was cancelled, or failed to start.")


# --- main ------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift-doctor",
        description="Check that CarbonShift can actually do its job.",
    )
    parser.add_argument("--quick", action="store_true",
                        help="Skip the live API and AWS calls.")
    args = parser.parse_args(argv)

    print()
    print(f"{BOLD}  CarbonShift health check{RESET}")
    if args.quick:
        print(f"  {DIM}quick mode - no network calls{RESET}")

    report = Report()
    has_boto3 = check_machine(report)
    check_carbon(report, args.quick)
    check_aws(report, has_boto3, args.quick)
    check_docker(report)
    check_history(report, has_boto3, args.quick)

    failures = report.count(FAIL)
    warnings = report.count(WARN)

    print()
    print("  " + "-" * 62)
    if failures == 0 and warnings == 0:
        print(f"  {GREEN}{BOLD}Everything works.{RESET}")
    elif failures == 0:
        print(f"  {GREEN}{BOLD}No failures.{RESET} {warnings} warning"
              f"{'s' if warnings != 1 else ''} above - none of them stop you "
              f"from scheduling.")
    else:
        print(f"  {RED}{BOLD}{failures} problem{'s' if failures != 1 else ''}{RESET}"
              f" to fix{f', {warnings} warning' if warnings else ''}"
              f"{'s' if warnings > 1 else ''}.")
        print(f"  {DIM}Each one above shows the command that fixes it.{RESET}")
    print("  " + "-" * 62)
    print()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
