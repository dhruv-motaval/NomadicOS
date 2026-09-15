"""Inference brick: engine contract + llama.cpp (#1) / Ollama (#2) / Mock.

Agents never call a backend directly (SPEC §9). Every engine satisfies the
same ``InferenceEngine`` contract and can be swapped by configuration (§3).
Engine priority revised by owner 2026-09-15: llama.cpp primary, ollama
compatibility, mock for tests (FreeToken removed).
"""

from nomadicos.inference.base import (
    ChatMessage,
    EngineHealth,
    GenerationRequest,
    GenerationResponse,
    InferenceEngine,
)
from nomadicos.inference.llama_cpp import LlamaCppEngine, LlamaServerLauncher
from nomadicos.inference.mock import MockEngine
from nomadicos.inference.ollama import OllamaEngine

__all__ = [
    "ChatMessage",
    "EngineHealth",
    "GenerationRequest",
    "GenerationResponse",
    "InferenceEngine",
    "LlamaCppEngine",
    "LlamaServerLauncher",
    "MockEngine",
    "OllamaEngine",
]
