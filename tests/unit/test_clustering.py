from mdm.clustering import build_clusters, finalize_cluster_membership


def test_build_clusters_simple_pair():
    clusters = build_clusters([("A", "B")], max_cluster_size=6, min_cluster_density=0.6)
    assert len(clusters) == 1
    assert clusters[0].members == ("A", "B")
    assert clusters[0].confidence == 1.0
    assert not clusters[0].flagged


def test_build_clusters_disjoint_pairs_stay_separate():
    clusters = build_clusters(
        [("A", "B"), ("C", "D")], max_cluster_size=6, min_cluster_density=0.6
    )
    assert len(clusters) == 2
    member_sets = {c.members for c in clusters}
    assert member_sets == {("A", "B"), ("C", "D")}


def test_chained_case_transitive_closure_is_flagged_by_density():
    # A~B and B~C but A is NEVER directly compared to C (the classic transitive-closure
    # trap, PROJECT_CONSTITUTION.md #13.2). Connected components still merges all three via
    # B -- the density guard is what's supposed to catch it: 2 edges out of 3 possible.
    edges = [("A", "B"), ("B", "C")]
    clusters = build_clusters(edges, max_cluster_size=6, min_cluster_density=0.7)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.members == ("A", "B", "C")
    assert cluster.scored_pairs == 2
    assert cluster.possible_pairs == 3
    assert cluster.confidence < 0.7
    assert cluster.flagged
    assert "density" in cluster.flag_reasons


def test_chained_case_not_flagged_when_density_threshold_is_lenient():
    # same chain, but a threshold low enough to tolerate it -- confirms the guard is
    # threshold-driven (config, not hardcoded) rather than a fixed rule about chains.
    edges = [("A", "B"), ("B", "C")]
    clusters = build_clusters(edges, max_cluster_size=6, min_cluster_density=0.5)
    assert not clusters[0].flagged


def test_size_guard_flags_oversized_cluster():
    edges = [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E"), ("A", "C"), ("A", "D")]
    clusters = build_clusters(edges, max_cluster_size=3, min_cluster_density=0.0)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert len(cluster.members) == 5
    assert cluster.flagged
    assert "size" in cluster.flag_reasons


def test_fully_connected_cluster_has_confidence_one():
    edges = [("A", "B"), ("B", "C"), ("A", "C")]
    clusters = build_clusters(edges, max_cluster_size=6, min_cluster_density=0.99)
    assert clusters[0].confidence == 1.0
    assert not clusters[0].flagged


def test_finalize_cluster_membership_keeps_clean_clusters_merged():
    clusters = build_clusters([("A", "B")], max_cluster_size=6, min_cluster_density=0.6)
    membership = finalize_cluster_membership(clusters)
    assert membership == {"A": ("A", "B"), "B": ("A", "B")}


def test_finalize_cluster_membership_never_merges_flagged_clusters():
    # P13: when uncertain, do not merge -- a flagged cluster's members each become their
    # own singleton instead of being merged into one identity.
    edges = [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E"), ("A", "C"), ("A", "D")]
    clusters = build_clusters(edges, max_cluster_size=3, min_cluster_density=0.0)
    membership = finalize_cluster_membership(clusters)
    for member in ("A", "B", "C", "D", "E"):
        assert membership[member] == (member,)
