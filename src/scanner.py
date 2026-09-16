"""Find the jobs in your infrastructure that could be carbon-shifted.

CarbonShift can move one job you already know is flexible. The harder question
is which of the forty jobs in your crontab are flexible at all -- that means
reading each one and judging whether anybody is waiting on its output. A
nightly database backup can slip six hours. An OTP sender cannot.

    python -m src.scanner --cron /etc/crontab
    python -m src.scanner --cron mycrontab --engine claude
    python -m src.scanner --aws                     # your EventBridge schedules
    python -m src.scanner --cron - < crontab.txt    # from stdin

Two classifiers:

  heuristic  the default. Rules over the command text and the run frequency.
             No API key, no network, no energy cost.
  claude     an LLM reads each job. Better on ambiguous names, needs
             ANTHROPIC_API_KEY.

The LLM runs once per job definition, not once per execution. A nightly job
classified once is reused for every one of its 365 runs a year, so the energy
spent classifying amortises to approximately nothing -- which matters, because
an LLM call that cost more carbon than the shift saved would defeat the point.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

REPO = Path(__file__).resolve().parents[1]

# Classification runs once per job definition, so the cheapest capable model is
# the right one -- and on a carbon-aware tool, the least wasteful one.
CLAUDE_MODEL = os.getenv("CARBONSHIFT_CLAUDE_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Assumed compute per run, matching the worker defaults.
WORKER_VCPU = float(os.getenv("WORKER_VCPU", "2"))
WORKER_RUNTIME_HOURS = float(os.getenv("WORKER_RUNTIME_HOURS", "0.5"))
KWH_PER_VCPU_HOUR = float(os.getenv("KWH_PER_VCPU_HOUR", "0.0074"))

# How many of the cleanest hours a flexible job is assumed to be able to reach.
FLEXIBLE_WINDOW_HOURS = 6

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m",
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    try:
        import colorama

        colorama.just_fix_windows_console()
    except Exception:
        BOLD = DIM = GREEN = RED = YELLOW = RESET = ""


# --- model -----------------------------------------------------------------


@dataclass
class Job:
    name: str
    schedule: str
    command: str
    runs_per_year: int
    source: str = "cron"


@dataclass
class Verdict:
    shiftable: bool
    confidence: str          # high | medium | low
    reason: str
    engine: str = "heuristic"


@dataclass
class Finding:
    job: Job
    verdict: Verdict
    annual_kg_saved: float = 0.0


# --- cron parsing ----------------------------------------------------------

CRON_ALIASES = {
    "@yearly": ("0 0 1 1 *", 1), "@annually": ("0 0 1 1 *", 1),
    "@monthly": ("0 0 1 * *", 12), "@weekly": ("0 0 * * 0", 52),
    "@daily": ("0 0 * * *", 365), "@midnight": ("0 0 * * *", 365),
    "@hourly": ("0 * * * *", 8760), "@reboot": ("@reboot", 1),
}


# A cron field is a star, a wildcard, numbers with ranges and steps, or a
# three-letter day/month name. Anything else means the line is not a crontab
# entry at all -- prose would otherwise parse as a job with a nonsense schedule.
CRON_FIELD = re.compile(
    r"^(\*|\?|[0-9*,/\-]+|[A-Za-z]{3}(-[A-Za-z]{3})?)$")


def valid_cron_fields(fields: Iterable[str]) -> bool:
    fields = list(fields)
    return len(fields) == 5 and all(CRON_FIELD.match(f) for f in fields)


def _field_count(spec: str, total: int) -> int:
    """Roughly how many distinct values a single cron field expands to."""
    # AWS writes '?' where cron writes '*' for "no specific value".
    if spec in ("*", "?"):
        return total
    count = 0
    for part in spec.split(","):
        if "/" in part:
            base, _, step = part.partition("/")
            try:
                stride = int(step)
            except ValueError:
                stride = 1
            span = total if base in ("*", "") else _field_count(base, total)
            count += max(1, span // max(1, stride))
        elif "-" in part:
            lo, _, hi = part.partition("-")
            try:
                count += max(1, int(hi) - int(lo) + 1)
            except ValueError:
                count += 1
        else:
            count += 1
    return max(1, count)


def runs_per_year(schedule: str) -> int:
    """Approximate annual executions. Precision is not the point; scale is."""
    schedule = schedule.strip()
    if schedule in CRON_ALIASES:
        expr, annual = CRON_ALIASES[schedule]
        if expr == "@reboot":
            return annual
        schedule = expr

    fields = schedule.split()
    if len(fields) < 5:
        return 365
    minute, hour, dom, month, dow = fields[:5]

    per_day = _field_count(minute, 60) * _field_count(hour, 24)
    days = 365
    if dow not in ("*", "?"):
        days = min(days, _field_count(dow, 7) * 52)
    if dom not in ("*", "?"):
        days = min(days, _field_count(dom, 31) * 12)
    if month not in ("*", "?"):
        days = min(days, int(days * _field_count(month, 12) / 12))

    return max(1, int(per_day * days))


def parse_crontab(text: str) -> list[Job]:
    jobs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[A-Z_]+\s*=", line):  # MAILTO=, PATH= and friends
            continue

        if line.startswith("@"):
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            schedule, command = parts[0], parts[1]
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            if not valid_cron_fields(parts[:5]):
                continue          # prose, or a line we do not understand
            schedule = " ".join(parts[:5])
            command = parts[5]

        name = _name_from_command(command)
        jobs.append(Job(name=name, schedule=schedule, command=command,
                        runs_per_year=runs_per_year(schedule)))
    return jobs


def _name_from_command(command: str) -> str:
    tokens = [t for t in command.split() if not t.startswith("-")]
    for token in tokens:
        base = os.path.basename(token)
        if base and not base.startswith(("/", "&", ">")) and "." in base or "_" in base:
            return base
    return os.path.basename(tokens[0]) if tokens else command[:30]


# --- heuristic classifier --------------------------------------------------

SHIFTABLE_WORDS = {
    "backup": "a backup", "archive": "an archive", "etl": "an ETL job",
    "batch": "a batch job", "report": "report generation",
    "digest": "a digest", "cleanup": "a cleanup", "clean": "a cleanup",
    "prune": "a prune", "vacuum": "a database vacuum",
    "reindex": "a reindex", "index": "an indexing job",
    "sync": "a sync", "export": "an export", "import": "an import",
    "dump": "a data dump", "snapshot": "a snapshot",
    "compress": "compression", "aggregate": "an aggregation",
    "rollup": "a rollup", "train": "model training",
    "retrain": "model retraining", "analytics": "analytics processing",
    "warehouse": "a warehouse load", "ingest": "an ingest",
    "rotate": "log rotation", "logrotate": "log rotation",
    "billing": "billing computation", "invoice": "invoice generation",
    "recompute": "a recomputation", "rebuild": "a rebuild",
}

URGENT_WORDS = {
    "otp": "one-time passcodes", "auth": "authentication",
    "login": "login handling", "session": "session handling",
    "payment": "payments", "checkout": "checkout",
    "alert": "alerting", "alarm": "alarming", "page": "paging",
    "notify": "notifications", "notification": "notifications",
    "sms": "SMS delivery", "push": "push delivery",
    "health": "health checking", "healthcheck": "health checking",
    "heartbeat": "heartbeats", "ping": "liveness probing",
    "monitor": "monitoring", "watchdog": "a watchdog",
    "realtime": "real-time processing", "stream": "stream processing",
    "webhook": "webhook delivery", "api": "an API process",
    "serve": "a serving process", "queue": "queue consumption",
    "fraud": "fraud detection", "security": "security response",
}


def classify_heuristic(job: Job) -> Verdict:
    text = f"{job.name} {job.command}".lower()
    words = set(re.findall(r"[a-z]+", text))

    urgent_hits = [desc for word, desc in URGENT_WORDS.items() if word in words]
    shift_hits = [desc for word, desc in SHIFTABLE_WORDS.items() if word in words]

    # Frequency is the strongest single signal. Anything running more often
    # than hourly is almost certainly keeping something alive, not batching.
    very_frequent = job.runs_per_year > 8760 * 1.5   # more often than hourly
    infrequent = job.runs_per_year <= 366            # daily or rarer

    if urgent_hits:
        return Verdict(False, "high",
                       f"looks like {urgent_hits[0]} - something is waiting on it")
    if very_frequent:
        return Verdict(False, "high",
                       f"runs ~{job.runs_per_year:,}x a year, too often to be batch work")
    if shift_hits and infrequent:
        return Verdict(True, "high",
                       f"looks like {shift_hits[0]}, running {job.runs_per_year}x a year")
    if shift_hits:
        return Verdict(True, "medium",
                       f"looks like {shift_hits[0]}, but runs {job.runs_per_year:,}x a year")
    if infrequent:
        return Verdict(True, "low",
                       f"runs only {job.runs_per_year}x a year, but the purpose is unclear")
    return Verdict(False, "low",
                   f"runs {job.runs_per_year:,}x a year and the purpose is unclear")


# --- claude classifier -----------------------------------------------------

SYSTEM_PROMPT = """You classify scheduled computing jobs by whether their \
execution time can be deferred by a few hours to run when the electricity grid \
is cleaner.

