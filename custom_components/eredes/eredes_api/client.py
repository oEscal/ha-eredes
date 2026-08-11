"""E-REDES API client."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .exceptions import (
    ERedesAuthenticationError,
    ERedesConnectionError,
    ERedesError,
    ERedesRequestRejectedError,
)
from .models import ConsumptionData, ConsumptionReading, MeterIndex

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://balcaodigital.e-redes.pt"
API_URL = f"{BASE_URL}/ms/reading/data-usage/edm/get"

# Load-curve timestamps are Lisbon wall-clock time despite their ``Z`` suffix.
LISBON = ZoneInfo("Europe/Lisbon")


def _active_import_index(reading: dict[str, Any]) -> tuple[float, int] | None:
    """Return cumulative active-import kWh and its quantized register count.

    E-REDES exposes different active register layouts depending on the tariff:
    ``S`` for simple, ``V``/``FV`` for bi-hourly, ``V``/``P``/``C`` for
    tri-hourly, and ``SV``/``VN``/``P``/``C`` for four-period meters.
    """

    def _sum_registers(registers: tuple[str, ...]) -> tuple[float, int] | None:
        values = [
            float(reading[register])
            for register in registers
            if reading.get(register) is not None
        ]
        return (sum(values), len(values)) if values else None

    if reading.get("S") is not None:
        return _sum_registers(("S",))
    if reading.get("SV") is not None or reading.get("VN") is not None:
        return _sum_registers(("SV", "VN", "P", "C"))
    if reading.get("FV") is not None:
        return _sum_registers(("V", "FV"))
    return _sum_registers(("V", "P", "C"))


def _to_utc_series(timestamps: list[datetime]) -> list[datetime]:
    """Convert naive Europe/Lisbon wall-clock timestamps to aware UTC.

    E-REDES labels load-curve timestamps with a ``Z`` but reports local Lisbon
    time (see CONTEXT.md → Load curve), so the UTC offset changes across DST.

    The autumn transition repeats one wall-clock hour, and the API returns both
    copies. The first occurrence of a repeated value is read as the
    pre-transition (WEST, UTC+1) reading and the second as post-transition
    (WET, UTC+0) — correct as long as the series arrives in chronological
    order, which is the only ordering that makes the duplicates meaningful.
    Without this, both copies collapse onto the same instant and one hour of
    consumption is double-counted into the other.

    The spring transition needs no handling: the skipped hour simply has no
    readings, leaving a gap that closes naturally in UTC.
    """
    seen: set[datetime] = set()
    utc_timestamps: list[datetime] = []
    for naive in timestamps:
        local = naive.replace(tzinfo=LISBON, fold=1 if naive in seen else 0)
        seen.add(naive)
        utc_timestamps.append(local.astimezone(UTC))
    return utc_timestamps


class ERedesClient:
    """Client for interacting with the E-REDES API."""

    def __init__(
        self,
        session: ClientSession,
        access_token: str,
    ) -> None:
        """Initialize the E-REDES client with an access token.

        Args:
            session: aiohttp ClientSession
            access_token: The pasted authentication value. Any of these
                shapes are accepted and treated identically:
                - a bare ``aat`` token value (``eyJ...``)
                - a prefixed pair (``aat=eyJ...``)
                - a full Cookie header
                  (``PHPSESSID=xxx; aat=xxx; SimpleSAML=xxx``)
        """
        self._session = session
        self._apply_access_token(access_token)

    def _apply_access_token(self, access_token: str) -> None:
        """Normalize the pasted value and derive the cookies, token and header."""
        self._cookies = self._normalize_cookies(access_token)
        self._aat_token = self._cookies.get("aat", "")
        self._cookie_header = self._build_cookie_header(self._cookies)

    def _normalize_cookies(self, access_token: str) -> dict[str, str]:
        """Normalize a pasted authentication value into a cookies dict.

        The input is trimmed and parsed as a Cookie header. A bare token value
        contains no ``key=value`` pair, so parsing yields no ``aat`` key; in
        that case the whole trimmed input is treated as the ``aat`` token.
        """
        stripped = access_token.strip()
        cookies = self._parse_cookies(stripped)
        if "aat" not in cookies:
            bare = stripped.rstrip(";").strip()
            if bare:
                cookies["aat"] = bare
        return cookies

    def _parse_cookies(self, cookie_string: str) -> dict[str, str]:
        """Parse a cookie string into a dictionary."""
        cookies: dict[str, str] = {}
        simple_cookie = SimpleCookie()
        simple_cookie.load(cookie_string)
        for key, morsel in simple_cookie.items():
            cookies[key] = morsel.value
        return cookies

    @staticmethod
    def _build_cookie_header(cookies: dict[str, str]) -> str:
        """Serialize a cookies dict into a Cookie request header."""
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    def update_access_token(self, access_token: str) -> None:
        """Update the access token."""
        self._apply_access_token(access_token)

    async def validate_token(self, cpe: str) -> bool:
        """Validate the token by making a simple API call.

        Args:
            cpe: The CPE to use for validation (must be a real CPE for the user)

        Returns:
            True if the token is valid, False otherwise
        """
        try:
            end_date = datetime.now()
            start_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
            await self.get_consumption(cpe, start_date, end_date)
            return True
        except ERedesAuthenticationError:
            return False
        except ERedesError:
            # Other errors mean the token is valid but something else failed
            return True

    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/consumptions/history",
            "User-Agent-Context": "WEB",
            "Show-Loader": "true",
            "Cookie": self._cookie_header,
        }
        # Also include Authorization-Request header if we have the aat token
        if self._aat_token:
            headers["Authorization-Request"] = self._aat_token
        return headers

    async def get_consumption(
        self,
        cpe: str,
        start_date: datetime,
        end_date: datetime,
    ) -> ConsumptionData:
        """Fetch consumption data for the specified date range.

        Args:
            cpe: The CPE (meter) identifier
            start_date: Start of the date range
            end_date: End of the date range

        Returns:
            ConsumptionData with readings for the specified period

        Raises:
            ERedesAuthenticationError: If authentication fails
            ERedesConnectionError: If connection fails
            ERedesError: For other API errors
        """
        # Format dates for API
        start_str = start_date.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_date.strftime("%Y-%m-%d %H:%M:%S")

        payload = {
            "cpe": cpe,
            "request_type": "3",  # 15-minute interval readings
            "start_date": start_str,
            "end_date": end_str,
            "wait": True,
            "formatted": False,
            "nif_requester": None,
            "serial_number": "",
            "nif": None,
        }

        try:
            async with self._session.post(
                API_URL,
                json=payload,
                headers=self._get_headers(),
            ) as response:
                # Capture refreshed session cookie from response
                self._refresh_session_from_response(response)

                if response.status == 401:
                    raise ERedesAuthenticationError(
                        "Token expired - please update your token"
                    )

                if response.status == 403:
                    raise ERedesAuthenticationError("Access denied - invalid token")

                if response.status != 200:
                    raise ERedesError(
                        f"API request failed with status {response.status}"
                    )

                data = await response.json()
                return self._parse_consumption_response(cpe, data, start_date, end_date)

        except ERedesError:
            raise
        except Exception as ex:
            _LOGGER.exception("Error fetching consumption data")
            raise ERedesConnectionError(f"Failed to fetch data: {ex}") from ex

    async def get_meter_indexes(
        self,
        cpe: str,
        start_date: datetime,
        end_date: datetime,
    ) -> list[MeterIndex]:
        """Fetch cumulative real meter indexes for the specified date range.

        This mirrors the Balcão Digital "Leituras > Consultar histórico" request:
        request type 1 with formatted output. Only valid real active-import
        readings are retained by the parser.
        """
        payload = {
            "cpe": cpe,
            "request_type": "1",
            "start_date": start_date.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end_date.strftime("%Y-%m-%d %H:%M:%S"),
            "wait": True,
            "formatted": True,
            "nif_requester": None,
            "serial_number": "",
            "nif": None,
        }

        try:
            async with self._session.post(
                API_URL,
                json=payload,
                headers=self._get_headers(),
            ) as response:
                self._refresh_session_from_response(response)

                if response.status == 401:
                    raise ERedesAuthenticationError(
                        "Token expired - please update your token"
                    )

                if response.status == 403:
                    raise ERedesAuthenticationError("Access denied - invalid token")

                if response.status != 200:
                    raise ERedesError(
                        f"API request failed with status {response.status}"
                    )

                return self._parse_meter_index_response(await response.json())

        except ERedesError:
            raise
        except Exception as ex:
            _LOGGER.exception("Error fetching meter indexes")
            raise ERedesConnectionError(f"Failed to fetch meter indexes: {ex}") from ex

    def _refresh_session_from_response(self, response: Any) -> None:
        """Update session cookie from API response Set-Cookie header."""
        set_cookie = response.headers.get("Set-Cookie", "")
        if "PHPSESSID=" in set_cookie:
            # Extract new PHPSESSID
            match = re.search(r"PHPSESSID=([^;]+)", set_cookie)
            if match:
                new_phpsessid = match.group(1)
                old_phpsessid = self._cookies.get("PHPSESSID", "")
                if new_phpsessid != old_phpsessid:
                    self._cookies["PHPSESSID"] = new_phpsessid
                    self._cookie_header = self._build_cookie_header(self._cookies)
                    _LOGGER.debug("Session refreshed with new PHPSESSID")

    @staticmethod
    def _meter_index_result(data: dict[str, Any]) -> Any:
        """Return request-type-1 result data or raise for a rejected response."""
        body = data.get("Body", {})
        if body.get("Success", False):
            return body.get("Result", [])

        status = data.get("Header", {}).get("Status", {})
        response_statuses = status.get("ResponseStatuses", {}).get(
            "ResponseStatus", []
        )
        if isinstance(response_statuses, dict):
            response_statuses = [response_statuses]
        if any(
            isinstance(item, dict)
            and (
                str(item.get("Code", "")) == "-1002"
                or str(item.get("Description", "")).lower() == "result is empty"
            )
            for item in response_statuses
        ):
            return []

        detail = {
            key: value
            for key, value in body.items()
            if key not in {"Success", "Result"}
        }
        if not detail:
            detail = {key: value for key, value in data.items() if key != "Body"}
        message = "API returned unsuccessful response"
        if detail:
            encoded_detail = json.dumps(
                detail,
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )[:1000]
            message = f"{message}: {encoded_detail}"
        raise ERedesRequestRejectedError(message)

    def _parse_meter_index_record(
        self,
        reading: Any,
        outer_serial: str,
    ) -> tuple[tuple[int, int], MeterIndex] | None:
        """Parse one valid real active-import cumulative reading."""
        if not isinstance(reading, dict):
            return None
        mr_type = str(reading.get("mrType", ""))
        if mr_type not in {"1", "2"}:
            return None
        reading_status = str(reading.get("status", "")).strip().lower()
        if reading_status not in {"activa", "corrigida"}:
            return None

        timestamp_str = reading.get("date")
        if not isinstance(timestamp_str, str):
            return None
        local_timestamp = self._parse_timestamp(timestamp_str)
        index_data = _active_import_index(reading)
        if local_timestamp is None or index_data is None:
            return None

        value_kwh, register_count = index_data
        meter_serial = str(reading.get("eqNumber") or outer_serial)
        timestamp = local_timestamp.replace(tzinfo=LISBON).astimezone(UTC)
        meter_index = MeterIndex(
            timestamp=timestamp,
            value_kwh=value_kwh,
            meter_serial=meter_serial,
            register_count=register_count,
        )
        rank = (
            1 if mr_type == "1" else 0,
            1 if reading_status == "corrigida" else 0,
        )
        return rank, meter_index

    def _parse_meter_index_response(self, data: dict[str, Any]) -> list[MeterIndex]:
        """Parse valid real cumulative active-import indexes from request type 1."""
        raw_result = self._meter_index_result(data)
        if isinstance(raw_result, dict):
            equipment_history = raw_result.get("equipmentHistory", [])
            if not equipment_history and "Readings" in raw_result:
                equipment_history = [raw_result]
        elif isinstance(raw_result, list):
            equipment_history = raw_result
        else:
            equipment_history = []

        # A corrected reading supersedes an active reading at the same meter and
        # instant. An operator reading is preferred to a customer-provided real
        # reading when both exist at the same instant.
        selected: dict[tuple[str, datetime], tuple[tuple[int, int], MeterIndex]] = {}
        for equipment in equipment_history:
            if not isinstance(equipment, dict):
                continue
            readings_data = equipment.get("Readings")
            if not isinstance(readings_data, dict):
                continue
            active = readings_data.get("active", [])
            if not isinstance(active, list):
                continue

            outer_serial = str(equipment.get("equipNumber") or "")
            for reading in active:
                parsed = self._parse_meter_index_record(reading, outer_serial)
                if parsed is None:
                    continue
                rank, meter_index = parsed
                key = (meter_index.meter_serial, meter_index.timestamp)
                current = selected.get(key)
                if current is None or rank > current[0]:
                    selected[key] = (rank, meter_index)

        return sorted(
            (item[1] for item in selected.values()),
            key=lambda index: (index.timestamp, index.meter_serial),
        )

    def _parse_consumption_response(
        self,
        cpe: str,
        data: dict[str, Any],
        start_date: datetime,
        end_date: datetime,
    ) -> ConsumptionData:
        """Parse the consumption API response.

        Response format:
        {
            "Body": {
                "Success": true,
                "Result": {
                    "utilitiesDevices": [{
                        "meterLoadCurves": [{
                            "register": "A+",
                            "loadCurves": [{
                                "loadCurveTimestamp": "2026-01-05T00:15:00Z",
                                "meterLoadCurve": 0.052,
                                "meterLoadCurveUnitMeasurement": "kwh"
                            }]
                        }]
                    }]
                }
            }
        }
        """
        readings: list[ConsumptionReading] = []

        try:
            body = data.get("Body", {})
            if not body.get("Success", False):
                # E-REDES reports a valid empty period as HTTP 200 with
                # Body.Success=false and Header status -1002/result is empty.
                # This commonly happens before the customer's contract began,
                # so it must behave like an empty result rather than aborting a
                # multi-month historical import.
                status = data.get("Header", {}).get("Status", {})
                response_statuses = status.get("ResponseStatuses", {}).get(
                    "ResponseStatus", []
                )
                if isinstance(response_statuses, dict):
                    response_statuses = [response_statuses]
                if any(
                    isinstance(item, dict)
                    and (
                        str(item.get("Code", "")) == "-1002"
                        or str(item.get("Description", "")).lower()
                        == "result is empty"
                    )
                    for item in response_statuses
                ):
                    _LOGGER.debug("API returned no readings for requested period")
                    return ConsumptionData(
                        cpe=cpe,
                        readings=[],
                        start_date=start_date,
                        end_date=end_date,
                    )

                # Other HTTP-200 rejections are genuine API errors. Keep their
                # metadata in the exception so callers do not silently create
                # gaps in historical data.
                detail = {
                    key: value
                    for key, value in body.items()
                    if key not in {"Success", "Result"}
                }
                if not detail:
                    detail = {
                        key: value for key, value in data.items() if key != "Body"
                    }

                message = "API returned unsuccessful response"
                if detail:
                    serialized = json.dumps(
                        detail,
                        ensure_ascii=False,
                        default=str,
                        sort_keys=True,
                    )
                    message = f"{message}: {serialized[:1000]}"
                raise ERedesRequestRejectedError(message)

            result = body.get("Result", {})
            devices = result.get("utilitiesDevices", [])

            for device in devices:
                load_curves_groups = device.get("meterLoadCurves", [])
                for group in load_curves_groups:
                    # We want "A+" register (active energy import)
                    register = group.get("register", "")
                    if register != "A+":
                        continue

                    load_curves = group.get("loadCurves", [])
                    # Collect the group's series before converting: resolving
                    # the repeated autumn hour needs the readings in order,
                    # not one timestamp at a time.
                    local_timestamps: list[datetime] = []
                    values_wh: list[float] = []
                    for curve in load_curves:
                        timestamp_str = curve.get("loadCurveTimestamp")
                        value = curve.get("meterLoadCurve")
                        unit = curve.get("meterLoadCurveUnitMeasurement", "").lower()

                        if timestamp_str and value is not None:
                            # Naive Lisbon wall clock — the trailing Z is a lie
                            timestamp = self._parse_timestamp(timestamp_str)
                            if timestamp:
                                # Value is in kWh, convert to Wh for internal use
                                values_wh.append(
                                    float(value) * 1000
                                    if unit == "kwh"
                                    else float(value)
                                )
                                local_timestamps.append(timestamp)

                    readings.extend(
                        ConsumptionReading(timestamp=timestamp, value_wh=value_wh)
                        for timestamp, value_wh in zip(
                            _to_utc_series(local_timestamps), values_wh, strict=True
                        )
                    )

        except (KeyError, TypeError, ValueError) as ex:
            raise ERedesError(f"Failed to parse consumption response: {ex}") from ex

        # Sort readings by timestamp
        readings.sort(key=lambda r: r.timestamp)

        return ConsumptionData(
            cpe=cpe,
            readings=readings,
            start_date=start_date,
            end_date=end_date,
        )

    def _parse_timestamp(self, timestamp_str: str) -> datetime | None:
        """Parse a load-curve timestamp into a naive Lisbon wall-clock time.

        The ``Z`` suffix is stripped by the format, not honoured: these are
        local times (see ``_to_utc_series``). The result stays naive; the
        caller converts the series to UTC once it has the readings in order.
        """
        formats = [
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(timestamp_str, fmt)
            except ValueError:
                continue
        return None
