# NomadicOS — UI Plan

> Status: planned (deferred by owner — backend first). This document is the build
> contract for when we pick it up. ADR-0014 constraints apply throughout.

## Core insight (owner, 2026-09-07)

The UI is **not a product layer — it is I/O plumbing**. The Input Layer already
unifies every modality into one `TaskRequest`; the runtime already emits every
event onto the EventBus. The UI is just three wires:

```text
IN:  UI input {type: chat|voice|cli}  →  matching Input Adapter  →  TaskRequest
                                           ↓
                              Runtime.run_goal()  (same Security Gate, no bypass — I5)
                                           ↓
OUT: EventBus events  →  WebSocket  →  terminal-style session panel (agent's work screen)
```

"Minimize and keep working" is free: the runtime owns the loop; the UI is a
disposable client that reconnects and replays state.

## Workstreams

### W1 — Input dispatcher (in-wire)
- One endpoint receiving `{type: chat|voice|cli, payload}`
- Dispatch: chat → `ChatAdapter`, voice → transcript → `VoiceAdapter`,
  cli-style commands → `CLIAdapter`
- All converge to `TaskRequest` → `Runtime.run_goal()` — identical mediation for
  every modality (I5: no UI bypass)

### W2 — Event out-wire (work-session screen)
- FastAPI + WebSocket hub subscribed to the EventBus (proposal / gate decision /
  tool call / verification / experience)
- Terminal panel = event-log renderer colored by event type:

```text
[gate]   filesystem.write  ALLOW   policy: filesystem-all
[tool ]  write report.txt (SHA-256 before→after)
[verify] 2/2 checks passed
[exp  ]  outcome=success quality=10.0
```

- **Decision recorded:** event-log stream (v1) vs true PTY (v2). Event-log is
  the agent's work session and trivially inside the gate. A user-owned PTY
  (interactive shell) is a separate v2 feature — it is *user input*, must be
  labeled as such, and never presented as agent action.
- TerminalTool upgrade: stream stdout chunks during long commands (currently
  captured at completion) — 1–2 hrs

### W3 — Voice (push-to-talk first, live streaming second)
- Tauri global hotkey (Ctrl+Space, works unfocused) → CPAL/WASAPI capture →
  PCM over local WebSocket → faster-whisper (local STT) → VoiceAdapter
- Live mode: silero-vad (~2 MB, CPU) segments speech; GPU whisper ≈ 0.5 GB VRAM
  (budget vs LLM), CPU ≈ 1–3 s latency
- Flaws: streaming transcription is chunk-based (mid-sentence corrections
  imperfect); push-to-talk is the honest v1

### W4 — Tauri shell (native, lightweight)
- Tauri v2: ~40–70 MB RAM, real window, tray, global hotkeys, ~5 MB installer
- Alternatives: pywebview (pure Python, ~50–90 MB), PySide6 (~100–150 MB),
  Electron eliminated (~200–400 MB — competes with LLM for RAM)
- UI loads local assets only — offline-capable (BP §57)

### W5 — Panels (post-core)
- Approvals: ASK queue → `PermissionEngine.grant(one_shot=True)` (BP §39)
- Screen view: VisionEngine JPEG frames over WS (local, I11)
- Memory manager, model fleet panel, audit view
- Frontend framework: htmx/vanilla first; React only if panels grow

## Sequencing

| Step | Scope | Effort |
|---|---|---|
| 1 | W1 dispatcher + W2 event hub + terminal panel | ~2 hrs |
| 2 | W3 voice push-to-talk (faster-whisper local) | ~1 day |
| 3 | W4 Tauri shell (window, tray, hotkey) | ~4 hrs |
| 4 | W5 panels (approvals, screen, memory, audit) | ~1–2 days |
| 5 | Live VAD streaming voice | v2 |

## Rules that survive

- UI inputs enter ONLY through the Input Layer adapters — same Security Gate,
  same budgets, same audit (I5)
- UI is a disposable client; the runtime is the product (headless-capable)
- Assets vendored/local — no CDN (BP §57)
