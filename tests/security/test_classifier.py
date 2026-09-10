"""Classifier red-team tests (BP §265-266, ADR-0019)."""

import pytest

from nomadicos.security.classifier import classify, classify_content, classify_path


@pytest.mark.security
@pytest.mark.parametrize(
    "content",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "api_key = sk-abcdefghijklmnopqrstuvwx",
        "password=SuperSecret123",
        "Authorization: Bearer eyJhbGciOi...",
    ],
)
def test_secret_patterns_classified_sensitive(content: str) -> None:
    assert classify_content(content) == "sensitive"


@pytest.mark.security
@pytest.mark.parametrize(
    "path",
    [
        "C:/Users/me/.ssh/id_rsa",
        "secrets/wallet.dat",
        ".env",
        "config/api_key.txt",
        "certs/server.pem",
        "C:/Windows/System32/config",
    ],
)
def test_sensitive_paths(path: str) -> None:
    assert classify_path(path) == "sensitive"


def test_public_and_internal_paths() -> None:
    assert classify_path("docs/architecture/README.md") == "public"
    assert classify_path("src/core/runtime.py") == "internal"


def test_high_entropy_content_is_sensitive() -> None:
    blob = "xJ9#mK2$pQ7@rT4vW9!xY1zA3#bC5dE7%fG9hJ2&kL4mN6pQ8(rT0vW2)zY4zA6bC8dE0fG2hQ5wR8tZ3"
    assert classify_content(blob) == "sensitive"


def test_combined_classification_takes_stricter() -> None:
    # benign path + secret content ⇒ sensitive
    assert classify("notes/readme.md", "password=hunter2") == "sensitive"
    # sensitive path + benign content ⇒ sensitive
    assert classify(".env", "hello world") == "sensitive"
