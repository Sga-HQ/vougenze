# Vougenze Lifecycle Guard-Action-Transition Specification

**Document Type:** RESEARCH & GOVERNED TEST-ORACLE SPECIFICATION  
**Scope:** Lifecycle state transitions across Condition, Availability, Temporal Lock, Reservation, and Session dimensions  
**Governing Principle:** State transitions are defined by explicit guard-action-mutation invariants. SQLite and Vougenze domain services remain the sole runtime authority.

---

## 1. Multi-Dimensional Orthogonal State Model

A physical garment or lifecycle entity is situated within orthogonal state dimensions:
1. **Condition State:** `CLEAN`, `WORN_REUSABLE`, `WORN_AWAITING_ASSESSMENT`, `AIRING`, `NEEDS_WASH`, `UNKNOWN_CONDITION`
2. **Availability State:** `AVAILABLE`, `IN_LAUNDRY`, `REPAIR`, `UNAVAILABLE`, `RETIRED`
3. **Temporal Reuse Lock:** `LOCKED` (under active wear or cooldown/airing interval), `UNLOCKED`
4. **Reservation State:** `NONE`, `RESERVED_PROPOSED`, `RESERVED_CONFIRMED`, `CONSUMED`
5. **Session Lifecycle:** `PLANNED`, `COMPLETED`, `SKIPPED`, `CANCELLED` (with audit-trail reopening)
6. **Laundry Batch Lifecycle:** `IN_LAUNDRY`, `DELAYED`, `PARTIAL_RETURN`, `RETURNED_CONFIRMED`

---

## 2. Governance Status Taxonomy & Semantic Separation

To ensure candidate semantics are never conflated with authoritative production code, every lifecycle operation is classified under one of four explicit implementation categories:

- **`CURRENT_EXECUTABLE`**: Code is fully implemented in active services (`core/`), matches schema constraints, and executes in the current test suite.
- **`CONFIRMED_DEFECT`**: Current code contains an authorized, reproducible defect (e.g., missing preflight guard) verified by counterexample tests.
- **`OPEN_DECISION` / `SPECIFICATION_DECISION_REQUIRED`**: Architectural or lifecycle behavior has an authorized direction from triage, but requires frozen specification contracts, migration governance, and formal acceptance tests prior to implementation.
- **`NOT_IMPLEMENTED`**: Target operation is part of the governed candidate lifecycle model, but lacks a dedicated service-level entry point in `core/`.

> [!IMPORTANT]
> **Key Distinctions:**
> 1. **VZ-W1 (`log_ordinary_wear` pre-status):** Currently an `OPEN_DECISION`. Current service marks session `COMPLETED` regardless of pre-state. The governed decision is strict `PLANNED`-only wear entry, requiring explicit reopen after voiding.
> 2. **VZ-W2 (`log_modified_wear` reason guard):** Confirmed defect. Reproducible counterexample verified (`blank_reason=None`). Repair is pending narrow authorization.
> 3. **Laundry Delay Plan Invalidation:** Currently `OPEN_DECISION` / `NOT_IMPLEMENTED`. Vocabulary `LAUNDRY_DELAYED` is not in schema `0001_v0_schema.sql` `invalidation_reason` CHECK constraint. Plan coordinator consequence is absent from `mark_batch_delayed`.
> 4. **Session Reopen (`reopen_session`):** `NOT_IMPLEMENTED`. Governed candidate operation; no production service method currently exists.

---

## 3. Comprehensive Transition Matrix

