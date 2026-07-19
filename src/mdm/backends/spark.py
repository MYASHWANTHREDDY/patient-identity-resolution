"""Spark backend (PROJECT_CONSTITUTION.md #8 -- "one codebase, two backends"). The pure
comparator/scoring functions in mdm.comparators / mdm.scoring are unchanged; only how
they're invoked differs. Locally (mdm.pipeline) they're called via a Python loop over a
pandas DataFrame; here, the identical functions run inside `mapInPandas`, once per Spark
partition, so the same logic scales out across a cluster without being rewritten.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pandas as pd

from mdm.comparators import build_nickname_index
from mdm.scoring import FIELDS as SCORING_FIELDS
from mdm.scoring import compare_record_pair, score_fs

SCORE_PAIRS_OUTPUT_COLUMNS = ("record_key_a", "record_key_b", "score")


def make_score_partition_fn(
    fs_params: dict, nickname_table: dict[str, list[str]]
) -> Callable[[Iterator[pd.DataFrame]], Iterator[pd.DataFrame]]:
    """Returns a mapInPandas-compatible function bound to fs_params/nickname_table.
    Spark ships this closure to every executor, so both arguments must be plain,
    picklable dicts -- exactly what yaml.safe_load already hands back from
    config/fs_params.yml and config/nicknames.yml."""
    nickname_index = build_nickname_index(nickname_table)

    def score_partition(iterator: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
        for batch in iterator:
            scores = []
            for row in batch.itertuples(index=False):
                record_a = {field: getattr(row, f"a_{field}") for field in SCORING_FIELDS}
                record_b = {field: getattr(row, f"b_{field}") for field in SCORING_FIELDS}
                agreement = compare_record_pair(
                    record_a, record_b, nickname_index=nickname_index
                )
                scores.append(score_fs(agreement, fs_params))
            yield pd.DataFrame(
                {
                    "record_key_a": batch["record_key_a"].to_numpy(),
                    "record_key_b": batch["record_key_b"].to_numpy(),
                    "score": scores,
                }
            )

    return score_partition


def join_pairs_with_records(candidate_pairs, patient_normalized):
    """Both Spark DataFrames: candidate_pairs has record_key_a/record_key_b;
    patient_normalized has record_key + SCORING_FIELDS. Returns one row per pair with
    a_/b_-prefixed copies of every scoring field -- the Spark-side equivalent of
    mdm.pipeline's records_by_key dict lookup, done via a join instead.

    Broadcasts patient_normalized rather than letting Spark shuffle-join it: at any tier
    this project targets, patient_normalized is small (a few hundred MB even at 5M records
    -- a handful of short string/date fields per row) while candidate_pairs is the thing
    that's actually large (hundreds of millions of rows at the scale tier). A shuffle join
    would shuffle both sides of *both* joins below; broadcasting the small side sends it to
    every executor once and then joins locally, no shuffle at all -- the difference between
    a few dollars and a potentially very large Dataproc bill at scale (see
    docs/design-decisions.md, Phase 14)."""
    from pyspark.sql.functions import broadcast

    side_a = broadcast(patient_normalized)
    side_b = broadcast(patient_normalized)
    for field in SCORING_FIELDS:
        side_a = side_a.withColumnRenamed(field, f"a_{field}")
        side_b = side_b.withColumnRenamed(field, f"b_{field}")
    side_a = side_a.withColumnRenamed("record_key", "record_key_a")
    side_b = side_b.withColumnRenamed("record_key", "record_key_b")

    return candidate_pairs.join(side_a, on="record_key_a").join(side_b, on="record_key_b")


def score_candidate_pairs(candidate_pairs, patient_normalized, fs_params, nickname_table):
    """The Spark-native equivalent of the scoring loop in mdm.pipeline.run_matching --
    same comparators, same score_fs, applied via mapInPandas instead of a Python loop."""
    from pyspark.sql.types import DoubleType, StringType, StructField, StructType

    output_schema = StructType(
        [
            StructField("record_key_a", StringType(), nullable=False),
            StructField("record_key_b", StringType(), nullable=False),
            StructField("score", DoubleType(), nullable=False),
        ]
    )

    joined = join_pairs_with_records(candidate_pairs, patient_normalized)
    score_partition = make_score_partition_fn(fs_params, nickname_table)
    return joined.mapInPandas(score_partition, schema=output_schema)


def _symmetrize_edges(edges):
    """edges: Spark DataFrame[record_key_a, record_key_b]. Returns DataFrame[src, dst] with
    both directions present, deduplicated -- connected components needs to walk edges in
    either direction, unlike the scoring join above which treats a/b as fixed sides."""
    forward = edges.selectExpr("record_key_a as src", "record_key_b as dst")
    backward = edges.selectExpr("record_key_b as src", "record_key_a as dst")
    return forward.union(backward).distinct()


def connected_components(edges, *, max_iterations: int = 50, checkpoint_dir: str | None = None):
    """mdm.clustering's union-find doesn't parallelize (it's one mutable structure walked
    serially); at scale, connected components instead use iterative min-label propagation --
    every node adopts the smallest label among itself and its neighbors, repeated until no
    label changes. `edges` must be distinct auto-match pairs (record_key_a, record_key_b),
    the same input build_clusters (mdm.clustering) takes locally. Returns Spark
    DataFrame[record_key, component] covering only nodes touched by at least one edge --
    the caller adds untouched records as singleton components, exactly as
    mdm.pipeline.run_matching does for `membership.setdefault(record_key, (record_key,))`.

    `checkpoint_dir` truncates Spark's query plan every few iterations (via DataFrame.
    checkpoint()) -- without it, a real 5M-record graph needing many iterations builds a
    query plan that grows with iteration count and can blow the driver's stack/memory on
    plan analysis alone. Pass a GCS or HDFS path for real runs; None is fine for small local
    tests where a handful of iterations never gets deep enough to matter."""
    from pyspark.sql import functions as F

    sym = _symmetrize_edges(edges).cache()
    labels = sym.select(F.col("src").alias("record_key")).distinct()
    labels = labels.withColumn("component", F.col("record_key"))

    spark = edges.sparkSession
    if checkpoint_dir:
        spark.sparkContext.setCheckpointDir(checkpoint_dir)

    for iteration in range(max_iterations):
        neighbor_labels = sym.join(labels, sym.src == labels.record_key).select(
            sym.dst.alias("record_key"), labels.component
        )
        new_labels = (
            neighbor_labels.union(labels)
            .groupBy("record_key")
            .agg(F.min("component").alias("component"))
        )
        if checkpoint_dir and iteration % 5 == 4:
            new_labels = new_labels.checkpoint(eager=True)

        changed = (
            new_labels.withColumnRenamed("component", "new_component")
            .join(labels.withColumnRenamed("component", "old_component"), "record_key")
            .where(F.col("new_component") != F.col("old_component"))
            .limit(1)
            .count()
        )
        labels = new_labels
        if changed == 0:
            break

    return labels


def build_clusters(edges, *, max_cluster_size: int, min_cluster_density: float, **cc_kwargs):
    """The Spark-native equivalent of mdm.clustering.build_clusters -- same density-guard
    logic (P13: a cluster held together by a thin chain of edges gets flagged, never
    silently merged), computed via joins/aggregates instead of Python dict bookkeeping.
    Returns one row per component: DataFrame[component, members (array<string>), size,
    scored_pairs, possible_pairs, confidence, flagged]."""
    from pyspark.sql import functions as F

    components = connected_components(edges, **cc_kwargs)

    edges_with_component = edges.join(
        components.withColumnRenamed("record_key", "record_key_a"), on="record_key_a"
    )
    edge_counts = edges_with_component.groupBy("component").agg(
        F.count("*").alias("scored_pairs")
    )

    member_stats = components.groupBy("component").agg(
        F.collect_list("record_key").alias("members"),
        F.count("*").alias("size"),
    )

    clusters = (
        member_stats.join(edge_counts, "component")
        .withColumn("possible_pairs", (F.col("size") * (F.col("size") - 1) / F.lit(2)).cast("long"))
        .withColumn(
            "confidence",
            F.when(F.col("possible_pairs") > 0, F.col("scored_pairs") / F.col("possible_pairs"))
            .otherwise(F.lit(1.0)),
        )
        .withColumn(
            "flagged",
            (F.col("size") > F.lit(max_cluster_size))
            | (F.col("confidence") < F.lit(min_cluster_density)),
        )
    )
    return clusters
