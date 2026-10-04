# Benchmarks Judges Registry and Dispatcher.
# Provides official benchmark judges for LongMemEval, LoCoMo, BEAM, and Generic/CommonTrace.
from __future__ import annotations

from typing import Union

from .beam import BEAM_ABILITIES, BEAMJudge, event_ordering_score, kendall_tau_b
from .dolphin import DolphinJudge
from .generic import GenericJudge
from .locomo import LoCoMoJudge, is_scorable_category
from .longmemeval import LongMemEvalJudge, get_anscheck_prompt

JudgeType = Union[GenericJudge, LongMemEvalJudge, LoCoMoJudge, BEAMJudge, DolphinJudge]

__all__ = [
    "BEAM_ABILITIES",
    "BEAMJudge",
    "DolphinJudge",
    "GenericJudge",
    "JudgeType",
    "LoCoMoJudge",
    "LongMemEvalJudge",
    "event_ordering_score",
    "get_anscheck_prompt",
    "get_default_judge_for_dataset",
    "get_judge",
    "get_judge_for_dataset",
    "is_scorable_category",
    "kendall_tau_b",
    "normalize_name",
]

JUDGE_CLASSES: dict[str, type] = {
    "generic": GenericJudge,
    "longmemeval": LongMemEvalJudge,
    "locomo": LoCoMoJudge,
    "beam": BEAMJudge,
    "dolphin": DolphinJudge,
}

DEFAULT_JUDGE_MODELS: dict[str, str] = {
    "generic": "claude-sonnet-5",
    "longmemeval": "gpt-4o",
    "locomo": "gpt-4o",
    "beam": "gpt-4.1-mini",
    "dolphin": "gpt-4o",
}

DATASET_DEFAULTS: dict[str, tuple[str, str]] = {
    "locomo": ("locomo", "gpt-4o"),
    "longmemeval": ("longmemeval", "gpt-4o"),
    "beam": ("beam", "gpt-4.1-mini"),
    "generic": ("generic", "claude-sonnet-5"),
    "dolphin": ("generic", "claude-sonnet-5"),
}


def normalize_name(name: str) -> str:
    """Normalize judge or dataset name by lowercasing and stripping hyphens/underscores."""
    return name.strip().lower().replace("-", "").replace("_", "")


def get_judge(name: str, model: str | None = None) -> JudgeType:
    """Registry and dispatcher returning a judge instance by name.

    Supports 'generic', 'longmemeval', 'locomo', 'beam'.
    """
    key = normalize_name(name)
    lookup = {
        "generic": "generic",
        "longmemeval": "longmemeval",
        "locomo": "locomo",
        "beam": "beam",
        "dolphin": "dolphin",
    }
    canonical = lookup.get(key)
    if canonical is None:
        raise ValueError(
            f"Unknown judge: {name!r}. Supported judges: {list(JUDGE_CLASSES.keys())}"
        )
    cls = JUDGE_CLASSES[canonical]
    return cls(model=model)


def get_default_judge_for_dataset(dataset: str) -> tuple[str, str]:
    """Helper to get official default (judge_name, default_model) for a dataset.

    Mappings:
      - locomo      -> ('locomo', 'gpt-4o')
      - longmemeval -> ('longmemeval', 'gpt-4o')
      - beam        -> ('beam', 'gpt-4.1-mini')
      - generic     -> ('generic', 'claude-sonnet-5')
      - dolphin     -> ('generic', 'claude-sonnet-5')
    """
    key = dataset.strip().lower().replace("-", "").replace("_", "")
    lookup = {
        "locomo": "locomo",
        "longmemeval": "longmemeval",
        "beam": "beam",
        "generic": "generic",
        "dolphin": "dolphin",
    }
    canonical = lookup.get(key, "generic")
    return DATASET_DEFAULTS.get(canonical, ("generic", "claude-sonnet-5"))


def get_default_judge(dataset: str) -> tuple[str, str]:
    """Alias for get_default_judge_for_dataset."""
    return get_default_judge_for_dataset(dataset)


def get_judge_for_dataset(dataset: str, model: str | None = None) -> JudgeType:
    """Helper that returns an instantiated judge configured with default model for the dataset."""
    judge_name, default_model = get_default_judge_for_dataset(dataset)
    return get_judge(judge_name, model=model or default_model)


__all__ = [
    "GenericJudge",
    "LongMemEvalJudge",
    "LoCoMoJudge",
    "BEAMJudge",
    "get_judge",
    "get_default_judge_for_dataset",
    "get_default_judge",
    "get_judge_for_dataset",
    "DEFAULT_JUDGE_MODELS",
    "DATASET_DEFAULTS",
    "kendall_tau_b",
    "event_ordering_score",
    "is_scorable_category",
    "get_anscheck_prompt",
]
