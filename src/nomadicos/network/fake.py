"""Fake network transport for tests — deterministic, no network access."""

from nomadicos.network.base import NetworkRequest, NetworkResponse, NetworkTransport


class FakeNetworkTransport(NetworkTransport):
    def __init__(self, responses: dict[str, NetworkResponse] | None = None) -> None:
        self._responses = responses or {}
        self.requests: list[NetworkRequest] = []

    def register(self, url_prefix: str, response: NetworkResponse) -> None:
        self._responses[url_prefix] = response

    async def request(self, request: NetworkRequest) -> NetworkResponse:
        self.requests.append(request)
        for prefix, response in self._responses.items():
            if request.url.startswith(prefix):
                return response
        return NetworkResponse(
            status=404, body=b"not found", url=request.url, elapsed_ms=0.1
        )


__all__ = ["FakeNetworkTransport"]
