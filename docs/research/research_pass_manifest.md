# Research Pass Manifest: Hypothesis Lifecycle & Defect Boundary Checkpoint

**Document Type:** RESEARCH REPRODUCIBILITY & TEST-CHECKPOINT MANIFEST  
**Pass Identifier:** `RESEARCH-PASS-01-HYPOTHESIS-LIFECYCLE`  
**Base Candidate Baseline:** `cf73ea0b95d029a90129f1a7524b2013e056c1c6` (VERIFIED CANDIDATE BASELINE)  
**Governance Posture:** `AUDIT STATUS: OPEN / CLOSURE NOT AUTHORIZED`  
**Implementation Authorization:** `TEST-ONLY HYPOTHESIS RESEARCH PASS`

---

## 1. Parent Verified Candidate Baseline Provenance

The parent candidate state was cryptographically verified prior to this research pass and remained untouched throughout:

- **Git Base Commit:** `cf73ea0b95d029a90129f1a7524b2013e056c1c6`
- **Staged Patch Hash:** `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` (0 bytes)
- **Tracked Patch Hash (Unstaged):** `b9086b7074ee090585921a1b2a948b3449431231cdcbf20b6e7e64b1a7de7541` (138,929 bytes across 15 files)
- **Frozen Audit Register SHA-256:** `ce5a2089ce48a72e1390f7d9c314f79351987ba26eff22c8455e306629dcd655` (50,393 bytes, `docs/audit/v0_audit_register.md`)
- **Frozen Errata Ledger SHA-256:** `65e88952a7754752c31c9f11b2278340336d06a44463926cbaccdb71f9962a33` (13,398 bytes, `docs/audit/specification_errata_ledger.md`)
- **Frozen Untracked Manifest SHA-256:** `eb082c8ec682f5a3f0de7be4124223b34fe30ae871487dc6cf6cc34578a26991` (777 bytes, `docs/audit/untracked_manifest.txt`)

---

## 2. Environment & Dependency Governance

- **Python Runtime:** `Python 3.14.6` (`AMD64`, Windows 11)
- **Installation Environment:** Compatibility probe completed in global Python environment (`C:\Users\mshre\AppData\Local\Programs\Python\Python314\Lib\site-packages`).
- **Direct Test Dependency:** `hypothesis==6.170.9` (License: `MPL-2.0`)
- **Resolved Transitive Dependency:** `sortedcontainers==2.4.0` (License: `Apache 2.0`)
- **Dependency Lock Policy:** Test-only direct pinning with explicit transitive resolution documented in `requirements-test.txt`. Zero production dependencies modified.

---

## 3. Research Pass File Artifacts & Cryptographic Hashes

| File Path | Byte Length | SHA-256 Checksum | Classification |
|:---|:---:|:---|:---:|
| `requirements-test.txt` | 158 B | `dc2465cdbdbdd1ef5d4a0e71fb59f01f9c1978ddb8267f0ee56aa1399023e348` | Pinned Test Dependency Spec |
| `docs/research/lifecycle_transition_table.md` | 11,328 B | `c6a6e443a428e1080bb70d49dc5d53de70cc7b62d01b508fa8664a70ff10c11c` | Guard-Action Specification |
| `tests/strategies/__init__.py` | 79 B | `c9954e475585e05429e153e2b592726903cfc89e2e981e0c502399bb99eb282d` | Package Init |
| `tests/strategies/domain_strategies.py` | 2,408 B | `bc8ee2869ba66bec8987704611bed8e925ff576a6f64ec8dff66ce4eb6446d7c` | Hypothesis Domain Generators |
| `tests/stateful/__init__.py` | 69 B | `b0dee6be1cf0e689dc9be32482a974db06061828ec52e3282f2675f07b28c111` | Package Init |
| `tests/stateful/test_lifecycle_state_machine.py` | 19,815 B | `5ee3efff06875eb23205f7fdc652db75c3cd4415d2d25697eababd314580b962` | Stateful Lifecycle Invariant Runner |
| `tests/stateful/test_boundary_counterexamples.py` | 8,396 B | `df5af5a1ac82d05d9f8ae81e656259a0d642aa6f38472307818e459599a13710` | Targeted Expected-Failure Probes |

