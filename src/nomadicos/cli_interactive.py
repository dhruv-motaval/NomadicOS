"""NomadicOS interactive CLI — Claude Code style (ADR-0015).

`python -m nomadicos` opens an arrow-key main menu (↑/↓ + Enter, Esc exits):

    ❯ Chat            — the REPL: any text = a real task for the agent
    ❯ Previous tasks  — task history
    ...

The runtime keeps working headlessly; the REPL is a client of the same
Security-Gated pipeline as every other input layer (I5 — no bypass).
"""

import asyncio
import sys
from pathlib import Path

from nomadicos.core.logging import configure_logging, get_logger

try:  # Windows raw-key reader; other platforms fall back to typed selection.
    import msvcrt
except ImportError:  # pragma: no cover — non-Windows
    msvcrt = None  # type: ignore[assignment]

logger = get_logger("cli")

# Structured logs go here for interactive sessions (console stays clean).
LOG_FILE = Path(__file__).resolve().parents[2] / "data" / "logs" / "nomadicos.log"

# (key, label, description) — key is also the direct-select hotkey.
MENU_ITEMS: list[tuple[str, str, str]] = [
    ("1", "Chat", "talk to the agent (each message = a task)"),
    ("2", "Previous tasks", "recent task history"),
    ("3", "Model fleet", "registered models & status"),
    ("4", "Sync models", "detect new/removed/re-pulled Ollama models"),
    ("5", "Memory", "search what the agent remembers"),
    ("6", "Status", "subsystem health"),
    ("0", "Close", "exit"),
]

MENU_HEADER = r"""
──────────────────────────────────────────
  ↑/↓ move · Enter select · Esc exit
──────────────────────────────────────────"""

CHAT_BANNER = (
    "\n  Chat mode — type a goal and press Enter. The agent executes it.\n"
    "  /help for commands · /menu to return to the menu · /close to exit\n"
)

SLASH_HELP = """
Commands:
  /help              show this list
  /sessions [n]      recent task history
  /models            registered model fleet
  /sync              re-sync fleet with Ollama (detect new/removed models)
  /memory <query>    search what the agent remembers
  /status            subsystem health
  /clear             clear screen
  /menu              return to the main menu
  /close             exit NomadicOS
"""

BANNER = "\n  NomadicOS v0.1.0 — local-first AI operating environment\n" + "─" * 56