Shiftable: nobody is waiting on the output within the hour. Backups, ETL, \
report generation, cleanup, model training, data exports, analytics.

Not shiftable: something or someone depends on it promptly. Health checks, \
monitoring, alerting, authentication, payments, notifications, anything serving \
live traffic, anything feeding a real-time system.

When a job runs more often than hourly it is almost never shiftable.

Reply with a JSON array only. One object per job, in the order given:
[{"shiftable": true, "confidence": "high", "reason": "<12 words or fewer>"}]
confidence is "high", "medium" or "low"."""


def classify_with_claude(jobs: list[Job]) -> list[Verdict] | None:
    """Returns None when Claude cannot be used, so the caller can fall back."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    import requests

    listing = "\n".join(
        f"{i + 1}. name={job.name!r} schedule={job.schedule!r} "
        f"runs_per_year={job.runs_per_year} command={job.command[:160]!r}"
        for i, job in enumerate(jobs)
    )

    try:
        response = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 2000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user",
                              "content": f"Classify these {len(jobs)} jobs:\n\n{listing}"}],
            },
            timeout=60,
        )
    except Exception as exc:
        print(f"  {YELLOW}Claude unreachable ({exc}); using the heuristic.{RESET}")
        return None

    if response.status_code != 200:
        print(f"  {YELLOW}Claude returned HTTP {response.status_code}; "
              f"using the heuristic.{RESET}")
        return None

    try:
        text = "".join(block.get("text", "")
                       for block in response.json().get("content", []))
        match = re.search(r"\[.*\]", text, re.S)
        parsed = json.loads(match.group(0) if match else text)
        verdicts = [
            Verdict(bool(item.get("shiftable")),
                    str(item.get("confidence", "medium")),
                    str(item.get("reason", ""))[:90],
                    engine="claude")
            for item in parsed
        ]
    except Exception:
        print(f"  {YELLOW}Could not read Claude's reply; using the heuristic.{RESET}")
        return None

    if len(verdicts) != len(jobs):
        print(f"  {YELLOW}Claude returned {len(verdicts)} verdicts for "
              f"{len(jobs)} jobs; using the heuristic.{RESET}")
        return None
    return verdicts