---

## 4. Hypothesis Engine Configuration & Rule Execution Accounting

### 4.1 State Machine Search Configuration
- `max_examples`: 25 traces
- `stateful_step_count`: 35 steps per trace
- `deadline`: None (disabled to accommodate in-memory SQLite schema migrations per run)
- `verbosity`: `Verbosity.normal`
- `suppress_health_check`: `[HealthCheck.too_slow, HealthCheck.filter_too_much]`
- `database / example store`: `.hypothesis/` local cache
- `derandomization`: Default PRNG with Hypothesis shrinking

### 4.2 Per-Rule Execution Accounting (Single Test Run Profile)
Across a complete test run, rule invocations were tracked as follows:
- `add_garment`: 444 invocations
- `create_session`: 1,114 invocations
- `grant_authorization`: 314 invocations
- `log_wear`: 322 invocations
- `log_modified_wear`: 591 invocations
- `post_wear_assess`: 253 invocations
- `partial_clean`: 320 invocations
- `full_wash`: 770 invocations
- `dispatch_laundry`: 355 invocations
- `delay_laundry`: 0 (preconditions unexercised in trace sampling)
- `return_laundry`: 0 (preconditions unexercised in trace sampling)
- `void_wear`: 60 invocations
- `retire_garment`: 278 invocations

### 4.3 Invariant Bounds Verified
The state machine proved that under the configured random step sequences:
1. `invariant_physical_item_unity`: Zero multi-role duplicates in `WearEvent`.
2. `invariant_cleaning_capacity_semantics`: Zero wear count resets from partial cleans; full washes reset counter to 0.
3. `invariant_reservation_mutual_exclusion`: Zero cross-plan authorization reservation conflicts.

---

## 5. Post-Rebase Test Discovery Results (Rebased on main a38827d)

Command executed:
```bash
python -m unittest discover tests
```

**Outcome:**
- **Total Tests Discovered:** 112
- **Passing Tests:** 111
- **Expected Failures (`x`):** 1
  - `test_laundry_delay_plan_consequence_counterexample`: Reproduces missing plan invalidation (`RecommendationPlan` remains `PROPOSED` despite laundry delay). Left explicitly unresolved.
- **Converted Regression (Passing):** 1
  - `test_vzw2_modification_reason_counterexample`: Formerly expected failure; converted to passing deterministic regression following the VZ-W2 repair in `wear_service.py`.
- **Unexpected Failures (`F`):** 0
- **Unexpected Successes (`u`):** 0
- **Total Execution Time:** ~8.2 seconds
- **Discovery Status:** `OK (expected failures=1)`

---

## 6. Post-Change Git Working Tree Status

```text
On branch master
Changes not staged for commit:
	modified:   core/candidate_generator.py
	modified:   core/db.py
	modified:   core/evidence_service.py
	modified:   core/intake_service.py
	modified:   core/planner.py
	modified:   core/scorers.py
	modified:   core/session_service.py
	modified:   core/simulator.py
	modified:   core/wear_service.py
	modified:   tests/test_acceptance_scenarios.py
	modified:   tests/test_db.py
	modified:   tests/test_invariants_81_100.py
	modified:   tests/test_p0_p1_invariants.py
	modified:   tests/test_planner.py
	modified:   tests/test_schema_invariants.sql

Untracked files:
	.hypothesis/
	docs/research/
	requirements-test.txt
	tests/stateful/
	tests/strategies/

no changes added to commit (use "git add" and/or "git commit -a")
```
*(Note: Tracked unstaged diff hash remains bit-for-bit identical to candidate hash `b9086b7074ee090585921a1b2a948b3449431231cdcbf20b6e7e64b1a7de7541`.)*
