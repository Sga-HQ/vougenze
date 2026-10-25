# Decision Record: DECISION-03 — Continuation Planner Lexicographic Ranking Order

**Decision ID:** DECISION-03-PLANNER-RANKING  
**Status:** APPROVED / GOVERNED  
**Date:** 2026-10-25  
**Governing Authority:** Owner  
**Affects:** `core.planner.ContinuationPlanner.plan_ranking_key`, `tests.test_planner.TestContinuationPlanner`

---

## 1. Context & Objective Order Adjustment

During multi-session continuation planning, competing beam search partial plans are ordered by a lexicographic ranking tuple.

In the initial implementation, `diversity_deficit` was placed at coordinate 6 (before `min_high_quality`, `total_quality`, and `repetition_burden`):
```python
# Previous tuple:
(
    uncovered_high,
    uncovered_normal,
    uncovered_low,
    uncertain_dependencies,
    fallback_assignments,
    diversity_deficit,
    -min_high_quality,
    -total_quality,
    repetition_burden,
)
```

This placed diversity progress above outfit quality and repetition minimization, allowing exploratory diversity targets to outrank objectively higher-quality, lower-repetition plans.

---

## 2. Policy Decision

1. **Protect Quality and Repetition Before Diversity Deficit:**
   - The lexicographic ranking key is adjusted to place `diversity_deficit` at the final coordinate:
     ```python
     high_floor = p.min_high_quality if p.min_high_quality is not None else 1.0
     return (
         p.uncovered_high,
         p.uncovered_normal,
         p.uncovered_low,
         p.uncertain_dependencies,
         p.fallback_count,
         -high_floor,
         -p.total_quality,
         p.total_repetition,
         diversity_deficit,
     )
     ```
   - Rationale: High-importance quality floor, overall plan quality, and repetition penalty minimization are primary objectives. Diversity is a secondary exploration mechanism and must not compromise baseline quality or force unwanted repetition.

2. **Nullable High-Quality Handling:**
   - When a planning horizon contains zero HIGH-importance sessions, `min_high_quality` is `None` rather than an arbitrary default, and `high_floor` defaults to `1.0`.

3. **Decoupling Exploration from Persisted Quality:**
   - Exploratory novelty bonuses are decoupled from persisted candidate quality scores, ensuring that stored evaluation scores reflect deterministic aesthetic quality without heuristic inflation.

---

## 3. Verification Criteria

- `tests.test_planner.TestContinuationPlanner.test_exact_search_oracle_comparison_and_regret` evaluates beam search against exhaustive enumeration on the 2-day multi-session fixture, verifying lexicographic tuple equality `(0, 0, 0, 0, 0, -0.745, -1.49, 0.0, 0)` with zero search regret.
