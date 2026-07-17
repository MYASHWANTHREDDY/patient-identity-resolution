# patient-dedup-system

**Status:** Under construction — Phase 0 (scaffolding). See
[PROJECT_CONSTITUTION.md](PROJECT_CONSTITUTION.md) for the full spec, architecture, and build
plan; this README will grow into the reviewer-facing summary as phases land (Phase 15).

## The problem

Three healthcare vendors each send patient records under their own schema, with no shared
identifier. The same person appears in all three feeds — spelled differently, with a
transposed birth date, under a nickname — and occasionally twice within one vendor's own feed.

## The solution

A cloud-native Master Data Management pipeline: three-layer conformance architecture,
probabilistic record linkage (Fellegi-Sunter) at multi-million-record scale, and golden-record
survivorship — producing one stable `patient_global_id` per real person with a full audit
trail. One codebase runs locally against 50k records in under two minutes (DuckDB/pandas) and
at 5,000,000 records on GCP (BigQuery + Dataproc Serverless), unchanged apart from a `--tier`
flag.

## Quick start

Not yet available — lands at Phase 9 as `make demo` (dev-tier pipeline, no GCP account
required, under two minutes). Until then, see the phase-by-phase build plan in
[PROJECT_CONSTITUTION.md #19](PROJECT_CONSTITUTION.md#19-build-phases).

## Repository map

See [PROJECT_CONSTITUTION.md #18](PROJECT_CONSTITUTION.md#18-repository-layout) for the target
layout and [#20](PROJECT_CONSTITUTION.md#20-definition-of-done) for what "done" means.

## License

[MIT](LICENSE)
