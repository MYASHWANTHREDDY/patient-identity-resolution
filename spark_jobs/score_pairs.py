#!/usr/bin/env python
"""Submittable Dataproc Serverless entrypoint (Phase 12, PROJECT_CONSTITUTION.md #8):
reads matching.candidate_pairs and conformance.patient_normalized from BigQuery, scores
every pair via mdm.backends.spark.score_candidate_pairs -- the identical comparators/
scoring functions mdm.pipeline.run_matching runs locally through a plain Python loop, here
run via mapInPandas -- and writes matching.pair_scores back to BigQuery. The batch reads,
scores, writes, and exits; nothing is left running afterward (P10).

Local dry run against DuckDB-exported Parquet (no cloud cost, exercises the same code path
short of the BigQuery read/write):
    python spark_jobs/score_pairs.py --local-parquet-dir data/dev/spark_parity

Real submission (Dataproc Serverless, billed):
    gcloud dataproc batches submit pyspark spark_jobs/score_pairs.py \\
        --project=patient-dedup-mdm --region=us-central1 \\
        --batch=score-pairs-$(date +%Y%m%d-%H%M%S) \\
        --py-files=dist/mdm.zip \\
        --files=config/fs_params.yml,config/nicknames.yml \\
        --deps-bucket=gs://patient-dedup-mdm-mdm-raw \\
        -- --project patient-dedup-mdm --bq-temp-bucket patient-dedup-mdm-mdm-raw

fs_params.yml/nicknames.yml ship via --files and are resolved through SparkFiles on the
driver -- the only place that reads them; make_score_partition_fn turns their contents into
plain picklable dicts before Spark ships the closure out to executors, so executors never
need the files themselves.
"""

from __future__ import annotations

import argparse
import os

import yaml

from mdm.backends.spark import score_candidate_pairs


def _load_yaml(filename: str) -> dict:
    try:
        from pyspark import SparkFiles

        local_path = SparkFiles.get(filename)
    except ImportError:
        local_path = filename
    if not os.path.exists(local_path):
        local_path = filename  # local dry run: file lives at the given path directly
    with open(local_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(spark, args) -> int:
    fs_params = _load_yaml(args.fs_params_file)
    nickname_table = _load_yaml(args.nicknames_file) or {}

    if args.local_parquet_dir:
        candidate_pairs = spark.read.parquet(f"{args.local_parquet_dir}/candidate_pairs.parquet")
        patient_normalized = spark.read.parquet(
            f"{args.local_parquet_dir}/patient_normalized.parquet"
        )
    else:
        candidate_pairs = (
            spark.read.format("bigquery")
            .option("table", f"{args.project}.{args.candidate_pairs_table}")
            .load()
        )
        patient_normalized = (
            spark.read.format("bigquery")
            .option("table", f"{args.project}.{args.patient_normalized_table}")
            .load()
        )

    candidate_pairs = candidate_pairs.select("record_key_a", "record_key_b").distinct()
    patient_normalized = patient_normalized.select(
        "record_key", "first_name", "last_name", "dob", "ssn", "gender"
    )

    scored = score_candidate_pairs(candidate_pairs, patient_normalized, fs_params, nickname_table)

    if args.local_parquet_dir:
        # Not spark_df.write.parquet(): Spark's Parquet writer goes through Hadoop's local
        # filesystem output committer, which calls RawLocalFileSystem.setPermission() and
        # hard-fails without winutils.exe on Windows (unlike *reading* Parquet, which never
        # touches that code path -- see docs/design-decisions.md, Phase 12). Irrelevant to
        # the real Dataproc job, which writes to BigQuery, never local disk; collecting via
        # toPandas() here keeps the local dry run exercising the real, shared
        # score_candidate_pairs logic without needing a Windows-only Hadoop binary.
        scored.toPandas().to_parquet(f"{args.local_parquet_dir}/pair_scores.parquet")
    else:
        (
            scored.write.format("bigquery")
            .option("table", f"{args.project}.{args.output_table}")
            .option("temporaryGcsBucket", args.bq_temp_bucket)
            .mode("overwrite")
            .save()
        )

    count = scored.count()
    print(f"wrote {count} scored pairs to " f"{args.local_parquet_dir or args.output_table}")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=None, help="Required unless --local-parquet-dir.")
    parser.add_argument("--candidate-pairs-table", default="matching.candidate_pairs")
    parser.add_argument("--patient-normalized-table", default="conformance.patient_normalized")
    parser.add_argument("--output-table", default="matching.pair_scores")
    parser.add_argument("--fs-params-file", default="fs_params.yml")
    parser.add_argument("--nicknames-file", default="nicknames.yml")
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
    args = parser.parse_args(argv)

    if not args.local_parquet_dir and not (args.project and args.bq_temp_bucket):
        parser.error("--project and --bq-temp-bucket are required unless --local-parquet-dir")

    from pyspark.sql import SparkSession

    builder = SparkSession.builder.appName("mdm-score-pairs")
    if args.local_parquet_dir:
        # Local dry run only -- Dataproc Serverless sets its own master and driver
        # networking when it submits the batch, so hardcoding either here would break the
        # real submission. Both settings sidestep Windows-only pyspark launch-path quirks
        # (see docs/design-decisions.md, Phase 12): without an explicit master, the JVM
        # bootstrap hard-fails on missing HADOOP_HOME/winutils.exe instead of just warning;
        # without an explicit driver host/bind address, the Python worker can time out
        # connecting back to the driver.
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
