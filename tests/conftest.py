"""Shared test fixtures.

Puts the repository root on sys.path so `src...` imports resolve without the
project needing to be pip-installed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.carbon_api import ForecastEntry  # noqa: E402


BASE_TIME = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)

# A 12-hour synthetic day: dirty overnight, cleanest at 06:00, dirty again.
# The true minimum is 120 gCO2/kWh at BASE_TIME + 6h.
SAMPLE_INTENSITIES = [480, 465, 430, 380, 310, 210, 120, 165, 240, 350, 455, 470]


@pytest.fixture
def sample_forecast() -> list[ForecastEntry]:
    return [
        ForecastEntry(
            timestamp=BASE_TIME + timedelta(hours=hour),
            carbon_intensity=float(value),
        )
        for hour, value in enumerate(SAMPLE_INTENSITIES)
    ]


@pytest.fixture
def base_time() -> datetime:
    return BASE_TIME
