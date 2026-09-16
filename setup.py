"""Guided first-time setup for CarbonShift.

Run this once after cloning. It asks for what it needs, checks each answer
works before moving on, and writes your .env file for you.

    python setup.py

Nothing is written until the end, and your existing .env is never overwritten
without asking. No value you type is ever sent anywhere except the API it
belongs to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
ENV_EXAMPLE = HERE / ".env.example"

# AWS region -> Electricity Maps zone, kept in step with src/scheduler.py
REGION_CHOICES = [
    ("eu-central-1", "DE", "Frankfurt, Germany", "best carbon swing (~2.6x)"),
    ("eu-west-2", "GB", "London, Britain", "cleanest grid (~2.5x swing)"),
    ("ap-south-1", "IN-WE", "Mumbai, India", "smaller swing (~1.4x)"),
    ("us-east-1", "US-MIDA-PJM", "N. Virginia, USA", "flat grid (~1.2x)"),
]

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m",
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    try:
        import colorama  # noqa: F401

        colorama.just_fix_windows_console()
    except Exception:
        BOLD = DIM = GREEN = RED = YELLOW = RESET = ""


def say(text=""):
    print(text, flush=True)


def step(n, total, title):
    say()
    say(f"{BOLD}[{n}/{total}] {title}{RESET}")
    say("-" * 60)


def ok(text):
    say(f"  {GREEN}OK{RESET}  {text}")


def bad(text):
    say(f"  {RED}!!{RESET}  {text}")


def note(text):
    say(f"  {DIM}{text}{RESET}")


def ask(prompt, default=None, secret=False):
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"  {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            say()
            bad("Setup cancelled. Nothing was written.")
            sys.exit(1)
        if not value and default is not None:
            return default
        if value:
            return value
        bad("That cannot be empty.")


def ask_yes_no(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    answer = input(f"  {prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


# --- checks ----------------------------------------------------------------


def check_python():
    version = sys.version_info
    if version < (3, 10):
        bad(f"Python {version.major}.{version.minor} found, but 3.10+ is needed.")
        say()
        say("  Install a newer Python from python.org, then run this again.")
        sys.exit(1)
    ok(f"Python {version.major}.{version.minor}.{version.micro}")


def check_dependencies():
    missing = []
    for module, package in [
        ("requests", "requests"),
        ("dotenv", "python-dotenv"),
        ("matplotlib", "matplotlib"),
    ]:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        bad(f"Missing packages: {', '.join(missing)}")
        say()
        say("  Install them first, then run this again:")
        say(f"  {BOLD}pip install -r requirements.txt{RESET}")
        sys.exit(1)
    ok("requests, python-dotenv, matplotlib installed")

    try:
        import boto3  # noqa: F401

        ok("boto3 installed (needed only to schedule real AWS jobs)")
        return True
    except ImportError:
        note("boto3 not installed - you can still preview decisions with --dry-run")
        return False


def test_api_key(key, zone):
    """Make one real call so a bad key is caught here, not three steps later."""
    import requests

    try:
        response = requests.get(
            "https://api.electricitymap.org/v3/carbon-intensity/forecast",
            params={"zone": zone},
            headers={"auth-token": key},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"Could not reach the API: {exc}"

    if response.status_code in (401, 403):
        return False, (
            f"The API rejected this key for zone {zone} (HTTP {response.status_code}). "
            "Either the key is wrong, or your plan does not cover this zone."
        )
    if response.status_code >= 400:
        return False, f"The API returned HTTP {response.status_code}."

    try:
        points = response.json().get("forecast", [])
    except ValueError:
        return False, "The API returned something that was not JSON."

    usable = [pt for pt in points if pt.get("carbonIntensity") is not None]
    if not usable:
        return False, f"No usable forecast data came back for zone {zone}."

    values = [pt["carbonIntensity"] for pt in usable]
    return True, (
        f"{len(usable)} hourly points for {zone}: "
        f"{min(values):.0f} to {max(values):.0f} gCO2/kWh "
        f"({max(values) / min(values):.1f}x swing)"
    )


def test_anthropic_key(key: str):
    """One tiny call, so a wrong key is caught here rather than mid scan."""
    import requests

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return False, f"Could not reach the service: {exc}"

    if response.status_code == 401:
        return False, "That key was rejected. Check you copied all of it."
    if response.status_code == 400:
        return False, "The service did not accept the request. Is the key complete?"
    if response.status_code == 429:
        return False, "Rate limited. Wait a moment and try again."
    if response.status_code >= 400:
        return False, f"The service returned HTTP {response.status_code}."
    return True, "Key works."


def _load_deploy():
    """Import infra/deploy.py by path, since infra/ is not a package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "carbonshift_deploy", HERE / "infra" / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def docker_is_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def build_and_push_image(image_uri: str, region: str) -> bool:
    """Build the worker image and put it where Fargate can pull it."""
    try:
        commands = _load_deploy().push_commands(image_uri, region)
    except Exception as exc:
        bad(f"Could not work out the push commands: {exc}")
        return False

    for command in commands:
        detail_name = command.split()[0] + " " + command.split()[1]
        say(f"    running {detail_name} ...")
        try:
            result = subprocess.run(command, shell=True, timeout=900)
        except (subprocess.TimeoutExpired, OSError) as exc:
            bad(f"That command failed: {exc}")
            return False
        if result.returncode != 0:
            bad("That command failed. Run it yourself to see why:")
            say(f"      {command}")
            return False
    return True


