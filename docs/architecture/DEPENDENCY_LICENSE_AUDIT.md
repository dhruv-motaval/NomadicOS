# Dependency License Audit — ADR-0025 prerequisite (2026-09-05)

## Runtime dependencies (shipped to users)

| Package | Version | License | Copyleft? | Notes |
|---|---|---|---|---|
| pydantic | 2.13.2 | MIT | no | schema/validation |
| pydantic_core | 2.46.2 | MIT | no | pydantic runtime |
| PyYAML | 6.0.3 | MIT | no | config/policies |
| httpx | 0.28.1 | BSD-3-Clause | no | Ollama/network transport |
| psycopg 3 | 3.3.5 | **LGPL-3.0-only** | weak | **key item** — used via `pip install` (separate process/library); dynamic use satisfies LGPL. Alternative if needed: `pg8000` (Apache-2.0) |
| psycopg-binary | 3.3.5 | LGPL-3.0 | weak | bundled binary build of the above |
| Pillow | 12.3.0 | MIT-CMU | no | screen capture |
| PyAutoGUI | 0.9.54 | BSD | no | computer control |

## Dev-only dependencies (never shipped)

| Package | License |
|---|---|
| pytest | MIT |
| pytest-asyncio | Apache-2.0 |
| ruff, mypy, pre-commit, types-PyYAML | MIT / Apache-2.0 |

## Optional model-stack dependencies (hardware path)

| Package | License | Notes |
|---|---|---|
| llama-cpp-python | MIT | ADR-0001 adapter |
| transformers | Apache-2.0 | Gemma vision path |
| torch (BSD-3) + model **weights** | varies per model | model weights have their own licenses (e.g., Gemma Terms of Use — Google license, not OSI). Weights are downloaded by the user, not distributed by NomadicOS ⇒ no redistribution issue |

## Audit conclusion

1. **No runtime dependency forces a copyleft license** on NomadicOS. The only
   weak-copyleft item is psycopg 3 (LGPL-3.0), which is satisfied by normal
   pip-installed library use (dynamic linking equivalent). A mitigation exists
   (`pg8000`, Apache-2.0) if the owner ever wants a 100%-permissive tree.
2. **Model weights** are user-downloaded artifacts (BP §150-151 supply-chain
   boundary) — NomadicOS never redistributes them, so Gemma/llama weight terms
   do not constrain the code license.
3. Therefore the owner has a free choice. The realistic options:
   - **MIT** — simplest, maximum reuse, no patent clause.
   - **Apache-2.0** — adds explicit patent grant + contribution license
     (defensive for a security-sensitive project).
   - **Proprietary / private** — valid if never distributed; everything works
     locally without a license file.
