"""Network Gateway interface (BP §23-24, §99, §268, ADR-0019 transport).

v0.1: OPEN INTERNET + CLOSED PRIVATE-DATA BOUNDARY — public GETs only; all
requests are mediated by the Security Gate before any transport runs.
"""

from nomadicos.network.base import (
    NetworkRequest,
    NetworkResponse,
    NetworkTransport,
)
from nomadicos.network.fake import FakeNetworkTransport

__all__ = ["FakeNetworkTransport", "NetworkRequest", "NetworkResponse", "NetworkTransport"]
