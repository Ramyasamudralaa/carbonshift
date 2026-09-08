"""The demo workload CarbonShift schedules.

This is deliberately simple and deterministic. Its job is to prove that the
scheduling mechanism fired and that the container really ran -- not to be a
realistic production workload. It processes a small fixed dataset, checksums
the result so the output is verifiable, and writes a clear completion log.

Everything it prints goes to stdout, which on Fargate lands in CloudWatch Logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone

# --- Configuration ---------------------------------------------------------

# How long to stay busy, so the run is visible in logs and in the ECS console.
RUN_SECONDS = float(os.getenv("WORKER_RUN_SECONDS", "5"))

# A fixed dataset: the same input every run, so the checksum is reproducible.
DATASET = [
    {"id": 1, "region": "eu-central-1", "kwh": 0.0074},
    {"id": 2, "region": "eu-west-1", "kwh": 0.0111},
    {"id": 3, "region": "us-east-1", "kwh": 0.0148},
    {"id": 4, "region": "ap-south-1", "kwh": 0.0037},
    {"id": 5, "region": "us-west-2", "kwh": 0.0222},
]

def process(dataset: list[dict]) -> dict:
    """The 'work': aggregate the dataset and checksum the result."""
    total_kwh = round(sum(row["kwh"] for row in dataset), 6)
    by_region = {row["region"]: row["kwh"] for row in dataset}
    canonical = json.dumps(
        {"total_kwh": total_kwh, "by_region": by_region}, sort_keys=True
    )
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {
        "rows_processed": len(dataset),
        "total_kwh": total_kwh,
        "checksum": checksum,
    }


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    print(f"[{stamp}] {message}", flush=True)


def main() -> int:
    started_at = datetime.now(timezone.utc)

    payload = os.getenv("CARBONSHIFT_JOB_PAYLOAD", "(no payload supplied)")
    scheduled_for = os.getenv("CARBONSHIFT_SCHEDULED_FOR", "(not set)")

    log("CarbonShift worker starting")
    log(f"  payload        : {payload}")
    log(f"  scheduled for  : {scheduled_for}")
    log(f"  actual start   : {started_at.isoformat()}")
    log(f"  python         : {platform.python_version()} on {platform.machine()}")

    log(f"Processing {len(DATASET)} rows...")
    time.sleep(max(0.0, RUN_SECONDS))
    result = process(DATASET)

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    log(f"  rows processed : {result['rows_processed']}")
    log(f"  total kWh      : {result['total_kwh']}")
    log(f"  checksum       : {result['checksum']}")
    log(f"  duration       : {duration:.2f}s")

    # The line to grep for in CloudWatch Logs when verifying a scheduled run.
    log(
        "CARBONSHIFT_JOB_COMPLETE "
        + json.dumps(
            {
                "payload": payload,
                "scheduled_for": scheduled_for,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round(duration, 2),
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
