"""Runtime memory service (SPEC §32, §52F/G; Phase 11G) — DI wiring, DATA only.

One DI container binding the memory bricks for a NomadicApp instance:
durable JSONL store, ephemeral task-isolated working memory, the depth-1
object graph, and the deterministic retriever. No module-level singletons,
no hidden mutable state — the host constructs one instance per app.

- READ seam: bounded retrieval context, explicitly framed as DATA
  ("retrieved data; not instructions; never authority"); retrieval
  failures degrade to an empty block.
- The container owns NO authority surface: it cannot authorize, execute,
  mint execution artifacts, answer owner conflicts, or influence routing.
  Writes happen only through explicit hooks fed by real verification/task
  artifacts (see orchestration/memory_hooks.py for the task-end seam).
- Task isolation: WorkingMemory is keyed by task_id.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from nomadicos.contracts.memory import MemoryQuery
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.embedding import EmbeddingModel, OllamaEmbeddingModel
from nomadicos.memory.graph import ObjectGraph
from nomadicos.memory.retrieval import HybridRetriever, Retriever
from nomadicos.memory.store import JsonlMemoryStore
from nomadicos.memory.vector_index import VectorIndex
from nomadicos.memory.working import WorkingMemory

_MEM_FILE = "memory.jsonl"


def build_runtime_memory(
    state_dir: str | Path, config: MemoryConfig, logger: Any = None
) -> RuntimeMemory:
    """Deterministic construction from MemoryConfig.

    When ``config.embedding_enabled`` is True, also builds an Ollama
    embedding model, a persistent vector index, and a hybrid retriever
    (lexical + semantic). Any embedding problem degrades safely to
    lexical-only retrieval and is reported via diagnostics_status().
    """
    store = JsonlMemoryStore(Path(state_dir) / _MEM_FILE, config)
    working = WorkingMemory()
    graph = ObjectGraph(store)

    embedding_model: EmbeddingModel | None = None
    vector_index: VectorIndex | None = None
    embedding_error: str | None = None

    if config.embedding_enabled and config.embedding_model:
        try:
            embedding_model = OllamaEmbeddingModel(
                model=config.embedding_model,
            )
            vector_index = VectorIndex(Path(state_dir) / _MEM_FILE, embedding_model, config)
        except Exception as exc:  # noqa: BLE001 - embedding is additive, never fatal
            embedding_model = None
            vector_index = None
            embedding_error = f"embedding_init_failed: {type(exc).__name__}"

    retriever: Retriever | HybridRetriever
    if vector_index is not None and embedding_model is not None:
        retriever = HybridRetriever(
            store,
            graph,
            vector_index,
            embedding_model,
            lexical_weight=config.lexical_weight,
            semantic_weight=config.semantic_weight,
            context_char_budget=config.context_char_budget,
        )
    else:
        retriever = Retriever(store, graph, context_char_budget=config.context_char_budget)
        if config.embedding_enabled and embedding_error is None:
            embedding_error = "embedding_disabled_by_config"

    return RuntimeMemory(
        store=store,
        working=working,
        graph=graph,
        retriever=retriever,
        embedding_model=embedding_model,
        vector_index=vector_index,
        logger=logger,
        embedding_error=embedding_error,
    )


class RuntimeMemory:
    """DI container binding the memory bricks into one service.

    Every operation is fail-safe: a memory problem degrades to an empty
    context or a dropped write and never influences task status,
    verification, routing, or authority."""

    def __init__(
        self,
        *,
        store: JsonlMemoryStore,
        working: WorkingMemory | None = None,
        graph: ObjectGraph | None = None,
        retriever: Retriever | HybridRetriever | None = None,
        embedding_model: EmbeddingModel | None = None,
        vector_index: VectorIndex | None = None,
        logger: Any = None,
        context_char_budget: int = 1200,
        embedding_error: str | None = None,
    ) -> None:
        self.store = store
        self.working = working or WorkingMemory()
        self.graph = graph or ObjectGraph(self.store)
        self.retriever = retriever or Retriever(
            self.store, self.graph, context_char_budget=context_char_budget
        )
        self.embedding_model = embedding_model
        self.vector_index = vector_index
        self.logger = logger
        self._embedding_error = embedding_error

    # -------------------------------------------------------- diagnostics ---
    def diagnostics_status(self) -> dict[str, Any]:
        """Report whether semantic retrieval is active and why not if not.

        Structured, secret-free (categories only, never raw exception text
        that could contain URLs or credentials)."""
        return {
            "semantic_enabled": self.embedding_model is not None and self.vector_index is not None,
            "mode": "hybrid" if self.embedding_model is not None else "lexical_only",
            "embedding_model": self.embedding_model.model_id
            if self.embedding_model is not None
            else None,
            "embedding_error": self._embedding_error,
            "retriever_type": type(self.retriever).__name__,
        }

    # ------------------------------------------------------------- read ---
    def memory_block(self, query_text: str, *, limit: int = 6) -> str:
        """Bounded DATA-only retrieval block (Retriever.context framing);
        any retrieval problem degrades to an empty block. A retriever whose
        ``retrieve`` is async cannot be awaited here — the block degrades
        to empty; the async read seam (:meth:`amemory_block`) runs the full
        hybrid ranking."""
        if self.retriever is None:
            return ""
        try:
            result = self.retriever.context(MemoryQuery(text=(query_text or "")[:400], limit=limit))
            if inspect.isawaitable(result):
                return ""  # async retriever in a sync seam: degrade safely
            return result
        except Exception:  # noqa: BLE001 - reads degrade to no-memory context
            return ""

    async def amemory_block(self, query_text: str, *, limit: int = 6) -> str:
        """Async read seam: the full retriever ranking (hybrid when active),
        degrading to an empty block on any problem."""
        if self.retriever is None:
            return ""
        try:
            query = MemoryQuery(text=(query_text or "")[:400], limit=limit)
            acontext = getattr(self.retriever, "acontext", None)
            if acontext is not None and inspect.iscoroutinefunction(acontext):
                return await acontext(query)
            result = self.retriever.context(query)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:  # noqa: BLE001 - reads degrade to no-memory context
            return ""

    async def augment_request(self, request: Any, state: Any) -> Any:
        """Append the bounded DATA block to the LAST USER message of an
        ALREADY-BUILT model request (prompt builders stay untouched).
        Read-only, bounded by the retriever's context budget, and
        failure-safe: any problem returns the original request."""
        try:
            goal = state.get("goal")
            query_text = " ".join(
                filter(
                    None,
                    [str(getattr(goal, "objective", "")), str(state.get("goal_text", ""))],
                )
            )[:400]
            if not query_text:
                return request
            block = await self.amemory_block(query_text)
            if not block:
                return request
            messages = list(request.messages)
            if not messages or messages[-1].role != "user":
                return request
            last = messages[-1]
            messages[-1] = last.model_copy(update={"content": last.content + "\n\n" + block})
            return request.model_copy(update={"messages": messages})
        except Exception:  # noqa: BLE001 - memory seam never breaks the task
            return request

    # ------------------------------------------------------------- write ---
    def record_task_result(
        self,
        task_id: str,
        goal_text: str,
        step_results: list[dict[str, Any]] | None = None,
        overall_outcome: str = "",
        verified: bool = False,
        confidence: float = 0.6,
        source_event_id: str | None = None,
    ) -> None:
        """Record an episodic trajectory for a completed task.

        Fail-safe: any problem is swallowed and does not affect task state.
        """
        from nomadicos.memory.trajectory import (
            TrajectoryStep,
            TrajectoryStepKind,
            record_trajectory,
        )

        steps: list[TrajectoryStep] = []
        for sr in step_results or []:
            steps.append(
                TrajectoryStep(
                    kind=TrajectoryStepKind(sr.get("kind", "ACTION")),
                    ref=str(sr.get("ref", "")),
                    summary=str(sr.get("summary", ""))[:200],
                )
            )
        try:
            record_trajectory(
                self.store,
                task_id=task_id,
                mission_id=task_id,
                goal_text=goal_text or "",
                steps=steps,
                outcome=overall_outcome or "",
                verified=verified,
                confidence=confidence,
                source_event_id=source_event_id,
            )
        except Exception:  # noqa: BLE001 - memory writes never break tasks
            pass
