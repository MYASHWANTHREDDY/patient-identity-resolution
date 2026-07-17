"""Pure corruption functions — the five required noise types (PROJECT_CONSTITUTION.md #8).

Every function here takes its randomness as an explicit `random.Random`, never touches
global state, and returns what it *actually* did rather than what was requested. A
requested noise type can be structurally impossible (a one-letter name can't be
transposed, Vendor C has no SSN field to blank) — in that case the function reports the
noise type it actually fell back to, so ground truth always reflects reality (P3).
"""

from __future__ import annotations

import random
from datetime import date, timedelta

NOISE_TYPES = ("exact", "nickname", "typo_name", "dob_error", "missing_ssn")

# Requested-noise-type weights for non-canonical appearances. Not derived from anything —
# a deliberate stress mix, not a claim about real vendor error rates (see
# docs/design-decisions.md).
DEFAULT_NOISE_WEIGHTS: dict[str, float] = {
    "exact": 0.25,
    "nickname": 0.15,
    "typo_name": 0.30,
    "dob_error": 0.20,
    "missing_ssn": 0.10,
}


def choose_requested_noise_type(rng: random.Random, *, allow_missing_ssn: bool) -> str:
    """Weighted draw over NOISE_TYPES, excluding missing_ssn where it can't apply."""
    types = list(DEFAULT_NOISE_WEIGHTS)
    weights = list(DEFAULT_NOISE_WEIGHTS.values())
    if not allow_missing_ssn:
        idx = types.index("missing_ssn")
        removed = weights.pop(idx)
        types.pop(idx)
        total = sum(weights)
        weights = [w + removed * (w / total) for w in weights]
    return rng.choices(types, weights=weights, k=1)[0]


def apply_typo(rng: random.Random, value: str) -> str:
    """Character transposition, drop, or duplication — the case fuzzy matching is for."""
    if len(value) < 2:
        return value
    op = rng.choice(("transpose", "drop", "duplicate"))
    i = rng.randrange(len(value) - 1) if op == "transpose" else rng.randrange(len(value))
    if op == "transpose":
        chars = list(value)
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
        return "".join(chars)
    if op == "drop":
        return value[:i] + value[i + 1 :]
    return value[: i + 1] + value[i] + value[i + 1 :]  # duplicate


def apply_nickname(
    rng: random.Random, first_name: str, nickname_table: dict[str, list[str]]
) -> tuple[str, bool]:
    """Swap a canonical first name for a table nickname. (Robert -> Bob has JW ~= 0.5;
    no threshold tuning recovers that — it needs a lookup table.)"""
    options = nickname_table.get(first_name)
    if not options:
        return first_name, False
    return rng.choice(options), True


def transpose_dob(rng: random.Random, dob: date) -> tuple[date, bool]:
    """Day/month transposition — a systematic error, not a random one. Falls back to a
    small random day offset when day > 12 (transposition would be an invalid month)."""
    if dob.day <= 12 and dob.day != dob.month:
        return date(dob.year, dob.day, dob.month), True
    offset = rng.choice((-3, -2, -1, 1, 2, 3))
    return dob + timedelta(days=offset), False


def apply_noise(
    rng: random.Random,
    *,
    first_name: str,
    last_name: str,
    dob: date,
    requested_noise_type: str,
    nickname_table: dict[str, list[str]],
    has_ssn_field: bool,
) -> tuple[dict, str]:
    """Apply `requested_noise_type` to (first_name, last_name, dob). Returns
    (field_overrides, actual_noise_type) — only the changed fields are in the override dict.
    `ssn` in the override dict means "blank this out"; the caller supplies the true ssn
    otherwise."""
    if requested_noise_type == "exact":
        return {}, "exact"

    if requested_noise_type == "nickname":
        new_first, applied = apply_nickname(rng, first_name, nickname_table)
        if applied:
            return {"first_name": new_first}, "nickname"
        return apply_noise(
            rng,
            first_name=first_name,
            last_name=last_name,
            dob=dob,
            requested_noise_type="typo_name",
            nickname_table=nickname_table,
            has_ssn_field=has_ssn_field,
        )

    if requested_noise_type == "typo_name":
        field = rng.choice(("first_name", "last_name"))
        value = first_name if field == "first_name" else last_name
        if len(value) < 2:
            return {}, "exact"
        return {field: apply_typo(rng, value)}, "typo_name"

    if requested_noise_type == "dob_error":
        new_dob, _transposed = transpose_dob(rng, dob)
        return {"dob": new_dob}, "dob_error"

    if requested_noise_type == "missing_ssn":
        if not has_ssn_field:
            return apply_noise(
                rng,
                first_name=first_name,
                last_name=last_name,
                dob=dob,
                requested_noise_type="typo_name",
                nickname_table=nickname_table,
                has_ssn_field=has_ssn_field,
            )
        return {"ssn": ""}, "missing_ssn"

    raise ValueError(f"Unknown noise type: {requested_noise_type}")
