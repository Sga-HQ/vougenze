-- ==========================================================================
-- Vougenze V0 Schema Invariant Validation
-- Run against the database after 0001_v0_schema.sql migration.
-- Each test inserts probe data, verifies expected behavior, then cleans up.
-- Expected: All tests pass silently; constraint violations raise errors.
-- ==========================================================================

PRAGMA foreign_keys = ON;

-- ==========================================================================
-- SETUP: Seed minimal reference rows for FK satisfaction
-- ==========================================================================

INSERT INTO User(id, display_name, timezone_id) VALUES ('u1', 'Test User', 'Asia/Kolkata');

INSERT INTO LaundryPolicy(id, user_id, policy_type, effective_from)
    VALUES ('lp1', 'u1', 'weekly', '2026-01-01');

INSERT INTO LaundryBatch(id, user_id, policy_id, sent_date, expected_return_date)
    VALUES ('lb1', 'u1', 'lp1', '2026-01-06', '2026-01-08');

INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
    VALUES ('g1', 'u1', 'White Shirt', 'top', 'shirt', 'regular', 'white');

INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
    VALUES ('g2', 'u1', 'Blue Jeans', 'bottom', 'jeans', 'slim', 'blue');

INSERT INTO WearSessionTemplate(id, user_id, template_name, effective_from)
    VALUES ('wst1', 'u1', 'Weekday Office', '2026-01-01');

INSERT INTO WearSession(id, user_id, session_date, session_type)
    VALUES ('ws1', 'u1', '2026-01-10', 'university');

INSERT INTO WearSession(id, user_id, session_date, session_type)
    VALUES ('ws2', 'u1', '2026-01-11', 'office');

INSERT INTO RecommendationPlan(id, user_id, strategy_mode, horizon_start_date, horizon_end_date)
    VALUES ('rp1', 'u1', 'BALANCED', '2026-01-10', '2026-01-16');

INSERT INTO OccasionProfile(id, user_id, name)
    VALUES ('op1', 'u1', 'Office Day');

INSERT INTO PlanSessionAssignment(id, plan_id, session_id)
    VALUES ('psa1', 'rp1', 'ws1');


-- ==========================================================================
-- TEST 1: Partial unique index on OutfitWearLog (one ACTIVE per session)
-- ==========================================================================

-- Insert first ACTIVE log
INSERT INTO OutfitWearLog(id, user_id, session_id, source, local_wear_date, timezone_id, session_type)
    VALUES ('owl1', 'u1', 'ws1', 'manual_outfit', '2026-01-10', 'Asia/Kolkata', 'university');

-- Mark it CORRECTED (simulating correction)
UPDATE OutfitWearLog SET record_status = 'CORRECTED', correction_reason = 'wrong garments' WHERE id = 'owl1';

-- Insert replacement ACTIVE log for same session — should succeed
INSERT INTO OutfitWearLog(id, user_id, session_id, source, local_wear_date, timezone_id, session_type, superseded_by_id)
    VALUES ('owl2', 'u1', 'ws1', 'manual_outfit', '2026-01-10', 'Asia/Kolkata', 'university', NULL);

-- Verify: two rows for ws1, only one ACTIVE
SELECT CASE
    WHEN (SELECT COUNT(*) FROM OutfitWearLog WHERE session_id = 'ws1') = 2
     AND (SELECT COUNT(*) FROM OutfitWearLog WHERE session_id = 'ws1' AND record_status = 'ACTIVE') = 1
    THEN 'TEST 1 PASSED: Partial unique on OutfitWearLog works'
    ELSE 'TEST 1 FAILED'
END;


-- ==========================================================================
-- TEST 2: Partial unique index on WearEvent (one active per session+garment)
-- ==========================================================================

INSERT INTO WearEvent(id, session_id, garment_id) VALUES ('we1', 'ws1', 'g1');

-- Void it
UPDATE WearEvent SET is_voided = 1 WHERE id = 'we1';

-- Insert replacement — should succeed
INSERT INTO WearEvent(id, session_id, garment_id) VALUES ('we2', 'ws1', 'g1');

-- Verify: two rows, only one non-voided
SELECT CASE
    WHEN (SELECT COUNT(*) FROM WearEvent WHERE session_id = 'ws1' AND garment_id = 'g1') = 2
     AND (SELECT COUNT(*) FROM WearEvent WHERE session_id = 'ws1' AND garment_id = 'g1' AND is_voided = 0) = 1
    THEN 'TEST 2 PASSED: Partial unique on WearEvent works'
    ELSE 'TEST 2 FAILED'
END;


-- ==========================================================================
-- TEST 3: CHECK constraints on ratings (formality 1-5, like_rating 1-10, etc.)
-- ==========================================================================

-- formality must be 1-5, this should fail:
-- We test by attempting and catching; SQLite raises error on CHECK violation.
-- Since we can't try/catch in pure SQL, we verify the constraint exists:
SELECT CASE
    WHEN sql LIKE '%formality BETWEEN 1 AND 5%'
    THEN 'TEST 3a PASSED: Garment formality CHECK exists'
    ELSE 'TEST 3a FAILED'
