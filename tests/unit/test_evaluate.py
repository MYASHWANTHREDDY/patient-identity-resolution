import pandas as pd

from mdm.evaluate import _pair_metrics, recall_by_noise_type, true_pairs_with_noise_type


def test_true_pairs_with_noise_type_labels_canonical_vs_corrupted():
    ground_truth = pd.DataFrame(
        [
            {"record_key": "A:1", "true_identity_id": "ID1", "noise_type": "exact"},
            {"record_key": "B:1", "true_identity_id": "ID1", "noise_type": "typo_name"},
        ]
    )
    pairs = true_pairs_with_noise_type(ground_truth)
    assert pairs == {("A:1", "B:1"): "typo_name"}


def test_true_pairs_with_noise_type_labels_both_exact():
    ground_truth = pd.DataFrame(
        [
            {"record_key": "A:1", "true_identity_id": "ID1", "noise_type": "exact"},
            {"record_key": "B:1", "true_identity_id": "ID1", "noise_type": "exact"},
        ]
    )
    pairs = true_pairs_with_noise_type(ground_truth)
    assert pairs == {("A:1", "B:1"): "exact"}


def test_true_pairs_with_noise_type_labels_multiple_when_both_corrupted():
    ground_truth = pd.DataFrame(
        [
            {"record_key": "A:1", "true_identity_id": "ID1", "noise_type": "typo_name"},
            {"record_key": "B:1", "true_identity_id": "ID1", "noise_type": "dob_error"},
        ]
    )
    pairs = true_pairs_with_noise_type(ground_truth)
    assert pairs == {("A:1", "B:1"): "multiple"}


def test_true_pairs_covers_all_combinations_within_an_identity():
    ground_truth = pd.DataFrame(
        [
            {"record_key": "A:1", "true_identity_id": "ID1", "noise_type": "exact"},
            {"record_key": "B:1", "true_identity_id": "ID1", "noise_type": "typo_name"},
            {"record_key": "C:1", "true_identity_id": "ID1", "noise_type": "nickname"},
            {"record_key": "A:2", "true_identity_id": "ID2", "noise_type": "exact"},
        ]
    )
    pairs = true_pairs_with_noise_type(ground_truth)
    assert set(pairs) == {("A:1", "B:1"), ("A:1", "C:1"), ("B:1", "C:1")}


def test_pair_metrics_perfect_prediction():
    true = {("A", "B"), ("C", "D")}
    metrics = _pair_metrics(true, true)
    assert (metrics.precision, metrics.recall, metrics.f1) == (1.0, 1.0, 1.0)


def test_pair_metrics_no_prediction():
    metrics = _pair_metrics(set(), {("A", "B")})
    assert (metrics.precision, metrics.recall, metrics.f1) == (0.0, 0.0, 0.0)


def test_pair_metrics_mixed():
    predicted = {("A", "B"), ("X", "Y")}
    true = {("A", "B"), ("C", "D")}
    metrics = _pair_metrics(predicted, true)
    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5


def test_recall_by_noise_type():
    pair_noise_type = {
        ("A", "B"): "exact",
        ("C", "D"): "exact",
        ("E", "F"): "typo_name",
    }
    predicted = {("A", "B"), ("E", "F")}
    result = recall_by_noise_type(predicted, pair_noise_type)
    assert result["exact"] == {"true_pairs": 2, "recovered": 1, "recall": 0.5}
    assert result["typo_name"] == {"true_pairs": 1, "recovered": 1, "recall": 1.0}
