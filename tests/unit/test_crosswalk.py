from mdm.crosswalk import CREATE, MERGE, SPLIT, CrosswalkEntry, resolve_crosswalk


def test_new_cluster_mints_an_id():
    clusters = {"A:1": ("A:1", "B:1"), "B:1": ("A:1", "B:1")}
    new_crosswalk, events = resolve_crosswalk({}, clusters, run_id="run1")

    assert new_crosswalk["A:1"].patient_global_id == new_crosswalk["B:1"].patient_global_id
    assert len(events) == 1
    assert events[0].event_type == CREATE
    assert events[0].surviving_id == new_crosswalk["A:1"].patient_global_id


def test_rerun_with_identical_clusters_reuses_the_same_id():
    clusters = {"A:1": ("A:1", "B:1"), "B:1": ("A:1", "B:1")}
    run1_crosswalk, _ = resolve_crosswalk({}, clusters, run_id="run1")

    run2_crosswalk, run2_events = resolve_crosswalk(run1_crosswalk, clusters, run_id="run2")

    assert run2_crosswalk["A:1"].patient_global_id == run1_crosswalk["A:1"].patient_global_id
    assert run2_crosswalk["B:1"].patient_global_id == run1_crosswalk["B:1"].patient_global_id
    assert run2_events == []  # pure reuse -- no create/merge/split


def test_idempotency_two_runs_from_scratch_produce_identical_ids():
    # not "reuse the same crosswalk twice" but "run the whole resolution twice starting
    # from nothing" -- P7's actual bar. Deterministic ID minting order makes this hold.
    clusters = {
        "A:1": ("A:1", "B:1"),
        "B:1": ("A:1", "B:1"),
        "C:1": ("C:1",),
    }
    crosswalk_run1, _ = resolve_crosswalk({}, clusters, run_id="run1")
    crosswalk_run2, _ = resolve_crosswalk({}, clusters, run_id="run1")

    assert {k: v.patient_global_id for k, v in crosswalk_run1.items()} == {
        k: v.patient_global_id for k, v in crosswalk_run2.items()
    }


def test_new_member_joins_an_existing_cluster_reuses_that_id():
    existing = {
        "A:1": CrosswalkEntry("A:1", "PGID000000000001", "run1", "run1"),
    }
    clusters = {"A:1": ("A:1", "B:1"), "B:1": ("A:1", "B:1")}
    new_crosswalk, events = resolve_crosswalk(existing, clusters, run_id="run2")

    assert new_crosswalk["A:1"].patient_global_id == "PGID000000000001"
    assert new_crosswalk["B:1"].patient_global_id == "PGID000000000001"
    assert new_crosswalk["B:1"].first_seen_run == "run2"
    assert new_crosswalk["A:1"].first_seen_run == "run1"  # unchanged
    assert events == []


def test_merge_oldest_id_survives():
    existing = {
        "A:1": CrosswalkEntry("A:1", "PGID000000000005", "run3", "run3"),
        "B:1": CrosswalkEntry("B:1", "PGID000000000001", "run1", "run1"),
    }
    clusters = {"A:1": ("A:1", "B:1"), "B:1": ("A:1", "B:1")}
    new_crosswalk, events = resolve_crosswalk(existing, clusters, run_id="run4")

    assert new_crosswalk["A:1"].patient_global_id == "PGID000000000001"
    assert new_crosswalk["B:1"].patient_global_id == "PGID000000000001"
    assert len(events) == 1
    assert events[0].event_type == MERGE
    assert events[0].surviving_id == "PGID000000000001"
    assert events[0].retired_id == "PGID000000000005"


def test_merge_of_three_existing_ids_retires_all_but_the_oldest():
    existing = {
        "A:1": CrosswalkEntry("A:1", "PGID000000000003", "run3", "run3"),
        "B:1": CrosswalkEntry("B:1", "PGID000000000001", "run1", "run1"),
        "C:1": CrosswalkEntry("C:1", "PGID000000000002", "run2", "run2"),
    }
    clusters = {k: ("A:1", "B:1", "C:1") for k in ("A:1", "B:1", "C:1")}
    new_crosswalk, events = resolve_crosswalk(existing, clusters, run_id="run4")

    assert all(e.patient_global_id == "PGID000000000001" for e in new_crosswalk.values())
    retired = {e.retired_id for e in events if e.event_type == MERGE}
    assert retired == {"PGID000000000002", "PGID000000000003"}


def test_split_when_a_former_clustermate_moves_away():
    existing = {
        "A:1": CrosswalkEntry("A:1", "PGID000000000001", "run1", "run1"),
        "B:1": CrosswalkEntry("B:1", "PGID000000000001", "run1", "run1"),
    }
    # B:1 no longer clusters with A:1 this run -- it's now entirely on its own. An id can
    # belong to only one cluster per run: clusters process in deterministic order (by
    # smallest member record_key), so A:1 (processed first) claims PGID1, and B:1 can no
    # longer reuse it -- it gets a fresh identity, logged as a split.
    clusters = {"A:1": ("A:1",), "B:1": ("B:1",)}
    new_crosswalk, events = resolve_crosswalk(existing, clusters, run_id="run2")

    assert new_crosswalk["A:1"].patient_global_id == "PGID000000000001"
    assert new_crosswalk["B:1"].patient_global_id != "PGID000000000001"

    split_events = [e for e in events if e.event_type == SPLIT]
    assert len(split_events) == 1
    assert split_events[0].surviving_id == "PGID000000000001"
    assert split_events[0].retired_id == new_crosswalk["B:1"].patient_global_id


def test_split_when_former_clustermates_end_up_under_different_surviving_ids():
    existing = {
        "A:1": CrosswalkEntry("A:1", "PGID000000000001", "run1", "run1"),
        "B:1": CrosswalkEntry("B:1", "PGID000000000001", "run1", "run1"),
        "C:1": CrosswalkEntry("C:1", "PGID000000000002", "run1", "run1"),
    }
    # B:1 now clusters with C:1 instead of A:1. A:1 (processed first, smallest key) claims
    # PGID1 for itself; B:1's own old id (PGID1) is therefore unavailable to the {B:1,C:1}
    # cluster, leaving only C:1's old id (PGID2) -- so {B:1,C:1} reuses PGID2, and PGID1
    # logs a split (a subset of its former members -- B:1 -- moved to PGID2).
    clusters = {"A:1": ("A:1",), "B:1": ("B:1", "C:1"), "C:1": ("B:1", "C:1")}
    new_crosswalk, events = resolve_crosswalk(existing, clusters, run_id="run2")

    assert new_crosswalk["A:1"].patient_global_id == "PGID000000000001"
    assert new_crosswalk["B:1"].patient_global_id == "PGID000000000002"
    assert new_crosswalk["C:1"].patient_global_id == "PGID000000000002"

    assert [e for e in events if e.event_type == MERGE] == []

    split_events = [e for e in events if e.event_type == SPLIT]
    assert len(split_events) == 1
    assert split_events[0].surviving_id == "PGID000000000001"
    assert split_events[0].retired_id == "PGID000000000002"
