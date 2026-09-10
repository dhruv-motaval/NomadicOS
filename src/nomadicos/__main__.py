"""`python -m nomadicos` entry point — interactive REPL (ADR-0015)."""

import sys

from nomadicos.cli_interactive import main_entry

if __name__ == "__main__":
    sys.exit(main_entry())
