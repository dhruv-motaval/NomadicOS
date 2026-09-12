"""STEP 3.5 — DNS → private-address SSRF guard tests (fully hermetic).

A fake resolver stands in for DNS; the transport handler records whether the
transport was EVER touched. No live network, no real DNS.
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.errors import NetworkDenied
from nomadicos.network.base import NetworkRequest, NetworkResponse, NetworkTransport
from nomadicos.network.gateway import NetworkGateway
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity

POLICY = """
version: "1.0.0"
owner:
  autonomy_level: assisted
  tools: []
  external_network:
    allow_public_get: true
"""

IDENT = SubjectIdentity(user_id="u", session_id="s", task_id="t")
PUBLIC = "93.184.216.34"
OK = NetworkResponse(status=200, body=b"<html>hi</html>", url="x", elapsed_ms=1.0)


class HandlerTransport(NetworkTransport):
    """Returns a canned response or applies a side-effect fn; records URLs."""

    def __init__(self, handler=None) -> None:
        self.requests: list[str] = []
        self._handler = handler

    async def request(self, request: NetworkRequest) -> NetworkResponse:
        self.requests.append(request.url)
        if self._handler is not None:
            return self._handler(request)
        return OK.model_copy(update={"url": request.url})


def _gateway(tmp_path, table, transport=None):
    """table: host(lowercase) -> list[str] or callable raising."""

    def resolver(host: str, port: int) -> list[str]:
        key = host.lower().strip("[]")
        if key not in table:
            raise socket.gaierror(f"no such host {key}")
        entry = table[key]
        if callable(entry):
            return entry()
        return list(entry)

    path = tmp_path / "policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    policy = PolicyEngine()
    policy.load_file(path)
    gate = SecurityGate(policy, PermissionEngine(), FakeAuditSink())
    sink = FakeAuditSink()
    transport = transport or HandlerTransport()
    return NetworkGateway(gate, sink, transport, resolver=resolver), transport, sink


# ---- integration-style requirement: public LOOKING hostname + private addr --
def test_public_looking_name_resolving_private_blocks_before_transport(tmp_path):
    gateway, transport, sink = _gateway(
        tmp_path, {"internal.example.com": ["127.0.0.1"]}
    )
    with pytest.raises(NetworkDenied) as exc:
        asyncio.run(gateway.fetch_public("https://internal.example.com/x", IDENT))
    assert transport.requests == []
    assert "NETWORK_PRIVATE_ADDRESS" in str(exc.value)
    blocks = [e for e in sink.events if e.decision == "BLOCK"]
    assert blocks and blocks[-1].fields.get("reason_code") == "NETWORK_PRIVATE_ADDRESS"


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1", "127.5.5.5",
        "10.1.2.3", "172.16.9.9", "192.168.1.1",
        "169.254.169.254", "100.64.0.1", "0.0.0.0",
        "::1", "fd12:3456::789a", "fe80::1", "::",
        "::ffff:127.0.0.1",
    ],
)
def test_blocked_address_families(tmp_path, addr):
    gateway, transport, _ = _gateway(tmp_path, {"evil.test": [addr]})
    with pytest.raises(NetworkDenied):
        asyncio.run(gateway.fetch_public("https://evil.test/p", IDENT))
    assert transport.requests == []


def test_private_sibling_in_multi_record_blocks(tmp_path):
    gateway, transport, _ = _gateway(
        tmp_path, {"mixed.test": [PUBLIC, "10.0.0.8"]}
    )
    with pytest.raises(NetworkDenied):
        asyncio.run(gateway.fetch_public("https://mixed.test/p", IDENT))
    assert transport.requests == []


def test_all_public_multi_record_passes(tmp_path):
    gateway, transport, _ = _gateway(
        tmp_path, {"ok.test": [PUBLIC, "2606:2800:220:1::26"]}
    )
    doc = asyncio.run(gateway.fetch_public("https://ok.test/p", IDENT))
    assert doc.untrusted is True
    assert len(transport.requests) == 1


def test_dns_failure_fails_closed(tmp_path):
    gateway, transport, sink = _gateway(tmp_path, {})
    with pytest.raises(NetworkDenied) as exc:
        asyncio.run(gateway.fetch_public("https://nowhere.test/", IDENT))
    assert "NETWORK_DNS_FAILURE" in str(exc.value)
    assert transport.requests == []
    assert any(e.decision == "BLOCK" for e in sink.events)


def test_empty_resolution_denied(tmp_path):
    gateway, _, _ = _gateway(tmp_path, {"empty.test": []})
    with pytest.raises(NetworkDenied):
        asyncio.run(gateway.fetch_public("https://empty.test/", IDENT))


def test_garbage_resolution_blocked(tmp_path):
    gateway, transport, _ = _gateway(tmp_path, {"junk.test": ["999.1.1.1"]})
    with pytest.raises(NetworkDenied):
        asyncio.run(gateway.fetch_public("https://junk.test/", IDENT))
    assert transport.requests == []


def test_redirect_host_change_revalidated(tmp_path):
    """Hop-1 public 302 -> hop-2 public-looking name resolving private: the
    redirected request must NEVER touch the transport."""

    def handler(request: NetworkRequest) -> NetworkResponse:
        if request.url == "https://start.test/":
            return NetworkResponse(
                status=302, headers={"location": "http://pivot.test/"},
                body=b"", url=request.url, elapsed_ms=1.0,
            )
        return NetworkResponse(status=200, body=b"<p>x</p>", url=request.url, elapsed_ms=1.0)

    gateway, transport, _ = _gateway(
        tmp_path,
        {"start.test": [PUBLIC], "pivot.test": ["192.168.0.9"]},
        HandlerTransport(handler),
    )
    with pytest.raises(NetworkDenied) as exc:
        asyncio.run(gateway.fetch_public("https://start.test/", IDENT))
    assert "NETWORK_PRIVATE_ADDRESS" in str(exc.value)
    assert transport.requests == ["https://start.test/"]


def test_case_insensitive_lookup_blocks_private(tmp_path):
    gateway, transport, _ = _gateway(tmp_path, {"upper.test": ["127.0.0.1"]})
    with pytest.raises(NetworkDenied):
        asyncio.run(gateway.fetch_public("https://UPPER.TEST/", IDENT))
    assert transport.requests == []


def test_resolver_none_keeps_string_policy(tmp_path):
    """Without an injected resolver, string-level gate policy still refuses
    literal private hosts (STEP-3 gate regression preserved)."""
    path = tmp_path / "p.yaml"
    path.write_text(POLICY, encoding="utf-8")
    policy = PolicyEngine()
    policy.load_file(path)
    sink = FakeAuditSink()
    gate = SecurityGate(policy, PermissionEngine(), sink)
    gateway = NetworkGateway(gate, sink, HandlerTransport())
    with pytest.raises(NetworkDenied):
        asyncio.run(gateway.fetch_public("http://127.0.0.1:8080/admin", IDENT))
    assert gateway._resolver is None
