"""Admission consumes signal snapshots and current opens, never completed trades."""
from __future__ import annotations
from dataclasses import replace
import math
from .config import Config
from .domain import Features, Signal
from .features import quantile


class F25History:
    def __init__(self, config: Config, origins: set | None = None) -> None:
        self.config = config
        self.origins = origins
        self.observations: list[tuple[str, str, float]] = []
        self.seen: set[str] = set()
        if config.f25_coverage == "registry" and origins is None:
            raise ValueError("registry coverage requires an explicit origin registry")
        if config.f25_coverage == "registry" and not config.allow_retrospective:
            raise ValueError("unverified frozen registry requires allow_retrospective=true")

    def feature(self, feature: Features) -> Features:
        if self.config.f25_coverage != "registry":
            return feature
        pair = feature.pair
        key = (pair.product, feature.day, pair.main, pair.secondary)
        return feature if key in self.origins else replace(feature, f25=None)

    def decision(self, feature: Features, *, enabled: bool) -> tuple[float | None, int, bool, str]:
        history = [score for available, day, score in self.observations if available <= feature.day and day < feature.day]
        if not enabled or not self.config.f25_enabled or feature.day < self.config.evaluation_start:
            return None, len(history), True, "disabled_or_pre_evaluation"
        if len(history) < self.config.f25_minimum_history:
            return None, len(history), self.config.f25_warmup == "keep", "insufficient_history_" + self.config.f25_warmup
        threshold = quantile(history, self.config.f25_quantile)
        if feature.f25 is None:
            return threshold, len(history), self.config.f25_missing == "neutral_keep", "missing_" + self.config.f25_missing
        passed = feature.f25 <= threshold
        return threshold, len(history), passed, "passes_tail" if passed else "high_tail"

    def observe(self, signal: Signal, available_day: str) -> None:
        # Observe only after VR/MRE/cost eligibility is actually known. Include factor rejections.
        feature = signal.features
        if signal.signal_id in self.seen:
            return
        self.seen.add(signal.signal_id)
        if feature.f25 is not None and math.isfinite(feature.f25):
            self.observations.append((available_day, feature.day, feature.f25))


def at_open(signal: Signal, near_open: float, far_open: float, *, config: Config,
            cost_threshold: float | None, enabled: bool) -> tuple[bool, bool, dict]:
    feature, model = signal.features, signal.model
    residual = near_open - far_open - feature.mean
    ratio = abs(residual) / abs(feature.residual) if feature.residual else None
    coverage = abs(residual) / (2 * (near_open + far_open) * config.assumed_cost_bps / 10000)
    trend = model.family == "trend_following"
    vr_passed = (not enabled or trend or not config.vr_enabled or
                 (feature.vr is not None and model.vr_threshold is not None and feature.vr >= model.vr_threshold))
    mre_passed = (not enabled or trend or not config.mre_enabled or
                  (feature.residual * residual > 0 and ratio is not None and ratio >= model.target))
    cost_passed = (not enabled or trend or not config.cost_edge_enabled or cost_threshold is None or coverage >= cost_threshold)
    before_factor = vr_passed and mre_passed and cost_passed
    passed = before_factor and signal.f25_passed
    return passed, before_factor, {
        "signal_id": signal.signal_id, "signal_date": feature.day, "product": feature.pair.product,
        "main_contract": feature.pair.main, "secondary_contract": feature.pair.secondary,
        "model_id": model.model_id, "vr": feature.vr, "vr_threshold": model.vr_threshold,
        "vr_passed": vr_passed, "signal_residual": feature.residual, "open_residual": residual,
        "mre_ratio": ratio, "mre_passed": mre_passed, "cost_coverage": coverage,
        "cost_threshold": cost_threshold, "cost_passed": cost_passed, "f25": feature.f25,
        "f25_threshold": signal.f25_threshold, "f25_history_count": signal.f25_history_count,
        "f25_passed": signal.f25_passed, "f25_reason": signal.f25_reason, "admitted": passed,
    }
