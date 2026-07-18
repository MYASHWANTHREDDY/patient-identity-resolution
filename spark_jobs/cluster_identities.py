#!/usr/bin/env python
"""Submittable Dataproc Serverless entrypoint for the clustering step at scale (Phase 12
deliverable per PROJECT_CONSTITUTION.md; not part of the phase's exit criterion, which is
scoring-only -- see docs/design-decisions.md). Reads matching.pair_scores (written by
spark_jobs/score_pairs.py), keeps pairs at or above the auto-match threshold, and runs
mdm.backends.spark.build_clusters -- the same density/size guard logic as
mdm.clustering.build_clusters (P13: a thinly-connected cluster gets flagged, never silently
merged), computed via DataFrame joins instead of a single-process union-find so it scales
past what fits in one Python process. Writes matching.clusters back to BigQuery. GraphFrames
is deliberately not used here -- connected_components is plain DataFrame joins, so this job
needs no extra JARs beyond what Dataproc Serverless already ships.

Local dry run (no cloud cost):
    python spark_jobs/cluster_identities.py --local-parquet-dir data/dev/spark_parity

Real submission (Dataproc Serverless, billed):
    gcloud dataproc batches submit pyspark spark_jobs/cluster_identities.py \\
        --project=patient-dedup-mdm --region=us-central1 \\
        --batch=cluster-identities-$(date +%Y%m%d-%H%M%S) \\
        --py-files=dist/mdm.zip \\
        --deps-bucket=gs://patient-dedup-mdm-mdm-raw \\
        -- --project patient-dedup-mdm --bq-temp-bucket patient-dedup-mdm-mdm-raw \\
           --upper-threshold 7.8924 --max-cluster-size 6 --min-cluster-density 0.6

(thresholds/guards are the same config/matching.yml values mdm.pipeline.run_matching loads
locally via _load_thresholds() -- a real submission should read them from there rather than
hand-typing them, see the TODO in the Terraform/wiring step of this phase.)
"""

from __future__ import annotations

import argparse

from mdm.backends.spark import build_clusters


def run(spark, args) -> int:
    if args.local_parquet_dir:
        pair_scores = spark.read.parquet(f"{args.local_parquet_dir}/pair_scores.parquet")
    else:
        pair_scores = (
            spark.read.format("bigquery")
            .option("table", f"{args.project}.{args.pair_scores_table}")
            .load()
        )

    auto_match_edges = pair_scores.where(
        pair_scores["score"] >= args.upper_threshold
    ).select("record_key_a", "record_key_b")

    clusters = build_clusters(
        auto_match_edges,
        max_cluster_size=args.max_cluster_size,
        min_cluster_density=args.min_cluster_density,
        checkpoint_dir=args.checkpoint_dir,
    )

    if args.local_parquet_dir:
        # See the matching comment in spark_jobs/score_pairs.py: Spark's own Parquet writer
        # needs winutils.exe on Windows; toPandas() sidesteps it for this local-only path.
        clusters.toPandas().to_parquet(f"{args.local_parquet_dir}/clusters.parquet")
    else:
        (
            clusters.write.format("bigquery")
            .option("table", f"{args.project}.{args.output_table}")
            .option("temporaryGcsBucket", args.bq_temp_bucket)
            .mode("overwrite")
            .save()
        )

    count = clusters.count()
    print(f"wrote {count} clusters to {args.local_parquet_dir or args.output_table}")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=None, help="Required unless --local-parquet-dir.")
    parser.add_argument("--pair-scores-table", default="matching.pair_scores")
    parser.add_argument("--output-table", default="matching.clusters")
    parser.add_argument(
        "--bq-temp-bucket",
        default=None,
        help="GCS bucket the spark-bigquery-connector stages writes through. "
        "Required unless --local-parquet-dir.",
    )
    parser.add_argument(
        "--local-parquet-dir",
        default=None,
        help="Skip BigQuery entirely and read/write Parquet at this path instead "
        "(local dry run, no cloud cost).",
    )
    parser.add_argument(
        "--checkpoint-dir",
        default=None,
        help="GCS/HDFS path for truncating connected-components' iterative query plan. "
        "Recommended for real (5M-scale) runs; unnecessary for small local tests.",
    )
    parser.add_argument("--upper-threshold", type=float, required=True)
    parser.add_argument("--max-cluster-size", type=int, required=True)
    parser.add_argument("--min-cluster-density", type=float, required=True)
    args = parser.parse_args(argv)

    if not args.local_parquet_dir and not (args.project and args.bq_temp_bucket):
        parser.error("--project and --bq-temp-bucket are required unless --local-parquet-dir")

    from pyspark.sql import SparkSession

    builder = SparkSession.builder.appName("mdm-cluster-identities")
    if args.local_parquet_dir:
        # See the matching comment in spark_jobs/score_pairs.py: local-only, sidesteps
        # Windows pyspark launch-path quirks; Dataproc Serverless sets its own master and
        # driver networking.
        builder = (
            builder.master("local[*]")
            .config("spark.driver.host", "127.0.0.1")
            .config("spark.driver.bindAddress", "127.0.0.1")
        )
    spark = builder.getOrCreate()
    try:
        run(spark, args)
    finally:
        spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