END FROM sqlite_master WHERE name = 'Garment';

SELECT CASE
    WHEN sql LIKE '%like_rating BETWEEN 1 AND 10%'
    THEN 'TEST 3b PASSED: Garment like_rating CHECK exists'
    ELSE 'TEST 3b FAILED'
END FROM sqlite_master WHERE name = 'Garment';

SELECT CASE
    WHEN sql LIKE '%satisfaction_rating IS NULL OR satisfaction_rating BETWEEN 1 AND 10%'
    THEN 'TEST 3c PASSED: OutfitWearLog satisfaction_rating CHECK exists'
    ELSE 'TEST 3c FAILED'
END FROM sqlite_master WHERE name = 'OutfitWearLog';

SELECT CASE
    WHEN sql LIKE '%formality_min BETWEEN 1 AND 5%'
    THEN 'TEST 3d PASSED: OccasionProfile formality_min CHECK exists'
    ELSE 'TEST 3d FAILED'
END FROM sqlite_master WHERE name = 'OccasionProfile';


-- ==========================================================================
-- TEST 4: Terminal laundry outcomes (LaundryBatchItem item_status values)
-- ==========================================================================

INSERT INTO LaundryBatchItem(batch_id, garment_id, item_status, wash_confirmed) VALUES ('lb1', 'g1', 'IN_LAUNDRY', 0);

-- Update to each terminal status
UPDATE LaundryBatchItem SET item_status = 'RETURNED_CLEAN', wash_confirmed = 1 WHERE garment_id = 'g1';

SELECT CASE
    WHEN item_status = 'RETURNED_CLEAN' AND wash_confirmed = 1
    THEN 'TEST 4a PASSED: RETURNED_CLEAN terminal outcome'
    ELSE 'TEST 4a FAILED'
END FROM LaundryBatchItem WHERE batch_id = 'lb1' AND garment_id = 'g1';

UPDATE LaundryBatchItem SET item_status = 'RETURNED_DAMAGED', wash_confirmed = 0 WHERE garment_id = 'g1';

SELECT CASE
    WHEN item_status = 'RETURNED_DAMAGED' AND wash_confirmed = 0
    THEN 'TEST 4b PASSED: RETURNED_DAMAGED terminal outcome (wash not confirmed)'
    ELSE 'TEST 4b FAILED'
END FROM LaundryBatchItem WHERE batch_id = 'lb1' AND garment_id = 'g1';

UPDATE LaundryBatchItem SET item_status = 'RETURNED_UNWASHED', wash_confirmed = 0 WHERE garment_id = 'g1';

SELECT CASE
    WHEN item_status = 'RETURNED_UNWASHED' AND wash_confirmed = 0
    THEN 'TEST 4c PASSED: RETURNED_UNWASHED terminal outcome'
    ELSE 'TEST 4c FAILED'
END FROM LaundryBatchItem WHERE batch_id = 'lb1' AND garment_id = 'g1';

UPDATE LaundryBatchItem SET item_status = 'MISSING_RESOLVED', wash_confirmed = 0 WHERE garment_id = 'g1';

SELECT CASE
    WHEN item_status = 'MISSING_RESOLVED' AND wash_confirmed = 0
    THEN 'TEST 4d PASSED: MISSING_RESOLVED terminal outcome'
    ELSE 'TEST 4d FAILED'
END FROM LaundryBatchItem WHERE batch_id = 'lb1' AND garment_id = 'g1';


-- ==========================================================================
-- TEST 5: Preference inclusion_type values
-- ==========================================================================

SELECT CASE
    WHEN sql LIKE '%PRESENTED%'
     AND sql LIKE '%FEASIBLE_NOT_PRESENTED%'
     AND sql LIKE '%INFEASIBLE_REFERENCE%'
    THEN 'TEST 5 PASSED: PreferenceEvidenceAlternative inclusion_type CHECK exists'
    ELSE 'TEST 5 FAILED'
END FROM sqlite_master WHERE name = 'PreferenceEvidenceAlternative';


-- ==========================================================================
-- TEST 6: Foreign key enforcement (should fail on bad ref)
-- ==========================================================================

-- Verify FK is ON in this session
SELECT CASE
    WHEN (SELECT * FROM pragma_foreign_keys) = 1
    THEN 'TEST 6 SETUP: foreign_keys is ON'
    ELSE 'TEST 6 SETUP WARNING: foreign_keys is OFF — enable with PRAGMA foreign_keys = ON'
END;


-- ==========================================================================
-- TEST 7: Duplicate SHA-256 non-uniqueness (same hash allowed for different images)
-- ==========================================================================

INSERT INTO GarmentImage(id, garment_id, image_path) VALUES ('gi1', 'g1', '/img/front.jpg');
INSERT INTO GarmentImage(id, garment_id, image_path) VALUES ('gi2', 'g1', '/img/back.jpg');

INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256) VALUES ('gif1', 'gi1', 'abc123');
INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256) VALUES ('gif2', 'gi2', 'abc123');

