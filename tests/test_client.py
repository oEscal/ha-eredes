"""Tests for the E-REDES API client access-token normalization."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from custom_components.eredes.eredes_api.client import ERedesClient
from custom_components.eredes.eredes_api.exceptions import ERedesRequestRejectedError

# A representative JWT-shaped token (base64url segments joined by dots).
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.abc-def_ghi"


def _make_client(access_token: str) -> ERedesClient:
    """Build a client with a stub session (the session is unused by headers)."""
    return ERedesClient(MagicMock(), access_token)


@pytest.mark.parametrize(
    "access_token",
    [
        pytest.param(TOKEN, id="bare-value"),
        pytest.param(f"aat={TOKEN}", id="prefixed"),
        pytest.param(f"  {TOKEN}  ", id="bare-with-whitespace"),
        pytest.param(f"aat={TOKEN};", id="prefixed-trailing-semicolon"),
        pytest.param(f"  aat={TOKEN} ; ", id="prefixed-whitespace-and-semicolon"),
    ],
)
def test_aat_token_normalized_from_various_shapes(access_token: str) -> None:
    """Bare, prefixed and whitespace/semicolon-padded inputs all yield the token."""
    client = _make_client(access_token)
    headers = client._get_headers()

    assert client._aat_token == TOKEN
    assert headers["Authorization-Request"] == TOKEN
    assert headers["Cookie"] == f"aat={TOKEN}"


def test_full_cookie_header_extracts_aat_and_forwards_all_cookies() -> None:
    """A full Cookie header keeps every cookie and extracts the aat token."""
    cookie = f"PHPSESSID=abc123; aat={TOKEN}; SimpleSAML=xyz789"
    client = _make_client(cookie)
    headers = client._get_headers()

    assert client._aat_token == TOKEN
    assert headers["Authorization-Request"] == TOKEN
    assert "PHPSESSID=abc123" in headers["Cookie"]
    assert f"aat={TOKEN}" in headers["Cookie"]
    assert "SimpleSAML=xyz789" in headers["Cookie"]


def test_full_cookie_header_with_trailing_whitespace_and_semicolon() -> None:
    """Trailing whitespace/semicolon in a full header doesn't drop cookies."""
    cookie = f"  PHPSESSID=abc123; aat={TOKEN}; SimpleSAML=xyz789 ; "
    client = _make_client(cookie)
    headers = client._get_headers()

    assert client._aat_token == TOKEN
    assert headers["Authorization-Request"] == TOKEN
    assert "PHPSESSID=abc123" in headers["Cookie"]
    assert "SimpleSAML=xyz789" in headers["Cookie"]


def test_update_access_token_applies_normalization() -> None:
    """update_access_token normalizes a bare token just like construction."""
    client = _make_client(f"aat={TOKEN}")
    new_token = "newheader.newpayload.newsig"

    client.update_access_token(new_token)
    headers = client._get_headers()

    assert client._aat_token == new_token
    assert headers["Authorization-Request"] == new_token
    assert headers["Cookie"] == f"aat={new_token}"


def test_blank_cookie_yields_no_authorization_header() -> None:
    """A blank input produces no aat token and omits the Authorization header."""
    client = _make_client("   ")
    headers = client._get_headers()

    assert client._aat_token == ""
    assert "Authorization-Request" not in headers


def test_empty_result_response_yields_no_readings() -> None:
    """E-REDES -1002 means the requested period has no consumption data."""
    client = _make_client(TOKEN)
    start = datetime(2025, 7, 10)
    end = datetime(2025, 7, 11)

    result = client._parse_consumption_response(
        "PT0002000012345678AB",
        {
            "Header": {
                "Status": {
                    "ResponseCode": -1,
                    "ResponseStatuses": {
                        "ResponseStatus": [
                            {"Code": "-1002", "Description": "result is empty"}
                        ]
                    },
                }
            },
            "Body": {"Success": False, "Result": None},
        },
        start,
        end,
    )

    assert result.readings == []


def test_other_unsuccessful_api_body_raises_rejected_error_with_details() -> None:
    """Other HTTP 200 rejections remain errors instead of creating silent gaps."""
    client = _make_client(TOKEN)
    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 2)

    with pytest.raises(ERedesRequestRejectedError, match="RANGE_TOO_LARGE"):
        client._parse_consumption_response(
            "PT0002000012345678AB",
            {
                "Body": {
                    "Success": False,
                    "Code": "RANGE_TOO_LARGE",
                }
            },
            start,
            end,
        )


def test_parse_real_meter_indexes_uses_valid_active_import_registers() -> None:
    """Request type 1 exposes the real cumulative index used to correct a day."""
    client = _make_client(TOKEN)

    indexes = client._parse_meter_index_response(
        {
            "Body": {
                "Success": True,
                "Result": [
                    {
                        "equipNumber": "12345678",
                        "Readings": {
                            "active": [
                                {
                                    "date": "2026-08-01 00:00:00",
                                    "status": "activa",
                                    "mrType": "1",
                                    "eqNumber": "12345678",
                                    "V": 4062,
                                    "P": 1910,
                                    "C": 4637,
                                },
                                {
                                    "date": "2026-08-02 00:00:00",
                                    "status": "corrigida",
                                    "mrType": "1",
                                    "eqNumber": "12345678",
                                    "V": 4064,
                                    "P": 1912,
                                    "C": 4645,
                                },
                                {
                                    "date": "2026-08-03 00:00:00",
                                    "status": "desactivada",
                                    "mrType": "1",
                                    "V": 9999,
                                    "P": 9999,
                                    "C": 9999,
                                },
                                {
                                    "date": "2026-08-04 00:00:00",
                                    "status": "activa",
                                    "mrType": "3",
                                    "V": 9999,
                                    "P": 9999,
                                    "C": 9999,
                                },
                            ]
                        },
                    }
                ],
            }
        }
    )

    assert [index.value_kwh for index in indexes] == [10609.0, 10621.0]
    assert indexes[0].meter_serial == "12345678"
    # Midnight in Lisbon is 23:00 UTC during WEST.
    assert indexes[0].timestamp.isoformat() == "2026-07-31T23:00:00+00:00"


def test_parse_real_meter_indexes_supports_simple_and_bihourly_registers() -> None:
    """The cumulative total follows the tariff register layout of each reading."""
    client = _make_client(TOKEN)

    indexes = client._parse_meter_index_response(
        {
            "Body": {
                "Success": True,
                "Result": [
                    {
                        "equipNumber": "simple",
                        "Readings": {
                            "active": [
                                {
                                    "date": "2026-01-01 00:00:00",
                                    "status": "activa",
                                    "mrType": "1",
                                    "S": 100.5,
                                }
                            ]
                        },
                    },
                    {
                        "equipNumber": "bi",
                        "Readings": {
                            "active": [
                                {
                                    "date": "2026-01-01 00:00:00",
                                    "status": "activa",
                                    "mrType": "1",
                                    "V": 40,
                                    "FV": 60,
                                }
                            ]
                        },
                    },
                ],
            }
        }
    )

    assert sorted(
        (index.meter_serial, index.value_kwh) for index in indexes
    ) == [
        ("bi", 100.0),
        ("simple", 100.5),
    ]
