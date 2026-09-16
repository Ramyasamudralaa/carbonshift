"""CarbonShift, without having to remember any commands.

    python carbonshift.py

Everything the project can do, as a menu that asks plain questions. The
individual commands still exist and still work -- this only drives them.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

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


def clear_ish():
    print("\n" * 2)


def rule(char="-", width=64):
    print("  " + char * width)


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("  Cancelled.")
        return None
    if not value and default is not None:
        return str(default)
    return value


def pause():
    try:
        input(f"\n  {DIM}Press Enter to return to the menu...{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- readiness -------------------------------------------------------------


def readiness():
    """Cheap, offline assessment of how far setup has got."""
    env_exists = (REPO / ".env").exists()
    key = os.getenv("ELECTRICITY_MAPS_API_KEY", "").strip()
    has_key = bool(key) and key != "your-electricity-maps-token"

    aws_vars = ["ECS_CLUSTER_ARN", "WORKER_TASK_DEFINITION_ARN",
                "SCHEDULER_ROLE_ARN", "WORKER_SUBNET_IDS"]
    has_aws = all(os.getenv(name, "").strip() for name in aws_vars)

    return env_exists, has_key, has_aws


def status_line():
    env_exists, has_key, has_aws = readiness()

    if not env_exists or not has_key:
        return (f"  {YELLOW}Not set up yet.{RESET} Start with option 1.", False, False)
    if not has_aws:
        return (f"  {GREEN}Ready to preview decisions.{RESET} "
                f"{DIM}AWS not configured, so jobs cannot run yet (option 8).{RESET}",
                True, False)
    return (f"  {GREEN}Fully set up.{RESET} "
            f"{DIM}Jobs can be scheduled and will really run.{RESET}", True, True)


# --- actions ---------------------------------------------------------------


def action_setup():
    print()
    module = load_module(REPO / "setup.py", "carbonshift_setup")
    try:
        module.main()
    except SystemExit:
        pass
    load_dotenv(override=True)


def action_schedule(has_key, has_aws):
    print()
    if not has_key:
        print(f"  {YELLOW}You need to set up first.{RESET} Choose option 1.")
        return

    print(f"{BOLD}  Schedule a job{RESET}")
    rule()
    print("  CarbonShift will find the cleanest hour before your deadline.")
    print()

    name = ask("What is the job called?", "my-job")
    if name is None:
        return

    print()
    print(f"  {DIM}The longer the deadline, the cleaner an hour it can reach.{RESET}")
    hours = ask("How many hours until it must have started?", "12")
    if hours is None:
        return
    try:
        float(hours)
    except ValueError:
        print(f"  {RED}That is not a number.{RESET}")
        return

    if has_aws:
        print()
        print(f"  {BOLD}preview{RESET} decides and shows you, but changes nothing.")
        print(f"  {BOLD}real{RESET}    creates the AWS schedule and the job will run.")
        mode = ask("preview or real?", "preview")
        if mode is None:
            return
        dry_run = not mode.lower().startswith("r")
    else:
        print()
        print(f"  {DIM}AWS is not configured, so this will be a preview.{RESET}")
        dry_run = True

    argv = ["--payload", name, "--deadline-hours", hours]
    if dry_run:
        argv.append("--dry-run")

    print()
    rule()
    from src import scheduler

    code = scheduler.main(argv)
    rule()

    if code == 0:
        print()
        answer = ask("Draw the chart now? (y/n)", "y")
        if answer and answer.lower().startswith("y"):
            print()
            chart = load_module(REPO / "demo" / "chart.py", "carbonshift_chart")
            chart.main([])


def action_dashboard():
    print()
    print(f"{BOLD}  Live dashboard{RESET}")
    rule()
    print("  Builds one self-contained HTML page and opens it: the carbon")
    print("  curve, a countdown to the next scheduled job, and the running")
    print("  total saved.")
    print()
    print(f"  {DIM}No internet needed once it is open - useful at a stall.{RESET}")
    print()

    watch = ask("Keep it refreshing as new jobs run? (y/n)", "n")
    if watch is None:
        return

    dashboard = load_module(REPO / "demo" / "dashboard.py", "carbonshift_dashboard")
    print()
    if watch.lower().startswith("y"):
        print(f"  {DIM}Ctrl+C stops the refreshing and returns here.{RESET}")
        print()
        try:
            dashboard.main(["--watch"])
        except KeyboardInterrupt:
            print("\n  Stopped refreshing.")
    else:
        dashboard.main([])


def action_history():
    print()
    from src import history

    verify = ask("Check each run against AWS logs? (slower) (y/n)", "n")
    if verify is None:
        return
    argv = ["--verify"] if verify.lower().startswith("y") else []
    history.main(argv)


def action_report():
    print()
    print(f"{BOLD}  Carbon report{RESET}")
    rule()
    print("  A document with the figures and the method behind them, for")
    print("  filing or handing to whoever asks for your emissions numbers.")
    print()
    print(f"  {DIM}Previews are never counted. Jobs that cannot be confirmed{RESET}")
    print(f"  {DIM}against AWS logs are listed but excluded from the totals.{RESET}")
    print()

    period = ask("Period - 'month', a month like 2026-09, a year, or 'all'", "month")
    if period is None:
        return

    argv = []
    if period.lower() == "all":
        argv.append("--all")
    elif re.fullmatch(r"\d{4}-\d{2}", period):
        argv += ["--month", period]
    elif re.fullmatch(r"\d{4}", period):
        argv += ["--year", period]

    check = ask("Confirm each job against AWS logs? (slower, but needed to file) (y/n)", "y")
    if check is None:
        return
    if check.lower().startswith("y"):
        argv.append("--verify")

    want_csv = ask("Also write a CSV of every job? (y/n)", "y")
    if want_csv and want_csv.lower().startswith("y"):
        argv.append("--csv")

    print()
    from src import report

    report.main(argv)


def action_scan():
    print()
    print(f"{BOLD}  Scan for shiftable jobs{RESET}")
    rule()
    print("  Reads your scheduled jobs and works out which ones could be")
    print("  delayed to a cleaner hour, and what that would save per year.")
    print()

    print(f"    {BOLD}1{RESET}. A crontab file")
    print(f"    {BOLD}2{RESET}. My AWS EventBridge schedules")
    print(f"    {BOLD}3{RESET}. The bundled example {DIM}(demo/sample-crontab){RESET}")
    print()
    source = ask("Which", "3")
    if source is None:
        return

    if source == "1":
        path = ask("Path to the crontab file")
        if not path:
            return
        argv = ["--cron", path]
    elif source == "2":
        argv = ["--aws"]
    else:
        argv = ["--cron", str(REPO / "demo" / "sample-crontab")]

    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        use = ask("Use Claude to classify them? (more accurate) (y/n)", "y")
        if use and use.lower().startswith("y"):
            argv += ["--engine", "claude"]
    else:
        print()
        print(f"  {DIM}No ANTHROPIC_API_KEY set, so the built-in rules will be{RESET}")
        print(f"  {DIM}used. They need no key and cost no energy.{RESET}")

    print()
    from src import scanner

    scanner.main(argv)


def action_doctor():
    print()
    from src import doctor

    doctor.main([])


def action_deploy():
    print()
    print(f"{BOLD}  Deploy to AWS{RESET}")
    rule()
    print("  This creates the AWS resources CarbonShift needs:")
    print("  an ECR repository, an ECS cluster, a log group, two IAM roles")
    print("  and a Fargate task definition.")
    print()
    print(f"  {DIM}Safe to run more than once - it reuses anything already there.{RESET}")
    print(f"  {DIM}You need 'aws configure' done first.{RESET}")
    print()

    answer = ask("Go ahead? (y/n)", "n")
    if answer is None or not answer.lower().startswith("y"):
        print("  Nothing was done.")
        return

    print()
    deploy = load_module(REPO / "infra" / "deploy.py", "carbonshift_deploy")
    try:
        code = deploy.main([])
    except SystemExit as exc:
        code = exc.code or 0

    if code == 0:
        print()
        print(f"  {YELLOW}Now paste those values into your .env file.{RESET}")
        print(f"  {DIM}Then come back and run option 4 to check everything.{RESET}")


# --- menu ------------------------------------------------------------------

ACTIONS = [
    ("Set up CarbonShift", "api key, region - writes your .env"),
    ("Schedule a job", "pick a job and a deadline"),
    ("Open the live dashboard", "charts and a countdown, in your browser"),
    ("See my history and savings", "every run, and the running total"),
    ("Generate a carbon report", "monthly report you can file or print"),
    ("Scan for shiftable jobs", "which of your jobs could be shifted"),
    ("Check my setup is healthy", "finds problems and how to fix them"),
    ("Deploy to AWS", "one-time, creates the cloud resources"),
    ("Quit", ""),
]


def main() -> int:
    while True:
        clear_ish()
        print(f"{BOLD}  CarbonShift{RESET}")
        print(f"  {DIM}Run cloud jobs when the electricity grid is cleanest.{RESET}")
        print()

        line, has_key, has_aws = status_line()
        print(line)
        print()
        rule()

        for index, (title, hint) in enumerate(ACTIONS, 1):
            label = f"  {BOLD}{index}{RESET}. {title}"
            if hint:
                pad = " " * max(1, 30 - len(title))
                label += f"{pad}{DIM}{hint}{RESET}"
            print(label)
        rule()
        print()

        choice = ask("Choose", "2" if has_key else "1")
        if choice is None:
            return 0

        if choice == "1":
            action_setup()
        elif choice == "2":
            _, has_key, has_aws = status_line()
            action_schedule(has_key, has_aws)
        elif choice == "3":
            action_dashboard()
        elif choice == "4":
            action_history()
        elif choice == "5":
            action_report()
        elif choice == "6":
            action_scan()
        elif choice == "7":
            action_doctor()
        elif choice == "8":
            action_deploy()
        elif choice in ("9", "q", "quit", "exit"):
            print()
            print("  Bye.")
            return 0
        else:
            print()
            print(f"  {RED}Pick a number from 1 to 9.{RESET}")

        pause()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("  Bye.")
        sys.exit(0)
