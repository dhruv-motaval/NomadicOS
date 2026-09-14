"""Selection policy (BP §8.6, §187, §371): bounded, explainable, local-only."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nomadicos.core.errors import ModelUnavailable


class SelectionPolicy(BaseModel):
    """Constraints the ModelSelector must respect (fail closed)."""

    model_config = ConfigDict(extra="forbid")

    max_fallbacks: int = Field(default=2, ge=0)
    min_attempts_for_learning: int = Field(default=3, ge=1)  # BP §67: no blind promotion
    history_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    capability_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    project_specificity_bonus: float = Field(default=1.0, ge=0.0, le=5.0)
    unverified_quality_cap: float = Field(default=5.0, ge=0.0, le=10.0)

    @model_validator(mode="after")
    def _weights_sum(self) -> "SelectionPolicy":
        total = self.history_weight + self.capability_weight
        if abs(total - 1.0) > 1e-6:
            raise ModelUnavailable("selection weights must sum to 1.0", context={"total": total})
        return self


__all__ = ["SelectionPolicy"]
