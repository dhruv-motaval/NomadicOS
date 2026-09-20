"""Working memory — bounded, task-isolated, ephemeral context (SPEC §32; Phase 11B).

Per-task scratch context for the CURRENT goal, plan digest, recent
observations/failures, and the latest critic feedback. DATA only:

- no authority, authorization, policy, executor, routing, or SUCCESS
  semantics exist here; the module imports nothing from those bricks;
- working memory is EPHEMERAL: nothing is written to the durable store and
  nothing is silently promoted to episodic/semantic/procedural memory
  (durable extraction is a later slice with explicit provenance);
- task isolation is structural: every lookup is keyed by task_id, so task
  A's data can never appear in task B's context.

Deterministic by construction:

- one insertion sequence per task; eviction is oldest-first by sequence;
- pinned items are never evicted automatically (explicit drop may remove
  them, and clear() removes everything);
- when no evictable victim can make room, the NEW item is rejected and the
  state is left unchanged — bounds are never exceeded;
- goal / plan digest / critic feedback are single slots: setting one
  replaces its previous value;
- unknown or empty task ids yield empty results and create no state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_ID_PREFIX = {"observation": "obs", "failure": "fail"}


@dataclass(frozen=True, slots=True)
class WorkingItem:
    """One snapshot entry; slots use their section name as item_id."""

    item_id: str
    section: str  # goal | plan | observation | failure | critic_feedback
    text: str
    pinned: bool = False


@dataclass(slots=True)
class _TaskWorking:
    goal: str = ""
    plan_digest: str = ""
    critic_feedback: str = ""
    #: insertion order == eviction order (oldest first)
    items: list[WorkingItem] = field(default_factory=list)
    next_seq: int = 1


class WorkingMemory:
    """Bounded per-task ephemeral context. Deterministic and DATA-only."""

    def __init__(self, *, max_items: int = 8, max_chars: int = 2000) -> None:
        if max_items < 1:
            raise ValueError("max_items must be >= 1")
        if max_chars < 1:
            raise ValueError("max_chars must be >= 1")
        self._max_items = max_items
        self._max_chars = max_chars
        self._tasks: dict[str, _TaskWorking] = {}

    # ------------------------------------------------------------- writes ---
    def set_goal(self, task_id: str, text: str) -> bool:
        return self._set_slot(task_id, "goal", text)

    def set_plan_digest(self, task_id: str, text: str) -> bool:
        return self._set_slot(task_id, "plan_digest", text)

    def set_critic_feedback(self, task_id: str, text: str) -> bool:
        return self._set_slot(task_id, "critic_feedback", text)

    def push_observation(self, task_id: str, text: str, *, pinned: bool = False) -> str | None:
        return self._push(task_id, "observation", text, pinned)

    def push_failure(self, task_id: str, text: str, *, pinned: bool = False) -> str | None:
        return self._push(task_id, "failure", text, pinned)

    def pin(self, task_id: str, item_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        for idx, item in enumerate(task.items):
            if item.item_id == item_id:
                task.items[idx] = WorkingItem(
                    item_id=item.item_id, section=item.section, text=item.text, pinned=True
                )
                return True
        return False

    def drop(self, task_id: str, item_id: str) -> bool:
        """Explicit drop removes ANY item, pinned included."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        for idx, item in enumerate(task.items):
            if item.item_id == item_id:
                del task.items[idx]
                return True
        return False

    def clear(self, task_id: str) -> None:
        """Remove ALL working data for the task (slots, items, pins)."""
        self._tasks.pop(task_id, None)

    # -------------------------------------------------------------- reads ---
    def snapshot(self, task_id: str) -> list[WorkingItem]:
        """Structured snapshot in fixed section order (deterministic)."""
        task = self._tasks.get(task_id)
        if task is None:
            return []
        out: list[WorkingItem] = []
        if task.goal:
            out.append(WorkingItem("goal", "goal", task.goal, True))
        if task.plan_digest:
            out.append(WorkingItem("plan", "plan", task.plan_digest, True))
        out += [i for i in task.items if i.section == "observation"]
        out += [i for i in task.items if i.section == "failure"]
        if task.critic_feedback:
            out.append(WorkingItem("critic", "critic_feedback", task.critic_feedback, True))
        return out

    def context(self, task_id: str) -> str:
        """Deterministic text assembly; fixed section order; empty for an
        unknown/empty task (never creates state)."""
        task = self._tasks.get(task_id) if task_id else None
        if task is None:
            return ""
        observations = [i for i in task.items if i.section == "observation"]
        failures = [i for i in task.items if i.section == "failure"]
        obs_block = "\n".join(f"- [{i.item_id}] {i.text}" for i in observations) or "(none)"
        fail_block = "\n".join(f"- [{i.item_id}] {i.text}" for i in failures) or "(none)"
        return (
            f"GOAL: {task.goal or '(none)'}\n"
            f"PLAN: {task.plan_digest or '(none)'}\n"
            f"OBSERVATIONS:\n{obs_block}\n"
            f"FAILURES:\n{fail_block}\n"
            f"CRITIC FEEDBACK: {task.critic_feedback or '(none)'}"
        )

    def known_tasks(self) -> tuple[str, ...]:
        """Read-only view; a task appears here only after a real write."""
        return tuple(sorted(self._tasks))

    # ------------------------------------------------------------ internal ---
    def _chars(self, task: _TaskWorking) -> int:
        stored = len(task.goal) + len(task.plan_digest) + len(task.critic_feedback)
        return stored + sum(len(i.text) for i in task.items)

    def _evict_plan(
        self, task: _TaskWorking, extra_chars: int, extra_items: int
    ) -> list[WorkingItem] | None:
        """Oldest-first victims needed to stay within bounds after an
        insertion of ``extra_chars``/``extra_items``; None = impossible
        without touching pinned items (caller must reject)."""
        items_over = len(task.items) + extra_items - self._max_items
        chars_over = self._chars(task) + extra_chars - self._max_chars
        if items_over <= 0 and chars_over <= 0:
            return []
        plan: list[WorkingItem] = []
        for item in task.items:
            if item.pinned:
                continue
            plan.append(item)
            chars_over -= len(item.text)
            items_over -= 1
            if items_over <= 0 and chars_over <= 0:
                return plan
        return None

    def _set_slot(self, task_id: str, slot: str, text: str) -> bool:
        text = text.strip()
        if not text or len(text) > self._max_chars:
            return False
        task = self._tasks.setdefault(task_id, _TaskWorking())
        old = getattr(task, slot)
        plan = self._evict_plan(task, extra_chars=len(text) - len(old), extra_items=0)
        if plan is None:
            return False  # even evicting everything unpinned cannot fit
        for victim in plan:
            task.items.remove(victim)
        setattr(task, slot, text)
        return True

    def _push(
        self, task_id: str, section: str, text: str, pinned: bool
    ) -> str | None:
        text = text.strip()
        if not text or len(text) > self._max_chars:
            return None
        task = self._tasks.setdefault(task_id, _TaskWorking())
        plan = self._evict_plan(task, extra_chars=len(text), extra_items=1)
        if plan is None:  # only pinned items remain: reject, stay bounded
            return None
        for victim in plan:
            task.items.remove(victim)
        item = WorkingItem(
            item_id=f"{_ID_PREFIX[section]}-{task.next_seq:04d}",
            section=section,
            text=text,
            pinned=pinned,
        )
        task.items.append(item)
        task.next_seq += 1
        return item.item_id