| # | Operation | Implementation Status | Entity | Pre-State | Guard | Authorizing Actor | Mutation | Rollback Behavior | Governed Expected Behavior | Current Executable Behavior |
|:---|:---|:---:|:---|:---|:---|:---|:---|:---|:---|:---|
| **1** | `reserve_authorization` | `CURRENT_EXECUTABLE` | `GarmentWearAuthorization` / `PlanWearAuthorizationReservation` | Auth: `AVAILABLE`, unreserved, unconsumed | Garment belongs to user; plan in `PROPOSED`/`RESERVED`; auth matches garment | Planner / Owner | `INSERT PlanWearAuthorizationReservation` | Rollback removes reservation row | Reservation mutually excluded from competing plans | Aligned in planner; generator lacks scope (VZ-P6) |
| **2** | `release_reservation` | `CURRENT_EXECUTABLE` | `PlanWearAuthorizationReservation` | Auth: `AVAILABLE`, reserved | Plan superseded/expired/cancelled, or wear logged without using auth | Service / Owner | `DELETE FROM PlanWearAuthorizationReservation` | Rollback restores reservation | Frees operational hold; wear count unchanged | Implemented: released on plan invalidation or wear commit |
| **3** | `log_ordinary_wear` | `OPEN_DECISION` (VZ-W1) | `OutfitWearLog`, `WearEvent`, `Garment`, `WearSession` | Garment: `AVAILABLE`, `CLEAN`/`WORN_REUSABLE`; Session: `PLANNED` | Exact role correspondence with recommended plan (or direct wear); hard limit resolved | Owner | `INSERT OutfitWearLog` (`was_modified=0`), `INSERT WearEvent`, increment wears, session $\to$ `COMPLETED` | Atomic rollback reverts all mutations | Must reject wear entry on `CANCELLED` or already `COMPLETED` sessions | Session marked `COMPLETED` without validating pre-status |
| **4** | `log_modified_wear` | `CONFIRMED_DEFECT` (VZ-W2) | `OutfitWearLog`, `WearEvent`, `Garment`, `WearSession` | Same as ordinary wear; logged roles differ from recommended plan | `was_modified_before_wear = 1` AND non-empty `modification_reason` | Owner | `INSERT OutfitWearLog` (`was_modified=1`, reason stored) | Atomic rollback reverts all mutations | Reject blank or whitespace `modification_reason` | Permits `was_modified_before_wear=1` with `modification_reason=None` |
| **5** | `post_wear_assess` | `CURRENT_EXECUTABLE` | `Garment`, `GarmentCondition` | Garment: `WORN_AWAITING_ASSESSMENT` | Valid assessment choice (`ready_to_reuse`, `needs_airing`, `needs_wash`, etc.) | Owner | Condition transitions per choice; temporal lock set if airing | Rollback reverts condition and removes assessment | Update condition without resetting wear count | Fully implemented in `StateEngine.record_post_wear_assessment` |
| **6** | `partial_clean` | `CURRENT_EXECUTABLE` | `Garment`, `WashEvent` | Condition: non-clean; Availability: `AVAILABLE` | Scope is `PARTIAL_CLEAN`; valid cleaning method | Owner | Condition $\to$ `WORN_REUSABLE` or `CLEAN`; wear count **UNALTERED** ($w' = w$) | Rollback restores condition and removes wash event | Wear count accumulation strictly preserved | Implemented: `record_wash_event` preserves wear counter |
| **7** | `confirmed_full_wash` | `CURRENT_EXECUTABLE` | `Garment`, `WashEvent` | Condition: any; Availability: `AVAILABLE` or `IN_LAUNDRY` | Scope is `FULL_CLEAN`; confirmed wash | Owner / Laundry | Condition $\to$ `CLEAN`; wear count **RESET** ($w' = 0$) | Rollback restores wear counter and condition | Full reset of confirmed-wash wear accumulation | Implemented in `StateEngine.record_wash_event` |
| **8** | `dispatch_laundry` | `CURRENT_EXECUTABLE` | `LaundryBatch`, `LaundryBatchItem`, `Garment` | Garments exist, belong to user, not in laundry/retired | Active batch; preflight validation on all items | Owner | Garment availability $\to$ `IN_LAUNDRY`; batch created | Rollback restores `AVAILABLE` and removes batch | Complete preflight atomicity across all garments | Implemented in `LaundryService.send_laundry_batch` |
| **9** | `delay_laundry` | `OPEN_DECISION` (Scenario 4) | `LaundryBatch`, `RecommendationPlan` | Batch in `IN_LAUNDRY` or `PARTIAL_RETURN` | Valid new return date specified; batch active | Laundry / Owner | Batch status $\to$ `DELAYED`; dependent plans superseded with `LAUNDRY_DELAYED` | Rollback restores return date and batch status | Breached active plans superseded; reservations freed | Updates batch status; does NOT supersede plans |
| **10** | `record_laundry_return` | `CURRENT_EXECUTABLE` | `LaundryBatchItem`, `Garment`, `LaundryBatch` | Batch in laundry/delayed; item `IN_LAUNDRY` | Terminal item status (`RETURNED_CLEAN`, `DAMAGED`, etc.) | Laundry / Owner | Availability updated (`AVAILABLE`, `REPAIR`); if clean, wash recorded | Rollback reverts item, availability, and wash | Terminal item consistency enforced | Implemented in `LaundryService.record_item_return` |
| **11** | `void_wear` | `CURRENT_EXECUTABLE` | `OutfitWearLog`, `WearEvent`, `Garment` | `OutfitWearLog`: `ACTIVE` | Non-empty `void_reason`; belongs to user | Owner | Log `VOIDED`, `WearEvents` voided, auths released, condition replayed | Rollback restores active wear log | Replay condition and decrement wear count | Implemented in `WearService.void_outfit_wear` |
| **12** | `correct_wear` | `CURRENT_EXECUTABLE` | `OutfitWearLog`, `WearEvent`, `Garment` | Previous log: `ACTIVE` | Non-empty correction reason; valid replacement outfit | Owner | Voids previous log, inserts replacement log, replays state | Rollback restores previous active wear log | Immutable correction audit trail preserved | Implemented in `WearService.correct_outfit_wear` |
| **13** | `retire_garment` | `CURRENT_EXECUTABLE` | `Garment`, `RecommendationPlan` | Availability: non-retired | Garment belongs to user; active transaction | Owner | Availability $\to$ `RETIRED`; invalidates active dependent plans | Rollback restores prior availability | Permanent exclusion from candidate generation | Implemented in `StateEngine` and `ContinuationPlanner` |
| **14** | `skip_session` | `NOT_IMPLEMENTED` | `WearSession`, `RecommendationPlan` | Session in `PLANNED` | Session belongs to user; no committed active wear log | Owner | Session $\to$ `SKIPPED`; supersede active plan; free holds | Rollback restores `PLANNED` | Bypass session without physical wear; free holds | Schema has enum value; no dedicated service method |
| **15** | `cancel_session` | `NOT_IMPLEMENTED` | `WearSession`, `RecommendationPlan` | Session in `PLANNED` | Session belongs to user | Owner | Session $\to$ `CANCELLED`; supersede plan; free holds | Rollback restores `PLANNED` | Formal cancellation; release reservations | Schema has enum value; no dedicated service method |
| **16** | `reopen_session` | `NOT_IMPLEMENTED` | `WearSession` | Session `COMPLETED` (post-void) or `CANCELLED` | Explicit owner authorization with reason | Owner | Session $\to$ `PLANNED`; audited in event ledger | Rollback leaves session in previous status | Re-enter `PLANNED` before logging replacement wear | Proposed candidate operation; no service method |

---

## 4. Invariant Verification Mapping

| Invariant | Implementation Mechanism | Test Assertion Location | Test Type | Current Result |
|:---|:---|:---|:---|:---:|
| **Physical Item Unity** | Single physical `WearEvent` per unique garment per wear log | `test_lifecycle_state_machine.py` (`invariant_physical_item_unity`) | Stateful Hypothesis invariant | **PASS** (Zero violations in 25 traces) |
| **Cleaning Capacity Semantics** | Partial clean preserves wear count; full clean resets to 0 | `test_lifecycle_state_machine.py` (`invariant_cleaning_capacity_semantics`) | Stateful Hypothesis invariant | **PASS** (Zero violations in 25 traces) |
| **Reservation Mutual Exclusion** | Authorizations held by at most one active plan | `test_lifecycle_state_machine.py` (`invariant_reservation_mutual_exclusion`) | Stateful Hypothesis invariant | **PASS** (Zero violations in 25 traces) |
| **Modification Reason Guard (VZ-W2)** | `was_modified_before_wear = 1` requires non-empty reason | `test_boundary_counterexamples.py` (`test_vzw2_modification_reason_counterexample`) | Targeted Property Attack | **EXPECTED FAILURE** (Confirmed Defect) |
| **Laundry Delay Plan Supersession** | Delay past session supersedes plan with `LAUNDRY_DELAYED` | `test_boundary_counterexamples.py` (`test_laundry_delay_plan_consequence_counterexample`) | Targeted Boundary Attack | **EXPECTED FAILURE** (Consequence Missing) |
