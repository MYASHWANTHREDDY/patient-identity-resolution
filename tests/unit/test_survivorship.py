import random

from mdm.survivorship import build_golden_record, survive_field

RULE_CHAIN = ["vendor_trust", "plurality", "completeness", "recency", "deterministic"]
VENDOR_TRUST = {"dob": ["VENDOR_A", "VENDOR_C", "VENDOR_B"]}


def _record(record_key, vendor, **fields):
    base = {
        "record_key": record_key,
        "source_vendor": vendor,
        "source_record_id": record_key.split(":")[1],
        "normalized_at": "2026-01-01T00:00:00",
        "first_name": None,
        "last_name": None,
        "dob": None,
        "gender": None,
        "ssn": None,
    }
    base.update(fields)
    return base


def test_survive_field_single_candidate_wins_trivially():
    members = [_record("A:1", "VENDOR_A", dob="1980-01-01")]
    value, winner, rule = survive_field("dob", members, rule_chain=RULE_CHAIN)
    assert value == "1980-01-01"
    assert winner["record_key"] == "A:1"
    assert rule == "single_candidate"


def test_survive_field_no_non_null_value():
    members = [_record("A:1", "VENDOR_A"), _record("B:1", "VENDOR_B")]
    value, _winner, rule = survive_field("dob", members, rule_chain=RULE_CHAIN)
    assert value is None
    assert rule == "no_non_null_value"


def test_vendor_trust_picks_the_highest_priority_vendor():
    members = [
        _record("B:1", "VENDOR_B", dob="1980-01-01"),
        _record("A:1", "VENDOR_A", dob="1980-06-15"),
    ]
    value, winner, rule = survive_field(
        "dob", members, rule_chain=RULE_CHAIN, vendor_trust=VENDOR_TRUST
    )
    assert value == "1980-06-15"
    assert winner["record_key"] == "A:1"
    assert rule == "vendor_trust"


def test_plurality_picks_the_majority_value():
    members = [
        _record("A:1", "VENDOR_A", first_name="ROBERT"),
        _record("B:1", "VENDOR_B", first_name="ROBERT"),
        _record("C:1", "VENDOR_C", first_name="BOB"),
    ]
    value, _winner, rule = survive_field("first_name", members, rule_chain=RULE_CHAIN)
    assert value == "ROBERT"
    assert rule == "plurality"


def test_plurality_defers_when_all_values_are_equally_rare():
    members = [
        _record("A:1", "VENDOR_A", first_name="ROBERT"),
        _record("B:1", "VENDOR_B", first_name="BOB"),
    ]
    # no vendor_trust configured for first_name -- falls through vendor_trust and
    # plurality (tied 1-1), lands on completeness/recency/deterministic
    value, winner, rule = survive_field("first_name", members, rule_chain=RULE_CHAIN)
    assert rule == "deterministic"
    assert winner["record_key"] == "A:1"
    assert value == "ROBERT"


def test_completeness_prefers_the_more_populated_record():
    members = [
        _record("A:1", "VENDOR_A", first_name="ROBERT", last_name="SMITH", dob="1980-01-01"),
        _record("B:1", "VENDOR_B", first_name="BOB"),
    ]
    value, winner, rule = survive_field("first_name", members, rule_chain=RULE_CHAIN)
    assert rule == "completeness"
    assert winner["record_key"] == "A:1"
    assert value == "ROBERT"


def test_recency_prefers_the_most_recently_normalized_record():
    members = [
        _record("A:1", "VENDOR_A", first_name="ROBERT", normalized_at="2026-01-01T00:00:00"),
        _record("B:1", "VENDOR_B", first_name="BOB", normalized_at="2026-06-01T00:00:00"),
    ]
    value, winner, rule = survive_field("first_name", members, rule_chain=RULE_CHAIN)
    assert rule == "recency"
    assert winner["record_key"] == "B:1"
    assert value == "BOB"


def test_deterministic_tiebreak_is_stable_across_shuffled_input_order():
    members = [
        _record("C:3", "VENDOR_C", first_name="ROBERT"),
        _record("A:1", "VENDOR_A", first_name="BOB"),
        _record("B:2", "VENDOR_B", first_name="ROBERTO"),
    ]
    rng = random.Random(0)
    results = set()
    for _ in range(20):
        shuffled = members[:]
        rng.shuffle(shuffled)
        value, winner, rule = survive_field("first_name", shuffled, rule_chain=RULE_CHAIN)
        results.add((value, winner["record_key"], rule))
    assert results == {("BOB", "A:1", "deterministic")}


def test_rule_chain_precedence_vendor_trust_before_plurality():
    # plurality alone would pick "ROBERT" (2 votes), but vendor_trust for dob prefers
    # VENDOR_A over VENDOR_B/VENDOR_C and should win first
    members = [
        _record("B:1", "VENDOR_B", dob="1975-05-05"),
        _record("C:1", "VENDOR_C", dob="1975-05-05"),
        _record("A:1", "VENDOR_A", dob="1980-01-01"),
    ]
    value, winner, rule = survive_field(
        "dob", members, rule_chain=RULE_CHAIN, vendor_trust=VENDOR_TRUST
    )
    assert rule == "vendor_trust"
    assert value == "1980-01-01"
    assert winner["record_key"] == "A:1"


def test_build_golden_record_produces_lineage_for_every_field():
    members = [
        _record(
            "A:1",
            "VENDOR_A",
            first_name="ROBERT",
            last_name="SMITH",
            dob="1980-01-01",
            gender="M",
            ssn="123456789",
        ),
    ]
    golden_record, lineage = build_golden_record(
        "PGID1", members, rule_chain=RULE_CHAIN, vendor_trust=VENDOR_TRUST
    )
    assert golden_record["patient_global_id"] == "PGID1"
    assert golden_record["first_name"] == "ROBERT"
    assert {row.field_name for row in lineage} == {
        "first_name",
        "last_name",
        "dob",
        "gender",
        "ssn",
    }
    for row in lineage:
        assert row.patient_global_id == "PGID1"
        assert row.record_key == "A:1"
        assert row.source_vendor == "VENDOR_A"