def run_provisioning(region: str) -> dict:
    """Create the AWS resources and hand back the settings they produced."""
    try:
        deploy = _load_deploy()
    except Exception as exc:
        bad(f"Could not load the provisioning script: {exc}")
        return {}

    say()
    try:
        settings, _ = deploy.provision(region)
    except Exception as exc:
        say()
        bad(f"{exc}")
        say()
        note("Nothing was written to your .env. Fix the problem above and")
        note("run 'python setup.py' again. Re-running is safe.")
        return {}

    say()
    ok("AWS resources ready. Settings captured, no copying needed.")

    image_uri = settings.get("WORKER_IMAGE_URI", "")
    say()
    say("  One thing left: the worker image has to be uploaded to AWS,")
    say("  or a scheduled job will have nothing to run.")
    say()

    if not docker_is_running():
        bad("Docker is not running, so the image cannot be built now.")
        say()
        say("  Start Docker Desktop, then run these four commands:")
        for command in build_commands_for(image_uri, region):
            say(f"    {command}")
        return settings

    if ask_yes_no("Build and upload it now? (a few minutes)", True):
        if build_and_push_image(image_uri, region):
            ok("Worker image uploaded.")
        else:
            say()
            note("The image was not uploaded. Everything else is set up.")
            note("Check 'python -m src.doctor' to see what is missing.")
    return settings


def build_commands_for(image_uri: str, region: str) -> list[str]:
    try:
        return _load_deploy().push_commands(image_uri, region)
    except Exception:
        return ["docker build -t carbonshift-worker src/worker",
                f"docker push {image_uri}"]


def check_aws():
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        return None

    try:
        identity = boto3.client("sts").get_caller_identity()
        return identity["Account"]
    except (NoCredentialsError, ClientError, BotoCoreError):
        return None
    except Exception:
        return None


# --- env writing -----------------------------------------------------------


def load_existing():
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def write_env(values):
    if ENV_PATH.exists():
        backup = ENV_PATH.with_name(".env.backup")
        shutil.copy2(ENV_PATH, backup)
        note(f"Existing .env backed up to {backup.name}")

    if ENV_EXAMPLE.exists():
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    written = set()
    out = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                written.add(key)
                continue
        out.append(line)

    for key, value in values.items():
        if key not in written:
            out.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


# --- main ------------------------------------------------------------------


