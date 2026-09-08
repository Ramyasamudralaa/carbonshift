"""Carbon-intensity forecast client (Electricity Maps).

Single responsibility: given a grid zone, return an hourly forecast of grid
carbon intensity as a list of ``ForecastEntry`` records. This module knows
nothing about AWS, scheduling or the job being run.

Failures are always raised as a ``CarbonApiError`` subclass. This module never
returns partial, malformed or silently-substituted data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

# --- Configuration ---------------------------------------------------------

API_BASE_URL = os.getenv(
    "ELECTRICITY_MAPS_BASE_URL", "https://api.electricitymap.org/v3"
)
API_KEY_ENV_VAR = "ELECTRICITY_MAPS_API_KEY"
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_FORECAST_HOURS = 24


# --- Errors ----------------------------------------------------------------


class CarbonApiError(Exception):
    """Base class for every failure raised by this module."""


class CarbonApiConfigError(CarbonApiError):
    """Required configuration (such as the API key) is missing or invalid."""


class CarbonApiTimeoutError(CarbonApiError):
    """The carbon-intensity API did not respond within the timeout."""


class CarbonApiConnectionError(CarbonApiError):
    """The carbon-intensity API could not be reached at all."""


class CarbonApiAuthError(CarbonApiError):
    """The API rejected our credentials, or the zone is not on our plan."""


class CarbonApiResponseError(CarbonApiError):
    """The API responded, but with an error status or an unusable payload."""


# --- Data model ------------------------------------------------------------


@dataclass(frozen=True)
class ForecastEntry:
    """One hourly forecast point."""

    timestamp: datetime  # timezone-aware, UTC
    carbon_intensity: float  # gCO2 per kWh

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "carbon_intensity": self.carbon_intensity,
        }


# --- Internals -------------------------------------------------------------


def _require_api_key() -> str:
    key = os.getenv(API_KEY_ENV_VAR, "").strip()
    if not key:
        raise CarbonApiConfigError(
            f"{API_KEY_ENV_VAR} is not set. Copy .env.example to .env and add "
            f"your Electricity Maps token, or export {API_KEY_ENV_VAR} in your "
            "shell. The key is never read from anywhere else."
        )
    return key


def _parse_timestamp(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise CarbonApiResponseError(
            f"Forecast entry has a non-string datetime: {raw!r}"
        )
    text = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CarbonApiResponseError(
            f"Forecast entry has an unparseable datetime: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_forecast_payload(payload: Any) -> list[ForecastEntry]:
    """Turn a raw Electricity Maps forecast response into ``ForecastEntry``s.

    Entries whose ``carbonIntensity`` is null are skipped -- the provider emits
    those for hours it has no model output for. If nothing usable survives we
    raise, rather than hand back an empty list that a caller could misread as
    "no clean hours available".
    """
    if not isinstance(payload, dict):
        raise CarbonApiResponseError(
            f"Expected a JSON object from the carbon API, got {type(payload).__name__}"
        )

    raw_entries = payload.get("forecast")
    if not isinstance(raw_entries, list):
        raise CarbonApiResponseError(
            "Carbon API response has no 'forecast' list. "
            f"Top-level keys were: {sorted(payload)}"
        )

    entries: list[ForecastEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise CarbonApiResponseError(f"Forecast entry is not an object: {raw!r}")
        intensity = raw.get("carbonIntensity")
        if intensity is None:
            continue
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
            raise CarbonApiResponseError(
                f"Forecast entry has a non-numeric carbonIntensity: {intensity!r}"
            )
        entries.append(
            ForecastEntry(
                timestamp=_parse_timestamp(raw.get("datetime")),
                carbon_intensity=float(intensity),
            )
        )

    if not entries:
        raise CarbonApiResponseError(
            "Carbon API returned a forecast with no usable carbon-intensity "
            "values. Check that your plan covers this zone."
        )

    entries.sort(key=lambda entry: entry.timestamp)
    return entries


def _body_snippet(response: Any, limit: int = 300) -> str:
    try:
        text = response.text or ""
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"
    text = text.strip().replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


# --- Public API ------------------------------------------------------------


def get_carbon_forecast(
    zone: str,
    hours: int = DEFAULT_FORECAST_HOURS,
    *,
    session: requests.Session | None = None,
) -> list[ForecastEntry]:
    """Fetch the hourly carbon-intensity forecast for ``zone``.

    Args:
        zone: Electricity Maps zone id, e.g. ``"DE"`` or ``"US-MIDA-PJM"``.
        hours: How many hourly points to return, counted from the earliest
            forecast point. Must be positive.
        session: Optional ``requests.Session``, mainly so tests can inject one.

    Returns:
        Chronologically sorted ``ForecastEntry`` records, at most ``hours`` long.

    Raises:
        CarbonApiError: on any configuration, network, auth or payload problem.
    """
    if not zone or not zone.strip():
        raise CarbonApiConfigError(
            "A carbon zone is required. Set CARBON_ZONE in .env, or pass a zone "
            "explicitly (e.g. 'DE')."
        )
    if hours <= 0:
        raise CarbonApiConfigError(f"hours must be positive, got {hours!r}")

    api_key = _require_api_key()
    url = f"{API_BASE_URL}/carbon-intensity/forecast"
    getter = session.get if session is not None else requests.get

    try:
        response = getter(
            url,
            params={"zone": zone.strip()},
            headers={"auth-token": api_key},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise CarbonApiTimeoutError(
            f"Carbon API did not respond within {REQUEST_TIMEOUT_SECONDS}s "
            f"(zone={zone}). No schedule was created."
        ) from exc
    except requests.RequestException as exc:
        raise CarbonApiConnectionError(
            f"Could not reach the carbon API at {url} (zone={zone}): {exc}"
        ) from exc

    if response.status_code in (401, 403):
        raise CarbonApiAuthError(
            f"Carbon API rejected the request for zone '{zone}' "
            f"(HTTP {response.status_code}). Either {API_KEY_ENV_VAR} is wrong, "
            "or your plan does not include this zone -- the Electricity Maps "
            "free tier is normally limited to a single home zone."
        )
    if response.status_code >= 400:
        raise CarbonApiResponseError(
            f"Carbon API returned HTTP {response.status_code} for zone '{zone}': "
            f"{_body_snippet(response)}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise CarbonApiResponseError(
            f"Carbon API returned a non-JSON body: {_body_snippet(response)}"
        ) from exc

    return parse_forecast_payload(payload)[:hours]


def summarise(entries: Iterable[ForecastEntry]) -> str:
    """Human-readable one-liner, handy for logs and CLI output."""
    items = list(entries)
    if not items:
        return "no forecast entries"
    lowest = min(items, key=lambda e: e.carbon_intensity)
    highest = max(items, key=lambda e: e.carbon_intensity)
    return (
        f"{len(items)} hourly points, "
        f"{items[0].timestamp.isoformat()} -> {items[-1].timestamp.isoformat()}, "
        f"low {lowest.carbon_intensity:.0f} / "
        f"high {highest.carbon_intensity:.0f} gCO2/kWh"
    )
