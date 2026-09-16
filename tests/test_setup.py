"""Tests for setup.py.

The failure that matters here is telling someone the wrong thing about their
machine. "Docker is not running" when Docker was never installed sends them
looking for an app they do not have, with no download link. These pin the
distinction.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SETUP_PATH = Path(__file__).resolve().parents[1] / "setup.py"


@pytest.fixture(scope="module")
def setup_mod():
    spec = importlib.util.spec_from_file_location("carbonshift_setup", SETUP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_colour(setup_mod, monkeypatch):
    for name in ("BOLD", "DIM", "GREEN", "RED", "YELLOW", "RESET"):
        monkeypatch.setattr(setup_mod, name, "")


def fake_run(returncode=0, stdout="", stderr=""):
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    return runner


# --- docker: missing is not the same as stopped ----------------------------


def test_docker_absent_reports_missing(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)

    state, detail = setup_mod.docker_status()

    assert state == "missing"
    assert "not installed" in detail


def test_docker_present_but_daemon_down_reports_stopped(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run(returncode=1))

    state, detail = setup_mod.docker_status()

    assert state == "stopped"
    assert "not started" in detail


def test_docker_timing_out_reports_stopped_not_missing(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/docker")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=20)

    monkeypatch.setattr(setup_mod.subprocess, "run", timeout)

    assert setup_mod.docker_status()[0] == "stopped"


def test_docker_running_reports_its_version(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run(stdout="29.7.2\n"))

    state, detail = setup_mod.docker_status()

    assert state == "running"
    assert "29.7.2" in detail


# --- aws cli ---------------------------------------------------------------


def test_aws_cli_absent_is_reported(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)

    state, detail = setup_mod.aws_cli_status()

    assert state == "missing"
    assert "not installed" in detail


def test_aws_cli_present_reports_its_version(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/aws")
    monkeypatch.setattr(setup_mod.subprocess, "run",
                        fake_run(stdout="aws-cli/2.36.40 Python/3.14"))

    state, detail = setup_mod.aws_cli_status()

    assert state == "installed"
    assert "2.36.40" in detail


# --- the up front report ---------------------------------------------------


def test_missing_docker_is_named_with_a_download_link(setup_mod, monkeypatch, capsys):
    monkeypatch.setattr(setup_mod, "docker_status", lambda: ("missing", "not installed"))
    monkeypatch.setattr(setup_mod, "aws_cli_status", lambda: ("installed", "aws-cli/2"))

    setup_mod.check_dependencies()

    out = capsys.readouterr().out
    assert "Docker Desktop" in out
    assert setup_mod.DOWNLOAD_DOCKER in out
    assert "will not actually run in the cloud" in out


def test_missing_aws_cli_is_named_with_a_download_link(setup_mod, monkeypatch, capsys):
    monkeypatch.setattr(setup_mod, "docker_status", lambda: ("running", "running, 29"))
    monkeypatch.setattr(setup_mod, "aws_cli_status", lambda: ("missing", "not installed"))

    setup_mod.check_dependencies()

    out = capsys.readouterr().out
    assert setup_mod.DOWNLOAD_AWS_CLI in out
    assert "aws configure" in out


def test_a_stopped_docker_is_told_to_start_not_to_install(setup_mod, monkeypatch, capsys):
    monkeypatch.setattr(setup_mod, "docker_status",
                        lambda: ("stopped", "installed, but not started"))
    monkeypatch.setattr(setup_mod, "aws_cli_status", lambda: ("installed", "aws-cli/2"))

    setup_mod.check_dependencies()

    out = capsys.readouterr().out
    assert "Engine running" in out
    assert setup_mod.DOWNLOAD_DOCKER not in out  # do not send them to download it


def test_a_complete_machine_says_so(setup_mod, monkeypatch, capsys):
    monkeypatch.setattr(setup_mod, "docker_status", lambda: ("running", "running, 29"))
    monkeypatch.setattr(setup_mod, "aws_cli_status", lambda: ("installed", "aws-cli/2"))

    setup_mod.check_dependencies()

    out = capsys.readouterr().out
    assert "Everything needed for the full product is present" in out
    assert "missing" not in out.lower()
