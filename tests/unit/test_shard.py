import random
from collections import Counter

import pytest

from mdm.generator.shard import generate_shard, plan_appearances, shard_ranges
from mdm.generator.vendors import VENDORS

NICKNAME_TABLE = {"Robert": ["Bob", "Bobby", "Rob"]}


@pytest.mark.parametrize(
    ("num_identities", "chunk_size"),
    [(5000, 2000), (2400, 2000), (100, 2000), (0, 2000), (2000, 2000)],
)
def test_shard_ranges_covers_every_identity_exactly_once(num_identities, chunk_size):
    ranges = shard_ranges(num_identities, chunk_size)
    covered = []
    for start, end in ranges:
        covered.extend(range(start, end))
    assert covered == list(range(num_identities))


def test_shard_ranges_respects_chunk_size():
    ranges = shard_ranges(5000, 2000)
    assert ranges == [(0, 2000), (2000, 4000), (4000, 5000)]


def test_plan_appearances_always_covers_at_least_two_distinct_vendors():
    rng = random.Random(0)
    for _ in range(200):
        appearances = plan_appearances(rng)
        vendors = {v for v, _seq in appearances}
        assert len(vendors) >= 2
        assert vendors <= set(VENDORS)


def test_plan_appearances_is_deterministic_given_seed():
    assert plan_appearances(random.Random(42)) == plan_appearances(random.Random(42))


def test_generate_shard_is_deterministic():
    result_a = generate_shard(0, 0, 50, seed_base=42, nickname_table=NICKNAME_TABLE)
    result_b = generate_shard(0, 0, 50, seed_base=42, nickname_table=NICKNAME_TABLE)
    assert result_a == result_b


def test_generate_shard_different_shard_index_changes_output():
    result_a = generate_shard(0, 0, 50, seed_base=42, nickname_table=NICKNAME_TABLE)
    result_b = generate_shard(1, 0, 50, seed_base=42, nickname_table=NICKNAME_TABLE)
    assert result_a != result_b


def test_generate_shard_first_appearance_per_identity_is_exact():
    _vendor_rows, ground_truth_rows, _fact_rows = generate_shard(
        0, 0, 300, seed_base=42, nickname_table=NICKNAME_TABLE
    )
    seen_identities = set()
    for row in ground_truth_rows:
        identity_id = row["true_identity_id"]
        if identity_id not in seen_identities:
            assert row["noise_type"] == "exact"
            seen_identities.add(identity_id)


def test_generate_shard_record_key_and_ground_truth_counts_match():
    vendor_rows, ground_truth_rows, _fact_rows = generate_shard(
        0, 0, 300, seed_base=42, nickname_table=NICKNAME_TABLE
    )
    total_vendor_records = sum(len(rows) for rows in vendor_rows.values())
    assert total_vendor_records == len(ground_truth_rows)
    assert len({row["record_key"] for row in ground_truth_rows}) == len(ground_truth_rows)


def test_generate_shard_average_records_per_identity_is_close_to_two():
    num_identities = 2000
    _vendor_rows, ground_truth_rows, _fact_rows = generate_shard(
        0, 0, num_identities, seed_base=42, nickname_table=NICKNAME_TABLE
    )
    avg = len(ground_truth_rows) / num_identities
    assert 1.9 <= avg <= 2.3


def test_generate_shard_produces_all_five_noise_types_at_scale():
    _vendor_rows, ground_truth_rows, _fact_rows = generate_shard(
        0, 0, 2000, seed_base=42, nickname_table=NICKNAME_TABLE
    )
    noise_types = Counter(row["noise_type"] for row in ground_truth_rows)
    for noise_type in ("exact", "nickname", "typo_name", "dob_error", "missing_ssn"):
        assert noise_types[noise_type] > 0, f"{noise_type} never occurred"
