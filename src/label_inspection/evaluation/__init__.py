"""Dataset and evaluation support utilities."""

from .dataset import (
    EXPECTED_FIELDS,
    IMAGE_ID_PATTERN,
    SUPPORTED_SCHEMA_VERSION,
    ValidationIssue,
    ValidationReport,
    is_valid_image_id,
    validate_dataset,
)
from .evaluator import (
    FAILURE_STAGES,
    DatasetEvaluator,
    EvaluationInitializationError,
    EvaluationStageError,
)
from .metrics import calculate_metrics, calculate_phase1_metrics, exact_match
from .reporting import write_phase1_outputs

__all__ = [
    "EXPECTED_FIELDS",
    "IMAGE_ID_PATTERN",
    "SUPPORTED_SCHEMA_VERSION",
    "ValidationIssue",
    "ValidationReport",
    "is_valid_image_id",
    "validate_dataset",
    "FAILURE_STAGES",
    "DatasetEvaluator",
    "EvaluationInitializationError",
    "EvaluationStageError",
    "calculate_metrics",
    "calculate_phase1_metrics",
    "exact_match",
    "write_phase1_outputs",
]
