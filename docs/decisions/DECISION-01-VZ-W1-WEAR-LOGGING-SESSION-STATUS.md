# Decision Record: VZ-W1 — Wear Logging Session Status Validation & Operation Modes

**Decision ID:** DECISION-01-VZ-W1  
**Status:** APPROVED / GOVERNED  
**Date:** 2026-10-25  
**Governing Authority:** Owner  
**Related Audit Item:** `VZ-W1`  
**Affects:** `core.wear_service.WearService.log_outfit_wear`

---

## 1. Context & Defect Statement

In baseline V0, `WearService.log_outfit_wear` did not validate `WearSession.status` prior to accepting wear events. It allowed logging wears against completed, cancelled, or skipped sessions without state-transition enforcement, bypassing lifecycle boundaries and relying solely on database uniqueness constraints.

---

## 2. Policy Decision

1. **Operation Mode Enforcement:**
   - **`NEW_WEAR` (Default Mode):**
     - Allowed **only** when `WearSession.status == 'PLANNED'`.
     - Upon successful wear logging, `WearSession.status` atomically transitions to `'COMPLETED'`.
     - Attempting `NEW_WEAR` on a `COMPLETED`, `CANCELLED`, or `SKIPPED` session raises `ValueError`.
   - **`CORRECTIVE_REPLACEMENT` Mode:**
     - Allowed **only** when `WearSession.status == 'COMPLETED'`.
     - Must be invoked via `WearService.correct_outfit_wear` with valid correction lineage (`superseded_by_id` linking the old `OutfitWearLog`).
     - Replaces the active wear log while preserving historical audit records.
   - **All Other Modes:**
     - Any unrecognized or unpermitted operation mode raises `ValueError`.

2. **Representation of Owner-Constructed / Manual Wears:**
   - Wears logged without a system recommendation are represented with:
     - `was_recommended = False`
     - `recommendation_plan_id = None`
     - `plan_assignment_id = None`
     - `source = "MANUAL_ENTRY"`
   - Such wears still require an existing `WearSession` in `'PLANNED'` status for that civil date.
   - If no session exists for the date, the session must first be materialized or explicitly created in `'PLANNED'` status before wear logging.

---

## 3. Verification Criteria

- Attempting `NEW_WEAR` on non-PLANNED sessions raises `ValueError`.
- Attempting `CORRECTIVE_REPLACEMENT` on non-COMPLETED sessions raises `ValueError`.
- Duplicate active wear retry against a PLANNED session triggers the database partial unique index (`sqlite3.IntegrityError`).
