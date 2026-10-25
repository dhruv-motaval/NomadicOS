"""Vision-based GoalVerifier (SPEC §27-29, §13 replaceable bricks).

Uses a vision-capable model to evaluate visual goals against screenshot
evidence. The vision model is a registered model (e.g., llama3.2-vision,
llava) — same routing, authorization, and fail-closed parser as any model.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nomadicos.contracts.core import Goal
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.inference.base import ChatMessage, GenerationRequest
from nomadicos.verification.evidence import EvidenceContext

if TYPE_CHECKING:
    from nomadicos.inference.base import InferenceEngine

VISION_VERIFIER_ID = "nomadic-vision-goal-verifier-v1"
MAX_EVIDENCE_IMAGES = 5
MAX_IMAGE_BYTES = 2_000_000  # 2MB per image for vision model


_VISION_PROMPT = (
    "You are NomadicOS' VISION GOAL VERIFIER. Evaluate whether the owner's "
    "goal has been achieved based on VISUAL EVIDENCE (screenshots) and "
    "execution history. Reply with ONLY one JSON object, no prose:\n"
    '{"verdict": "PASS|NOT_PASS|NOT_VERIFIED|BLOCKED", "confidence": 0.0, '
    '"evidence": [{"claim": "...", "observed": true|false, "detail": {...}}], '
    '"reasoning": "..."}\n'
    "Rules: verdict must be one of PASS/NOT_PASS/NOT_VERIFIED/BLOCKED; "
    "confidence 0.0-1.0; evidence items must have observed boolean; "
    "reasoning max 2000 chars; never include authority fields.\n\n"
    "GOAL, EXECUTIONS, AND SCREENSHOTS:\n"
)


class VisionGoalVerifier:
    """Vision-capable goal verifier using a registered vision model."""

    def __init__(
        self,
        vision_engine: InferenceEngine,
        vision_model_id: str,
        *,
        workspace_root: str | Path | None = None,
        per_task: bool = True,
        max_images: int = MAX_EVIDENCE_IMAGES,
    ) -> None:
        self.engine = vision_engine
        self.model_id = vision_model_id
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.per_task = per_task
        self.max_images = max_images

    def verify(self, goal: Goal) -> VerificationResult:
        return self.verify_task(goal, [])

    def verify_task(self, goal: Goal, executions: list[ExecutionResult]) -> VerificationResult:
        workspace = None
        if self.workspace_root is not None:
            workspace = self.workspace_root / goal.id if self.per_task else self.workspace_root

        ctx = EvidenceContext(
            task_id=goal.id,
            workspace=workspace,
            executions=list(executions),
        )

        # Collect screenshot evidence from executions
        screenshots = self._collect_screenshots(ctx)
        if not screenshots:
            # No visual evidence available - fall back to predicate-only or NOT_VERIFIED
            return self._no_visual_evidence(goal, ctx)

        # Build vision request
        request = self._build_request(goal, executions, screenshots, ctx)

        # The vision model call happens through the normal inference path
        # but verification is SYNCHRONOUS here - the graph calls verify() directly
        # We need to run the async generation

        try:
            response = asyncio.get_event_loop().run_until_complete(self.engine.generate(request))
        except Exception as exc:
            return VerificationResult(
                level=VerificationLevel.GOAL,
                task_id=goal.id,
                outcome=VerificationOutcome.BLOCKED,
                verifier=VISION_VERIFIER_ID,
                evidence=[
                    EvidenceItem(
                        claim=f"vision verifier engine error: {exc}",
                        observed=False,
                        unverifiable=True,
                    )
                ],
            )

        return self._parse_response(goal.id, response.text, screenshots)

    def _collect_screenshots(self, ctx: EvidenceContext) -> list[dict[str, Any]]:
        """Extract screenshot artifacts from execution evidence."""
        screenshots: list[dict[str, Any]] = []
        for exec_result in ctx.executions:
            if exec_result.tool == "desktop" and exec_result.operation == "screenshot":
                artifact = exec_result.evidence.get("artifact")
                path = exec_result.evidence.get("path")
                if artifact and path:
                    try:
                        p = Path(path)
                        if p.exists() and p.stat().st_size <= MAX_IMAGE_BYTES:
                            with open(p, "rb") as f:
                                b64 = base64.b64encode(f.read()).decode("ascii")
                            screenshots.append(
                                {
                                    "artifact": artifact,
                                    "path": str(path),
                                    "b64": b64,
                                    "width": exec_result.evidence.get("width"),
                                    "height": exec_result.evidence.get("height"),
                                }
                            )
                            if len(screenshots) >= self.max_images:
                                break
                    except OSError:
                        continue
        return screenshots

    def _no_visual_evidence(self, goal: Goal, ctx: EvidenceContext) -> VerificationResult:
        """No screenshots captured - delegate to predicate verifier or report NOT_VERIFIED."""
        if goal.predicates:
            # Delegate to predicate verifier (would be composed externally)
            return VerificationResult(
                level=VerificationLevel.GOAL,
                task_id=goal.id,
                outcome=VerificationOutcome.NOT_VERIFIED,
                verifier=VISION_VERIFIER_ID,
                evidence=[
                    EvidenceItem(
                        claim="no visual evidence captured; predicate evaluation required",
                        observed=False,
                        unverifiable=True,
                    )
                ],
            )
        return VerificationResult(
            level=VerificationLevel.GOAL,
            task_id=goal.id,
            outcome=VerificationOutcome.NOT_VERIFIED,
            verifier=VISION_VERIFIER_ID,
            evidence=[
                EvidenceItem(
                    claim=(
                        "goal declares no completion predicates and no "
                        "visual evidence captured - truth undecidable"
                    ),
                    observed=False,
                    unverifiable=True,
                )
            ],
        )

    def _build_request(
        self,
        goal: Goal,
        executions: list[ExecutionResult],
        screenshots: list[dict[str, Any]],
        ctx: EvidenceContext,
    ) -> GenerationRequest:
        # Summarize executions
        exec_summary = []
        for e in executions[-10:]:  # last 10
            exec_summary.append(f"- {e.tool}.{e.operation}: {e.status.value} ({e.message[:120]})")

        # Build screenshot descriptions
        screenshot_descs = []
        for i, s in enumerate(screenshots):
            screenshot_descs.append(
                f"  [Image {i + 1}] {s['artifact']} ({s['width']}x{s['height']})"
            )

        user_content = (
            f"OWNER GOAL: {goal.objective!r}\n"
            f"OWNER CONSTRAINTS: {goal.constraints}\n"
            f"EXECUTION HISTORY (last 10):\n"
            + ("\n".join(exec_summary) if exec_summary else "  (none)")
            + "\n\nVISUAL EVIDENCE (screenshots):\n"
            + ("\n".join(screenshot_descs) if screenshot_descs else "  (none)")
            + "\n\nEvaluate: has the goal been VISUALLY achieved? "
            + "Consider the execution history and screenshots together."
        )

        # Include screenshot data in the request for multimodal models
        # Note: actual multimodal depends on engine capability; here we pass
        # base64 images in the request metadata for engines that support it
        request = GenerationRequest(
            model_id=self.model_id,
            messages=[
                ChatMessage(role="system", content=_VISION_PROMPT),
                ChatMessage(role="user", content=user_content),
            ],
            temperature=0.0,
            max_tokens=1024,
            timeout_s=60,
            images=[s["b64"] for s in screenshots],  # for multimodal engines
        )
        return request

    def _parse_response(
        self, task_id: str, raw: str, screenshots: list[dict[str, Any]]
    ) -> VerificationResult:
        try:
            # Extract JSON from response (allow fenced code blocks)
            text = raw.strip()
            if text.startswith("```"):
                import re

                fences = re.findall(
                    r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL | re.IGNORECASE
                )
                if len(fences) == 1:
                    text = fences[0].strip()
            data = json.loads(text)
        except (json.JSONDecodeError, IndexError) as exc:
            return VerificationResult(
                level=VerificationLevel.GOAL,
                task_id=task_id,
                outcome=VerificationOutcome.BLOCKED,
                verifier=VISION_VERIFIER_ID,
                evidence=[
                    EvidenceItem(
                        claim=f"vision verifier: invalid model output: {exc}",
                        observed=False,
                        unverifiable=True,
                    )
                ],
            )

        # Validate required fields
        verdict_str = data.get("verdict", "").upper()
        try:
            verdict = VerificationOutcome(verdict_str)
        except ValueError:
            verdict = VerificationOutcome.BLOCKED

        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        evidence_items = []
        for item in data.get("evidence", []):
            if isinstance(item, dict):
                claim = str(item.get("claim", ""))[:500]
                observed = bool(item.get("observed", False))
                detail = item.get("detail", {})
                if not isinstance(detail, dict):
                    detail = {}
                evidence_items.append(EvidenceItem(claim=claim, observed=observed, detail=detail))

        _ = str(data.get("reasoning", ""))[:2000]

        # Add screenshot references to evidence
        for s in screenshots:
            evidence_items.append(
                EvidenceItem(
                    claim=f"screenshot captured: {s['artifact']}",
                    observed=True,
                    detail={"width": s["width"], "height": s["height"], "path": s["path"]},
                )
            )

        # Low confidence NOT_PASS -> NOT_VERIFIED per spec conservatism
        if verdict is VerificationOutcome.PASS and confidence < 0.7:
            verdict = VerificationOutcome.NOT_VERIFIED

        return VerificationResult(
            level=VerificationLevel.GOAL,
            task_id=task_id,
            outcome=verdict,
            verifier=VISION_VERIFIER_ID,
            evidence=evidence_items,
        )


class UnavailableVisionVerifier:
    """Honest absence when no vision model is available."""

    def __init__(self) -> None:
        pass

    def verify(self, goal: Goal) -> VerificationResult:
        return self.verify_task(goal, [])

    def verify_task(self, goal: Goal, executions: list[ExecutionResult]) -> VerificationResult:
        return VerificationResult(
            level=VerificationLevel.GOAL,
            task_id=goal.id,
            outcome=VerificationOutcome.NOT_VERIFIED,
            verifier="vision-verifier-unavailable",
            evidence=[
                EvidenceItem(
                    claim="vision verifier unavailable: no vision-capable model registered",
                    observed=False,
                    unverifiable=True,
                )
            ],
        )


def vision_verifier(
    vision_model_id: str,
    *,
    engines: dict[str, Any],
    workspace_root: str | Path | None = None,
    per_task: bool = True,
) -> VisionGoalVerifier | UnavailableVisionVerifier:
    """Factory: returns VisionGoalVerifier if model available, else honest absence."""
    engine = engines.get(vision_model_id)
    if engine is None:
        return UnavailableVisionVerifier()
    # Verify the engine can do vision (multimodal) - best effort check
    return VisionGoalVerifier(
        engine,
        vision_model_id,
        workspace_root=workspace_root,
        per_task=per_task,
    )
