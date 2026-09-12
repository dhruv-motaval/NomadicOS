"""Deterministic DNS validation for public web access (STEP 3.5, SSRF guard).

A public-looking hostname that RESOLVES to a non-public address is treated
like a private destination: blocked before any transport request is made.
Resolution is injected so tests are hermetic (no live DNS needed).
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# (host, port) -> addresses; may raise (DNS failure) or return empty.
Resolver = Callable[[str, int], Sequence[str]]

# IPvFuture/zone markers e.g. fe80::1%eth0
_ZONE_RE = re.compile(r"%[0-9A-Za-z_.\-]+$")

# Python 3.12's is_private does not include CGNAT — treat it as non-public.
_EXTRA_FORBIDDEN_V4 = (ipaddress.ip_network("100.64.0.0/10"),)


def is_forbidden_address(raw: str) -> bool:
    """True when a resolved IP must not be used as an outbound destination
    (loopback/private/CGNAT/ULA/link-local/reserved/multicast/unspecified +
    IPv4-mapped IPv6)."""
    candidate = _ZONE_RE.sub("", raw.strip().strip("[]"))
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return True  # unparseable resolution result => fail closed
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.version == 4 and any(ip in net for net in _EXTRA_FORBIDDEN_V4):
        return True
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def validate_targets(host: str, addrs: Sequence[str]) -> None:
    """Raise ValueError(f"NETWORK_DNS_FAILURE|NETWORK_PRIVATE_ADDRESS", detail)."""
    if not addrs:
        raise ValueError(f"NETWORK_DNS_FAILURE: host {host!r} resolved no addresses")
    bad = [a for a in addrs if is_forbidden_address(str(a))]
    if bad:
        raise ValueError(
            f"NETWORK_PRIVATE_ADDRESS: host {host!r} resolves to blocked "
            f"address(es): {sorted(set(bad))[:4]}"
        )


def system_resolver(host: str, port: int) -> list[str]:
    """Production resolver over the system DNS stack (blocking; call in thread)."""
    import socket

    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


__all__ = ["Resolver", "is_forbidden_address", "system_resolver", "validate_targets"]
