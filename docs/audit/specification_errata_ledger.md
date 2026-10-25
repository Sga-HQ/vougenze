# Vougenze Specification Errata, Discrepancy & Evolution Ledger

**Document Location:** `docs/audit/specification_errata_ledger.md`  
**Governing Baseline:** `SPECIFICATION.md` rev `2.2.1-final.1` (SHA-256: `CE715D37CAB49FAE68FEBDF549CAFF149794DDB958F59C187470943542F643AD`)  
**Audit Context:** READ-ONLY BASELINE FREEZE & DEFECT CLASSIFICATION  
**Audit Status:** AUDIT OPEN / CLOSURE NOT AUTHORIZED  
**Implementation Authorization:** IMPLEMENTATION AUTHORIZATION: NONE  
**Date:** 2026-10-25  

---

## 1. Purpose & Scope

This ledger systematically identifies, catalogues, and reconciles all instances where `SPECIFICATION.md` rev `2.2.1-final.1` contains **outdated, incomplete, contradictory, or untrue data** when measured against the audited SQLite schema (`migrations/0001_v0_schema.sql`) and current production implementations (`core/`).

It serves as the formal bridge between the frozen pre-fix baseline specification and future authorized specification revisions (e.g. `rev 2.2.2`).

---

## 2. Schema DDL Gaps & Discrepancies (Part 2 of Specification)

| # | Specification Item | `SPECIFICATION.md` Claim | Audited Reality (`0001_v0_schema.sql` / `core/`) | Operational Consequence & Classification |
|:---|:---|:---|:---|:---|
| **S-01** | Missing Table: `PlanWearAuthorizationReservation` | Table is completely omitted from Part 2 schema DDL. | Table is explicitly defined in `0001_v0_schema.sql:608-616` with composite primary key `(plan_id, session_id, garment_id)` and `UNIQUE(authorization_id)`. | **CRITICAL SCHEMA GAP**: Required to prevent active `PROPOSED` and `RESERVED` plan reservations from being stolen by competing plans (VZ-P2). |
| **S-02** | Missing Table: `GarmentSimilarityLink` | Table is completely omitted from Part 2 schema DDL. | Table is explicitly defined in `0001_v0_schema.sql:598-606` to store owner-confirmed similarity links (`PRIMARY KEY (garment_id_a, garment_id_b)` with `CHECK (garment_id_a < garment_id_b)`). | **SCHEMA GAP**: Required to support intake duplicate candidate resolution status `LINKED_SIMILAR`. |
| **S-03** | Missing Table: `SchemaMigration` | Table is omitted from Part 2 schema DDL. | Table is defined in `0001_v0_schema.sql:16-24` storing `version`, `applied_at`, and `checksum` to enforce migration tamper detection. | **INFRASTRUCTURE GAP**: Required for migration hash verification tested in Invariant 95. |
| **S-04** | Missing Partial Unique Index: `idx_one_consumed_authorization_per_wear` | Index is omitted from Part 2.15 index listings. | Defined in `0001_v0_schema.sql:633-635`: `CREATE UNIQUE INDEX idx_one_consumed_authorization_per_wear ON GarmentWearAuthorization(consumed_by_wear_event_id) WHERE consumed_by_wear_event_id IS NOT NULL;` | **GOVERNANCE IDENTIFIER ERROR**: Previous documentation erroneously referred to `idx_one_active_consumption_per_authorization`. |
| **S-05** | Obsolete `PreferenceEvidence` Inclusion Value (`CONSIDERED`) | Part 2.13 and earlier drafts referenced `CONSIDERED` as an alternative inclusion type. | `migrations/0001_v0_schema.sql:568-570` strictly limits inclusion types to `('PRESENTED', 'FEASIBLE_NOT_PRESENTED', 'INFEASIBLE_REFERENCE')`. | **CONTRADICTORY ENUM**: `CONSIDERED` is illegal at SQL check constraint level. |
| **S-06** | Tier 4 Unenforced JSON and Timestamp SQL Checks | Part 2 implies structured JSON and canonical ISO timestamps are mechanically validated at SQL level. | SQLite text columns storing JSON structures lack `CHECK (json_valid(...))` and timestamps lack regex formatting checks. | **DOCUMENTED SCHEMA RISK**: Enforced at service layer only. |

---

## 3. Lifecycle & State Machine Discrepancies (Parts 1 & 3 of Specification)

