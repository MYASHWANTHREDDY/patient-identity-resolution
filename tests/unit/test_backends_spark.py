"""mdm.backends.spark's mapInPandas scoring and DataFrame-join connected components,
exercised against a real (local) SparkSession -- these functions only do anything
meaningful once actual Spark execution is involved, unlike the rest of mdm's pure-Python
logic. See docs/design-decisions.md, Phase 12, for the Windows-only quirks this module's
local dry-run tooling had to work around (none of which apply here: this test only reads
data Spark already holds in memory and collects via toPandas(), the one code path proven
not to need winutils.exe)."""

from datetime import date

import pytest

pyspark = pytest.importorskip("pyspark")

from pyspark.sql import SparkSession  # noqa: E402

from mdm.backends.spark import (  # noqa: E402
    build_clusters,
    connected_components,
    score_candidate_pairs,
)

FS_PARAMS = {
    "first_name": {"exact": {"weight": 5.0}, "different": {"weight": -3.0}},
    "last_name": {"exact": {"weight": 5.0}, "different": {"weight": -3.0}},
    "dob": {"exact": {"weight": 8.0}, "different": {"weight": -4.0}},
    "ssn": {"exact": {"weight": 20.0}, "missing": {"weight": 0.1}, "different": {"weight": -20.0}},
    "gender": {"exact": {"weight": 1.0}, "different": {"weight": -0.5}},
}


@pytest.fixture(scope="module")
def spark():
    session = (
        SparkSession.builder.appName("mdm-backends-spark-tests")
        .master("local[2]")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.ui.enabled", "false")
        # Default 200 shuffle partitions is tuned for real cluster-sized data, not a
        # handful of test rows -- connected_components' iterative joins otherwise spend
        # far more time scheduling near-empty partitions than doing actual work.
        .config("spark.sql.shuffle.partitions", "4")
        .getOrCreate()
    )
    yield session
    session.stop()


def test_score_candidate_pairs_matches_manual_fs_score(spark):
    patient_normalized = spark.createDataFrame(
        [
            ("A", "ROBERT", "SMITH", date(1980, 1, 1), "123456789", "M"),
            ("B", "ROBERT", "SMITH", date(1980, 1, 1), "123456789", "M"),
            ("C", "ZELMIRA", "QUINTANILLA", date(1990, 6, 6), "987654321", "F"),
        ],
        ["record_key", "first_name", "last_name", "dob", "ssn", "gender"],
    )
    candidate_pairs = spark.createDataFrame(
        [("A", "B"), ("A", "C")], ["record_key_a", "record_key_b"]
    )

    scored = score_candidate_pairs(candidate_pairs, patient_normalized, FS_PARAMS, {})
    scores = {(r.record_key_a, r.record_key_b): r.score for r in scored.collect()}

    # identical pair: every field exact
    assert scores[("A", "B")] == pytest.approx(5.0 + 5.0 + 8.0 + 20.0 + 1.0)
    # different pair scores lower than the identical one
    assert scores[("A", "C")] < scores[("A", "B")]


def test_connected_components_groups_by_edges(spark):
    # two triangles (A-B-C, D-E-F) plus one isolated pair (G-H) -> 3 components
    edges = spark.createDataFrame(
        [("A", "B"), ("B", "C"), ("A", "C"), ("D", "E"), ("E", "F"), ("G", "H")],
        ["record_key_a", "record_key_b"],
    )

    components = connected_components(edges).collect()
    groups: dict[str, set[str]] = {}
    for row in components:
        groups.setdefault(row.component, set()).add(row.record_key)

    group_sets = {frozenset(g) for g in groups.values()}
    assert group_sets == {
        frozenset({"A", "B", "C"}),
        frozenset({"D", "E", "F"}),
        frozenset({"G", "H"}),
    }


def test_build_clusters_flags_low_density_and_oversized_clusters(spark):
    # D-E-F is a 2-edge chain (not a full triangle): density 2/3
    edges = spark.createDataFrame(
        [("A", "B"), ("B", "C"), ("A", "C"), ("D", "E"), ("E", "F")],
        ["record_key_a", "record_key_b"],
    )

    lenient = {
        r.component: r
        for r in build_clusters(edges, max_cluster_size=10, min_cluster_density=0.5).collect()
    }
    chain_row = next(r for r in lenient.values() if r.scored_pairs == 2)
    assert chain_row.confidence == pytest.approx(2 / 3)
    assert not chain_row.flagged

    strict_density = build_clusters(edges, max_cluster_size=10, min_cluster_density=0.9).collect()
    chain_row_strict = next(r for r in strict_density if r.scored_pairs == 2)
    assert chain_row_strict.flagged

    strict_size = build_clusters(edges, max_cluster_size=2, min_cluster_density=0.0).collect()
    triangle_row = next(r for r in strict_size if r.scored_pairs == 3)
    assert triangle_row.flagged
