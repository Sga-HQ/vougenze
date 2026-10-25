# Vougenze Current State

## Repository identity
- **Current Head Commit:** `81627c9` (`feat(core): apply governed rewrite candidate for VZ-P6, VZ-W1, VZ-W2, and VZ-SIM1`)
- **Audit Revision:** `2.2.1-final.1`
- **Research Checkpoint:** `RESEARCH-PASS-01-HYPOTHESIS-LIFECYCLE`
- **Test Suite Status:** 106 deterministic unit tests + 1 headless simulator test passing (100% OK)

## Authority
- **SQLite Database:** Authoritative runtime state and transaction boundary.
- **Specification:** [`SPECIFICATION.md`](../../SPECIFICATION.md) (Frozen V0 baseline 2.2.1-final.1).
- **Owner Decisions:** Applied governed candidate rewrite under owner authorization.

## Current status
- **Candidate Implementation:** Applied & Verified
- **Audit:** Reconciliation in progress

## Frozen artifacts
- [`docs/audit/v0_audit_register.md`](../audit/v0_audit_register.md) (`ce5a2089ce48a72e1390f7d9c314f79351987ba26eff22c8455e306629dcd655`)
- [`docs/audit/specification_errata_ledger.md`](../audit/specification_errata_ledger.md) (`65e88952a7754752c31c9f11b2278340336d06a44463926cbaccdb71f9962a33`)
- [`docs/audit/untracked_manifest.txt`](../audit/untracked_manifest.txt) (`eb082c8ec682f5a3f0de7be4124223b34fe30ae871487dc6cf6cc34578a26991`)

## Resolution status of findings
- **VZ-P6 (Resolved in 81627c9):** Candidate generation excludes authorizations reserved by active `PROPOSED` or `RESERVED` plans. Canonical replacement cores implemented.
- **VZ-W1 (Resolved in 81627c9):** `WearService` enforces `operation_mode`: `NEW_WEAR` requires `PLANNED` session; `CORRECTIVE_REPLACEMENT` requires `COMPLETED` session.
- **VZ-W2 (Resolved in 81627c9):** Non-empty trimmed `modification_reason` guard enforced on modified wear.
- **VZ-SIM1 (Resolved in 81627c9):** Simulator evidence tagged as synthetic with `training_eligible: 0`.
- **Planner Quality Invariance (Resolved in 81627c9):** Novelty bonus removed from persisted quality score; exploration decoupled cleanly.
- **Laundry-delay consequence (Pending Decision):** Delayed return plan invalidation consequence remains pending owner decision on schema migration vs coordinator semantics.

## Accepted research directions
- **Hypothesis:** Test-only lifecycle invariant and state-machine property testing.
- **OR-Tools:** Research-only integer programming oracle for $(U_H, U_N, U_L, D, F_b)$ coverage.
- **Hypergraph set packing:** Bounded oracle for discrete outfit compatibility.
- **Exact tiny-world DP:** Full FutureImpact dynamic programming evaluation on bounded horizons.

## Authorized next operation
Governed triage only
