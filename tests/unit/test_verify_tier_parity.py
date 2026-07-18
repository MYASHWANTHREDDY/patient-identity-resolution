import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from verify_tier_parity import _normalize_for_comparison  # noqa: E402


def test_normalize_for_comparison_sorts_and_stringifies():
    df = pd.DataFrame({"a": [2, 1], "b": ["y", "x"]})
    normalized = _normalize_for_comparison(df, sort_columns=["a"])
    assert list(normalized["a"]) == ["1", "2"]
    assert list(normalized["b"]) == ["x", "y"]


def test_normalize_for_comparison_makes_row_order_irrelevant():
    df_a = pd.DataFrame({"key": [3, 1, 2], "value": ["c", "a", "b"]})
    df_b = pd.DataFrame({"key": [1, 2, 3], "value": ["a", "b", "c"]})

    normalized_a = _normalize_for_comparison(df_a, sort_columns=["key"])
    normalized_b = _normalize_for_comparison(df_b, sort_columns=["key"])

    assert normalized_a.equals(normalized_b)


def test_normalize_for_comparison_detects_real_differences():
    df_a = pd.DataFrame({"key": [1, 2], "value": ["a", "b"]})
    df_b = pd.DataFrame({"key": [1, 2], "value": ["a", "DIFFERENT"]})

    normalized_a = _normalize_for_comparison(df_a, sort_columns=["key"])
    normalized_b = _normalize_for_comparison(df_b, sort_columns=["key"])

    assert not normalized_a.equals(normalized_b)
