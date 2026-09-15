"""Property tests: no untrusted string escapes the boundary un-checked.

Invariant under fuzz:

    arbitrary malformed model output
        -> either raises InvalidProposal (fail closed)
        -> or a valid, closed ActionProposal/CompletionClaim contract,
           which additionally contains no authority keys anywhere.

Fuzzing must never produce an *executable* artifact: the parser cannot mint
AuthorizedAction/AuthorizationGrant by construction (no imports from the
authority layer, and pydantic contracts forbid extras).
"""

from __future__ import annotations

import random
import string

from nomadicos.action_ir import parse_model_output
from nomadicos.contracts.action import MODEL_AUTHORITY_FIELDS, ActionProposal, CompletionClaim
from nomadicos.kernel.errors import InvalidProposal

ALPHABET = string.printable + "日本語🔥" + "\x00"


def _iter_corrupt(text: str, rng: random.Random, count: int = 6):
    """Random corruption: truncation, char flips, splices, brace noise."""
    for _ in range(count):
        mode = rng.randrange(5)
        if mode == 0:
            yield text[: max(0, rng.randrange(len(text) + 1))]
        elif mode == 1:
            if text:
                i = rng.randrange(len(text))
                yield text[:i] + rng.choice(ALPHABET) + text[i + 1 :]
            else:
                yield rng.choice(ALPHABET)
        elif mode == 2:
            i, j = sorted(rng.randrange(len(text) + 1) for _ in range(2))
            yield text[:i] + rng.choice(["{", "}", '"', ":,", "[", "\\"]) + text[j:]
        elif mode == 3:
            yield rng.choice(ALPHABET) * rng.randrange(200)
        else:
            yield text + "".join(rng.sample(["```json {}", '{"a":', "null", '}"'], k=2))


def test_fuzz_model_output_never_escapes_checks() -> None:
    rng = random.Random(20260915)
    valid_bodies = [
        '{"tool": "filesystem", "operation": "write", "args": {"path": "a", "content": "b"}}',
        '{"finished": true}',
        '```json\n{"tool": "x", "operation": "y"}\n```',
        '{"tool": "filesystem", "operation": "write", "args": {}, "authorized": true}',
    ]

    def walk_keys(node: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key).lower())
                keys |= walk_keys(value)
        elif isinstance(node, list):
            for item in node:
                keys |= walk_keys(item)
        return keys

    successes = 0
    for _ in range(3000):
        seed_text = (
            rng.choice(valid_bodies)
            if rng.random() < 0.7
            else "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(0, 40)))
        )
        for raw in [seed_text, *_iter_corrupt(seed_text, rng)]:
            try:
                parsed = parse_model_output(
                    str(raw), task_id="task_1", step_id="step_1", model_id="m1"
                )
            except InvalidProposal:
                continue
            # Any pass must be a closed contract carrying zero authority keys.
            value = parsed.value
            assert isinstance(value, ActionProposal | CompletionClaim)
            assert not (walk_keys(value.model_dump()) & MODEL_AUTHORITY_FIELDS)
            successes += 1
    assert successes > 0  # fuzz does reach the accept path sometimes
    # every accepted fuzz artifact is still untrusted intent, never a decision:
    assert not hasattr(ActionProposal, "execute")


def test_no_json_shape_fabricates_execution_artifacts() -> None:
    """Structural guarantee: the parser's module has no path to authority."""
    import inspect

    import nomadicos.action_ir.parser as parser_module

    src = inspect.getsource(parser_module)
    for forbidden in ("AuthorizedAction", "AuthorizationGrant", "executor", "authorize"):
        assert forbidden not in src
