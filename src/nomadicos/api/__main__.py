"""`python -m nomadicos.api` — owner-local uvicorn runner (loopback only)."""

from __future__ import annotations

import argparse
import os
import secrets
import sys

import uvicorn

from nomadicos.api.app import create_app
from nomadicos.tools.terminal import TOOL_PROCESS_ENV


def main() -> None:
    parser = argparse.ArgumentParser(prog="nomadicos.api", description="NomadicOS owner API")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    # a task-spawned API would print a fresh owner token into the task's own
    # captured stdout: the owner surface never starts inside a task
    if os.environ.get(TOOL_PROCESS_ENV):
        sys.exit("refused: a NomadicOS task process cannot start the owner API")
    # minted here and shown only on the owner's console; never put in the
    # environment, which every tool process inherits
    token = secrets.token_urlsafe(32)
    print(f"NomadicOS owner token: {token}")
    print(f"Owner UI (dev): http://localhost:5173/#token={token}", flush=True)
    # loopback default: this is the OWNER's surface, not a network service
    uvicorn.run(create_app(owner_token=token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
