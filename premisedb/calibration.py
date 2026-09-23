"""Model-bound sigmoid calibrations fitted on labeled financial passages."""

from dataclasses import dataclass
import json
from math import exp, isfinite, log
from pathlib import Path


def score_logit(score: float) -> float:
    if type(score) not in (int, float) or not 0 <= score <= 1:
        raise ValueError("Expected a finite score in [0, 1]")
    bounded = min(1 - 1e-6, max(1e-6, score))
    return log(bounded / (1 - bounded))


@dataclass(frozen=True)
class ScoreCalibration:
    name: str
    model: str
    slope: float
    intercept: float
    irrelevant_chunk_penalty: float = 0.001
    minimum_value: float = 0.01

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.name, self.model)):
            raise ValueError("Calibration requires a name and model")
        for value in (self.slope, self.intercept, self.irrelevant_chunk_penalty, self.minimum_value):
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError("Calibration parameters must be finite numbers")
        if self.slope <= 0 or self.irrelevant_chunk_penalty <= 0 or self.minimum_value <= 0:
            raise ValueError("Slope, penalty and minimum value must be positive")

    def apply(self, scores) -> tuple[float, ...]:
        result = []
        for score in scores:
            value = self.slope * score_logit(score) + self.intercept
            # Stable sigmoid, including callers' extreme but finite parameters.
            result.append(1 / (1 + exp(-value)) if value >= 0 else exp(value) / (1 + exp(value)))
        return tuple(result)


def calibration_for(model: str) -> ScoreCalibration:
    profiles = json.loads(Path(__file__).with_name("calibrations.json").read_text())
    if model not in profiles:
        raise ValueError("No calibration for this model; supply a model-matched ScoreCalibration")
    return ScoreCalibration(**profiles[model])
