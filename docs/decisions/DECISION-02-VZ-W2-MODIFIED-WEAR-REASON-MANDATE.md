# Decision Record: VZ-W2 — Mandatory Non-Blank Reason on Modified Wear

**Decision ID:** DECISION-02-VZ-W2  
**Status:** APPROVED / GOVERNED  
**Date:** 2026-10-25  
**Governing Authority:** Owner  
**Related Audit Item:** `VZ-W2`  
**Affects:** `core.wear_service.WearService.log_outfit_wear`

---

## 1. Context & Defect Statement

In baseline V0, `WearService.log_outfit_wear` accepted `was_modified_before_wear = True` without requiring an accompanying explanation, allowing `None`, empty strings, or whitespace-only strings to be persisted. This violated the data readiness contract for future preference learning by recording modification events without recording the owner's rationale.

---

## 2. Policy Decision

1. **Mandatory Non-Empty Rationale:**
   - Whenever `was_modified_before_wear` evaluates to `True`, `modification_reason` is strictly mandatory.
   - Whitespace trimming/normalization must be applied:
     ```python
     if was_modified_before_wear and not (modification_reason or "").strip():
         raise ValueError("Modified wear requires a non-empty modification_reason")
     ```
   - Passing `None`, `""`, `"   "`, `"\t\n"`, or any whitespace-only string raises `ValueError`.

2. **Unmodified Wears:**
   - When `was_modified_before_wear` is `False` (or omitted), `modification_reason` may be `None` or omitted.

---

## 3. Verification Criteria

- Passing `was_modified_before_wear=True` with `modification_reason in (None, "", "   ", "\t\n")` raises `ValueError`.
- Passing a valid non-empty explanation succeeds and persists the reason in `OutfitWearLog.modification_reason`.
- Deterministic regression test: `tests.test_wear_service.TestWearService.test_vzw2_modified_wear_requires_nonblank_reason`.
