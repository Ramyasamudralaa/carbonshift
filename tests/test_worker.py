"""Tests for src/worker/job.py.

The worker is loaded from its path rather than imported as a package, so that
src/worker/ stays exactly as the Dockerfile copies it -- job.py and nothing else.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

WORKER_PATH = Path(__file__).resolve().parents[1] / "src" / "worker" / "job.py"


@pytest.fixture(scope="module")
def worker():
    spec = importlib.util.spec_from_file_location("carbonshift_worker_job", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_workload_is_deterministic(worker):
    """Same input, same checksum -- so a scheduled run is verifiable."""
    first = worker.process(worker.DATASET)
    second = worker.process(worker.DATASET)

    assert first == second
    assert first["rows_processed"] == len(worker.DATASET)
    assert len(first["checksum"]) == 16


def test_the_checksum_changes_when_the_data_changes(worker):
    modified = [dict(row) for row in worker.DATASET]
    modified[0]["kwh"] = 999.0

    assert worker.process(modified)["checksum"] != worker.process(worker.DATASET)["checksum"]


def test_it_prints_a_greppable_completion_line(worker, monkeypatch, capsys):
    monkeypatch.setenv("WORKER_RUN_SECONDS", "0")
    monkeypatch.setattr(worker, "RUN_SECONDS", 0.0)
    monkeypatch.setenv("CARBONSHIFT_JOB_PAYLOAD", "test-payload")

    assert worker.main() == 0

    output = capsys.readouterr().out
    assert "CARBONSHIFT_JOB_COMPLETE" in output

    marker = "CARBONSHIFT_JOB_COMPLETE "
    line = next(line for line in output.splitlines() if marker in line)
    record = json.loads(line.split(marker, 1)[1])

    assert record["payload"] == "test-payload"
    assert record["rows_processed"] == len(worker.DATASET)
    assert "finished_at" in record


def test_the_dockerfile_copies_the_job_and_sets_an_entrypoint():
    dockerfile = (WORKER_PATH.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY job.py" in dockerfile
    assert "ENTRYPOINT" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile  # logs must reach CloudWatch live
