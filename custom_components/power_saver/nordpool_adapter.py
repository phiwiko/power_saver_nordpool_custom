"""Adapter for fetching prices from HACS Nordpool, native HA Nordpool, or custom Nordpool sensors."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import NORDPOOL_TYPE_HACS, NORDPOOL_TYPE_NATIVE

_LOGGER = logging.getLogger(__name__)

def detect_nordpool_type(hass: HomeAssistant, entity_id: str) -> str:
    """Detect whether an entity is a HACS Nord Pool, native HA Nord Pool, or custom Nordpool sensor.

    Returns:
        "hacs", "native", or "unknown".
    """
    state = hass.states.get(entity_id)
    if state is not None and state.attributes.get("raw_today") is not None:
        return NORDPOOL_TYPE_HACS  # Erkennt HACS-Nordpool UND nordpool_custom-Sensoren

    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    if entity_entry is not None and entity_entry.platform == "nordpool":
        return NORDPOOL_TYPE_NATIVE

    # NEU: Prüfe auf nordpool_custom-Sensoren
    if entity_entry is not None and entity_entry.domain == "nordpool_custom":
        return NORDPOOL_TYPE_HACS  # Behandle nordpool_custom als HACS-Nordpool

    return "unknown"

def _get_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Get the friendly name for an entity, falling back to entity_id."""
    state = hass.states.get(entity_id)
    if state is not None:
        return state.attributes.get("friendly_name", entity_id)
    return entity_id

def find_all_nordpool_sensors(
    hass: HomeAssistant,
) -> list[tuple[str, str, str]]:
    """Find all available Nord Pool sensors (HACS, native, and custom).

    For native Nord Pool, only returns the main "current price" sensor per
    config entry (filters out diagnostic/statistical sensors).

    Returns:
        List of (entity_id, nordpool_type, label) tuples.
    """
    registry = er.async_get(hass)
    found: list[tuple[str, str, str]] = []
    seen_entity_ids: set[str] = set()

    # Check for HACS Nord Pool and nordpool_custom sensors
    for entity_entry in registry.entities.values():
        if entity_entry.domain != "sensor":
            continue

        # Prüfe auf HACS-Nordpool oder nordpool_custom
        if entity_entry.platform == "nordpool" or entity_entry.domain == "nordpool_custom":
            state = hass.states.get(entity_entry.entity_id)
            if state is not None and state.attributes.get("raw_today") is not None:
                label = _get_friendly_name(hass, entity_entry.entity_id)
                _LOGGER.debug("Found Nord Pool sensor: %s (type: %s)",
                             entity_entry.entity_id,
                             "hacs" if entity_entry.platform == "nordpool" else "custom")
                found.append((entity_entry.entity_id, NORDPOOL_TYPE_HACS, label))
                seen_entity_ids.add(entity_entry.entity_id)

    # Check for native Nord Pool: all config entries with domain "nordpool"
    # Native unique_id format: "{area}-{key}" — only include "current_price" sensors
    for config_entry in hass.config_entries.async_entries("nordpool"):
        entity_entries = er.async_entries_for_config_entry(
            registry, config_entry.entry_id
        )
        for entity_entry in entity_entries:
            if (
                entity_entry.domain == "sensor"
                and entity_entry.entity_id not in seen_entity_ids
                and entity_entry.unique_id is not None
                and entity_entry.unique_id.endswith("-current_price")
            ):
                label = _get_friendly_name(hass, entity_entry.entity_id)
                _LOGGER.debug(
                    "Found native Nord Pool sensor: %s",
                    entity_entry.entity_id,
                )
                found.append((entity_entry.entity_id, NORDPOOL_TYPE_NATIVE, label))
                seen_entity_ids.add(entity_entry.entity_id)

    return found

def auto_detect_nordpool(
    hass: HomeAssistant,
) -> tuple[str, str] | tuple[None, None]:
    """Auto-detect a Nord Pool integration (HACS, native, or custom).

    Checks for HACS Nord Pool first (entity with raw_today attribute),
    then falls back to native HA Nord Pool (config entry with domain "nordpool").

    Returns:
        Tuple of (entity_id, nordpool_type) or (None, None) if not found.
    """
    sensors = find_all_nordpool_sensors(hass)
    if sensors:
        entity_id, nordpool_type, _label = sensors[0]
        return entity_id, nordpool_type
    return None, None

async def async_get_prices(
    hass: HomeAssistant,
    entity_id: str,
    nordpool_type: str,
) -> tuple[list[dict], list[dict]]:
    """Fetch today's and tomorrow's prices, normalized to [{start, end, value}].

    Args:
        hass: Home Assistant instance.
        entity_id: The Nord Pool sensor entity ID.
        nordpool_type: "hacs" or "native".

    Returns:
        Tuple of (raw_today, raw_tomorrow) in HACS-compatible format.
    """
    if nordpool_type == NORDPOOL_TYPE_HACS:
        return _get_hacs_prices(hass, entity_id)
    if nordpool_type == NORDPOOL_TYPE_NATIVE:
        return await _async_get_native_prices(hass, entity_id)

    _LOGGER.error("Unknown nordpool_type: %s", nordpool_type)
    return [], []

