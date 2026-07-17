"""Threshold sweep: precision-recall curve and empirical threshold selection
(PROJECT_CONSTITUTION.md #11.5). `upper` = lowest score where precision >= 0.99; `lower` =
highest score where recall >= 0.99. Set empirically, not by taste.
"""

from __future__ import annotations

import pandas as pd

DEFAULT_TARGET_PRECISION = 0.99
DEFAULT_TARGET_RECALL = 0.99


def precision_recall_curve(scores: pd.Series, labels: pd.Series) -> pd.DataFrame:
    """One row per distinct score, sorted descending, with precision/recall of the
    "auto-match everything >= this score" rule at that cutoff."""
    df = pd.DataFrame({"score": scores, "label": labels.astype(bool)}).sort_values(
        "score", ascending=False
    )
    total_positives = int(df["label"].sum())

    df["tp_cum"] = df["label"].cumsum()
    df["fp_cum"] = (~df["label"]).cumsum()

    # keep the last row per distinct score so ties are fully absorbed into one cutoff
    curve = df.groupby("score", as_index=False).last()
    curve["precision"] = curve["tp_cum"] / (curve["tp_cum"] + curve["fp_cum"])
    curve["recall"] = curve["tp_cum"] / total_positives if total_positives else 0.0
    curve = curve.sort_values("score", ascending=False).reset_index(drop=True)
    return curve[["score", "precision", "recall"]]


def best_f1(curve: pd.DataFrame) -> float:
    p, r = curve["precision"], curve["recall"]
    f1 = (2 * p * r / (p + r)).where((p + r) > 0, 0.0)
    return float(f1.max()) if len(f1) else 0.0


def find_thresholds(
    curve: pd.DataFrame,
    *,
    target_precision: float = DEFAULT_TARGET_PRECISION,
    target_recall: float = DEFAULT_TARGET_RECALL,
) -> tuple[float, float]:
    """upper = lowest score where precision >= target; lower = highest score where
    recall >= target. Falls back to the most conservative/permissive score in the curve
    if the target is unreachable.

    The two targets are found independently, so on a very cleanly-separated score
    distribution (few false positives anywhere, nearly all true matches scoring high)
    they can legitimately cross: recall can already hit its target at a *higher* score
    than where precision starts to erode, because both hold over a wide overlapping
    range. Left uncorrected, `lower > upper` silently empties the review band --
    `triage.decide()` checks `score >= upper` first, so nothing ever reaches the `lower`
    check. Clamping `lower` to `upper` makes the review band collapse to zero-width
    exactly at `upper` instead of leaving `lower` as dead code with a misleading value.
    """
    if curve.empty:
        raise ValueError("Cannot find thresholds from an empty curve")

    meets_precision = curve[curve["precision"] >= target_precision]
    upper = meets_precision["score"].min() if not meets_precision.empty else curve["score"].max()

    meets_recall = curve[curve["recall"] >= target_recall]
    lower = meets_recall["score"].max() if not meets_recall.empty else curve["score"].min()
    lower = min(lower, upper)

    return float(upper), float(lower)
