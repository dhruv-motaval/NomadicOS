"""Cross-process atomicity test for DurableFileLedger.claim_intent().

Verifies that two independent processes racing to claim the same action
fingerprint result in exactly one winner — as required by SPEC §56.14
crash-gap hardening.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nomadicos.contracts.execution import ExecutionIntent
from nomadicos.persistence.errors import PersistenceCorrupt
from nomadicos.persistence.ledger import _KEY_RE, DurableFileLedger

#: Deterministic key for the contested claim (SHA256[:32] format required)
CONTESTED_KEY = "a" * 32


def _claim_in_subprocess(state_dir: str, results_path: str) -> None:
    """Run in a separate process: hydrate its own DurableFileLedger, claim
    the contested key, write result to results_path."""
    ledger = DurableFileLedger(state_dir)
    intent = ExecutionIntent(
        task_id="task-cross",
        step_id="s0",
        fingerprint=CONTESTED_KEY,
        authorization_epoch=1,
        tool="filesystem",
        operation="write",
        args={"path": "x.txt", "content": "y"},
    )
    won = ledger.claim_intent(CONTESTED_KEY, intent)
    Path(results_path).write_text(json.dumps({"won": won}), encoding="utf-8")


def _race_claim_in_subprocess(
    state_dir: str,
    results_path: str,
    ready_path: str,
    go_path: str,
    key: str,
) -> None:
    """Run in a separate process: construct an INDEPENDENT ledger instance,
    signal readiness, block on the 'go' barrier, then claim immediately so
    both processes hit the claim at the same moment."""
    ledger = DurableFileLedger(state_dir)  # independent instance
    intent = ExecutionIntent(
        task_id="task-race",
        step_id="s0",
        fingerprint=key,
        authorization_epoch=1,
        tool="filesystem",
        operation="write",
        args={"path": "race.txt", "content": "c"},
    )
    Path(ready_path).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 30.0
    while not Path(go_path).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("barrier 'go' signal never appeared")
        time.sleep(0.001)
    won = ledger.claim_intent(key, intent)
    Path(results_path).write_text(json.dumps({"won": won}), encoding="utf-8")


def _spawn_racers(
    tmp_path: Path,
    state_dir: str,
    *,
    n_processes: int,
    key: str,
    round_name: str,
) -> list[subprocess.Popen]:
    """Spawn n independent processes that all race to claim ``key`` behind a
    shared start barrier."""
    go_path = tmp_path / f"go-{round_name}"
    procs: list[subprocess.Popen] = []
    for i in range(n_processes):
        results_path = str(tmp_path / f"results-{round_name}-{i}.json")
        ready_path = str(tmp_path / f"ready-{round_name}-{i}.json")
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{Path(__file__).resolve().parent.parent.parent}')\n"
            "from tests.executor.test_cross_process_claim import _race_claim_in_subprocess\n"
            f"_race_claim_in_subprocess(r'{state_dir}', r'{results_path}', "
            f"r'{ready_path}', r'{go_path}', r'{key}')\n"
        )
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    # wait until every racer has built its ledger and is parked on the barrier
    deadline = time.monotonic() + 30.0
    for i in range(n_processes):
        ready = Path(str(tmp_path / f"ready-{round_name}-{i}.json"))
        while not ready.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"racer {i} never became ready")
            time.sleep(0.005)
    # release the barrier: all racers claim at the same instant
    go_path.write_text("go", encoding="utf-8")
    return procs


def test_cross_process_claim_atomicity(tmp_path: Path) -> None:
    """Two independent processes claiming the same intent: exactly one wins."""
    state_dir = str(tmp_path / "state")

    results_a = str(tmp_path / "results_a.json")
    results_b = str(tmp_path / "results_b.json")

    # Launch two independent processes, each with its own ledger instance
    proc_a = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import sys, json
sys.path.insert(0, r'{Path(__file__).resolve().parent.parent.parent}')
from tests.executor.test_cross_process_claim import _claim_in_subprocess
_claim_in_subprocess(r'{state_dir}', r'{results_a}')
""",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc_b = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import sys, json
sys.path.insert(0, r'{Path(__file__).resolve().parent.parent.parent}')
from tests.executor.test_cross_process_claim import _claim_in_subprocess
_claim_in_subprocess(r'{state_dir}', r'{results_b}')
""",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    proc_a.wait(timeout=30)
    proc_b.wait(timeout=30)

    assert proc_a.returncode == 0, f"proc_a failed: {proc_a.stderr.read()}"
    assert proc_b.returncode == 0, f"proc_b failed: {proc_b.stderr.read()}"

    won_a = json.loads(Path(results_a).read_text())["won"]
    won_b = json.loads(Path(results_b).read_text())["won"]

    # Exactly one must win
    assert won_a != won_b, f"both processes claimed: {won_a}, {won_b}"
    assert won_a + won_b == 1, f"neither or both won: {won_a}, {won_b}"


def test_cross_process_claim_synchronized_race(tmp_path: Path) -> None:
    """Barrier-synchronized race: two independent processes, each with its
    own ledger instance, claim the SAME key at the same instant across
    multiple rounds. Every round must elect exactly one winner (SPEC §56.14).

    The barrier guarantees both processes are inside claim_intent() at the
    same moment — an unsynchronized pair could trivially serialize.
    """
    state_dir = str(tmp_path / "state")
    ROUNDS = 5
    for round_no in range(ROUNDS):
        key = f"{round_no:031d}a"  # 32 hex-ish chars, unique per round
        procs = _spawn_racers(
            tmp_path, state_dir, n_processes=2, key=key, round_name=f"r{round_no}"
        )
        wins = 0
        for i, proc in enumerate(procs):
            proc.wait(timeout=30)
            assert proc.returncode == 0, f"racer {i} failed: {proc.stderr.read()}"
            won = json.loads((tmp_path / f"results-r{round_no}-{i}.json").read_text())["won"]
            wins += int(won)
        assert wins == 1, f"round {round_no}: expected exactly 1 winner, got {wins} (key={key!r})"


def test_cross_process_claim_four_way_race(tmp_path: Path) -> None:
    """Four independent processes racing on one key: still exactly one
    winner — the O_CREAT|O_EXCL lock must serialize all four."""
    state_dir = str(tmp_path / "state")
    key = "b" * 32
    procs = _spawn_racers(tmp_path, state_dir, n_processes=4, key=key, round_name="four")
    wins = 0
    for i, proc in enumerate(procs):
        proc.wait(timeout=30)
        assert proc.returncode == 0, f"racer {i} failed: {proc.stderr.read()}"
        won = json.loads((tmp_path / f"results-four-{i}.json").read_text())["won"]
        wins += int(won)
    assert wins == 1, f"four-way race must elect exactly one winner, got {wins}"


def test_claim_intent_stale_lock_fails_closed(tmp_path: Path) -> None:
    """An abandoned lock file (crashed process) makes claim_intent REFUSE the
    claim (returns False) rather than proceeding unsafely. Fail-closed: no
    double execution is ever possible because of a stale lock."""
    ledger = DurableFileLedger(str(tmp_path / "state"))
    intent = ExecutionIntent(
        task_id="t",
        step_id="s",
        fingerprint="f",
        authorization_epoch=1,
        tool="fs",
        operation="w",
        args={},
    )
    # forge a stale lock exactly where claim_intent looks for it
    ledger._DurableFileLedger__lock_path.write_text("stale", encoding="utf-8")
    # a claim against the stale lock must refuse (bounded wait, no hang)
    won = ledger.claim_intent("c" * 32, intent)
    assert won is False, "stale lock must fail closed, never proceed"


def test_claim_intent_rejects_malformed_key(tmp_path: Path) -> None:
    """claim_intent with a non-SHA256[:32] key raises PersistenceCorrupt."""
    ledger = DurableFileLedger(str(tmp_path / "state"))
    intent = ExecutionIntent(
        task_id="t",
        step_id="s",
        fingerprint="fp",
        authorization_epoch=1,
        tool="fs",
        operation="w",
        args={},
    )
    with pytest.raises(PersistenceCorrupt):
        ledger.claim_intent("not-a-valid-key", intent)


def test_claim_intent_validates_key_format(tmp_path: Path) -> None:
    """_KEY_RE only accepts 32 lowercase hex chars."""
    assert _KEY_RE.fullmatch("a" * 32) is not None
    assert _KEY_RE.fullmatch("0" * 32) is not None
    assert _KEY_RE.fullmatch("G" * 32) is None  # not hex
    assert _KEY_RE.fullmatch("a" * 31) is None  # too short
    assert _KEY_RE.fullmatch("a" * 33) is None  # too long
    assert _KEY_RE.fullmatch("A" * 32) is None  # uppercase
