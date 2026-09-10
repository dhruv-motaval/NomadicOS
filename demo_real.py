"""Real-model demo: Ollama fleet → ModelSelector → Agent Runtime → verified execution.

Run:  python demo_real.py "Write a short hello message into hello.txt"
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from nomadicos.agent.runtime import AgentRuntime
from nomadicos.audit.base import AuditEventCategory
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.agent.selector import HardwareConstraints, ModelSelector
from nomadicos.agent.selector_policies import SelectionPolicy
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.errors import ModelFailure, ModelResourceError, ModelUnavailable
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.evaluation.model_eval import ModelPerformanceTracker
from nomadicos.experience.recorder import ExperienceRecorder
from nomadicos.experience.store import InMemoryExperienceStore
from nomadicos.models.manager import ModelManager
from nomadicos.models.ollama_adapter import OllamaModel
from nomadicos.security.budgets import TaskBudget
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway

POLICY = """
version: "1.0.0"
owner:
  autonomy_level: assisted
  tools:
    - id: filesystem-all
      tool: filesystem
      risk: medium
      default_decision: allow
  external_network:
    allow_public_get: true
"""


async def main() -> None:
    goal = sys.argv[1] if len(sys.argv) > 1 else (
        'Create hello.txt containing "NomadicOS live" then report done. '
        "Reply with one JSON object only."
    )
    workspace = Path(tempfile.mkdtemp()) / "ws"
    workspace.mkdir(parents=True)

    # 1. Discovery (BP §148): real models from the local Ollama server.
    fleet = await OllamaModel.discover()
    print(f"Discovered {len(fleet)} local models (BP §148).")

    # 2. Registry (BP §62) + health (BP §62 probe) — quarantine unhealthy.
    manager = ModelManager(max_resident=2)
    for model in fleet:
        manager.register(model, status=__import__(
            "nomadicos.models.base", fromlist=["ModelStatus"]
        ).ModelStatus.ENABLED)

    # 3. Constitution + mediation stack (BP §98).
    policy_dir = Path("config/policies")
    policy = PolicyEngine()
    policy.load_directory(policy_dir)
    permissions = PermissionEngine()
    audit = FakeAuditSink()
    gate = SecurityGate(policy, permissions, audit)

    # 4. Tools (BP §13).
    gateway = ToolGateway(gate, audit)
    gateway.register(FilesystemTool(workspace_root=str(workspace)))

    # 5. Adaptive selection (BP §97/§320) + runtime (BP §185).
    selector = ModelSelector(
        manager,
        ModelPerformanceTracker(),
        policy=SelectionPolicy(min_attempts_for_learning=2, unverified_quality_cap=6.0),
        hardware=HardwareConstraints(ram_mb=16384),
    )
    runtime = AgentRuntime(
        selector=selector,
        manager=manager,
        gateway=gateway,
        audit_sink=audit,
        recorder=ExperienceRecorder(InMemoryExperienceStore()),
        evaluator=EvaluationEngine(),
        budget=TaskBudget(max_steps=4, max_model_calls=6),
    )

    identity = __import__(
        "nomadicos.security.permissions", fromlist=["SubjectIdentity"]
    ).SubjectIdentity(user_id="local-owner")

    print(f"Goal: {goal}")
    report = await runtime.execute_task(goal, identity, max_steps=4)
    print()
    print("=" * 60)
    print(report.render())
    print("=" * 60)
    print("Experience:", report.experience_id)

    # Show the workspace evidence (BP §146).
    created = workspace / "hello.txt"
    if created.exists():
        print(f"Evidence — {created.name}: {created.read_text()!r}")

    from nomadicos.models.ollama_adapter import OllamaModel as OM

    for model in fleet:
        await model.aclose()


if __name__ == "__main__":
    asyncio.run(main())
