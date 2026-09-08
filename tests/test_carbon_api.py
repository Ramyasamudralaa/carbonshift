"""Tests for src/carbon_api.py.

Covers spec check #2: a failed or timed-out carbon API call must surface as a
clear, catchable error rather than crashing the program or returning junk.
"""

from __future__ import annotations

import json

import pytest
import requests

from src import carbon_api
from src.carbon_api import (
    CarbonApiAuthError,
    CarbonApiConfigError,
    CarbonApiConnectionError,
    CarbonApiResponseError,
    CarbonApiTimeoutError,
    get_carbon_forecast,
    parse_forecast_payload,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeSession:
    """Stands in for requests.Session, returning or raising whatever we want."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv(carbon_api.API_KEY_ENV_VAR, "test-token")


def _payload(points):
    return {
        "zone": "DE",
        "forecast": [
            {"datetime": stamp, "carbonIntensity": value} for stamp, value in points
        ],
    }


# --- Happy path ------------------------------------------------------------


def test_parses_a_well_formed_forecast():
    session = FakeSession(
        FakeResponse(
            payload=_payload(
                [
                    ("2026-09-09T00:00:00.000Z", 480),
                    ("2026-09-09T01:00:00.000Z", 300),
                ]
            )
        )
    )
    entries = get_carbon_forecast("DE", hours=24, session=session)

    assert len(entries) == 2
    assert entries[0].carbon_intensity == 480.0
    assert entries[0].timestamp.isoformat() == "2026-09-09T00:00:00+00:00"


def test_sends_the_api_key_as_a_header_not_a_query_param():
    session = FakeSession(
        FakeResponse(payload=_payload([("2026-09-09T00:00:00Z", 400)]))
    )
    get_carbon_forecast("DE", session=session)

    _, kwargs = session.calls[0]
    assert kwargs["headers"]["auth-token"] == "test-token"
    assert "auth-token" not in kwargs["params"]


def test_truncates_to_the_requested_number_of_hours():
    points = [(f"2026-09-09T{hour:02d}:00:00Z", 400 - hour) for hour in range(12)]
    session = FakeSession(FakeResponse(payload=_payload(points)))

    entries = get_carbon_forecast("DE", hours=5, session=session)
    assert len(entries) == 5


def test_returns_entries_in_chronological_order():
    session = FakeSession(
        FakeResponse(
            payload=_payload(
                [
                    ("2026-09-09T05:00:00Z", 200),
                    ("2026-09-09T01:00:00Z", 480),
                    ("2026-09-09T03:00:00Z", 300),
                ]
            )
        )
    )
    entries = get_carbon_forecast("DE", session=session)
    assert [entry.timestamp.hour for entry in entries] == [1, 3, 5]


# --- Spec check #2: failures are clear and catchable -----------------------


def test_timeout_raises_a_clear_error():
    session = FakeSession(raises=requests.Timeout("timed out"))

    with pytest.raises(CarbonApiTimeoutError) as excinfo:
        get_carbon_forecast("DE", session=session)

    assert "did not respond" in str(excinfo.value)
    assert "No schedule was created" in str(excinfo.value)


def test_connection_failure_raises_a_clear_error():
    session = FakeSession(raises=requests.ConnectionError("dns failure"))

    with pytest.raises(CarbonApiConnectionError):
        get_carbon_forecast("DE", session=session)


def test_every_failure_is_catchable_as_the_base_error():
    """A caller can wrap the whole module in one except clause."""
    session = FakeSession(raises=requests.Timeout("timed out"))

    with pytest.raises(carbon_api.CarbonApiError):
        get_carbon_forecast("DE", session=session)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_explains_the_free_tier_limitation(status):
    session = FakeSession(FakeResponse(status_code=status, payload={}))

    with pytest.raises(CarbonApiAuthError) as excinfo:
        get_carbon_forecast("DE", session=session)

    assert "free tier" in str(excinfo.value)


def test_server_error_includes_the_status_and_body():
    session = FakeSession(
        FakeResponse(status_code=500, payload=None, text="upstream exploded")
    )

    with pytest.raises(CarbonApiResponseError) as excinfo:
        get_carbon_forecast("DE", session=session)

    assert "500" in str(excinfo.value)
    assert "upstream exploded" in str(excinfo.value)


def test_non_json_body_raises_rather_than_returning_junk():
    session = FakeSession(FakeResponse(status_code=200, payload=None, text="<html>"))

    with pytest.raises(CarbonApiResponseError):
        get_carbon_forecast("DE", session=session)


def test_missing_api_key_is_reported_before_any_network_call(monkeypatch):
    monkeypatch.delenv(carbon_api.API_KEY_ENV_VAR, raising=False)
    session = FakeSession(FakeResponse(payload=_payload([])))

    with pytest.raises(CarbonApiConfigError) as excinfo:
        get_carbon_forecast("DE", session=session)

    assert session.calls == []  # never left the process
    assert carbon_api.API_KEY_ENV_VAR in str(excinfo.value)


def test_blank_zone_is_rejected():
    with pytest.raises(CarbonApiConfigError):
        get_carbon_forecast("   ")


# --- Payload validation ----------------------------------------------------


def test_missing_forecast_key_raises():
    with pytest.raises(CarbonApiResponseError) as excinfo:
        parse_forecast_payload({"zone": "DE"})

    assert "forecast" in str(excinfo.value)


def test_null_intensities_are_skipped_not_treated_as_zero():
    entries = parse_forecast_payload(
        {
            "forecast": [
                {"datetime": "2026-09-09T00:00:00Z", "carbonIntensity": None},
                {"datetime": "2026-09-09T01:00:00Z", "carbonIntensity": 300},
            ]
        }
    )
    assert len(entries) == 1
    assert entries[0].carbon_intensity == 300.0


def test_an_all_null_forecast_raises_rather_than_returning_empty():
    with pytest.raises(CarbonApiResponseError):
        parse_forecast_payload(
            {"forecast": [{"datetime": "2026-09-09T00:00:00Z", "carbonIntensity": None}]}
        )


def test_unparseable_timestamp_raises():
    with pytest.raises(CarbonApiResponseError):
        parse_forecast_payload(
            {"forecast": [{"datetime": "not-a-date", "carbonIntensity": 300}]}
        )


def test_non_numeric_intensity_raises():
    with pytest.raises(CarbonApiResponseError):
        parse_forecast_payload(
            {"forecast": [{"datetime": "2026-09-09T00:00:00Z", "carbonIntensity": "low"}]}
        )


def test_naive_timestamps_are_treated_as_utc():
    entries = parse_forecast_payload(
        {"forecast": [{"datetime": "2026-09-09T00:00:00", "carbonIntensity": 300}]}
    )
    assert entries[0].timestamp.tzinfo is not None
