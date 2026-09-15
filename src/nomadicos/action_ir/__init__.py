"""Action IR boundary (SPEC §19, §53 Phase 4).

MODEL OUTPUT -> PARSER -> ACTION PROPOSAL -> VALIDATION -> (capability
resolution in the authority brick) -> ... The executor is never reached
directly from model text. Everything here fails closed.
"""

from nomadicos.action_ir.parser import (
    ParsedProposal,
    extract_json_candidate,
    parse_model_output,
    reject_authority_fields,
)
from nomadicos.action_ir.validation import ProposalValidator, ToolCatalog, ValidationContext

__all__ = [
    "ParsedProposal",
    "ProposalValidator",
    "ToolCatalog",
    "ValidationContext",
    "extract_json_candidate",
    "parse_model_output",
    "reject_authority_fields",
]
