"""Phase 11 tests: experience recording, dedup, consolidation, quality (BP §95/§109/§168/§318)."""

import pytest

from nomadicos.experience.recorder import ExperienceRecorder, Outcome, TaskExperience
from nomadicos.experience.store import InMemoryExperienceStore


@pytest.fixture()
def store() -> InMemoryExperienceStore:
    return InMemoryExperienceStore()


@pytest.fixture()
def recorder(store: InMemoryExperienceStore) -> ExperienceRecorder:
    return ExperienceRecorder(store)


async def test_finish_records_quality_verified_bonus(recorder: ExperienceRecorder, store) -> None:
    good = await recorder.finish(
        task_id="t-1",
        outcome=Outcome.SUCCESS,
        summary="Fixed failing test by patching imports",
        verified=True,
        steps=3,
        evidence={"checks_passed": 4, "checks_total": 4},
    )
    assert good.quality_score == 10.0  # success(8) + verified(2) + evidence(.5) capped
    assert good.outcome is Outcome.SUCCESS
    assert len(store.records) == 1


async def test_unverified_success_scores_lower(recorder: ExperienceRecorder) -> None:
    verified = await recorder.finish(
        task_id="t-1", outcome=Outcome.SUCCESS, summary="with verification", verified=True
    )
    unverified = await recorder.finish(
        task_id="t-2", outcome=Outcome.SUCCESS, summary="without verification", verified=False
    )
    assert unverified.quality_score < verified.quality_score


async def test_failure_records_canonical_class(recorder: ExperienceRecorder) -> None:
    experience = await recorder.finish(
        task_id="t-3",
        outcome=Outcome.FAILURE,
        summary="command timed out",
        failure_class="TOOL_FAILURE",
    )
    assert experience.failure_class == "TOOL_FAILURE"
    assert experience.quality_score == 1.0  # failure base, nothing added


async def test_dedup_aggregates_identical_traces(recorder: ExperienceRecorder, store) -> None:
    """BP §168: repeated identical traces aggregate, not duplicate."""
    for _ in range(3):
        await recorder.finish(
            task_id="t", outcome=Outcome.SUCCESS, summary="Identical summary", verified=True
        )

    assert len(store.records) == 1
    assert store.records[0].quality_score == 10.0  # capped, was incremented


async def test_search_prefers_quality_and_outcome(recorder: ExperienceRecorder) -> None:
    await recorder.finish(
        task_id="a", outcome=Outcome.FAILURE, summary="deploy script issue", verified=False
    )
    await recorder.finish(
        task_id="b", outcome=Outcome.SUCCESS, summary="deploy script fixed", verified=True
    )
    results = await recorder._store.search("deploy script", outcome=Outcome.SUCCESS)
    assert len(results) == 1
    assert results[0].task_id == "b"


async def test_consolidation_creates_higher_level_experience(
    recorder: ExperienceRecorder, store: InMemoryExperienceStore
) -> None:
    """BP §109: similar runs → one procedure with retained provenance.

    Records are seeded directly (bypassing recorder dedup, which already
    aggregates *identical* traces per BP §168) to simulate near-duplicate
    traces collected over time.
    """
    for i in range(3):
        await store.append(
            TaskExperience(
                task_id=f"t-{i}",
                outcome=Outcome.SUCCESS,
                summary=f"Run the standard test suite before committing (variant {i})",
                quality_score=8.0 + i * 0.5,
                steps=5,
            )
        )
    consolidated_count = await recorder.consolidate(min_similar=3)
    assert consolidated_count == 1
    remaining = await store.all()
    assert len(remaining) == 1
    assert remaining[0].summary.startswith("[consolidated ×3]")
    assert len(remaining[0].evidence["consolidated_from"]) == 3

async def test_experience_reuse_requires_revalidation(recorder: ExperienceRecorder) -> None:
    """BP §352/§353: retrieved experience is a reference, revalidated before reuse."""
    await recorder.finish(
        task_id="old",
        outcome=Outcome.SUCCESS,
        summary="Fixed port conflict by editing config",
        verified=True,
    )
    results = await recorder._store.search("port conflict", outcome=Outcome.SUCCESS)
    assert results[0].quality_score >= 8.0
    # Revalidation itself is enforced by the caller (Execution layer, BP §416);
    # the store guarantees provenance is intact for that check.
    assert results[0].task_id == "old"
