"""E-REDES API client library."""

from .client import ERedesClient
from .exceptions import (
    ERedesAuthenticationError,
    ERedesConnectionError,
    ERedesError,
    ERedesRequestRejectedError,
)
from .models import ConsumptionData, ConsumptionReading

__all__ = [
    "ConsumptionData",
    "ConsumptionReading",
    "ERedesAuthenticationError",
    "ERedesClient",
    "ERedesConnectionError",
    "ERedesError",
    "ERedesRequestRejectedError",
]
