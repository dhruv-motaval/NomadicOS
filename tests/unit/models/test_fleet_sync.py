"""FleetSynchronizer tests (BP §8.4, §148, §107, §151)."""

from nomadicos.core.errors import NomadicError
from nomadicos.models.base import ModelStatus
from nomadicos.models.fake import FakeLocalModel
from nomadicos.models.fleet import FleetSynchronizer
from nomadicos.models.manager import ModelManager


def make_sync(models: dict[str, str] | None = None):
    """models: model_id → digest (used by the real Ollama adapter, not here).
    Health: healthy unless id contains 'sick'."""
    manager = ModelManager(max_resident=1)
    registry: dict[str, FakeLocalModel] = {}

    def discover_fn():
        out = []
        for mid in models or {}:
            if mid not in registry:
                registry[mid] = FakeLocalModel(
                    mid,
                    capabilities=__import__(
                        "nomadicos.models.base", fromlist=["ModelCapabilities"]
                    ).ModelCapabilities(text_generation=True),
                )
                registry[mid]._responses = ["healthy"]
            if "sick" in mid:
                registry[mid].set_fail_generate(True)
            out.append(registry[mid])
        return out

    sync = FleetSynchronizer(manager, sync_cooldown_seconds=0)
    # inject the discovery function (Ollama-specific adapter is bypassed in tests)
    sync._discover_fn = discover_fn  # type: ignore[attr-defined]
    return manager, sync


async def test_new_model_detected_and_enabled() -> None:
    manager, sync = make_sync(models={"ollama/new-model": "digest-1"})
    # pretend discover uses our injected fn
    sync.discover = sync._discover_fn  # type: ignore[attr-defined]
    report = await sync.sync(force=True)
    assert "ollama/new-model" in report.healthy
    assert (
        manager.get("ollama/new-model").is_loaded
        or manager.snapshot()["status"]["ollama/new-model"] == ModelStatus.ENABLED.value
    )


async def test_removed_model_quarantined() -> None:
    manager, sync = make_sync(models={"ollama/gone-model": "digest-1"})
    sync.discover = sync._discover_fn  # type: ignore[attr-defined]
    await sync.sync(force=True)
    assert manager.snapshot()["status"]["ollama/gone-model"] == ModelStatus.ENABLED.value

    sync._discover_fn = lambda: []  # model vanished
    sync.discover = sync._discover_fn  # type: ignore[attr-defined]
    report = await sync.sync(force=True)
    assert "ollama/gone-model" in report.removed
    assert manager.snapshot()["status"]["ollama/gone-model"] == ModelStatus.QUARANTINED.value


async def test_repeated_sync_is_quiet() -> None:
    manager, sync = make_sync(models={"ollama/stable": "digest-1"})
    sync.discover = sync._discover_fn  # type: ignore[attr-defined]
    await sync.sync(force=True)
    report = await sync.sync(force=True)
    assert report.quiet  # no changes → nothing reported


async def test_sync_survives_discovery_outage() -> None:
    """BP §242: server down ⇒ skip sync, never crash the watch loop."""

    def broken_discover():
        raise NomadicError("ollama unreachable")

    manager, sync = make_sync()
    sync.discover = broken_discover  # type: ignore[attr-defined]
    report = await sync.sync(force=True)
    assert report.quiet
