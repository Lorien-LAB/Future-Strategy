"""Frozen MR / channel-trend exits and explicitly dated research routing."""
from __future__ import annotations
from .config import Config
from .domain import LifeCycle

EMPIRICAL_TREND = frozenset({"P", "Y", "M", "OI", "SC", "SA", "PP"})
EMPIRICAL_ABSTAIN = frozenset({"B", "BR", "C", "CF", "JD", "LC", "LH", "MA", "NR", "PS", "UR"})
EMPIRICAL_SELECTED_AT = "2026-07-23"


def families(product: str, deployment_day: str, config: Config) -> tuple[str, ...]:
    if config.router == "fixed_empirical_research":
        if product in EMPIRICAL_ABSTAIN:
            return ()
        result = ("trend_following",) if product in EMPIRICAL_TREND else ("mean_reversion",)
        return () if deployment_day >= config.mr_only_from and result == ("trend_following",) else result
    if config.router == "mr_only" or deployment_day >= config.mr_only_from:
        return ("mean_reversion",)
    return ("mean_reversion", "trend_following")


def observe_exit(life: LifeCycle, spread: float, normalized: float, config: Config) -> str | None:
    """Consume exactly one close; freeze the first confirmed exit."""
    if life.pending_exit:
        return life.pending_exit
    feature, model = life.signal.features, life.signal.model
    if model.family == "mean_reversion":
        residual = spread - feature.mean
        if config.barrier_mode == "absolute_residual":
            win = abs(residual) < model.target * abs(feature.residual)
            loss = abs(residual) > model.stop * abs(feature.residual)
        else:
            oriented = residual * (1 if feature.residual > 0 else -1)
            win = oriented < model.target * abs(feature.residual)
            loss = oriented > model.stop * abs(feature.residual)
        life.win_streak = life.win_streak + 1 if win else 0
        life.loss_streak = life.loss_streak + 1 if loss else 0
        reason = ("convergence" if life.win_streak >= config.take_profit_confirm else
                  "divergence" if life.loss_streak >= config.stop_loss_confirm else None)
    else:
        direction, scale = feature.direction, feature.trend_scale
        if scale <= 0:
            raise ValueError("trend lifecycle needs a positive frozen scale")
        prior_best = feature.normalized_spread if life.favorable is None else life.favorable
        life.favorable = max(prior_best, normalized) if direction > 0 else min(prior_best, normalized)
        signed_move = direction * (normalized - feature.normalized_spread)
        favorable_move = direction * (life.favorable - feature.normalized_spread)
        retreat = direction * (life.favorable - normalized)
        channel = life.trend_history[-model.exit_lookback:]
        channel_exit = len(channel) >= model.exit_lookback and (
            (direction > 0 and normalized < min(channel)) or (direction < 0 and normalized > max(channel)))
        reason = ("hard_stop" if signed_move <= -model.hard_stop * scale else
                  "trailing_stop" if favorable_move > 0 and retreat >= model.trailing_stop * scale else
                  "channel_exit" if channel_exit else None)
        life.trend_history.append(normalized)
    if reason:
        life.pending_exit = reason
    return reason