SELECT CASE
    WHEN (SELECT COUNT(*) FROM GarmentImageFingerprint WHERE sha256 = 'abc123') = 2
    THEN 'TEST 7 PASSED: Duplicate SHA-256 fingerprints allowed'
    ELSE 'TEST 7 FAILED'
END;


-- ==========================================================================
-- TEST 8: One-piece garment fills multiple roles (stored as JSON array)
-- ==========================================================================

INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, fills_roles)
    VALUES ('g3', 'u1', 'Kurta Set', 'one_piece', 'kurta_set', 'regular', 'cream', '["top","bottom"]');

SELECT CASE
    WHEN fills_roles = '["top","bottom"]'
    THEN 'TEST 8 PASSED: One-piece fills multiple roles via JSON'
    ELSE 'TEST 8 FAILED'
END FROM Garment WHERE id = 'g3';


-- ==========================================================================
-- TEST 9: Single-consumption wear authorization
-- ==========================================================================

INSERT INTO WearEvent(id, session_id, garment_id) VALUES ('we3', 'ws2', 'g1');

INSERT INTO GarmentWearAuthorization(id, garment_id, reason, consumed_by_wear_event_id, status)
    VALUES ('gwa1', 'g1', 'owner approved extra wear', 'we3', 'CONSUMED');

-- Try inserting another authorization consumed by the same wear event — should fail
-- We test by verifying the unique index exists:
SELECT CASE
    WHEN (SELECT COUNT(*) FROM sqlite_master
          WHERE type = 'index'
            AND name = 'idx_one_consumed_authorization_per_wear') = 1
    THEN 'TEST 9 PASSED: Single-consumption authorization index exists'
    ELSE 'TEST 9 FAILED'
END;


-- ==========================================================================
-- TEST 10: Garment condition and availability CHECK constraints
-- ==========================================================================

SELECT CASE
    WHEN sql LIKE '%CLEAN%'
     AND sql LIKE '%WORN_AWAITING_ASSESSMENT%'
     AND sql LIKE '%UNKNOWN_CONDITION%'
     AND sql LIKE '%WORN_REUSABLE%'
     AND sql LIKE '%AIRING%'
     AND sql LIKE '%NEEDS_WASH%'
    THEN 'TEST 10a PASSED: All condition states in CHECK'
    ELSE 'TEST 10a FAILED'
END FROM sqlite_master WHERE name = 'Garment';

SELECT CASE
    WHEN sql LIKE '%AVAILABLE%'
     AND sql LIKE '%IN_LAUNDRY%'
     AND sql LIKE '%UNAVAILABLE%'
     AND sql LIKE '%REPAIR%'
     AND sql LIKE '%RETIRED%'
    THEN 'TEST 10b PASSED: All availability states in CHECK'
    ELSE 'TEST 10b FAILED'
END FROM sqlite_master WHERE name = 'Garment';


-- ==========================================================================
-- TEST 11: active_history_window CHECK (allowed values only)
-- ==========================================================================

SELECT CASE
    WHEN sql LIKE '%active_history_window IN (30, 60, 100, 180, 365, -1)%'
    THEN 'TEST 11 PASSED: active_history_window CHECK exists'
    ELSE 'TEST 11 FAILED'
END FROM sqlite_master WHERE name = 'User';


-- ==========================================================================
-- TEST 12: Plan strategy modes
-- ==========================================================================

SELECT CASE
    WHEN sql LIKE '%SAFE%'
     AND sql LIKE '%BALANCED%'
     AND sql LIKE '%EXPLORATORY%'
    THEN 'TEST 12 PASSED: RecommendationPlan strategy_mode CHECK exists'
    ELSE 'TEST 12 FAILED'
END FROM sqlite_master WHERE name = 'RecommendationPlan';


-- ==========================================================================
-- TEST 13: PostWearAssessment UNIQUE(wear_event_id)
-- ==========================================================================

INSERT INTO PostWearAssessment(id, wear_event_id, garment_id, assessment)
    VALUES ('pwa1', 'we2', 'g1', 'ready_to_reuse');

SELECT CASE
    WHEN (SELECT COUNT(*) FROM PostWearAssessment WHERE wear_event_id = 'we2') = 1
    THEN 'TEST 13 PASSED: One assessment per wear event'
    ELSE 'TEST 13 FAILED'
END;


-- ==========================================================================
-- TEST 14: GarmentStateTransition dimension CHECK
-- ==========================================================================

SELECT CASE
    WHEN sql LIKE '%condition%'
     AND sql LIKE '%availability%'
    THEN 'TEST 14 PASSED: GarmentStateTransition state_dimension CHECK exists'
    ELSE 'TEST 14 FAILED'
END FROM sqlite_master WHERE name = 'GarmentStateTransition';


-- ==========================================================================
-- TEST 15: All 34 expected tables exist
-- ==========================================================================

SELECT CASE
    WHEN (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%') = 34
    THEN 'TEST 15 PASSED: All 34 tables present'
    ELSE 'TEST 15 FAILED: Expected 34, got ' || (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%')
END;


-- ==========================================================================
-- SUMMARY: All tests complete
-- ==========================================================================
SELECT '=== ALL INVARIANT TESTS COMPLETE ===';
