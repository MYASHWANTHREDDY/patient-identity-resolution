import pandas as pd
import pytest

from mdm.threshold_sweep import best_f1, find_thresholds, precision_recall_curve

# 3 true matches, 3 non-matches, scores perfectly separate them at score >= 5
SCORES = pd.Series([10.0, 8.0, 5.0, 4.0, 1.0, -2.0])
LABELS = pd.Series([True, True, True, False, False, False])


def test_precision_recall_curve_perfect_separation_hits_precision_and_recall_one():
    curve = precision_recall_curve(SCORES, LABELS)
    # cutoff at score=5.0 (the lowest true positive) captures all 3 positives, 0 negatives
    row = curve[curve["score"] == 5.0].iloc[0]
    assert row["precision"] == 1.0
    assert row["recall"] == 1.0


def test_precision_recall_curve_lowest_cutoff_has_full_recall():
    curve = precision_recall_curve(SCORES, LABELS)
    assert curve.iloc[-1]["recall"] == 1.0


def test_precision_recall_curve_highest_cutoff_has_full_precision():
    curve = precision_recall_curve(SCORES, LABELS)
    assert curve.iloc[0]["precision"] == 1.0


def test_best_f1_perfect_separation_is_one():
    curve = precision_recall_curve(SCORES, LABELS)
    assert best_f1(curve) == pytest.approx(1.0)


def test_best_f1_no_positives_is_zero():
    curve = precision_recall_curve(pd.Series([1.0, 2.0]), pd.Series([False, False]))
    assert best_f1(curve) == 0.0


def test_find_thresholds_perfect_separation():
    curve = precision_recall_curve(SCORES, LABELS)
    upper, lower = find_thresholds(curve, target_precision=0.99, target_recall=0.99)
    assert upper == 5.0
    assert lower == 5.0


def test_find_thresholds_falls_back_when_precision_target_unreachable():
    # positives and negatives overlap at every cutoff -- precision never reaches 0.99
    scores = pd.Series([5.0, 4.0, 3.0, 2.0])
    labels = pd.Series([True, False, True, False])
    curve = precision_recall_curve(scores, labels)
    upper, lower = find_thresholds(curve, target_precision=0.99, target_recall=0.99)
    assert upper == curve["score"].max()


def test_find_thresholds_raises_on_empty_curve():
    with pytest.raises(ValueError):
        find_thresholds(pd.DataFrame(columns=["score", "precision", "recall"]))


def test_find_thresholds_never_returns_lower_above_upper():
    # cleanly separated scores: true matches cluster very high (21+), false positives
    # only creep in below 8 -- recall hits 0.99 at a *higher* score than where precision
    # starts to erode, so the naive independent-target computation would cross.
    scores = pd.Series([30.0, 25.0, 21.0, 15.0, 10.0, 8.0, 5.0, 2.0])
    labels = pd.Series([True, True, True, True, True, True, False, False])
    curve = precision_recall_curve(scores, labels)
    upper, lower = find_thresholds(curve, target_precision=0.99, target_recall=0.99)
    assert lower <= upper
