# Domain linking strategy

Phase 17 (`PROJECT_CONSTITUTION.md`). Extends member-only identity resolution (Phases 0–16)
into a real multi-domain member 360: six domains, four vendors, and — for every (vendor,
domain) pair — an explicit, written decision for how that domain's records reach
`patient_global_id`. Written before any fact-table code depends on it, per this phase's own
exit criterion.

## Vendors and domains

Four vendors: `VENDOR_A`, `VENDOR_B`, `VENDOR_C` (existing, previously sending member data
only) and `VENDOR_D` (new — a lab, not a payer, so it never has its own eligibility
relationship to a member at all).

Six domains: `member_eligibility` (the existing member domain, renamed and enriched — see
below), `medical_history`, `medical_claims`, `pharmacy_claims`, `pharmacy_info`,
`lab_results`.

## The matrix

| Vendor | member_eligibility | medical_history | medical_claims | pharmacy_claims | pharmacy_info | lab_results |
| --- | --- | --- | --- | --- | --- | --- |
| `VENDOR_A` | existing, enriched | Path A | Path A | Path B | — | — |
| `VENDOR_B` | existing, enriched | — | Path A | — | **Match-path** | — |
| `VENDOR_C` | existing, enriched | Path A | — | Path B | — | — |
| `VENDOR_D` | — | — | — | — | — | **Match-path** |

**Path A** — the domain's records carry the same member ID as that vendor's own
`member_eligibility` records; one join through `member_alternate_identifier` resolves
`patient_global_id`, no new matching.

**Path B** — the domain's records carry a *different* ID than that vendor's
`member_eligibility` records (a separate system, same vendor); a small per-vendor
`vendor_id_map` (that ID → the vendor's enrollment ID) runs first, then the same join.

**Match-path** — no shared ID exists at all; the domain's records carry only demographics
(and, where present, address/phone/email), and reach `patient_global_id` through the same
comparator/blocking/Fellegi-Sunter pipeline `member_eligibility` already uses — Phase 20,
confirmed necessary by this doc (two domains land here, not zero: this is no longer the
"skip if nothing needs it" conditional Phase 20 was written to allow for).

## Reasoning

**Claims and medical history stay on the payer's own member ID (Path A).** A payer
processing its own claims and clinical history under the ID it already issued at enrollment
is the common case, not the exception.

**Pharmacy claims consistently go through a separate PBM relationship (Path B, `VENDOR_A` and
`VENDOR_C`).** Realistic and deliberate: US health plans routinely contract pharmacy benefit
management to a different company than the one administering medical claims, so pharmacy
claims carry the PBM's own member ID, not the payer's.

**`VENDOR_B`'s pharmacy relationship goes one step further — no ID at all, just a member-level
benefit file (match-path).** Distinguishes "different ID system" (Path B) from "no linkable
system" (match-path) with a real example of each, rather than only ever exercising the easier
case.

**`VENDOR_D` (lab results) is entirely match-path.** A lab receives orders and reports results
for patients referred by other providers/payers — it has no eligibility relationship, no
enrollment system, nothing to key a join off of. Every one of its records resolves to
`patient_global_id` purely through demographic matching. Chosen specifically as a *second*,
independently-motivated match-path case (transactional, not benefit-file-shaped like
`pharmacy_info`) so Phase 20 isn't validated against only one scenario.

## Field contracts

**`member_eligibility`** (existing domain, enriched) — `record_key`, `first_name`,
`last_name`, `dob`, `gender`, `ssn`, plus real eligibility attributes: `plan_id`, `group_id`,
`coverage_effective_date`, `coverage_termination_date`, `relationship_to_subscriber`
(self/spouse/dependent). Identity resolution is unchanged — still matches on name/DOB/gender/
SSN; the new fields ride along on already-resolved records rather than participating in
matching. A person can legitimately have multiple `member_eligibility` records from the same
vendor over time (re-enrollment, a new plan year) — already correctly handled by the existing
crosswalk/matching pipeline (P7 idempotency; the same-vendor-multiple-records case was already
part of Phase 0's ground-truth design).

**`medical_history`** (Path A, `VENDOR_A`/`VENDOR_C`) — one row per encounter: `source_vendor`,
`source_encounter_id`, `member_id` (that vendor's enrollment ID), `encounter_date`,
`condition_code` (ICD-10-CM), `encounter_type`.

**`medical_claims`** (Path A, `VENDOR_A`/`VENDOR_B`) — one row per claim: `source_vendor`,
`source_claim_id`, `member_id`, `claim_date`, `diagnosis_code` (ICD-10-CM), `procedure_code`
(HCPCS Level II), `billed_amount`, `paid_amount`, `claim_status`.

**`pharmacy_claims`** (Path B, `VENDOR_A`/`VENDOR_C`) — one row per fill: `source_vendor`,
`source_rx_id`, `pbm_member_id` (resolved via `vendor_id_map`), `fill_date`, `ndc_code` (NDC),
`days_supply`, `quantity`.

**`pharmacy_info`** (match-path, `VENDOR_B`) — member-level, not transactional:
`source_record_id` (the PBM's own ID, unrelated to `VENDOR_B`'s enrollment ID), `first_name`,
`last_name`, `dob`, `gender`, optionally `address`/`phone`, `plan_tier`.

**`lab_results`** (match-path, `VENDOR_D`) — one row per test: `source_record_id` (Vendor D's
own patient/order ID), `first_name`, `last_name`, `dob`, `gender`, optionally `address`/
`phone`, `test_date`, `test_code` (LOINC), `result_value`, `result_unit`, `abnormal_flag`
(normal/high/low/critical).

## Reference code standards

Four real, public-domain code systems, sourced in Phase 18:

| Standard | Covers | Publisher | License |
| --- | --- | --- | --- |
| ICD-10-CM | Diagnoses | CMS/NCHS | Public domain |
| NDC | Drugs | FDA | Public domain |
| HCPCS Level II | Procedures | CMS | Public domain |
| LOINC | Lab tests | Regenstrief Institute | Free to use/redistribute |

**Not CPT** for procedures, even though it's the more commonly seen standard in real claims
data — CPT is AMA-copyrighted, and this project has no license held. HCPCS Level II fills the
same role (procedure/service coding) without the licensing question. This project has no real
patient data and no license for any of these standards beyond what each publisher grants for
free public use — a fact worth restating here since it's the reason CPT was ruled out and the
other three weren't.

## What this unlocks

- **Phase 18** sources all four code sets above.
- **Phase 19** builds `fct_medical_history`, `fct_medical_claims`, `fct_pharmacy_claims` — every
  Path A/Path B cell in the matrix.
- **Phase 20** builds match-path linking for `pharmacy_info` and `lab_results` — confirmed
  necessary by this doc, not skipped.
- **Phase 21** grows `member_360` to summarize all six domains.
- **Phase 22** exposes the whole thing through the Member 360 API.