# --- savings ---------------------------------------------------------------


def grid_profile(zone: str | None = None) -> tuple[float, float, str] | None:
    """(typical intensity, reachable intensity, zone) from a live forecast."""
    try:
        from src.carbon_api import get_carbon_forecast
        from src.scheduler import resolve_zone

        resolved = zone or resolve_zone(os.getenv("AWS_REGION", "eu-central-1"))
        values = sorted(e.carbon_intensity
                        for e in get_carbon_forecast(resolved, hours=24))
    except Exception:
        return None
    if not values:
        return None

    typical = sum(values) / len(values)
    window = values[:min(FLEXIBLE_WINDOW_HOURS, len(values))]
    reachable = sum(window) / len(window)
    return typical, reachable, resolved


def estimate_annual_kg(job: Job, typical: float, reachable: float) -> float:
    energy_per_run = WORKER_VCPU * WORKER_RUNTIME_HOURS * KWH_PER_VCPU_HOUR
    saved_g_per_run = energy_per_run * (typical - reachable)
    return max(0.0, saved_g_per_run * job.runs_per_year / 1000.0)


# --- aws source ------------------------------------------------------------


def jobs_from_aws() -> list[Job]:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        print(f"  {RED}boto3 is not installed.{RESET}")
        return []

    region = os.getenv("AWS_REGION", "eu-central-1")
    jobs: list[Job] = []
    try:
        client = boto3.client("scheduler", region_name=region)
        for page in client.get_paginator("list_schedules").paginate():
            for entry in page.get("Schedules", []):
                detail = client.get_schedule(Name=entry["Name"],
                                             GroupName=entry.get("GroupName", "default"))
                expression = detail.get("ScheduleExpression", "")
                jobs.append(Job(
                    name=entry["Name"],
                    schedule=expression,
                    command=detail.get("Description", "") or entry["Name"],
                    runs_per_year=_runs_from_aws_expression(expression),
                    source="eventbridge",
                ))
    except NoCredentialsError:
        print(f"  {RED}No AWS credentials. Run 'aws configure'.{RESET}")
    except (ClientError, BotoCoreError) as exc:
        print(f"  {RED}Could not list schedules: {exc}{RESET}")
    return jobs