class InteractiveCLI:
    """REPL client over the same Runtime interface as every input layer."""

    def __init__(self, runtime, *, input_fn=input, output_fn=print) -> None:
        self.runtime = runtime
        self._input = input_fn
        self._print = output_fn

    # ------------------------------------------------------------------ run

    def run(self) -> int:
        # One event loop for the whole session: async resources (Ollama HTTP
        # clients) stay bound to it — per-task asyncio.run() would close the
        # loop under them ("Event loop is closed").
        try:
            return asyncio.run(self._session())
        except KeyboardInterrupt:
            self._print("\nClosing NomadicOS. Goodbye.")
            return 0

    async def _session(self) -> int:
        try:
            await self.runtime.register_ollama_models()
        except Exception as exc:  # noqa: BLE001 — fleet offline ⇒ fakes remain (BP §237)
            logger.warning("fleet registration unavailable: %s", type(exc).__name__)

        while True:
            key = self._menu_select()
            if key == "0":
                self._print("\nClosing NomadicOS. Goodbye.")
                return 0
            if key == "1":
                if await self._chat():
                    return 0
                continue

            menu_map = {
                "2": "/sessions",
                "3": "/models",
                "4": "/sync",
                "5": "/memory",
                "6": "/status",
            }
            await self._slash_command(menu_map[key])

    # --------------------------------------------------------------- chat mode

    async def _chat(self) -> bool:
        """The task REPL: any non-slash line = a goal; /menu returns to the menu.

        Returns True only for /close (exit the session)."""
        self._print(CHAT_BANNER)
        while True:
            try:
                line = self._input("\n❯ ").strip()
            except (EOFError, KeyboardInterrupt):
                return False  # back to the menu

            if not line:
                continue
            # Legacy digit shortcuts (0=back to menu, 2-6=actions).
            if line in ("0", "2", "3", "4", "5", "6"):
                if line == "0":
                    return False
                await self._slash_command(
                    {
                        "2": "/sessions",
                        "3": "/models",
                        "4": "/sync",
                        "5": "/memory",
                        "6": "/status",
                    }[line]
                )
                continue
            if line.startswith("/"):
                if line.split(maxsplit=1)[0].lower() in ("/menu", "/back"):
                    return False  # back to the main menu
                if await self._slash_command(line):
                    return True  # /close already said goodbye
                continue

            self._print("[agent working…]")
            try:
                report = await self.runtime.run_goal(line)
            except KeyboardInterrupt:
                self._print("[interrupted — task aborted]")
                continue
            except Exception as exc:  # noqa: BLE001 — truthful error surface (BP §69)
                self._print(f"[failed] {type(exc).__name__}: {exc}")
                continue
            self._print(report.render())

    # ------------------------------------------------------------- menu select

    def _menu_select(self) -> str:
        """Arrow-key menu; returns the selected item key ("0".."6").

        The screen is cleared on every render so the menu always occupies
        fixed rows at the top — redraw cursor math can never cross stale
        scrolled content (this was the source of duplicated/garbled rows).
        Falls back to typed selection when stdin is not a console."""
        selected = 0
        if msvcrt is not None and sys.stdin.isatty():
            import os

            os.system("")  # enable ANSI escape processing on Windows consoles
            self._print("\x1b[2J\x1b[H", end="")  # clear screen, cursor home
            self._print(BANNER)
            self._print(MENU_HEADER)
            self._draw_menu(selected)
            while True:
                key = self._read_menu_key()
                moved = 0
                if key == "up":
                    moved = -1 if selected > 0 else 0  # clamp — list stays still
                elif key == "down":
                    moved = 1 if selected < len(MENU_ITEMS) - 1 else 0
                if moved:
                    selected += moved
                    self._redraw_menu(selected)
                elif key == "esc":
                    return "0"
                elif key in ("enter", " "):
                    return MENU_ITEMS[selected][0]
                elif key in {item[0] for item in MENU_ITEMS}:
                    return key
        # Typed fallback (piped stdin / no raw console).
        valid = {item[0] for item in MENU_ITEMS}
        while True:
            try:
                line = self._input("\n❯ select: ").strip()
            except (EOFError, KeyboardInterrupt):
                return "0"
            if line in valid:
                return line
            self._print(f"[?] choose one of: {', '.join(sorted(valid))}")

    def _read_menu_key(self) -> str:
        """Read one raw keypress; map arrows/enter/esc to names."""
        try:
            ch = msvcrt.getwch()
        except (OSError, KeyboardInterrupt):
            return "esc"
        if ch in ("\x00", "\xe0"):  # extended key code follows
            ext = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ext, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":  # Ctrl+C
            return "esc"
        return ch

    def _draw_menu(self, selected: int) -> None:
        # Every line ends with a newline: the cursor always rests on the row
        # BELOW the block, so later prints never overwrite the last item.
        for _, label, desc in MENU_ITEMS:
            self._print(self._menu_line(selected, label, desc))

    def _redraw_menu(self, selected: int) -> None:
        self._print(f"\x1b[{len(MENU_ITEMS)}A", end="")  # up to the first row
        self._draw_menu(selected)

    @staticmethod
    def _menu_line(selected: int, label: str, desc: str) -> str:
        cursor = "❯" if label == MENU_ITEMS[selected][1] else " "
        text = f" {cursor} {label:<15} — {desc}"
        # \r guards the column; \x1b[2K clears the whole row before rewrite.
        body = f"\x1b[1m{text}\x1b[0m" if cursor == "❯" else text
        return f"\r\x1b[2K{body}"

    # ------------------------------------------------------- slash commands

    async def _slash_command(self, line: str) -> bool:
        """Returns True only for /close (exit signal)."""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/close", "/quit", "/exit", "/q"):
            self._print("Closing NomadicOS. Goodbye.")
            return True
        if cmd == "/help":
            self._print(SLASH_HELP)
        elif cmd == "/sessions":
            self._sessions(arg)
        elif cmd == "/models":
            self._models()
        elif cmd == "/sync":
            await self._sync()
        elif cmd == "/memory":
            await self._memory(arg)
        elif cmd == "/status":
            self._print(self.runtime.status_line())
        elif cmd == "/clear":
            self._print("\033[2J\033[H", end="")
        else:
            self._print(f"[?] Unknown command {cmd!r} — /help for the list.")
        return False

    def _sessions(self, arg: str) -> None:
        try:
            limit = int(arg) if arg else 10
        except ValueError:
            limit = 10
        tasks = self.runtime.recent_tasks(limit=limit)
        if not tasks:
            self._print("No tasks recorded yet (persistence requires PostgreSQL).")
            return
        self._print("\nRecent tasks:")
        for task in tasks:
            self._print(
                f"  {task['created_at']:%Y-%m-%d %H:%M}  [{task['status']:>21}]  "
                f"{str(task['goal'])[:80]}"
            )

    def _models(self) -> None:
        models = self.runtime.manager.list_available()
        if not models:
            self._print("No models registered. Try /sync with Ollama running.")
            return
        self._print("\nRegistered models:")
        for model in models:
            caps = [
                c
                for c, v in (
                    ("text", model.capabilities.text_generation),
                    ("vision", model.capabilities.vision),
                    ("tools", model.capabilities.tool_use),
                    ("long-ctx", model.capabilities.long_context),
                )
                if v
            ]
            self._print(f"  {model.model_id:<40} [{model.status.value}] {','.join(caps) or '-'}")

    async def _sync(self) -> None:
        self._print("Syncing model fleet with Ollama server…")
        counts = await self.runtime.sync_fleet(force=True)
        self._print(
            f"Fleet sync: added={counts['added']} removed={counts['removed']} "
            f"changed={counts['changed']} healthy={counts['healthy']} "
            f"failed={counts['failed']}"
        )
        self._models()

    async def _memory(self, query: str) -> None:
        if not query:
            self._print("[?] /memory <query>")
            return
        results = await self.runtime.memory.search(query, limit=5)
        if not results:
            self._print("No matching memories.")
            return
        self._print("\nMemories:")
        for record in results:
            flag = "✓" if record.verified else " "
            self._print(f"  [{flag}] {record.created_at:%Y-%m-%d} {record.content[:100]}")


def main_entry() -> int:
    """Entry point for `python -m nomadicos` and the `nomadicos` command."""
    # Windows console piping (cp1252) cannot encode the Unicode banner — force UTF-8.
    encoding = getattr(sys.stdout, "encoding", "") or ""
    if encoding.lower() not in ("utf-8", "utf8"):
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    configure_logging(log_file=LOG_FILE)
    from nomadicos.core.runtime import Runtime

    cli = InteractiveCLI(Runtime())
    return cli.run()


__all__ = ["InteractiveCLI", "SLASH_HELP", "MENU_ITEMS", "main_entry"]
