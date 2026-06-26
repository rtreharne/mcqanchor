from __future__ import annotations

from datetime import date, datetime

from django.utils import timezone


def engagement_release_date(block) -> date | None:
    config = getattr(block, "config", None)
    value = getattr(config, "release_date", None) if config is not None else None
    if value is None:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value).date()
        return value.date()
    if isinstance(value, date):
        return value
    return None


def base_practice_weights(course) -> dict[str, int]:
    if not hasattr(course, "config"):
        mastery = 40
        coverage = 30
        engagement = 30
    else:
        mastery = int(course.config.mastery_weight or 0)
        coverage = int(course.config.coverage_weight or 0)
        engagement = int(course.config.engagement_weight or 0)
    return {
        "mastery": mastery,
        "coverage": coverage,
        "engagement": engagement,
        "total": mastery + coverage + engagement,
    }


def weighted_practice_score_from_weights(metrics: dict, weights: dict[str, int]) -> float:
    total_weight = int(weights.get("total") or 0)
    if total_weight <= 0:
        return 0.0
    weighted_total = (
        float(metrics.get("mastery", 0) or 0) * int(weights.get("mastery") or 0)
        + float(metrics.get("coverage", 0) or 0) * int(weights.get("coverage") or 0)
        + float(metrics.get("engagement", 0) or 0) * int(weights.get("engagement") or 0)
    )
    return round(weighted_total / total_weight, 2)


def weighted_practice_score(course, metrics: dict) -> float:
    return weighted_practice_score_from_weights(metrics, base_practice_weights(course))


def combine_block_practice_metrics(course, block_metric_pairs: list[dict]) -> dict:
    weights = base_practice_weights(course)
    if not block_metric_pairs:
        return {
            "mastery": 0.0,
            "coverage": 0.0,
            "engagement": 0.0,
            "overall": 0.0,
            "weights": weights,
        }

    block_count = len(block_metric_pairs)
    mastery = round(sum(float(item["metrics"].get("mastery", 0) or 0) for item in block_metric_pairs) / block_count, 2)
    coverage = round(sum(float(item["metrics"].get("coverage", 0) or 0) for item in block_metric_pairs) / block_count, 2)
    engagement = round(sum(float(item["metrics"].get("engagement", 0) or 0) for item in block_metric_pairs) / block_count, 2)
    overall = round(
        sum(weighted_practice_score_from_weights(item["metrics"], weights) for item in block_metric_pairs) / block_count,
        2,
    )
    return {
        "mastery": mastery,
        "coverage": coverage,
        "engagement": engagement,
        "overall": overall,
        "weights": weights,
    }
