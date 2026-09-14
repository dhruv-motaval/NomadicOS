"""Token auth for the local API (ADR-0032).

The token is generated once at first boot and persisted to data/api-token with
user-only permissions. It is never logged (I12). Every endpoint requires
`Authorization: Bearer <token>`.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_TOKEN_FILE = Path("data") / "api-token"


def get_or_create_token(data_dir: Path | None = None) -> str:
    """Return the API token, creating it on first use."""
    path = _TOKEN_FILE if data_dir is None else data_dir / "api-token"
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:  # Windows: user-only file; best effort elsewhere
        path.chmod(0o600)
    except OSError:
        pass
    return token


_bearer = HTTPBearer(auto_error=False)


def require_token(
    credentials: HTTPAuthorizationCredentials | None,
    expected: str,
) -> None:
    """Fail closed on missing/invalid tokens (BP §85)."""
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
