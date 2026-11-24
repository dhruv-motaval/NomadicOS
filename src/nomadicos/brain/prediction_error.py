"""Prediction error — compare what the brain predicted for the action it
selected with what the action really did (spec §29 PREDICTION ERROR, §30).

Deterministic and DATA only: the record is measured from a REAL execution
observation (its ``data`` carries ``status`` and, for commands,
``exit_code``). It never verifies, never decides success, never authorizes.
"""

from __future__ import annotations

from nomadicos.brain.schemas import MAX_PREDICTION_ERRORS, PredictionError
from nomadicos.brain.state import BrainState
from nomadicos.contracts.execution import Observation


def measure_prediction_error(
    state: BrainState, prediction_id: str, observation: Observation
) -> tuple[BrainState, PredictionError | None]:
    """Measure the prediction ``prediction_id`` (the one deliberation chose
    the executed action by) against ``observation``. Keyed by prediction,
    never by candidate id: models reuse ids like "c1" every cycle. Returns
    the state unchanged and ``None`` when the prediction is gone or the
    observation is not an execution result."""
    status = observation.data.get("status")
    if status is None:
        return state, None
    prediction = next((p for p in reversed(state.predictions) if p.id == prediction_id), None)
    if prediction is None:
        return state, None
    matched: list[str] = []
    mismatched: list[str] = []
    for key, fact in prediction.predicted_world.facts.items():
        if key not in observation.data:
            continue
        same = _norm(fact.value) == _norm(observation.data[key])
        (matched if same else mismatched).append(key)
    outcome = 1.0 if str(status).upper() == "SUCCEEDED" else 0.0
    record = PredictionError(
        candidate_id=prediction.candidate_id,
        prediction_id=prediction.id,
        observation_id=observation.id,
        confidence=prediction.confidence,
        outcome=outcome,
        calibration_error=round(abs(prediction.confidence - outcome), 4),
        matched=matched,
        mismatched=mismatched,
    )
    errors = [*state.prediction_errors, record][-MAX_PREDICTION_ERRORS:]
    return (
        state.model_copy(
            update={"prediction_errors": errors, "state_version": state.state_version + 1}
        ),
        record,
    )


def _norm(value: object) -> str:
    return str(value).strip().upper()


__all__ = ["measure_prediction_error"]
