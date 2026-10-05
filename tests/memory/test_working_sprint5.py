"""Sprint 5 — working memory extensions: active subgoal, execution context,
hypotheses, decisions, and the goal-aware retrieved view. All bounded,
task-isolated, deterministic."""

from __future__ import annotations

from nomadicos.memory.working import WorkingMemory


def make(max_items: int = 8, max_chars: int = 2000) -> WorkingMemory:
    return WorkingMemory(max_items=max_items, max_chars=max_chars)


def test_new_sections_appear_in_fixed_snapshot_order() -> None:
    m = make()
    assert m.set_goal("t", "goal g") is True
    assert m.set_subgoal("t", "sub: write the test") is True
    assert m.push_observation("t", "saw failure") == "obs-0001"
    assert m.push_failure("t", "exit 1") == "fail-0002"
    assert m.push_hypothesis("t", "config drift") == "hyp-0003"
    assert m.push_decision("t", "pick structural fix") == "dec-0004"
    assert m.set_retrieved("t", "2 relevant memories") is True
    assert m.set_critic_feedback("t", "tighten") is True
    assert m.set_execution_context("t", "cwd=repo branch=main") is True
    snap = m.snapshot("t")
    assert [(i.section, i.item_id) for i in snap] == [
        ("goal", "goal"),
        ("subgoal", "subgoal"),
        ("observation", "obs-0001"),
        ("failure", "fail-0002"),
        ("hypothesis", "hyp-0003"),
        ("decision", "dec-0004"),
        ("retrieved", "retrieved"),
        ("critic_feedback", "critic"),
        ("execution_context", "exec_ctx"),
    ]


def test_subgoal_slot_replaces_value() -> None:
    m = make()
    assert m.set_subgoal("t", "sub v1") is True
    assert m.set_subgoal("t", "sub v2") is True
    assert [i.text for i in m.snapshot("t") if i.section == "subgoal"] == ["sub v2"]
    assert "sub v1" not in m.context("t")
    assert m.context("t").count("SUBGOAL:") == 1


def test_sections_present_in_context_once() -> None:
    m = make()
    m.set_goal("t", "g")
    m.set_subgoal("t", "sub v1")
    m.set_execution_context("t", "cwd=repo")
    m.push_hypothesis("t", "h1")
    m.push_decision("t", "d1")
    m.set_retrieved("t", "3 memories")
    text = m.context("t")
    assert text.count("HYPOTHESES:") == 1
    assert text.count("DECISIONS:") == 1
    assert text.count("RETRIEVED:") == 1
    assert text.count("EXECUTION CONTEXT:") == 1
    assert text.count("SUBGOAL:") == 1
    # deterministic section order preserved
    assert (
        text.index("GOAL:")
        < text.index("PLAN:")
        < text.index("OBSERVATIONS:")
        < text.index("FAILURES:")
        < text.index("HYPOTHESES:")
        < text.index("DECISIONS:")
        < text.index("RETRIEVED:")
        < text.index("CRITIC FEEDBACK:")
        < text.index("EXECUTION CONTEXT:")
    )


def test_hypotheses_and_decisions_evict_oldest_first() -> None:
    m = make(max_items=2)
    assert m.push_hypothesis("t", "h1") == "hyp-0001"
    assert m.push_hypothesis("t", "h2") == "hyp-0002"
    assert m.push_decision("t", "d1") == "dec-0003"  # evicts hyp-0001 (oldest)
    assert m.push_decision("t", "d2") == "dec-0004"  # evicts hyp-0002
    sections = [(i.section, i.item_id) for i in m.snapshot("t")]
    assert sections == [("decision", "dec-0003"), ("decision", "dec-0004")]
    # pinned hypothesis would survive instead
    m2 = make(max_items=2)
    m2.push_hypothesis("t", "pinned", pinned=True)
    m2.push_hypothesis("t", "h2")
    m2.push_decision("t", "d1")  # evicts unpinned h2
    m2.push_decision("t", "d2")  # evicts unpinned dec-0003 (oldest)
    assert [i.item_id for i in m2.snapshot("t")] == ["hyp-0001", "dec-0004"]


def test_retrieved_view_is_replaced_not_grown() -> None:
    m = make()
    assert m.set_retrieved("t", "cycle 1: 5 memories") is True
    assert m.set_retrieved("t", "cycle 2: 3 memories") is True
    assert [i.text for i in m.snapshot("t") if i.section == "retrieved"] == [
        "cycle 2: 3 memories"
    ]
    assert sum(1 for i in m.snapshot("t") if i.section == "retrieved") == 1


def test_new_sections_respect_char_budget_and_eviction() -> None:
    m = make(max_items=2, max_chars=80)
    assert m.set_goal("t", "g" * 10) is True
    assert m.push_hypothesis("t", "h" * 10) == "hyp-0001"
    assert m.push_decision("t", "d" * 10) == "dec-0002"
    # a new item evicts the oldest hypothesis first, bounds never exceeded
    assert m.push_decision("t", "e" * 10) == "dec-0003"
    items = m.snapshot("t")
    assert len(items) <= 3  # goal slot + 2 items
    assert sum(len(i.text) for i in items) <= 80


def test_retrieved_slot_eviction_keeps_state_bounded() -> None:
    m = make(max_items=1, max_chars=100)
    assert m.set_retrieved("t", "x" * 30) is True
    assert m.set_retrieved("t", "y" * 30) is True
    assert [i.text for i in m.snapshot("t") if i.section == "retrieved"] == ["y" * 30]


def test_new_sections_deterministic_across_instances() -> None:
    def run(m: WorkingMemory) -> tuple[str, list[str]]:
        m.set_goal("t", "same goal")
        m.set_subgoal("t", "same sub")
        m.set_execution_context("t", "same ctx")
        m.set_retrieved("t", "same retrieved")
        m.push_hypothesis("t", "h1", pinned=True)
        m.push_decision("t", "d1")
        m.push_observation("t", "o1")
        return m.context("t"), [i.item_id for i in m.snapshot("t")]

    a, b = make(max_items=4), make(max_items=4)
    assert run(a) == run(b)


def test_unknown_task_new_reads_create_no_state() -> None:
    m = make()
    assert m.snapshot("ghost") == []
    assert m.context("ghost") == ""
    assert m.known_tasks() == ()
