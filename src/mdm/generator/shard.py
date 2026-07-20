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

from mdm.generator.facts import (
    MEDICAL_CLAIMS_VENDORS,
    MEDICAL_HISTORY_VENDORS,
    PHARMACY_CLAIMS_VENDORS,
    generate_medical_claims,
    generate_medical_history,
    generate_pharmacy_claims,
    mint_pbm_member_id,
)
from mdm.generator.identity import synthesize_identity
from mdm.generator.matchpath import (
    LAB_APPEARANCE_PROB,
    PHARMACY_INFO_APPEARANCE_PROB,
    generate_lab_identity_appearance,
    generate_lab_results,
    generate_pharmacy_info_appearance,
)
from mdm.generator.noise import apply_noise, choose_requested_noise_type
from mdm.generator.vendors import VENDOR_HAS_SSN, VENDORS, render_record

FACT_DOMAINS = ("medical_history", "medical_claims", "pharmacy_claims")

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


def _empty_fact_rows() -> dict[str, dict[str, list[dict]] | list[dict]]:
    return {
        "medical_history": {v: [] for v in MEDICAL_HISTORY_VENDORS},
        "medical_claims": {v: [] for v in MEDICAL_CLAIMS_VENDORS},
        "pharmacy_claims": {v: [] for v in PHARMACY_CLAIMS_VENDORS},
        "vendor_id_map": [],
        "pharmacy_info": [],
        "lab_identity": [],
        "lab_results": [],
        "matchpath_ground_truth": [],
    }


