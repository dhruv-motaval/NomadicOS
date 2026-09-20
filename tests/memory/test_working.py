"""Phase 11B — working memory tests.

Bounded, task-isolated, deterministic ephemeral context: eviction order,
pin/drop semantics, rejection instead of bound violation, and structural
task isolation.
"""

from __future__ import annotations

from nomadicos.memory.working import WorkingMemory


def make(max_items: int = 8, max_chars: int = 2000) -> WorkingMemory:
    return WorkingMemory(max_items=max_items, max_chars=max_chars)


def test_basic_set_push_and_context_order() -> None:
    m = make()
    assert m.set_goal("t1", "fix the login bug") is True
    assert m.set_plan_digest("t1", "3 steps: run, repair, rerun") is True
    assert m.push_observation("t1", "saw failing test") == "obs-0001"
    assert m.push_failure("t1", "pytest exit 1") == "fail-0002"
    assert m.set_critic_feedback("t1", "add regression coverage") is True
    text = m.context("t1")
    assert text.index("GOAL:") < text.index("PLAN:") < text.index("OBSERVATIONS:")
    assert text.index("PLAN:") < text.index("OBSERVATIONS:") < text.index("FAILURES:")
    assert text.index("FAILURES:") < text.index("CRITIC FEEDBACK:")
    assert "- [obs-0001] saw failing test" in text
    assert "- [fail-0002] pytest exit 1" in text
    snap = m.snapshot("t1")
    assert [(i.section, i.item_id) for i in snap] == [
        ("goal", "goal"),
        ("plan", "plan"),
        ("observation", "obs-0001"),
        ("failure", "fail-0002"),
        ("critic_feedback", "critic"),
    ]


def test_slot_replacement_keeps_single_value() -> None:
    m = make()
    assert m.set_goal("t1", "first goal") is True
    assert m.set_goal("t1", "second goal") is True
    assert [i.text for i in m.snapshot("t1") if i.section == "goal"] == ["second goal"]
    assert m.set_plan_digest("t1", "plan v1") is True
    assert m.set_plan_digest("t1", "plan v2") is True
    assert [i.text for i in m.snapshot("t1") if i.section == "plan"] == ["plan v2"]
    assert m.set_critic_feedback("t1", "add tests") is True
    assert m.set_critic_feedback("t1", "tighten mocks") is True
    assert m.context("t1").count("CRITIC FEEDBACK:") == 1
    assert "tighten mocks" in m.context("t1")
    assert "first goal" not in m.context("t1")


def test_empty_and_unknown_task_creates_no_state() -> None:
    m = make()
    assert m.snapshot("ghost") == []
    assert m.context("ghost") == ""
    assert m.context("") == ""
    assert m.pin("ghost", "obs-0001") is False
    assert m.drop("ghost", "obs-0001") is False
    m.clear("ghost")
    assert m.known_tasks() == ()


def test_max_items_eviction_is_oldest_first() -> None:
    m = make(max_items=3)
    ids = [m.push_observation("t1", f"o{i}") for i in range(5)]
    assert ids == ["obs-0001", "obs-0002", "obs-0003", "obs-0004", "obs-0005"]
    assert [i.item_id for i in m.snapshot("t1")] == ["obs-0003", "obs-0004", "obs-0005"]


def test_char_budget_evicts_oldest_to_stay_bounded() -> None:
    m = make(max_items=8, max_chars=40)
    assert m.set_goal("t1", "goal-text-1234567890") is True  # 20 chars
    assert m.push_observation("t1", "aaaa") == "obs-0001"
    assert m.push_observation("t1", "b" * 15) == "obs-0002"  # total 39
    # a 10-char item forces BOTH unpinned observations out (oldest first);
    # the goal slot survives and total stays within budget
    assert m.push_observation("t1", "c" * 10) == "obs-0003"
    obs = [i.item_id for i in m.snapshot("t1") if i.section == "observation"]
    assert obs == ["obs-0003"]
    assert sum(len(i.text) for i in m.snapshot("t1")) <= 40


