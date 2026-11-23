"""Headline sentiment: FinBERT run locally, behind a small interface so the model can be swapped.

score = P(positive) - P(negative), so it runs from -1 (certainly negative) to +1
(certainly positive); a neutral headline sits near 0. The model (ProsusAI/finbert,
about 440 MB) is downloaded by Hugging Face on first use into the user's cache folder
and loaded lazily, in the news worker, the first time a headline needs scoring. If
transformers or torch are not installed, every score is missing with that reason:
the app never fills a sentiment in.

FinBERT's licence is CC BY-NC-SA 4.0: personal, non-commercial use.
"""
from __future__ import annotations

import logging
import os
import threading
import warnings
from typing import Any, Optional, Protocol

log = logging.getLogger(__name__)

NOT_INSTALLED = "FinBERT not installed (pip install torch transformers)"


class Scorer(Protocol):
    name: str

    def score(self, texts: list[str]) -> list[Optional[float]]: ...

    def status(self) -> str: ...


def finbert_score(labels: list[dict[str, Any]]) -> float:
    """From the pipeline's per-label scores ([{label, score}, ...]) to P(positive) - P(negative)."""
    probs = {str(entry.get("label", "")).lower(): float(entry.get("score", 0.0)) for entry in labels}
    return probs.get("positive", 0.0) - probs.get("negative", 0.0)


class NoScorer:
    """Scores nothing; every headline's sentiment is missing with `reason`."""

    name = "none"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def score(self, texts: list[str]) -> list[Optional[float]]:
        return [None for _ in texts]

    def status(self) -> str:
        return f"MISSING: {self.reason}"


class FinBertScorer:
    name = "finbert"

    def __init__(self, model: str = "ProsusAI/finbert", batch_size: int = 16, pipeline_factory: Any = None) -> None:
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self._factory = pipeline_factory or self._build_pipeline
        self._pipeline: Any = None
        self._error: Optional[str] = None
        self._lock = threading.Lock()

    @staticmethod
    def _quiet_environment() -> None:
        # Progress bars and advisory warnings would be drawn over the Textual screen. Seen on the first live run
        # (2026-11-22): the Windows "no symlinks" cache warning and the "unauthenticated requests" notice, both harmless.
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")
        warnings.filterwarnings("ignore", category=FutureWarning, module="transformers")

    def _build_pipeline(self) -> Any:
        self._quiet_environment()
        from huggingface_hub.utils import logging as hub_logging
        from transformers import pipeline  # imported here so the app starts without torch installed
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        hf_logging.disable_progress_bar()
        hub_logging.set_verbosity_error()
        return pipeline("text-classification", model=self.model, top_k=None, truncation=True, device="cpu")

    def load(self) -> bool:
        with self._lock:
            if self._pipeline is not None:
                return True
            if self._error is not None:
                return False
            log.info("loading %s (first run downloads about 440 MB)", self.model)
            try:
                self._pipeline = self._factory()
            except Exception as exc:  # noqa: BLE001  any load failure becomes a missing sentiment, not a crash
                self._error = f"{type(exc).__name__}: {exc}"[:160]
                log.warning("sentiment model unavailable: %s", self._error)
                return False
            log.info("%s loaded", self.model)
            return True

    def score(self, texts: list[str]) -> list[Optional[float]]:
        if not texts:
            return []
        if not self.load():
            return [None for _ in texts]
        out: list[Optional[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            try:
                results = self._pipeline(batch, batch_size=self.batch_size)
            except Exception as exc:  # noqa: BLE001
                log.warning("sentiment scoring failed for a batch of %d: %s", len(batch), exc)
                out.extend(None for _ in batch)
                continue
            for labels in results:
                out.append(finbert_score(list(labels)))
        return out

    def status(self) -> str:
        if self._pipeline is not None:
            return f"ok ({self.model})"
        if self._error:
            return f"MISSING: {self._error}"
        return f"idle, loads when needed ({self.model})"


def build_scorer(cfg: dict[str, Any]) -> Scorer:
    settings = cfg.get("sentiment", {})
    if not settings.get("enabled", True):
        return NoScorer("sentiment disabled in config.yaml")
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return NoScorer(NOT_INSTALLED)
    return FinBertScorer(model=str(settings.get("model", "ProsusAI/finbert")), batch_size=int(settings.get("batch_size", 16)))
