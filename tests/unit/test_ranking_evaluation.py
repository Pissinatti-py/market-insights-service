"""Verify benchmark arithmetic against known perfect and reversed rankings."""

import pytest

from scripts.evaluate_ranker import auc_delta_interval, cross_validate, metrics


@pytest.mark.parametrize(
    "scores,auc,brier,balanced",
    [([1.0, 1.0, 0.0, 0.0], 1.0, 0.0, 1.0), ([0.0, 0.0, 1.0, 1.0], 0.0, 1.0, 0.0), ([0.5] * 4, 0.5, 0.25, 0.5)],
)
def test_metrics_match_known_rankings(scores, auc, brier, balanced):
    result = metrics(scores, [True, True, False, False])
    assert result["auc"] == auc
    assert result["brier"] == brier
    assert result["balanced_accuracy_at_0_5"] == balanced


def test_paired_interval_keeps_comparison_direction():
    assert auc_delta_interval([1.0, 0.0], [0.0, 1.0], [True, False]) == [1.0, 1.0]


def test_conflicting_duplicate_labels_cannot_leak_across_validation_folds():
    samples = [
        {"id": "a", "text": "same source", "approved": True},
        {"id": "b", "text": "same source", "approved": False},
    ]
    with pytest.raises(ValueError, match="conflicting"):
        cross_validate([[1.0], [1.0], [1.0]], samples)