def generate_shard(
    shard_index: int,
    id_start: int,
    id_end: int,
    seed_base: int,
    nickname_table: dict[str, list[str]],
    icd10cm_codes: list[str] | None = None,
    hcpcs_codes: list[str] | None = None,
    ndc_codes: list[str] | None = None,
    loinc_codes: list[str] | None = None,
) -> tuple[dict[str, list[dict]], list[dict], dict]:
    """icd10cm_codes/hcpcs_codes/ndc_codes/loinc_codes are None in tests that only care
    about the member domain; fact-domain and match-path generation are each skipped
    entirely when their inputs are absent rather than failing, since not every caller needs
    Phase 19/20's tables (P7: this function's original member-only behavior stays
    available, not silently changed underneath it)."""
    shard_seed = seed_base + shard_index
    faker = Faker()
    faker.seed_instance(shard_seed)
    rng = random.Random(shard_seed)
    # Independently seeded, mirroring facts_rng/matchpath_rng below -- match-path
    # generation calls faker.address()/faker.phone_number(), and Faker keeps its own
    # internal RNG state on the instance. Sharing the member-domain `faker` here would
    # reintroduce exactly the Phase 19 class of bug (this time via Faker's state instead
    # of random.Random's): found by testing (ci-tier record counts and per-vendor totals
    # shifted -- 5043 records before this fix vs. 5040 after -- even though matchpath_rng
    # was already a separate random.Random stream).
    matchpath_faker = Faker()
    matchpath_faker.seed_instance(shard_seed + 2_000_000)
    # Independently seeded, not a second use of `rng` -- fact generation draws a different
    # number of random values per identity depending on _record_count's outcome, and if it
    # shared `rng` with the member-domain loop below, that would shift every *later*
    # identity's member-domain rng state too (noise types, appearance patterns), silently
    # changing already-verified Phase 0-15 output the moment Phase 19 fact generation was
    # turned on. A separate stream keeps the two concerns from perturbing each other, so
    # generate_shard(..., icd10cm_codes=None) and a facts-enabled call produce byte-identical
    # member-domain output for the same identities (found by testing: they didn't, before
    # this fix -- record counts differed between the two).
    facts_rng = random.Random(shard_seed + 1_000_000)
    # A third independent stream, same reasoning as facts_rng above -- match-path generation
    # (Phase 20) must not perturb either the member domain or Phase 19's fact domains.
    matchpath_rng = random.Random(shard_seed + 2_000_000)

    generate_facts = icd10cm_codes is not None and hcpcs_codes is not None and ndc_codes is not None
    generate_matchpath = loinc_codes is not None

    vendor_rows: dict[str, list[dict]] = {v: [] for v in VENDORS}
    ground_truth_rows: list[dict] = []
    fact_rows = _empty_fact_rows()

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

            # Fact-domain records key off the primary registration only (appearance_seq
            # == 0) -- a rare within-vendor duplicate enrollment doesn't get its own
            # separate claims history, since it's the same person's real activity either
            # way (docs/domain-linking-strategy.md doesn't model claims-level dedup).
            if generate_facts and appearance_seq == 0:
                if vendor in MEDICAL_HISTORY_VENDORS:
                    fact_rows["medical_history"][vendor].extend(
                        generate_medical_history(
                            facts_rng,
                            member_id=record_id,
                            identity_index=identity_index,
                            icd10cm_codes=icd10cm_codes,
                        )
                    )
                if vendor in MEDICAL_CLAIMS_VENDORS:
                    fact_rows["medical_claims"][vendor].extend(
                        generate_medical_claims(
                            facts_rng,
                            member_id=record_id,
                            identity_index=identity_index,
                            icd10cm_codes=icd10cm_codes,
                            hcpcs_codes=hcpcs_codes,
                        )
                    )
                if vendor in PHARMACY_CLAIMS_VENDORS:
                    pbm_member_id = mint_pbm_member_id(facts_rng, identity_index)
                    claims = generate_pharmacy_claims(
                        facts_rng,
                        pbm_member_id=pbm_member_id,
                        identity_index=identity_index,
                        ndc_codes=ndc_codes,
                    )
                    if claims:
                        fact_rows["pharmacy_claims"][vendor].extend(claims)
                        fact_rows["vendor_id_map"].append(
                            {
                                "source_vendor": vendor,
                                "pbm_member_id": pbm_member_id,
                                "enrollment_member_id": record_id,
                            }
                        )

        # Match-path (Phase 20): independent of the member-domain appearances above -- a
        # PBM or lab relationship doesn't depend on which of VENDOR_A/B/C this identity
        # happened to enroll under. Ground truth for these goes to its own table
        # (matchpath_ground_truth), never merged into the core ground_truth_rows above --
        # mixing them in would introduce "true pairs" between core member records and
        # match-path records that mdm.evaluate's existing blocking/scoring evaluation was
        # never designed to find (they're never candidates against each other in
        # matching.candidate_pairs), silently dragging down already-verified Phase 0-15
        # recall/precision numbers the same way the Phase 19 shared-rng bug silently
        # changed member-domain output -- a mistake worth not repeating.
        if generate_matchpath:
            if matchpath_rng.random() < PHARMACY_INFO_APPEARANCE_PROB:
                record, noise_type = generate_pharmacy_info_appearance(
                    matchpath_rng, matchpath_faker, identity, identity_index, nickname_table
                )
                fact_rows["pharmacy_info"].append(record)
                fact_rows["matchpath_ground_truth"].append(
                    {
                        "record_key": f"VENDOR_B_PHARMACY:{record['source_record_id']}",
                        "true_identity_id": identity.identity_id,
                        "noise_type": noise_type,
                    }
                )
            if matchpath_rng.random() < LAB_APPEARANCE_PROB:
                record, noise_type = generate_lab_identity_appearance(
                    matchpath_rng, matchpath_faker, identity, identity_index, nickname_table
                )
                fact_rows["lab_identity"].append(record)
                fact_rows["matchpath_ground_truth"].append(
                    {
                        "record_key": f"VENDOR_D:{record['source_record_id']}",
                        "true_identity_id": identity.identity_id,
                        "noise_type": noise_type,
                    }
                )
                fact_rows["lab_results"].extend(
                    generate_lab_results(
                        matchpath_rng,
                        source_record_id=record["source_record_id"],
                        loinc_codes=loinc_codes,
                    )
                )

    return vendor_rows, ground_truth_rows, fact_rows


def generate_shard_task(
    args: tuple[
        int,
        int,
        int,
        int,
        dict[str, list[str]],
        list[str] | None,
        list[str] | None,
        list[str] | None,
        list[str] | None,
    ],
) -> tuple[dict[str, list[dict]], list[dict], dict]:
    """multiprocessing-friendly single-argument wrapper around generate_shard."""
    (
        shard_index,
        id_start,
        id_end,
        seed_base,
        nickname_table,
        icd10cm_codes,
        hcpcs_codes,
        ndc_codes,
        loinc_codes,
    ) = args
    return generate_shard(
        shard_index,
        id_start,
        id_end,
        seed_base,
        nickname_table,
        icd10cm_codes,
        hcpcs_codes,
        ndc_codes,
        loinc_codes,
    )