| # | Lifecycle Mechanism | `SPECIFICATION.md` Claim | Audited Reality (`core/`) | Operational Consequence & Classification |
|:---|:---|:---|:---|:---|
| **L-01** | Laundry Dispatch Preflight Validation | Preflight checks active policy date validity and verifies all garments are in clean/reusable/needs-wash condition. | `LaundryService.send_laundry_batch` verifies `expected_return_date >= sent_date`, user ownership of batch and garments, policy existence and ownership (if policy_id supplied), garments not already in laundry or retired, and garments have no active batch linkage (`current_laundry_batch_id IS NULL`). It does NOT check policy date ranges or garment cleanliness states. | **UNVERIFIED SERVICE CLAIM**: Documentation claimed checks that do not exist in the executed codebase. |
| **L-02** | Full Wash Authorization Cancellation | Line 757: *"A full wash cancels all unused authorizations unless explicitly preserved."* | `StateEngine.record_wash_event` resets wear count to 0 and transitions condition to `CLEAN`, but executes NO update to `GarmentWearAuthorization`. | **UNIMPLEMENTED SPECIFICATION RULE**: Unused wear authorizations remain in `AVAILABLE` status post-wash. |
| **L-03** | Damaged Laundry Return Condition Semantics | Part 3.2 line 740 states that `RETURNED_DAMAGED` with `wash_confirmed = 0` results in *"preserve/owner-assess"*, while earlier text/tests indicated `NEEDS_WASH`. | `LaundryService.record_item_return` preserves existing condition while transitioning availability to `REPAIR`. | **UNRESOLVED SPECIFICATION AMBIGUITY**: Specification table and text describe contradictory condition outcomes. |
| **L-04** | Normal Wear Logging Session State Guard (VZ-W1) | Wear logging operates on planned sessions. | `WearService.log_outfit_wear` checks user ownership and session existence, but lacks an explicit `session.status == 'PLANNED'` guard. | **UNTESTED SERVICE BOUNDARY**: Cancelled or completed sessions may accept wear logs if no active wear log exists. |
| **L-05** | Modified Recommendation Modification Reason Guard (VZ-W2) | Modifying a recommended outfit before wear mandates a non-empty `modification_reason`. | `WearService.log_outfit_wear` permits `was_modified_before_wear = 1` with `modification_reason = None` despite error text stating it is required. | **CANDIDATE DEFECT**: Error message demands reason, but implementation only checks boolean flag. |
| **L-06** | Candidate Generation Ignores Active Plan Reservations (VZ-P6) | Garments at hard limit are available if an authorization has `status = 'AVAILABLE'`. | `CandidateGenerator` checks `status = 'AVAILABLE'` but does not exclude authorizations reserved by active `PROPOSED` or `RESERVED` plans. | **UNTESTED BOUNDARY**: Garments operationally reserved by competing plans may appear in candidate pools until planner beam search rejects them. |

---

## 4. Planning, Scoring & Algorithm Discrepancies (Parts 4 & 5 of Specification)

| # | Algorithm Subsystem | `SPECIFICATION.md` Claim | Audited Reality (`core/`) | Operational Consequence & Classification |
|:---|:---|:---|:---|:---|
| **P-01** | Multi-Stage Continuation Mathematics (VZ-M1) | Part 4 & 5 describe continuous mathematical optimization, perceptual diversity spaces, and multi-stage continuation models. | `core/planner.py` implements a bounded discrete beam search with heuristic slot-difference diversity targets and static score weights. | **RESEARCH-ONLY NON-GOAL**: Specification describes post-V0 research mathematics replaced by bounded heuristics in V0. |
| **P-02** | Beam State Representation | Part 5.1 specifies a complex frozen dataclass `PlanningStateKey` (with tuples for `remaining_wears`, `condition_states`, `availability_states`, `reservation_state`, etc.). | `ContinuationPlanner` uses `PartialPlan` tracking dictionaries (`simulated_wears`, `simulated_last_worn`, `reserved_authorizations`). | **STALE ARCHITECTURAL SPECIFICATION**: `PlanningStateKey` is not implemented in production code. |
| **P-03** | Critical Item Retention Mechanism (VZ-P3) | Stage 0–1 critical item continuation retention. | `CandidateGenerator._truncate_role_pool` implements a static heuristic (`formality >= 4`, `formal/presentation/office` context, or `FAVOURITE` priority via nested helper `is_critical`). | **CALIBRATED HEURISTIC**: Code uses discrete priority heuristics rather than continuous stage 0-1 continuation math. |
| **P-04** | Substitute Packing Logic (VZ-P4) | General cross-category substitute packing for one-piece garments. | `CandidateGenerator.compute_candidate_substitutes` implements candidate role mappings with one-piece top/bottom identity; cross-category 1-to-2 piece alternatives remain out of scope. | **CALIBRATED SCOPE**: Category-level heuristic calibrated to candidate role mappings. |
| **P-05** | Shared Candidate Evaluation Mutation (VZ-P1) | Assumed independent path evaluation during beam search. | `ContinuationPlanner.generate_plan` previously mutated shared `OutfitCandidate` objects in-place; hardened via inline `copy.deepcopy(cand)` (L236). | **VERIFIED IMPLEMENTATION DEFECT**: Fixed in candidate code; dedicated adversarial test pending. |

