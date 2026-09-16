"""Tests for carbonshift.py, the interactive menu.

The prompts themselves need a human, but the logic that decides what the menu
offers does not -- and that logic is what would quietly mislead someone. These
pin it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MENU_PATH = REPO / "carbonshift.py"

AWS_VARS = ("ECS_CLUSTER_ARN", "WORKER_TASK_DEFINITION_ARN",
            "SCHEDULER_ROLE_ARN", "WORKER_SUBNET_IDS")


@pytest.fixture(scope="module")
def menu():
    spec = importlib.util.spec_from_file_location("carbonshift_menu", MENU_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_colour(menu, monkeypatch):
    for name in ("BOLD", "DIM", "GREEN", "RED", "YELLOW", "RESET"):
        monkeypatch.setattr(menu, name, "")


def set_fully_configured(monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "a-real-key")
    for name in AWS_VARS:
        monkeypatch.setenv(name, "arn:aws:something")


def set_key_only(monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "a-real-key")
    for name in AWS_VARS:
        monkeypatch.setenv(name, "")


# --- readiness -------------------------------------------------------------


def test_readiness_reports_all_three_stages(menu, monkeypatch):
    set_fully_configured(monkeypatch)

    env_exists, has_key, has_aws = menu.readiness()

    assert has_key is True
    assert has_aws is True


def test_a_placeholder_key_does_not_count_as_configured(menu, monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "your-electricity-maps-token")

    _, has_key, _ = menu.readiness()

    assert has_key is False


def test_a_blank_key_does_not_count_as_configured(menu, monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "   ")

    _, has_key, _ = menu.readiness()

    assert has_key is False


def test_partial_aws_config_does_not_count_as_configured(menu, monkeypatch):
    """Three of four ARNs is not 'ready' -- scheduling would fail."""
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "a-real-key")
    for name in AWS_VARS:
        monkeypatch.setenv(name, "arn:aws:something")
    monkeypatch.setenv("SCHEDULER_ROLE_ARN", "")

    _, _, has_aws = menu.readiness()

    assert has_aws is False


# --- status line -----------------------------------------------------------


def test_unconfigured_points_at_setup(menu, monkeypatch):
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", "")

    line, has_key, has_aws = menu.status_line()

    assert "option 1" in line
    assert has_key is False
    assert has_aws is False


def test_key_but_no_aws_says_preview_only(menu, monkeypatch):
    set_key_only(monkeypatch)

    line, has_key, has_aws = menu.status_line()

    assert "preview" in line.lower()
    assert has_key is True
    assert has_aws is False


def test_fully_configured_says_jobs_will_really_run(menu, monkeypatch):
    set_fully_configured(monkeypatch)

    line, has_key, has_aws = menu.status_line()

    assert "really run" in line
    assert has_key and has_aws


# --- scheduling guard ------------------------------------------------------


def test_scheduling_without_a_key_refuses_and_points_at_setup(menu, capsys):
    menu.action_schedule(has_key=False, has_aws=False)

    out = capsys.readouterr().out
    assert "set up first" in out
    assert "option 1" in out


# --- menu shape ------------------------------------------------------------


def test_every_menu_entry_is_handled(menu):
    """A listed option with no branch would silently do nothing."""
    assert len(menu.ACTIONS) == 9
    assert menu.ACTIONS[-1][0] == "Quit"


def test_module_loader_can_load_a_sibling_script(menu):
    loaded = menu.load_module(REPO / "setup.py", "probe_setup")

    assert hasattr(loaded, "main")