def _runs_from_aws_expression(expression: str) -> int:
    expression = expression.strip()
    if expression.startswith("at("):
        return 1
    match = re.match(r"rate\((\d+)\s+(minute|hour|day)s?\)", expression)
    if match:
        every, unit = int(match.group(1)), match.group(2)
        per_year = {"minute": 525600, "hour": 8760, "day": 365}[unit]
        return max(1, per_year // max(1, every))
    match = re.match(r"cron\((.*)\)", expression)
    if match:
        fields = match.group(1).split()
        if len(fields) >= 5:
            return runs_per_year(" ".join(fields[:5]))
    return 365


# --- output ----------------------------------------------------------------


def render(findings: list[Finding], profile, engine: str) -> None:
    shiftable = [f for f in findings if f.verdict.shiftable]
    fixed = [f for f in findings if not f.verdict.shiftable]
    total_kg = sum(f.annual_kg_saved for f in shiftable)

    print()
    print(f"{BOLD}  Shiftable workload scan{RESET}  {DIM}engine: {engine}{RESET}")
    print("  " + "-" * 74)

    if shiftable:
        print(f"  {GREEN}{BOLD}SHIFTABLE{RESET}")
        for finding in sorted(shiftable, key=lambda f: -f.annual_kg_saved):
            job, verdict = finding.job, finding.verdict
            saving = (f"{finding.annual_kg_saved:6.3f} kg/yr"
                      if profile else f"{job.runs_per_year:>7,} runs/yr")
            print(f"    {job.name[:26]:<26} {saving}  {DIM}{verdict.confidence}{RESET}")
            print(f"      {DIM}{verdict.reason}{RESET}")
        print()

    if fixed:
        print(f"  {DIM}{BOLD}NOT SHIFTABLE{RESET}")
        for finding in fixed:
            job, verdict = finding.job, finding.verdict
            print(f"    {DIM}{job.name[:26]:<26} {verdict.reason}{RESET}")
        print()

    print("  " + "-" * 74)
    print(f"  {len(shiftable)} of {len(findings)} jobs look shiftable.")
    if profile:
        typical, reachable, zone = profile
        print(f"  {BOLD}{GREEN}Potential saving: {total_kg:.3f} kg CO2 per year"
              f"{RESET}")
        print(f"  {DIM}Assumes {WORKER_VCPU:.0f} vCPU x {WORKER_RUNTIME_HOURS} h per run "
              f"on the {zone} grid,{RESET}")
        print(f"  {DIM}moving from a typical {typical:.0f} to a reachable "
              f"{reachable:.0f} gCO2/kWh.{RESET}")
    else:
        print(f"  {DIM}No live forecast available, so no saving was estimated.{RESET}")
    print("  " + "-" * 74)
    print()

    if shiftable:
        best = max(shiftable, key=lambda f: f.annual_kg_saved)
        print(f"  Try the biggest one:")
        print(f"    python -m src.scheduler --payload \"{best.job.name}\" "
              f"--deadline-hours 12 --dry-run")
        print()
    print(f"  {DIM}These are suggestions from names and frequencies, not a "
          f"guarantee.{RESET}")
    print(f"  {DIM}Check each one against what you know before shifting it.{RESET}")
    print()


# --- main ------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift-scanner",
        description="Find which scheduled jobs could be carbon-shifted.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cron", help="A crontab file, or - for stdin.")
    source.add_argument("--aws", action="store_true",
                        help="Read your EventBridge schedules.")
    parser.add_argument("--engine", choices=["heuristic", "claude"],
                        default="heuristic",
                        help="How to classify. claude needs ANTHROPIC_API_KEY.")
    parser.add_argument("--zone", help="Grid zone for the saving estimate.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead.")
    args = parser.parse_args(argv)

    if args.aws:
        jobs = jobs_from_aws()
    elif args.cron == "-":
        jobs = parse_crontab(sys.stdin.read())
    else:
        path = Path(args.cron)
        if not path.exists():
            print(f"No such file: {path}", file=sys.stderr)
            return 1
        jobs = parse_crontab(path.read_text(encoding="utf-8", errors="replace"))

    if not jobs:
        print()
        print("  No scheduled jobs found to look at.")
        print()
        return 1

    engine = args.engine
    verdicts = None
    if engine == "claude":
        verdicts = classify_with_claude(jobs)
        if verdicts is None:
            if not os.getenv("ANTHROPIC_API_KEY", "").strip():
                print(f"  {YELLOW}ANTHROPIC_API_KEY is not set; using the "
                      f"heuristic instead.{RESET}")
            engine = "heuristic"
    if verdicts is None:
        verdicts = [classify_heuristic(job) for job in jobs]

    profile = grid_profile(args.zone)
    findings = []
    for job, verdict in zip(jobs, verdicts):
        saving = (estimate_annual_kg(job, profile[0], profile[1])
                  if profile and verdict.shiftable else 0.0)
        findings.append(Finding(job=job, verdict=verdict, annual_kg_saved=saving))

    if args.json:
        print(json.dumps([{
            "name": f.job.name, "schedule": f.job.schedule,
            "runs_per_year": f.job.runs_per_year,
            "shiftable": f.verdict.shiftable,
            "confidence": f.verdict.confidence,
            "reason": f.verdict.reason,
            "annual_kg_saved": round(f.annual_kg_saved, 4),
            "engine": engine,
        } for f in findings], indent=2))
        return 0

    render(findings, profile, engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
