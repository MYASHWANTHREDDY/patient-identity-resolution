"""Golden record survivorship (PROJECT_CONSTITUTION.md #13.3): per-field rule chain --
vendor_trust -> plurality -> completeness -> recency -> deterministic tiebreak. Every rule
either narrows the candidate set or passes it through unchanged; the deterministic tiebreak
(lexicographically smallest record_key) always narrows to exactly one, so the chain is
guaranteed to terminate. Without it, two runs over identical data could produce different
golden records depending on row order -- and under Spark, partition ordering isn't stable.

Every winning value's provenance becomes a field_lineage row.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from mdm.comparators import is_missing

FIELDS = ("first_name", "last_name", "dob", "gender", "ssn")


@dataclass(frozen=True)
class FieldLineage:
    patient_global_id: str
    field_name: str
    winning_value: object
    source_vendor: str
    source_record_id: str
    record_key: str
    rule_applied: str


def _apply_vendor_trust(
    candidates: list[dict], vendor_order: list[str] | None
) -> tuple[list[dict], bool]:
    if not vendor_order:
        return candidates, False
    for vendor in vendor_order:
        subset = [c for c in candidates if c["source_vendor"] == vendor]
        if subset:
            return subset, len(subset) < len(candidates)
    return candidates, False


def _apply_plurality(candidates: list[dict], field_name: str) -> tuple[list[dict], bool]:
    counts = Counter(c[field_name] for c in candidates)
    max_count = max(counts.values())
    if max_count == 1:
        return candidates, False  # every value is equally rare -- plurality can't decide
    top_values = {value for value, count in counts.items() if count == max_count}
    narrowed = [c for c in candidates if c[field_name] in top_values]
    return narrowed, len(narrowed) < len(candidates)


def _apply_completeness(candidates: list[dict]) -> tuple[list[dict], bool]:
    def score(record: dict) -> int:
        return sum(1 for f in FIELDS if not is_missing(record.get(f)))

    max_score = max(score(c) for c in candidates)
    narrowed = [c for c in candidates if score(c) == max_score]
    return narrowed, len(narrowed) < len(candidates)


def _apply_recency(candidates: list[dict]) -> tuple[list[dict], bool]:
    max_ts = max(c["normalized_at"] for c in candidates)
    narrowed = [c for c in candidates if c["normalized_at"] == max_ts]
    return narrowed, len(narrowed) < len(candidates)


def _apply_deterministic(candidates: list[dict]) -> tuple[list[dict], bool]:
    min_key = min(c["record_key"] for c in candidates)
    return [c for c in candidates if c["record_key"] == min_key], True


_RULE_FUNCS = {
    "plurality": lambda candidates, field_name, vendor_order: _apply_plurality(
        candidates, field_name
    ),
    "completeness": lambda candidates, field_name, vendor_order: _apply_completeness(candidates),
    "recency": lambda candidates, field_name, vendor_order: _apply_recency(candidates),
    "deterministic": lambda candidates, field_name, vendor_order: _apply_deterministic(candidates),
    "vendor_trust": lambda candidates, field_name, vendor_order: _apply_vendor_trust(
        candidates, vendor_order
    ),
}


def survive_field(
    field_name: str,
    members: list[dict],
    *,
    rule_chain: list[str],
    vendor_trust: dict[str, list[str]] | None = None,
) -> tuple[object, dict, str]:
    """Returns (winning_value, winning_record, rule_applied). `members` are full records
    (dicts with FIELDS + source_vendor + source_record_id + record_key + normalized_at).

    `rule_applied` names the rule that decided the *value* -- once every remaining
    candidate agrees on the value, later rules (including the guaranteed-to-apply
    `deterministic` tiebreak) only pick which of the value-tied records gets credited as
    the source, and that no longer counts as "the" deciding rule."""
    candidates = [m for m in members if not is_missing(m.get(field_name))]
    if not candidates:
        return None, members[0], "no_non_null_value"

    vendor_order = (vendor_trust or {}).get(field_name)
    value_decided_by = None
    remaining = candidates
    for rule in rule_chain:
        if len({c[field_name] for c in remaining}) == 1:
            break
        remaining, applied = _RULE_FUNCS[rule](remaining, field_name, vendor_order)
        if applied:
            value_decided_by = rule

    winner = min(remaining, key=lambda c: c["record_key"])
    fallback_rule = "deterministic" if len(candidates) > 1 else "single_candidate"
    rule_applied = value_decided_by or fallback_rule
    return winner[field_name], winner, rule_applied


def build_golden_record(
    patient_global_id: str,
    members: list[dict],
    *,
    rule_chain: list[str],
    vendor_trust: dict[str, list[str]] | None = None,
) -> tuple[dict, list[FieldLineage]]:
    """Returns (golden_record, field_lineage_rows) for one cluster's members."""
    golden_record = {"patient_global_id": patient_global_id}
    lineage: list[FieldLineage] = []

    for field_name in FIELDS:
        value, winner, rule = survive_field(
            field_name, members, rule_chain=rule_chain, vendor_trust=vendor_trust
        )
        golden_record[field_name] = value
        lineage.append(
            FieldLineage(
                patient_global_id=patient_global_id,
                field_name=field_name,
                winning_value=value,
                source_vendor=winner.get("source_vendor"),
                source_record_id=winner.get("source_record_id"),
                record_key=winner.get("record_key"),
                rule_applied=rule,
            )
        )

    return golden_record, lineage
