"""Evaluation harness -- not a test, a reporting tool. Every number in docs/results.md is
produced by running this script; results.md is generated, never hand-written (P3).

    python -m mdm.evaluate --tier dev
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from mdm.blocking_metrics import (
    all_pairs_from_block_keys,
    blocking_metrics_by_pass,
    pair_completeness,
)
from mdm.comparators import build_nickname_index
from mdm.config import REPO_ROOT, VALID_TIERS
from mdm.deterministic import deterministic_match_pairs
from mdm.scoring import compare_record_pair, score_fs, score_naive
from mdm.threshold_sweep import best_f1, find_thresholds, precision_recall_curve
from mdm.triage import decide

PairKey = tuple[str, str]

DEFAULT_NICKNAME_TABLE_PATH = REPO_ROOT / "config" / "nicknames.yml"
DEFAULT_FS_PARAMS_PATH = REPO_ROOT / "config" / "fs_params.yml"
DEFAULT_PR_CURVE_PATH = REPO_ROOT / "docs" / "img" / "pr_curve.png"


@dataclass
class PairMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


def _pair_metrics(predicted: set[PairKey], true: set[PairKey]) -> PairMetrics:
    tp = len(predicted & true)
    fp = len(predicted - true)
    fn = len(true - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return PairMetrics(tp, fp, fn, precision, recall, f1)


def true_pairs_with_noise_type(ground_truth: pd.DataFrame) -> dict[PairKey, str]:
    """Every unordered true-match pair, labeled with a best-effort noise type. Most
    identities have exactly one canonical ('exact') appearance plus corrupted ones, so a
    pair is usually (canonical, corrupted) -- labeled with the corrupted side's noise type.
    Two corrupted, non-canonical appearances of the same identity (only possible when an
    identity appears in all 3 vendors) are labeled 'multiple'."""
    pair_noise_type: dict[PairKey, str] = {}
    for _identity, group in ground_truth.groupby("true_identity_id"):
        records = group[["record_key", "noise_type"]].sort_values("record_key").to_numpy()
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                key_a, noise_a = records[i]
                key_b, noise_b = records[j]
                if noise_a == "exact" and noise_b == "exact":
                    label = "exact"
                elif noise_a == "exact":
                    label = noise_b
                elif noise_b == "exact":
                    label = noise_a
                else:
                    label = "multiple"
                pair_noise_type[(key_a, key_b)] = label
    return pair_noise_type


def recall_by_noise_type(
    predicted: set[PairKey], pair_noise_type: dict[PairKey, str]
) -> dict[str, dict]:
    true_pairs = set(pair_noise_type)
    totals = Counter(pair_noise_type[p] for p in true_pairs)
    found = Counter(pair_noise_type[p] for p in (predicted & true_pairs))
    return {
        noise_type: {
            "true_pairs": totals[noise_type],
            "recovered": found.get(noise_type, 0),
            "recall": found.get(noise_type, 0) / totals[noise_type] if totals[noise_type] else 0.0,
        }
        for noise_type in sorted(totals)
    }


def load_pair_noise_type(db_path: Path) -> dict[PairKey, str]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        ground_truth = con.execute(
            "SELECT record_key, true_identity_id, noise_type FROM ground_truth.ground_truth"
        ).df()
    finally:
        con.close()
    return true_pairs_with_noise_type(ground_truth)


def run_baseline_evaluation(tier: str, db_path: Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        patient_normalized = con.execute(
            "SELECT record_key, first_name, last_name, dob, ssn FROM conformance.patient_normalized"
        ).df()
    finally:
        con.close()
    pair_noise_type = load_pair_noise_type(db_path)

    started = time.monotonic()
    predicted_df = deterministic_match_pairs(patient_normalized)
    elapsed = time.monotonic() - started

    predicted_pairs: set[PairKey] = set(
        zip(predicted_df["record_key_a"], predicted_df["record_key_b"], strict=False)
    )
    true_pairs = set(pair_noise_type)

    metrics = _pair_metrics(predicted_pairs, true_pairs)

    return {
        "tier": tier,
        "num_records": len(patient_normalized),
        "num_true_pairs": len(true_pairs),
        "num_predicted_pairs": len(predicted_pairs),
        "metrics": asdict(metrics),
        "recall_by_noise_type": recall_by_noise_type(predicted_pairs, pair_noise_type),
        "elapsed_seconds": round(elapsed, 3),
    }


def run_blocking_evaluation(tier: str, db_path: Path, true_pairs: set[PairKey]) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        num_records = con.execute("SELECT count(*) FROM conformance.patient_normalized").fetchone()[
            0
        ]
        candidate_pairs_df = con.execute(
            "SELECT record_key_a, record_key_b, blocking_pass FROM matching.candidate_pairs"
        ).df()
        block_keys_df = con.execute(
            "SELECT record_key, blocking_pass, block_key FROM matching.block_keys"
        ).df()
    finally:
        con.close()

    by_pass = blocking_metrics_by_pass(candidate_pairs_df, true_pairs, num_records)

    uncapped_pairs = all_pairs_from_block_keys(block_keys_df)
    uncapped_pc = pair_completeness(uncapped_pairs, true_pairs)

    return {
        "tier": tier,
        "num_records": num_records,
        "by_pass": by_pass,
        "uncapped_pair_completeness": uncapped_pc,
        "capped_pair_completeness": by_pass["unioned"]["pair_completeness"],
        "pair_completeness_cost_of_cap": uncapped_pc - by_pass["unioned"]["pair_completeness"],
    }


def load_records_by_key(db_path: Path) -> dict[str, dict]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT record_key, first_name, last_name, dob, ssn, gender "
            "FROM conformance.patient_normalized"
        ).df()
    finally:
        con.close()
    return df.set_index("record_key").to_dict(orient="index")


def load_distinct_candidate_pairs(db_path: Path) -> pd.DataFrame:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            "SELECT DISTINCT record_key_a, record_key_b FROM matching.candidate_pairs"
        ).df()
    finally:
        con.close()
    return df


def run_scoring_evaluation(
    tier: str,
    db_path: Path,
    true_pairs: set[PairKey],
    fs_params: dict,
    nickname_index: dict[str, str],
    *,
    plot_path: Path | None = DEFAULT_PR_CURVE_PATH,
) -> dict:
    records_by_key = load_records_by_key(db_path)
    candidate_pairs = load_distinct_candidate_pairs(db_path)

    fs_scores = []
    naive_scores = []
    labels = []
    for record_key_a, record_key_b in zip(
        candidate_pairs["record_key_a"], candidate_pairs["record_key_b"], strict=False
    ):
        agreement = compare_record_pair(
            records_by_key[record_key_a],
            records_by_key[record_key_b],
            nickname_index=nickname_index,
        )
        fs_scores.append(score_fs(agreement, fs_params))
        naive_scores.append(score_naive(agreement))
        labels.append((record_key_a, record_key_b) in true_pairs)

    fs_scores = pd.Series(fs_scores)
    naive_scores = pd.Series(naive_scores)
    labels = pd.Series(labels)

    fs_curve = precision_recall_curve(fs_scores, labels)
    naive_curve = precision_recall_curve(naive_scores, labels)

    upper, lower = find_thresholds(fs_curve)

    decisions = [decide(s, upper=upper, lower=lower) for s in fs_scores]
    decision_counts = Counter(decisions)
    total = len(decisions)

    if plot_path is not None:
        plot_pr_curve(fs_curve, naive_curve, plot_path)

    return {
        "tier": tier,
        "num_candidate_pairs": total,
        "num_true_positives_available": int(labels.sum()),
        "fs_best_f1": best_f1(fs_curve),
        "naive_best_f1": best_f1(naive_curve),
        "upper_threshold": upper,
        "lower_threshold": lower,
        "decision_counts": dict(decision_counts),
        "review_queue_rate": decision_counts.get("review", 0) / total if total else 0.0,
        "plot_path": str(plot_path) if plot_path is not None else None,
    }


def plot_pr_curve(fs_curve: pd.DataFrame, naive_curve: pd.DataFrame, out_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.plot(fs_curve["recall"], fs_curve["precision"], label="Fellegi-Sunter", color="#1f77b4")
    ax.plot(naive_curve["recall"], naive_curve["precision"], label="Naive", color="#ff7f0e")
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title("Precision-Recall: Fellegi-Sunter vs. naive scorer")
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, 1.02)
    ax.legend(loc="lower left")
    ax.grid(alpha=0.3)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def render_results_md(
    result: dict, blocking_result: dict | None = None, scoring_result: dict | None = None
) -> str:
    m = result["metrics"]
    lines = [
        "# Results",
        "",
        "Generated by `python -m mdm.evaluate` -- never hand-edited (P3). Regenerate with:",
        "",
        f"    python -m mdm.evaluate --tier {result['tier']}",
        "",
        f"## Deterministic baseline ({result['tier']} tier)",
        "",
        f"- Records: {result['num_records']}",
        f"- True match pairs (ground truth): {result['num_true_pairs']}",
        f"- Predicted pairs: {result['num_predicted_pairs']}",
        f"- Runtime: {result['elapsed_seconds']}s",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Precision | {m['precision']:.4f} |",
        f"| Recall | {m['recall']:.4f} |",
        f"| F1 | {m['f1']:.4f} |",
        f"| True positives | {m['true_positives']} |",
        f"| False positives | {m['false_positives']} |",
        f"| False negatives | {m['false_negatives']} |",
        "",
        "### Recall by noise type",
        "",
        "| Noise type | True pairs | Recovered | Recall |",
        "|---|---|---|---|",
    ]
    for noise_type, stats in result["recall_by_noise_type"].items():
        lines.append(
            f"| {noise_type} | {stats['true_pairs']} | {stats['recovered']} | "
            f"{stats['recall']:.4f} |"
        )
    lines.append("")

    if blocking_result is not None:
        lines += [
            f"## Blocking ({blocking_result['tier']} tier)",
            "",
            f"- Records: {blocking_result['num_records']}",
            "",
            "| Pass | Candidate pairs | Reduction ratio | Pair completeness |",
            "|---|---|---|---|",
        ]
        for pass_name, stats in blocking_result["by_pass"].items():
            lines.append(
                f"| {pass_name} | {stats['candidate_pairs']} | "
                f"{stats['reduction_ratio']:.6f} | {stats['pair_completeness']:.4f} |"
            )
        lines += [
            "",
            "### Cost of the block size cap",
            "",
            f"- Pair completeness with cap: {blocking_result['capped_pair_completeness']:.4f}",
            f"- Pair completeness without cap: {blocking_result['uncapped_pair_completeness']:.4f}",
            f"- Cost of the cap: {blocking_result['pair_completeness_cost_of_cap']:.4f}",
            "",
        ]

    if scoring_result is not None:
        counts = scoring_result["decision_counts"]
        lines += [
            f"## Scoring: Fellegi-Sunter vs. naive ({scoring_result['tier']} tier)",
            "",
            f"- Candidate pairs scored: {scoring_result['num_candidate_pairs']}",
            f"- Of which true matches: {scoring_result['num_true_positives_available']}",
            "",
            "| Scorer | Best F1 (across all thresholds) |",
            "|---|---|",
            f"| Fellegi-Sunter | {scoring_result['fs_best_f1']:.4f} |",
            f"| Naive (hand-tuned weights) | {scoring_result['naive_best_f1']:.4f} |",
            "",
            "### Triage thresholds",
            "",
            f"- Upper (auto-match, precision >= 0.99): {scoring_result['upper_threshold']:.4f}",
            f"- Lower (review floor, recall >= 0.99): {scoring_result['lower_threshold']:.4f}",
            "",
            "| Decision | Count | Share |",
            "|---|---|---|",
        ]
        total = scoring_result["num_candidate_pairs"]
        for decision in ("auto_match", "review", "non_match"):
            count = counts.get(decision, 0)
            share = count / total if total else 0.0
            lines.append(f"| {decision} | {count} | {share:.4f} |")
        lines += [
            "",
            f"- Review queue rate: {scoring_result['review_queue_rate']:.4f}",
            "",
        ]
        if scoring_result.get("plot_path"):
            plot_path = Path(scoring_result["plot_path"])
            lines += [f"![PR curve](img/{plot_path.name})", ""]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, default="dev")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "results.md")
    parser.add_argument(
        "--skip-blocking",
        action="store_true",
        help="Skip blocking metrics (requires dbt build to have populated the matching schema).",
    )
    parser.add_argument(
        "--skip-scoring",
        action="store_true",
        help="Skip Fellegi-Sunter/naive scoring (requires config/fs_params.yml and dbt build).",
    )
    parser.add_argument("--fs-params", type=Path, default=DEFAULT_FS_PARAMS_PATH)
    parser.add_argument("--nicknames", type=Path, default=DEFAULT_NICKNAME_TABLE_PATH)
    args = parser.parse_args(argv)

    db_path = args.db_path or (REPO_ROOT / "data" / args.tier / "mdm.duckdb")
    result = run_baseline_evaluation(args.tier, db_path)

    true_pairs = None
    blocking_result = None
    if not args.skip_blocking:
        true_pairs = set(load_pair_noise_type(db_path))
        blocking_result = run_blocking_evaluation(args.tier, db_path, true_pairs)

    scoring_result = None
    if not args.skip_scoring:
        if true_pairs is None:
            true_pairs = set(load_pair_noise_type(db_path))
        with args.fs_params.open("r", encoding="utf-8") as f:
            fs_params = yaml.safe_load(f)
        with args.nicknames.open("r", encoding="utf-8") as f:
            nickname_table = yaml.safe_load(f) or {}
        nickname_index = build_nickname_index(nickname_table)
        scoring_result = run_scoring_evaluation(
            args.tier, db_path, true_pairs, fs_params, nickname_index
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        render_results_md(result, blocking_result, scoring_result), encoding="utf-8"
    )

    m = result["metrics"]
    print(
        f"tier={args.tier} precision={m['precision']:.4f} recall={m['recall']:.4f} "
        f"f1={m['f1']:.4f} -> {args.out}"
    )
    if blocking_result:
        pc = blocking_result["by_pass"]["unioned"]["pair_completeness"]
        rr = blocking_result["by_pass"]["unioned"]["reduction_ratio"]
        print(f"blocking: reduction_ratio={rr:.6f} pair_completeness={pc:.4f}")
    if scoring_result:
        print(
            f"scoring: fs_best_f1={scoring_result['fs_best_f1']:.4f} "
            f"naive_best_f1={scoring_result['naive_best_f1']:.4f} "
            f"upper={scoring_result['upper_threshold']:.4f} "
            f"lower={scoring_result['lower_threshold']:.4f} "
            f"review_rate={scoring_result['review_queue_rate']:.4f}"
        )
    return result


if __name__ == "__main__":
    main()
