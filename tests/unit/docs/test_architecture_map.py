"""CI check: architecture map cannot drift from the code (BP §298/§294-style).

Validates that every node's declared code files and test paths exist, that the
manifest is valid JSON with required fields, and that node count covers every
subsystem package.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
MANIFEST = ROOT / "docs/architecture/architecture.json"


def test_manifest_is_valid_json_with_required_sections() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for section in ("meta", "layers", "nodes", "edges", "flow"):
        assert section in data
    assert len(data["nodes"]) >= 20
    assert len(data["flow"]) >= 10


def test_every_node_declares_required_metadata() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    layer_ids = {layer["id"] for layer in data["layers"]}
    for node in data["nodes"]:
        assert node["layer"] in layer_ids, node["id"]
        for field in ("title", "summary", "what", "how", "tech", "why", "bp", "code", "tests"):
            assert field in node, f"{node['id']} missing {field}"
        assert node["why"], f"{node['id']} missing rationale (BP §295)"


def test_declared_code_files_exist() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    missing = []
    for node in data["nodes"]:
        if node.get("status") == "planned":
            continue  # planned nodes may have no code yet (BP §295)
        for code_path in node["code"]:
            if not (ROOT / code_path).exists():
                missing.append(f"{node['id']}: {code_path}")
    assert not missing, f"stale code refs: {missing}"


def test_declared_test_paths_exist() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    missing = []
    for node in data["nodes"]:
        if node.get("status") == "planned":
            continue
        for test_path in node["tests"]:
            if not (ROOT / test_path).exists():
                missing.append(f"{node['id']}: {test_path}")
    assert not missing, f"stale test refs: {missing}"


def test_edges_reference_real_nodes() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = {n["id"] for n in data["nodes"]}
    for edge in data["edges"]:
        assert edge["from"] in ids, edge
        assert edge["to"] in ids, edge


def test_every_subsystem_package_is_mapped() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mapped = " ".join(code for node in data["nodes"] for code in node["code"])
    planned = {node["id"] for node in data["nodes"] if node.get("status") == "planned"}
    for pkg in (ROOT / "src/nomadicos").iterdir():
        if pkg.is_dir() and pkg.name != "__pycache__":
            if pkg.name in planned:
                continue  # planned subsystems have no code yet (BP §295)
            assert pkg.name in mapped, f"subsystem not in map: {pkg.name}"


def test_flow_covers_the_canonical_loop() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    steps = [step["title"].lower() + " " + step["what"].lower() for step in data["flow"]]
    joined = " ".join(steps)
    stages = (
        "intake", "model selection", "proposal", "security gate",
        "execution", "verification", "report",
    )
    for stage in stages:
        assert stage in joined, f"flow missing stage: {stage}"
