"""Deterministic, worker-count-independent sharding (P6).

Shard boundaries are a pure function of `num_identities` and a fixed `CHUNK_SIZE` — never
of how many worker processes happen to be running. Shard `i` seeds its own Faker instance
and random.Random with `seed_base + i`. That combination is what makes
`--tier ci --seed 42` produce byte-identical output whether it runs with one worker or
eight: the work is partitioned before any parallelism is introduced, not by it.
"""

from __future__ import annotations

import random

from faker import Faker

from mdm.generator.identity import synthesize_identity
from mdm.generator.noise import apply_noise, choose_requested_noise_type
from mdm.generator.vendors import VENDOR_HAS_SSN, VENDORS, render_record

CHUNK_SIZE = 2000  # identities per shard, independent of --workers

# An identity appears in exactly 2 vendors by default, occasionally all 3 (mean records
# per identity = 2 + TRIPLE_VENDOR_PROB, tuned to match config/matching.yml's
# target_records / num_identities ratio of ~2.083 -- see docs/design-decisions.md).
TRIPLE_VENDOR_PROB = 1 / 12
# "Occasionally the same person appears twice within a single vendor's feed because they
# were registered twice" (PROJECT_CONSTITUTION.md #1) -- rare, layered on top.
WITHIN_VENDOR_DUP_PROB = 0.02


def shard_ranges(num_identities: int, chunk_size: int = CHUNK_SIZE) -> list[tuple[int, int]]:
    ranges = []
    start = 0
    while start < num_identities:
        end = min(start + chunk_size, num_identities)
        ranges.append((start, end))
        start = end
    return ranges


def plan_appearances(rng: random.Random) -> list[tuple[str, int]]:
    """Returns [(vendor, appearance_seq), ...] for one identity. appearance_seq is 0 for
    an identity's normal appearance in that vendor, 1 for the rare within-vendor dup."""
    base_two = rng.sample(VENDORS, 2)
    appearances = [(v, 0) for v in base_two]

    remaining = [v for v in VENDORS if v not in base_two]
    if rng.random() < TRIPLE_VENDOR_PROB:
        appearances.append((remaining[0], 0))

    if rng.random() < WITHIN_VENDOR_DUP_PROB:
        dup_vendor = rng.choice([v for v, _seq in appearances])
        appearances.append((dup_vendor, 1))

    return appearances


def generate_shard(
    shard_index: int,
    id_start: int,
    id_end: int,
    seed_base: int,
    nickname_table: dict[str, list[str]],
) -> tuple[dict[str, list[dict]], list[dict]]:
    shard_seed = seed_base + shard_index
    faker = Faker()
    faker.seed_instance(shard_seed)
    rng = random.Random(shard_seed)

    vendor_rows: dict[str, list[dict]] = {v: [] for v in VENDORS}
    ground_truth_rows: list[dict] = []

    for identity_index in range(id_start, id_end):
        identity = synthesize_identity(identity_index, faker, rng, nickname_table)
        appearances = plan_appearances(rng)

        for appearance_order, (vendor, appearance_seq) in enumerate(appearances):
            if appearance_order == 0:
                requested = "exact"
            else:
                requested = choose_requested_noise_type(
                    rng, allow_missing_ssn=VENDOR_HAS_SSN[vendor]
                )

            overrides, actual_noise_type = apply_noise(
                rng,
                first_name=identity.first_name,
                last_name=identity.last_name,
                dob=identity.dob,
                requested_noise_type=requested,
                nickname_table=nickname_table,
                has_ssn_field=VENDOR_HAS_SSN[vendor],
            )

            record_id = (
                f"{identity_index:08d}"
                if appearance_seq == 0
                else f"{identity_index:08d}-{appearance_seq}"
            )
            record = render_record(vendor, record_id, identity, overrides)
            vendor_rows[vendor].append(record)
            ground_truth_rows.append(
                {
                    "record_key": f"{vendor}:{record_id}",
                    "true_identity_id": identity.identity_id,
                    "noise_type": actual_noise_type,
                }
            )

    return vendor_rows, ground_truth_rows


def generate_shard_task(
    args: tuple[int, int, int, int, dict[str, list[str]]],
) -> tuple[dict[str, list[dict]], list[dict]]:
    """multiprocessing-friendly single-argument wrapper around generate_shard."""
    shard_index, id_start, id_end, seed_base, nickname_table = args
    return generate_shard(shard_index, id_start, id_end, seed_base, nickname_table)