---

## 5. Acceptance Scenario True-State Reconciliation (Part 6 of Specification)

The following 17 acceptance scenarios from `SPECIFICATION.md` Part 6 contain claims that are only **PARTIALLY DEMONSTRATED**, **UNTESTED**, or **HEURISTIC** in the current repository baseline:

1. **Scenario 4 (Delayed Laundry Batch):** `SPECIFICATION.md` claims plan is `SUPERSEDED` with `LAUNDRY_DELAYED`. Plan supersession is **not present** in `LaundryService.mark_batch_delayed`.
2. **Scenario 7 (Planned ≠ Worn):** `SPECIFICATION.md` claims proposed plans leave inventory unaltered. Mapped test only tests committed wear; negative assertion for proposed plan is **missing**.
3. **Scenario 13 (Multi-Session Day):** `SPECIFICATION.md` claims multi-session same-day wear -> assessment -> reuse lifecycle chain. Mapped test only tests session materialization; end-to-end multi-wear lifecycle chain is **unexecuted**.
4. **Scenario 14 (Event Injection Invalidation):** `SPECIFICATION.md` claims replan protects required presentation garments. Presentation protection in replacement plan is **untested**.
5. **Scenario 15 (Laundry Policy Change):** `SPECIFICATION.md` claims historical batches are immutable. Historical batch immutability across policy change is **untested**.
6. **Scenario 17 (Duplicate Wear-Event Retry):** `SPECIFICATION.md` claims wear counter increments exactly once. Test asserts `sqlite3.IntegrityError` but **fails to assert** post-retry state preservation.
7. **Scenario 18 (Atomic Laundry Return):** `SPECIFICATION.md` claims atomic return rollback on failure. Test wraps outer transaction rather than injecting internal failure in `record_item_return`.
8. **Scenario 26 (Modified Recommendation Logged):** `SPECIFICATION.md` claims `was_modified_before_wear = 1` is recorded. Positive logging test is **missing** (only negative rejection without flag is tested).
9. **Scenario 29 (Contextual & Cross-Context Repetition Isolation):** `SPECIFICATION.md` claims same-context repetition incurs penalty. Positive same-context penalty is **unasserted** in mapped test.
10. **Scenario 30 (Historical Wear Correction Rebuilds State):** `SPECIFICATION.md` claims affected plans are superseded. Plan supersession is **not present** in `correct_outfit_wear` or `void_outfit_wear`.
11. **Scenario 33 (Pairwise Evidence Captures Normalized Alternatives):** `SPECIFICATION.md` claims presentation ordering and inclusion types are captured. Queries and assertions for `PreferenceEvidenceAlternative` are **absent** from test.
12. **Scenario 34 (Recent Taste Shift Data Readiness):** `SPECIFICATION.md` claims alternative sets for rejections are stored. Rejection alternative preservation is **unasserted** in test.
13. **Scenario 38 (Multi-Angle Image Attachment):** `SPECIFICATION.md` claims candidate status becomes `SAME_GARMENT`. Candidate status is **unasserted** in test.
14. **Scenario 39 (Similar But Distinct Garments):** `SPECIFICATION.md` claims candidate status becomes `DIFFERENT_GARMENT`. Candidate status is **unasserted** in test.
15. **Scenario 40 (Duplicate Candidate Advisory & Non-Blocking):** `SPECIFICATION.md` claims pending candidates do not block planning or wear. Planning and wear behavior with pending candidate are **untested** (only advisory status tested).
16. **Scenario 41 (Terminal Outcomes Close Partial-Return Batch):** `SPECIFICATION.md` claims complete terminal outcomes matrix. Tested only on narrow fixture (`RETURNED_CLEAN` + `MISSING_RESOLVED`).
17. **Scenario 42 (Damaged Laundry Item Routes to REPAIR):** `SPECIFICATION.md` condition semantics for `RETURNED_DAMAGED` with `wash_confirmed = 0` remain unresolved between specification text and implementation.

---

## 6. Governed Specification Evolution Recommendations

When the owner authorizes specification updates:
1. **Promote `SPECIFICATION.md` to `rev 2.2.2`**:
   - Add DDL for `PlanWearAuthorizationReservation`, `GarmentSimilarityLink`, and `SchemaMigration`.
   - Add index DDL for `idx_one_consumed_authorization_per_wear`.
   - Update Part 3 terminal laundry outcomes and preflight validation to match audited code.
   - Replace obsolete `PlanningStateKey` with actual `PartialPlan` beam search architecture.
   - Accurately calibrate the 17 acceptance scenario verification claims.
2. **Preserve Baseline Integrity**:
   - Until authorized, retain `SPECIFICATION.md rev 2.2.1-final.1` as the immutable frozen audit baseline.
   - Anchor all errata and reconciliation notes to this document (`docs/audit/specification_errata_ledger.md`).