def _get_hacs_prices(
    hass: HomeAssistant, entity_id: str
) -> tuple[list[dict], list[dict]]:
    """Read prices from HACS Nord Pool or custom sensor attributes."""
    state = hass.states.get(entity_id)
    if state is None:
        return [], []

    raw_today = state.attributes.get("raw_today") or []
    raw_tomorrow = state.attributes.get("raw_tomorrow") or []
    return raw_today, raw_tomorrow

async def _async_get_native_prices(
    hass: HomeAssistant, entity_id: str
) -> tuple[list[dict], list[dict]]:
    """Fetch prices from native HA Nord Pool.

    Tries to read directly from the native coordinator's cached data first
    (which already contains tomorrow's prices from the batch API call).
    Falls back to individual service calls if direct read is unavailable.
    """
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    if entity_entry is None or entity_entry.config_entry_id is None:
        _LOGGER.error(
            "Cannot find config entry for native Nord Pool entity %s", entity_id
        )
        return [], []

    config_entry_id = entity_entry.config_entry_id

    # Primary: read directly from native coordinator's cached data.
    config_entry = hass.config_entries.async_get_entry(config_entry_id)
    if config_entry is not None:
        result = _get_native_coordinator_prices(config_entry)
        if result is not None:
            raw_today, raw_tomorrow = result
            _LOGGER.debug(
                "Read prices from native coordinator cache: today=%d, tomorrow=%d",
                len(raw_today),
                len(raw_tomorrow),
            )
            return raw_today, raw_tomorrow

    # Fallback: individual service calls
    _LOGGER.debug("Falling back to service calls for native Nord Pool prices")
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)

    raw_today = await _async_fetch_native_date(hass, config_entry_id, today)
    raw_tomorrow = await _async_fetch_native_date(hass, config_entry_id, tomorrow)

    return raw_today, raw_tomorrow

def _get_native_coordinator_prices(
    config_entry,
) -> tuple[list[dict], list[dict]] | None:
    """Read prices directly from the native Nord Pool coordinator's cached data."""
    coordinator = getattr(config_entry, "runtime_data", None)
    if coordinator is None:
        return None

    data = getattr(coordinator, "data", None)
    if data is None:
        return None

    entries = getattr(data, "entries", None)
    if not entries:
        return None

    # Get area(s) from the native config entry's data
    areas = config_entry.data.get("areas", [])
    if not areas:
        _LOGGER.debug("No areas configured in native Nord Pool config entry")
        return None
    area = areas[0]

    now = dt_util.now()
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    today_prices: list[dict] = []
    tomorrow_prices: list[dict] = []

    try:
        for delivery_period in entries:
            requested_date = getattr(delivery_period, "requested_date", None)
            period_entries = getattr(delivery_period, "entries", [])

            if requested_date == today_str:
                target = today_prices
            elif requested_date == tomorrow_str:
                target = tomorrow_prices
            else:
                continue

            for entry in period_entries:
                start = entry.start
                end = entry.end
                price_mwh = entry.entry.get(area)
                if price_mwh is None:
                    continue

                target.append({
                    "start": (
                        start if isinstance(start, str) else start.isoformat()
                    ),
                    "end": end if isinstance(end, str) else end.isoformat(),
                    "value": float(price_mwh) / 1000.0,
                })
    except (AttributeError, TypeError, KeyError, ValueError) as exc:
        _LOGGER.warning(
            "Malformed cached Nord Pool data: %s", exc,
            exc_info=True,
        )
        return None

    if not today_prices:
        return None

    return today_prices, tomorrow_prices

async def _async_fetch_native_date(
    hass: HomeAssistant, config_entry_id: str, target_date: date
) -> list[dict]:
    """Call nordpool.get_prices_for_date and convert to standard format."""
    try:
        response = await hass.services.async_call(
            "nordpool",
            "get_prices_for_date",
            {
                "config_entry": config_entry_id,
                "date": str(target_date),
            },
            blocking=True,
            return_response=True,
        )
    except (HomeAssistantError, KeyError, ValueError):
        _LOGGER.debug(
            "Failed to fetch native Nord Pool prices for %s (may not be available yet)",
            target_date,
            exc_info=True,
        )
        return []

    if not response:
        return []

    return _convert_native_response(response)

def _convert_native_response(response: dict | list) -> list[dict]:
    """Convert native Nord Pool service response to HACS-compatible format."""
    price_list: list[dict] = []

    if isinstance(response, dict):
        for _area, prices in response.items():
            if isinstance(prices, list):
                price_list = prices
                _LOGGER.debug("Using prices from area: %s", _area)
                break
    elif isinstance(response, list):
        price_list = response

    if not price_list:
        return []

    converted: list[dict] = []
    for entry in price_list:
        try:
            start = entry.get("start")
            end = entry.get("end")
            price_mwh = entry.get("price")

            if start is None or price_mwh is None:
                continue

            price_kwh = float(price_mwh) / 1000.0

            if end is None:
                start_dt = (
                    start
                    if isinstance(start, datetime)
                    else datetime.fromisoformat(start)
                )
                end_dt = start_dt + timedelta(hours=1)
                end = end_dt if isinstance(start, datetime) else end_dt.isoformat()

            converted.append({
                "start": start,
                "end": end,
                "value": price_kwh,
            })
        except (ValueError, TypeError) as exc:
            _LOGGER.warning("Error converting native Nord Pool entry: %s", exc)
            continue

    return converted
