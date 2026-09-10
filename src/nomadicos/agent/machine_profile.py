"""Machine profile: one-time local scan of THIS PC, injected into every task.

Solves the "open chrome" class of failures permanently: the model already
knows what Chrome IS - it lacks the local launch verb (start chrome) and OS
facts (no bash/which on Windows). The profile is generated locally (registry
scan + static verb table) and stored under data/ (I11: never leaves the PC).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_PROFILE_NAME = "machine-profile.md"

# Static launch verbs for universally-known apps. The model knows the app;
# this table gives it the local command.
COMMON_APPS: dict[str, str] = {
    "chrome": "start chrome",
    "edge": "start msedge",
    "firefox": "start firefox",
    "notepad": "notepad",
    "calculator": "start calc",
    "paint": "start mspaint",
    "explorer": "explorer",
    "task manager": "start taskmgr",
    "cmd": "start cmd",
    "vscode": "code",
    "spotify": "start spotify",
    "terminal": "start powershell",
}

_APP_PATHS_CMD = (
    "powershell -NoProfile -Command "
    "(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\*'"
    " -ErrorAction SilentlyContinue).PSChildName"
)


def scan_installed_apps() -> list[str]:
    """Locally read registered apps from the Windows App Paths registry."""
    try:
        raw = subprocess.run(
            _APP_PATHS_CMD.split(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        apps = [
            line.strip().removesuffix(".exe").lower()
            for line in raw.stdout.splitlines()
            if line.strip().endswith(".exe")
        ]
        return sorted(set(apps))
    except Exception:  # noqa: BLE001 — scan is best effort, table covers basics
        return []


def build_profile(installed_apps: list[str] | None = None) -> str:
    """Compose the machine profile text (small: ~25 lines, always injected)."""
    apps = installed_apps if installed_apps is not None else scan_installed_apps()
    lines = [
        "## THIS MACHINE (Windows, PowerShell) - facts, always true",
        "- OS: Windows. Shell: PowerShell. There is NO bash, NO which, NO /home.",
        "- Launch an app: start <appname>. Example: start chrome",
        "- Check an app exists: where.exe <appname>",
        "- Common apps and their EXACT launch commands:",
    ]
    lines += [f"  - {name}: {cmd}" for name, cmd in COMMON_APPS.items()]
    if apps:
        shown = ", ".join(apps[:40])
        lines.append(f"- Apps registered on this PC (launch with start <name>): {shown}")
    lines.append(
        "- Music/videos: launch the browser with a URL as an argument, e.g. "
        "start chrome https://www.youtube.com/results?search_query=<q>"
    )
    lines.append(
        "- Files written for tasks go under the task workspace, not /tmp or /home."
    )
    return "\n".join(lines)


def ensure_profile(data_dir: Path, *, rescan: bool = False) -> str:
    """Return the profile text, generating it once and caching under data/."""
    path = data_dir / _PROFILE_NAME
    if not path.exists() or rescan:
        text = build_profile()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    return path.read_text(encoding="utf-8", errors="replace")


__all__ = ["build_profile", "ensure_profile", "scan_installed_apps"]
