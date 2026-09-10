# Current phase — ADR-0027 phases 0–14 COMPLETE (2026-09-05)

## Status: v0.1 architecture implemented and verified end-to-end

All 14 phases built, each gated by: component unit tests → full-suite regression
→ ruff + mypy. **Final state: 243 tests passed, 1 skipped (hardware-only),
ruff clean, mypy clean.**

- P0 repo/tooling/interfaces (BP §209) · P1 PostgreSQL + core state ·
  P2 local model runtime + registry (ADR-0001/0006) ·
  P3 Security Gate + policies + permissions + audit (BP §85/§90/§98) ·
  P4 Tool Gateway + filesystem + terminal (BP §142-143, §193-194) ·
  P5 vision runtime (BP §158-159, §240) ·
  P6 computer control (BP §145, §172-177) ·
  P7 network gateway + web cache + provenance (BP §88, §125-126, §196-197) ·
  P8 memory engine (BP §376-420: cross-session, scopes, promotion) ·
  P9 native vector engine V0 exact-search (ADR-0007/0008) ·
  P10 evaluation engine + model performance (BP §100, §144-146) ·
  P11 experience system (BP §95, §109, §168, §318) ·
  P12 adaptive model selection (BP §320, §386) ·
  P13 self-improvement lifecycle (BP §67, §147, §216, §289) ·
  P14 benchmark matrix + retrieval metrics (BP §314)

## Runnable demo

- `python demo.py` — BP §78 milestone on the fake planner: write + verify + report
- `python -m nomadicos.cli status` — subsystem status
- `python -m nomadicos.cli task create "<goal>"` — CLI task intake (ADR-0015)
- `python -m nomadicos.cli stop` — emergency stop (BP §122)
- `docker compose -f docker-compose.dev.yml up -d` — PostgreSQL for the
  integration tests (ADR-0009)

## LIVE HARDWARE VALIDATED (2026-09-05)

- Ollama fleet: 5 models discovered, capabilities verified via /api/show (BP §150)
- Text generation live: gemma-3-4b + qwen3-coder through LocalModel
- Vision live: gemma3:4b multimodal (pulled) — real screen 1920x1080 described locally
- Computer control live: pyautogui screenshot + cursor move validated
- NOTE: gemma-3-4b:latest is completion-only; use gemma3:4b for vision
- NOTE: gemma vision HTTP 400 handling proven: adapter surfaces MODEL_FAILURE

## What remains (hardware / real-model work, not architecture)

- Real model path: install llama-cpp-python, download a GGUF model (ADR-0005),
  register via `LlamaCppModel` — the adapter and interfaces are ready.
- Real vision: Gemma adapter (ADR-0004) + Pillow provider (hardware-marked).
- Real computer control: Windows adapter (pyautogui) — hardware-marked.
- License decision after dependency audit (ADR-0025).
- PostgreSQL persistence wiring for registries/experience stores (in-memory
  fakes are the default; repositories already exist).