def main():
    say()
    say(f"{BOLD}CarbonShift setup{RESET}")
    say("Runs your cloud jobs when the electricity grid is cleanest.")
    say()
    say("This asks a few questions and writes your .env file.")
    say("It checks each answer works before moving on.")

    total = 6
    values = load_existing()

    # 1 --------------------------------------------------------------------
    step(1, total, "Checking your machine")
    check_python()
    has_boto3 = check_dependencies()

    # 2 --------------------------------------------------------------------
    step(2, total, "Choosing a region")
    say("  Which cloud region will your jobs run in?")
    say("  The grid there decides how much carbon you can save.")
    say()
    for i, (region, zone, place, hint) in enumerate(REGION_CHOICES, 1):
        say(f"    {i}. {region:<15} {place:<22} {DIM}{hint}{RESET}")
    say(f"    {len(REGION_CHOICES) + 1}. something else")
    say()

    while True:
        choice = ask("Pick a number", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(REGION_CHOICES):
            region, zone, place, _ = REGION_CHOICES[int(choice) - 1]
            break
        if choice == str(len(REGION_CHOICES) + 1):
            region = ask("AWS region (e.g. eu-west-1)")
            zone = ask("Electricity Maps zone for that region (e.g. IE)")
            place = region
            break
        bad("Please enter one of the numbers listed.")

    ok(f"{region} ({place}), grid zone {zone}")

    # 3 --------------------------------------------------------------------
    step(3, total, "Your Electricity Maps API key")
    say("  CarbonShift needs carbon-intensity forecasts. They are free.")
    say()
    say(f"  1. Go to {BOLD}https://portal.electricitymaps.com/{RESET}")
    say("  2. Sign up, then copy your API token")
    say()
    note("Your key is stored only in .env on this machine. It is never sent")
    note("anywhere except Electricity Maps, and .env is excluded from git.")
    say()

    existing_key = values.get("ELECTRICITY_MAPS_API_KEY", "")
    if existing_key and existing_key != "your-electricity-maps-token":
        if ask_yes_no("An API key is already saved. Keep using it?", True):
            key = existing_key
        else:
            key = ask("Paste your API key")
    else:
        key = ask("Paste your API key")

    say()
    say("  Testing it against the live API...")
    for attempt in range(3):
        good, message = test_api_key(key, zone)
        if good:
            ok(message)
            break
        bad(message)
        say()
        if attempt == 2:
            bad("Could not verify the key after 3 tries. Nothing was written.")
            sys.exit(1)
        if "does not cover this zone" in message:
            say("  Your free plan may only cover one zone. Try a different one,")
            say("  or check which zone your account has at portal.electricitymaps.com")
            zone = ask("Zone to try", zone)
        else:
            key = ask("Paste your API key again")

    # 4 --------------------------------------------------------------------
    step(4, total, "AWS")
    say("  You need AWS only if you want jobs to actually RUN.")
    say("  Without it, CarbonShift still shows you every decision and chart.")
    say()

    aws_settings: dict[str, str] = {}
    account = check_aws() if has_boto3 else None

    if not has_boto3:
        bad("boto3 is not installed, so AWS cannot be set up.")
        note("Fix it with: pip install -r requirements.txt")
    elif not account:
        bad("No AWS credentials found on this machine.")
        say()
        say("  To use AWS, do this once in another terminal window:")
        say()
        say("    1. Install the AWS CLI from aws.amazon.com/cli")
        say(f"    2. Run {BOLD}aws configure{RESET} and paste your access key")
        say()
        note("Do not paste your secret key here. It goes into aws configure.")
        say()
        if ask_yes_no("Done that in another window? Check again now?", False):
            account = check_aws()
            if not account:
                bad("Still no credentials. You can finish the AWS part later.")

    if account:
        ok(f"AWS credentials found for account {account}")
        say()
        say("  I can create the AWS resources now and write every setting into")
        say("  your .env for you. There is nothing to copy by hand.")
        say()
        note("Safe to run even if you have done it before. It reuses whatever")
        note("already exists rather than making duplicates.")
        say()
        if ask_yes_no("Set up AWS now? (about a minute)", True):
            aws_settings = run_provisioning(region)
        else:
            note("Skipped. Run 'python setup.py' again whenever you want it.")

    if not account and not aws_settings:
        say()
        note("Carrying on without AWS. Everything except running jobs will work.")

    # 5 --------------------------------------------------------------------
    step(5, total, "AI job scanner (optional)")
    say("  CarbonShift can read your scheduled jobs and work out which ones")
    say("  could be delayed to a cleaner hour, and which ones must not be.")
    say()
    say("  It does this two ways:")
    say()
    say(f"    {BOLD}Built in rules{RESET}   Free. No key, no internet, no energy used.")
    say("                     Good on clear names like backup or healthcheck.")
    say()
    say(f"    {BOLD}Claude{RESET}           Better on unclear names. Needs a paid API key.")
    say()
    note("The AI reads each job once, not once per run, so the energy it costs")
    note("is spread across every future run of that job.")
    say()
    note("You can skip this. The built in rules work without any key.")
    say()

    existing_ai = values.get("ANTHROPIC_API_KEY", "").strip()
    if existing_ai:
        ok("An Anthropic key is already saved.")
        if not ask_yes_no("Replace it?", False):
            ai_key = existing_ai
        else:
            ai_key = ""
    else:
        ai_key = ""

    if not existing_ai or not ai_key:
        if ask_yes_no("Add an Anthropic API key for the AI scanner?", False):
            say()
            say("  Get one at console.anthropic.com. It starts with sk-ant-")
            say()
            candidate = ask("Paste your Anthropic key")
            say()
            say("  Testing it...")
            good, message = test_anthropic_key(candidate)
            if good:
                ok(message)
                ai_key = candidate
            else:
                bad(message)
                note("Skipping the AI. The built in rules still work.")
                ai_key = existing_ai
        else:
            note("Skipped. The scanner will use the built in rules.")
            note("Add a key later by running setup.py again.")
            ai_key = existing_ai

    # 5 --------------------------------------------------------------------
    step(6, total, "Writing your settings")
    values.update({
        "ELECTRICITY_MAPS_API_KEY": key,
        "CARBON_ZONE": zone,
        "AWS_REGION": region,
    })
    values.update(aws_settings)
    if ai_key:
        values["ANTHROPIC_API_KEY"] = ai_key
    write_env(values)
    ok(f"Wrote {ENV_PATH.name}")

    say()
    say("=" * 60)
    say(f"{GREEN}{BOLD}  Setup complete.{RESET}")
    say("=" * 60)
    say()
    say(f"  {BOLD}Try it now - no AWS needed:{RESET}")
    say()
    say('    python -m src.scheduler --payload "my-first-job" \\')
    say("        --deadline-hours 12 --dry-run")
    say()
    say(f"  {BOLD}Then see the chart:{RESET}")
    say()
    say("    python demo/chart.py")
    say()
    say(f"  {BOLD}And your saved history:{RESET}")
    say()
    say("    python -m src.history")
    say()
    say(f"  {BOLD}Find which of your jobs could be shifted:{RESET}")
    say()
    say("    python -m src.scanner --cron demo/sample-crontab")
    say()
    say(f"  {BOLD}Or just use the menu:{RESET}")
    say()
    say("    python carbonshift.py")
    say()


if __name__ == "__main__":
    main()