def test_pinned_item_survives_automatic_eviction() -> None:
    m = make(max_items=3)
    m.push_observation("t1", "pinned oldest")
    m.push_observation("t1", "second")
    m.push_observation("t1", "third")
    assert m.pin("t1", "obs-0001") is True
    assert m.push_observation("t1", "fourth") == "obs-0004"  # evicts obs-0002
    assert m.push_observation("t1", "fifth") == "obs-0005"  # evicts obs-0003
    ids = [i.item_id for i in m.snapshot("t1")]
    assert "obs-0001" in ids  # pinned survived automatic eviction
    assert "obs-0002" not in ids
    assert len(ids) == 3


def test_all_pinned_rejects_new_item_instead_of_exceeding() -> None:
    m = make(max_items=2)
    m.push_observation("t1", "one", pinned=True)
    m.push_observation("t1", "two", pinned=True)
    before = m.snapshot("t1")
    assert m.push_observation("t1", "rejected") is None
    assert m.push_failure("t1", "also rejected") is None
    assert m.snapshot("t1") == before
    assert [i.item_id for i in m.snapshot("t1")] == ["obs-0001", "obs-0002"]


def test_oversized_item_is_rejected_without_state() -> None:
    m = make(max_chars=30)
    assert m.push_observation("t1", "x" * 31) is None
    assert m.push_failure("t1", "y" * 31) is None
    assert m.snapshot("t1") == []
    assert m.set_goal("t1", "z" * 31) is False
    assert m.known_tasks() == ()
    assert m.push_observation("t1", "fits") == "obs-0001"


def test_drop_removes_pinned_item_and_clear_empties_task() -> None:
    m = make(max_items=2)
    m.push_observation("t1", "one", pinned=True)
    m.push_observation("t1", "two")
    assert m.pin("t1", "obs-0002") is True
    assert m.drop("t1", "obs-0001") is True  # explicit drop removes pinned
    assert [i.item_id for i in m.snapshot("t1")] == ["obs-0002"]
    assert m.drop("t1", "obs-9999") is False
    assert m.pin("t1", "obs-9999") is False
    m.clear("t1")
    assert m.context("t1") == ""
    assert m.known_tasks() == ()


def test_task_isolation_no_cross_task_leakage() -> None:
    m = make(max_items=2)
    assert m.set_goal("A", "goal A") is True
    m.push_observation("A", "secret of A", pinned=True)
    assert m.set_goal("B", "goal B") is True
    assert m.set_goal("B", "goal B") is True
    assert m.push_failure("B", "failure of B") == "fail-0001"  # per-task sequence
    assert "secret of A" not in m.context("B")
    assert "failure of B" not in m.context("A")
    assert m.context("B").index("failure of B") > m.context("B").index("GOAL:")
    # evictions in B never touch A's items
    m.push_observation("B", "b1")
    m.push_observation("B", "b2")
    m.push_observation("B", "b3")
    assert "secret of A" in m.context("A")
    assert "secret" not in m.context("B")
    assert m.clear("A") is None
    assert "secret" not in m.context("B")  # clearing A never touches B


def test_repeated_identical_operations_are_deterministic() -> None:
    def run(m: WorkingMemory) -> tuple[str, list[str]]:
        m.set_goal("t", "same goal")
        m.push_observation("t", "o1")
        m.push_failure("t", "f1", pinned=True)
        m.push_observation("t", "o2")
        m.push_observation("t", "o3")
        return m.context("t"), [i.item_id for i in m.snapshot("t")]

    a, b = make(max_items=2), make(max_items=2)
    assert run(a) == run(b)


def test_rejected_push_does_not_consume_sequence() -> None:
    m = make(max_items=1)
    assert m.push_observation("t", "a") == "obs-0001"
    m.pin("t", "obs-0001")
    assert m.push_observation("t", "rejected") is None  # only pinned remains
    assert m.push_failure("t", "rejected too") is None
    assert m.drop("t", "obs-0001") is True
    assert m.push_failure("t", "f1") == "fail-0002"  # seq advanced exactly once


def test_bounds_enforce_no_unbounded_accumulation() -> None:
    m = make(max_items=2, max_chars=25)
    for i in range(50):
        m.push_observation("t", f"item-{i}")
    items = m.snapshot("t")
    assert len([i for i in items if i.section == "observation"]) <= 3
    assert sum(len(i.text) for i in m.snapshot("t")) <= 2000
