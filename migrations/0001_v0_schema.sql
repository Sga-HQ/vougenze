-- ==========================================================================
-- Vougenze V0 Schema Migration
-- Specification revision: 2.2.1-final.1
-- ==========================================================================
-- This migration must be executed as a single unit against an empty database.
-- Table creation order resolves all forward references.
-- ==========================================================================

PRAGMA foreign_keys = ON;

-- ==========================================================================
-- 1. Independent foundation tables (no foreign key dependencies)
-- ==========================================================================

CREATE TABLE User (
    id                      TEXT PRIMARY KEY,
    display_name            TEXT NOT NULL,
    climate_zone            TEXT,
    timezone_id             TEXT NOT NULL DEFAULT 'UTC',
    active_history_window   INTEGER NOT NULL DEFAULT 100 CHECK (active_history_window IN (30, 60, 100, 180, 365, -1)),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ==========================================================================
-- 2. Laundry infrastructure (needed by Garment forward reference)
-- ==========================================================================

CREATE TABLE LaundryPolicy (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES User(id),
    policy_type         TEXT NOT NULL,
    handover_weekday    INTEGER CHECK (handover_weekday BETWEEN 0 AND 6),
    return_weekday      INTEGER CHECK (return_weekday BETWEEN 0 AND 6),
    interval_days       INTEGER,
    dirty_threshold     INTEGER,
    effective_from      TEXT NOT NULL,
    effective_until     TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (effective_until IS NULL OR effective_until >= effective_from)
);

CREATE TABLE LaundryBatch (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES User(id),
    policy_id               TEXT REFERENCES LaundryPolicy(id),
    sent_date               TEXT NOT NULL,
    expected_return_date    TEXT NOT NULL,
    confirmed_return_date   TEXT,
    status                  TEXT NOT NULL DEFAULT 'IN_LAUNDRY' CHECK (
        status IN ('IN_LAUNDRY', 'RETURNED_CONFIRMED', 'PARTIAL_RETURN', 'DELAYED')
    ),
    wash_method             TEXT DEFAULT 'laundry_service',
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (expected_return_date >= sent_date)
);

-- ==========================================================================
-- 3. Garment core (references LaundryBatch, GarmentCondition forward-ref)
-- ==========================================================================

CREATE TABLE Garment (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES User(id),
    name                TEXT NOT NULL,

    -- Physical Classification
    physical_category   TEXT NOT NULL,
    subcategory         TEXT NOT NULL,
    cut_and_fit         TEXT NOT NULL,
    fills_roles         TEXT NOT NULL DEFAULT '[]',

    -- Aesthetics
    primary_color       TEXT NOT NULL,
    secondary_colors    TEXT DEFAULT '[]',
    pattern_type        TEXT DEFAULT 'solid',
    formality           INTEGER NOT NULL DEFAULT 2 CHECK (formality BETWEEN 1 AND 5),
    style_tags          TEXT NOT NULL DEFAULT '[]',
    layering_role       TEXT,

    -- Usage & Cultural Context
    usage_contexts      TEXT NOT NULL DEFAULT '[]',
    cultural_context    TEXT,
    weather_suitability TEXT NOT NULL DEFAULT '[]',

    -- Reuse Policy
    research_review_after_wears     INTEGER NOT NULL DEFAULT 1 CHECK (research_review_after_wears >= 1),
    owner_review_after_wears        INTEGER CHECK (owner_review_after_wears IS NULL OR owner_review_after_wears >= 1),
    hard_limit_wears                INTEGER CHECK (hard_limit_wears IS NULL OR hard_limit_wears >= 1),
    consecutive_wear_tolerance      TEXT NOT NULL DEFAULT 'avoid',
    context_wear_adjustments        TEXT DEFAULT '{}',
    usually_worn_over_inner_layer   INTEGER NOT NULL DEFAULT 0,
    expected_airing_hours           REAL DEFAULT 8.0 CHECK (expected_airing_hours >= 0),
    airing_review_due_at            TEXT,

    -- Personal Desirability
    like_rating         INTEGER DEFAULT 5 CHECK (like_rating BETWEEN 1 AND 10),
    priority_level      TEXT NOT NULL DEFAULT 'NORMAL' CHECK (priority_level IN ('FAVOURITE','NORMAL','FALLBACK_ONLY','DO_NOT_RECOMMEND')),
    context_priority_overrides TEXT DEFAULT '{}',

    -- Condition State (Hygiene dimension - NO READY_FOR_REVIEW)
    condition_state     TEXT NOT NULL DEFAULT 'CLEAN' CHECK (
        condition_state IN ('CLEAN', 'WORN_AWAITING_ASSESSMENT', 'UNKNOWN_CONDITION', 'WORN_REUSABLE', 'AIRING', 'NEEDS_WASH')
    ),

    -- Availability State (Physical access dimension)
    availability_state  TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (
        availability_state IN ('AVAILABLE', 'IN_LAUNDRY', 'UNAVAILABLE', 'REPAIR', 'RETIRED')
    ),
    unavailability_reason TEXT,

    -- Wear & Laundry Tracking
    wears_since_confirmed_wash INTEGER NOT NULL DEFAULT 0 CHECK (wears_since_confirmed_wash >= 0),
    last_worn_at            TEXT,
    last_confirmed_wash_at  TEXT,
    current_laundry_batch_id TEXT REFERENCES LaundryBatch(id),

    -- Condition Assessment Link (forward reference, resolved after GarmentCondition)
    current_condition_assessment_id TEXT REFERENCES GarmentCondition(id),

    -- Provenance
    owner_verified      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ==========================================================================
-- 4. LaundryBatchItem (references LaundryBatch and Garment)
-- ==========================================================================

CREATE TABLE LaundryBatchItem (
    batch_id        TEXT NOT NULL REFERENCES LaundryBatch(id) ON DELETE CASCADE,
    garment_id      TEXT NOT NULL REFERENCES Garment(id),
    item_status     TEXT NOT NULL DEFAULT 'IN_LAUNDRY' CHECK (
        item_status IN (
            'IN_LAUNDRY',
            'RETURNED_CLEAN',
            'RETURNED_DAMAGED',
            'RETURNED_UNWASHED',
            'MISSING_RESOLVED'
        )
    ),
    wash_confirmed  INTEGER NOT NULL DEFAULT 0 CHECK (wash_confirmed IN (0, 1)),
    PRIMARY KEY (batch_id, garment_id),
    CHECK (
        (item_status = 'RETURNED_CLEAN' AND wash_confirmed = 1)
        OR
        (item_status = 'RETURNED_UNWASHED' AND wash_confirmed = 0)
        OR
        (item_status = 'MISSING_RESOLVED' AND wash_confirmed = 0)
        OR
        (item_status = 'IN_LAUNDRY' AND wash_confirmed = 0)
        OR
        item_status = 'RETURNED_DAMAGED'
    )
);

-- ==========================================================================
-- 5. Garment images, fingerprints, intake staging, duplicate detection
-- ==========================================================================

CREATE TABLE GarmentImage (
    id          TEXT PRIMARY KEY,
    garment_id  TEXT NOT NULL REFERENCES Garment(id) ON DELETE CASCADE,
    image_path  TEXT NOT NULL,
    image_type  TEXT NOT NULL DEFAULT 'full',
    is_primary  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE GarmentImageFingerprint (
    id                  TEXT PRIMARY KEY,
    garment_image_id    TEXT NOT NULL REFERENCES GarmentImage(id) ON DELETE CASCADE,
    sha256              TEXT NOT NULL,
    perceptual_hash     TEXT,
    embedding_model     TEXT,
    embedding_vector    BLOB,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE GarmentIntakeDraft (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES User(id),
    temporary_image_path    TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'ATTACHED_TO_EXISTING', 'CREATED_NEW_GARMENT', 'CANCELLED')
    ),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE GarmentIntakeFingerprint (
    id                  TEXT PRIMARY KEY,
    intake_draft_id     TEXT NOT NULL REFERENCES GarmentIntakeDraft(id) ON DELETE CASCADE,
    sha256              TEXT NOT NULL,
    perceptual_hash     TEXT,
    embedding_model     TEXT,
    embedding_vector    BLOB,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE GarmentDuplicateCandidate (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES User(id),
    intake_draft_id         TEXT NOT NULL REFERENCES GarmentIntakeDraft(id) ON DELETE CASCADE,
    existing_garment_id     TEXT NOT NULL REFERENCES Garment(id),
    exact_hash_match        INTEGER NOT NULL DEFAULT 0,
    perceptual_distance     INTEGER,
    visual_similarity       REAL,
    metadata_similarity     REAL,
    detector_version        TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'SAME_GARMENT', 'DIFFERENT_GARMENT', 'LINKED_SIMILAR', 'DISMISSED')
    ),
    resolution_note         TEXT,
    resolved_at             TEXT
);

-- ==========================================================================
-- 6. Garment analysis, condition, state transitions
-- ==========================================================================

CREATE TABLE GarmentAnalysisImport (
    id                  TEXT PRIMARY KEY,
    garment_id          TEXT NOT NULL REFERENCES Garment(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL,
    model_version       TEXT,
    prompt_version      TEXT,
    raw_json            TEXT NOT NULL,
    imported_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    owner_accepted      INTEGER NOT NULL DEFAULT 0,
    owner_corrections   TEXT DEFAULT '{}'
);

CREATE TABLE GarmentCondition (
    id              TEXT PRIMARY KEY,
    garment_id      TEXT NOT NULL REFERENCES Garment(id) ON DELETE CASCADE,
    assessed_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    condition_grade TEXT NOT NULL,
    issues          TEXT DEFAULT '[]',
    assessed_by     TEXT NOT NULL DEFAULT 'owner',
    notes           TEXT
);

CREATE TABLE GarmentStateTransition (
    id              TEXT PRIMARY KEY,
    garment_id      TEXT NOT NULL REFERENCES Garment(id) ON DELETE CASCADE,
    state_dimension TEXT NOT NULL CHECK (state_dimension IN ('condition', 'availability')),
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    trigger_event   TEXT NOT NULL,
    trigger_details TEXT DEFAULT '{}',
    occurred_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ==========================================================================
-- 7. Wear sessions, templates, occasion profiles
-- ==========================================================================

CREATE TABLE WearSessionTemplate (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES User(id),
    template_name   TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_until TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (effective_until IS NULL OR effective_until >= effective_from)
);

CREATE TABLE WearSessionTemplateDay (
    id              TEXT PRIMARY KEY,
    template_id     TEXT NOT NULL REFERENCES WearSessionTemplate(id) ON DELETE CASCADE,
    day_of_week     INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    session_type    TEXT NOT NULL,
    importance      TEXT NOT NULL DEFAULT 'NORMAL' CHECK (importance IN ('LOW', 'NORMAL', 'HIGH')),
    required_roles  TEXT NOT NULL DEFAULT '["top","bottom","footwear"]',
    start_time      TEXT,
    expected_duration_hours REAL,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE OccasionProfile (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES User(id),
    name            TEXT NOT NULL,
    formality_min   INTEGER DEFAULT 1 CHECK (formality_min BETWEEN 1 AND 5),
    formality_max   INTEGER DEFAULT 5 CHECK (formality_max BETWEEN 1 AND 5),
    required_roles  TEXT NOT NULL DEFAULT '["top","bottom","footwear"]',
    style_direction TEXT DEFAULT '[]',
    prohibited_items TEXT DEFAULT '[]',
    is_recurring    INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    CHECK (formality_min <= formality_max)
);

CREATE TABLE WearSession (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES User(id),
    session_date            TEXT NOT NULL,
    session_type            TEXT NOT NULL,
    importance              TEXT NOT NULL DEFAULT 'NORMAL' CHECK (importance IN ('LOW', 'NORMAL', 'HIGH')),
    required_roles          TEXT NOT NULL DEFAULT '["top","bottom","footwear"]',
    weather_context         TEXT,
    start_time              TEXT,
    expected_duration_hours REAL,
    generated_from_template_id TEXT REFERENCES WearSessionTemplate(id),
    status                  TEXT NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'COMPLETED', 'CANCELLED')),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ==========================================================================
-- 8. Saved outfits
-- ==========================================================================

CREATE TABLE SavedOutfit (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES User(id),
    name            TEXT,
    occasion_tags   TEXT DEFAULT '[]',
    owner_rating    INTEGER CHECK (owner_rating BETWEEN 1 AND 10),
    times_worn      INTEGER NOT NULL DEFAULT 0,
    last_worn_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE SavedOutfitItem (
    outfit_id       TEXT NOT NULL REFERENCES SavedOutfit(id) ON DELETE CASCADE,
    garment_id      TEXT NOT NULL REFERENCES Garment(id),
    outfit_role     TEXT NOT NULL,
    sort_order      INTEGER DEFAULT 0,
    PRIMARY KEY (outfit_id, garment_id, outfit_role)
);

-- ==========================================================================
-- 9. Recommendation plans
-- ==========================================================================

CREATE TABLE RecommendationPlan (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES User(id),
    version             INTEGER NOT NULL DEFAULT 1,
    strategy_mode       TEXT NOT NULL CHECK (strategy_mode IN ('SAFE', 'BALANCED', 'EXPLORATORY')),
    horizon_start_date  TEXT NOT NULL,
    horizon_end_date    TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (
        status IN ('PROPOSED', 'RESERVED', 'COMMITTED', 'SUPERSEDED')
    ),
    invalidation_reason TEXT,
    superseded_by_id    TEXT REFERENCES RecommendationPlan(id),
    plan_input_fingerprint TEXT,
    diversity_target    INTEGER DEFAULT 0,
    diversity_achieved  INTEGER DEFAULT 0,
    diversity_notes     TEXT,
    feasibility_summary TEXT DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (horizon_end_date >= horizon_start_date)
);

CREATE TABLE PlanEvaluation (
    plan_id                     TEXT PRIMARY KEY REFERENCES RecommendationPlan(id) ON DELETE CASCADE,
    uncovered_high              INTEGER NOT NULL DEFAULT 0,
    uncovered_normal            INTEGER NOT NULL DEFAULT 0,
    uncovered_low               INTEGER NOT NULL DEFAULT 0,
    uncertain_dependencies      INTEGER NOT NULL DEFAULT 0,
    fallback_assignments        INTEGER NOT NULL DEFAULT 0,
    min_high_importance_quality REAL,
    total_quality               REAL NOT NULL DEFAULT 0.0,
    repetition_burden           REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE PlanSessionAssignment (
    id                  TEXT PRIMARY KEY,
    plan_id             TEXT NOT NULL REFERENCES RecommendationPlan(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL REFERENCES WearSession(id),
    assignment_status   TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (
        assignment_status IN ('PROPOSED', 'RESERVED', 'COMMITTED', 'UNASSIGNED')
    ),
    quality_score       REAL,
    compatibility_score REAL,
    preference_score    REAL,
    repetition_penalty  REAL DEFAULT 0.0,
    explanation         TEXT DEFAULT '[]',
    is_fallback         INTEGER NOT NULL DEFAULT 0 CHECK (is_fallback IN (0, 1)),
    UNIQUE(plan_id, session_id)
);

CREATE TABLE PlanAssignmentItem (
    assignment_id   TEXT NOT NULL REFERENCES PlanSessionAssignment(id) ON DELETE CASCADE,
    garment_id      TEXT NOT NULL REFERENCES Garment(id),
    outfit_role     TEXT NOT NULL,
    PRIMARY KEY (assignment_id, garment_id, outfit_role)
);

CREATE TABLE PlanResolutionRequest (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES RecommendationPlan(id) ON DELETE CASCADE,
    session_id      TEXT REFERENCES WearSession(id),
    garment_id      TEXT NOT NULL REFERENCES Garment(id),
    required_action TEXT NOT NULL CHECK (
        required_action IN ('confirm_reusable', 'confirm_airing_complete', 'locate_item')
    ),
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED', 'DISMISSED')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ==========================================================================
-- 10. Outfit wear log (references RecommendationPlan, PlanSessionAssignment, OccasionProfile)
-- ==========================================================================

CREATE TABLE OutfitWearLog (
    id                          TEXT PRIMARY KEY,
    user_id                     TEXT NOT NULL REFERENCES User(id),
    session_id                  TEXT NOT NULL REFERENCES WearSession(id),
    recommendation_plan_id      TEXT REFERENCES RecommendationPlan(id),
    plan_assignment_id          TEXT REFERENCES PlanSessionAssignment(id),

    source                      TEXT NOT NULL CHECK (
        source IN ('recommended_selected', 'manual_outfit', 'saved_outfit', 'modified_recommendation')
    ),

    started_at_utc              TEXT,
    ended_at_utc                TEXT,
    local_wear_date             TEXT NOT NULL,
    timezone_id                 TEXT NOT NULL,

    occasion_profile_id         TEXT REFERENCES OccasionProfile(id),
    session_type                TEXT NOT NULL,
    importance                  TEXT NOT NULL DEFAULT 'NORMAL' CHECK (importance IN ('LOW', 'NORMAL', 'HIGH')),

    was_recommended             INTEGER NOT NULL DEFAULT 0 CHECK (was_recommended IN (0, 1)),
    was_modified_before_wear    INTEGER NOT NULL DEFAULT 0 CHECK (was_modified_before_wear IN (0, 1)),
    modification_reason         TEXT,

    satisfaction_rating         INTEGER CHECK (satisfaction_rating IS NULL OR satisfaction_rating BETWEEN 1 AND 10),
    comfort_rating              INTEGER CHECK (comfort_rating IS NULL OR comfort_rating BETWEEN 1 AND 10),
    occasion_fit_rating         INTEGER CHECK (occasion_fit_rating IS NULL OR occasion_fit_rating BETWEEN 1 AND 10),
    would_wear_again            INTEGER CHECK (would_wear_again IS NULL OR would_wear_again IN (0, 1)),

    -- Safe Correction / Voiding
    record_status               TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (record_status IN ('ACTIVE', 'CORRECTED', 'VOIDED')),
    correction_reason           TEXT,
    superseded_by_id            TEXT REFERENCES OutfitWearLog(id),

    feedback_notes              TEXT,
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (ended_at_utc IS NULL OR started_at_utc IS NULL OR ended_at_utc >= started_at_utc)
);

CREATE TABLE OutfitWearLogItem (
    outfit_wear_log_id  TEXT NOT NULL REFERENCES OutfitWearLog(id),
    garment_id          TEXT NOT NULL REFERENCES Garment(id),
    outfit_role         TEXT NOT NULL,

    condition_at_wear   TEXT NOT NULL,
    availability_at_wear TEXT NOT NULL,
    wears_before_event  INTEGER NOT NULL,

    garment_snapshot    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (outfit_wear_log_id, garment_id, outfit_role)
);

-- ==========================================================================
-- 11. Wear events & assessments
-- ==========================================================================

CREATE TABLE WearEvent (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES WearSession(id),
    garment_id          TEXT NOT NULL REFERENCES Garment(id),
    outfit_wear_log_id  TEXT REFERENCES OutfitWearLog(id),
    worn_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    duration_hours      REAL,
    activity_level      TEXT DEFAULT 'moderate',
    is_voided           INTEGER NOT NULL DEFAULT 0 CHECK (is_voided IN (0, 1))
);

CREATE TABLE PostWearAssessment (
    id              TEXT PRIMARY KEY,
    wear_event_id   TEXT NOT NULL REFERENCES WearEvent(id),
    garment_id      TEXT NOT NULL REFERENCES Garment(id),
    assessment      TEXT NOT NULL CHECK (
        assessment IN ('ready_to_reuse', 'needs_airing', 'needs_wash', 'stained', 'wet', 'not_sure')
    ),
    assessed_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notes           TEXT,
    UNIQUE(wear_event_id)
);

-- ==========================================================================
-- 12. Wash events
-- ==========================================================================

CREATE TABLE WashEvent (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES User(id),
    garment_id          TEXT NOT NULL REFERENCES Garment(id),
    laundry_batch_id    TEXT REFERENCES LaundryBatch(id),
    cleaning_scope      TEXT NOT NULL DEFAULT 'FULL_CLEAN' CHECK (cleaning_scope IN ('FULL_CLEAN', 'PARTIAL_CLEAN')),
    method              TEXT NOT NULL CHECK (
        method IN ('laundry_service', 'home_machine', 'hand_wash', 'dry_clean', 'spot_clean', 'other')
    ),
    confirmed_clean_at  TEXT NOT NULL,
    confirmed_by        TEXT NOT NULL DEFAULT 'owner',
    notes               TEXT,
    CHECK (
        (method = 'spot_clean' AND cleaning_scope = 'PARTIAL_CLEAN')
        OR method != 'spot_clean'
    )
);

-- ==========================================================================
-- 13. Wear authorization (references WearEvent)
-- ==========================================================================

CREATE TABLE GarmentWearAuthorization (
    id                  TEXT PRIMARY KEY,
    garment_id          TEXT NOT NULL REFERENCES Garment(id) ON DELETE CASCADE,
    authorized_by       TEXT NOT NULL DEFAULT 'owner',
    reason              TEXT,
    authorized_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    consumed_by_wear_event_id TEXT REFERENCES WearEvent(id),
    status              TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (
        status IN ('AVAILABLE', 'CONSUMED', 'CANCELLED')
    ),
    CHECK (
        (status = 'CONSUMED' AND consumed_by_wear_event_id IS NOT NULL)
        OR
        (status IN ('AVAILABLE', 'CANCELLED')
         AND consumed_by_wear_event_id IS NULL)
    )
);

-- ==========================================================================
-- 14. Candidate snapshots & preference evidence
-- ==========================================================================

CREATE TABLE OutfitCandidateSnapshot (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES User(id),
    session_id              TEXT REFERENCES WearSession(id),
    generator_version       TEXT NOT NULL,
    feature_schema_version  TEXT NOT NULL,
    compatibility_score     REAL,
    occasion_score          REAL,
    preference_score        REAL,
    repetition_penalty      REAL,
    feature_snapshot        TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE OutfitCandidateSnapshotItem (
    candidate_id    TEXT NOT NULL REFERENCES OutfitCandidateSnapshot(id) ON DELETE CASCADE,
    garment_id      TEXT NOT NULL REFERENCES Garment(id),
    outfit_role     TEXT NOT NULL,
    garment_snapshot TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (candidate_id, garment_id, outfit_role)
);

CREATE TABLE PreferenceEvidence (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES User(id),
    evidence_type           TEXT NOT NULL,
    session_id              TEXT REFERENCES WearSession(id),
    occasion_context        TEXT,
    selected_candidate_id   TEXT REFERENCES OutfitCandidateSnapshot(id),
    rejected_candidate_id   TEXT REFERENCES OutfitCandidateSnapshot(id),
    selection_reason        TEXT DEFAULT '[]',
    rejection_reason        TEXT,
    satisfaction_rating     TEXT,
    constraint_flags        TEXT DEFAULT '{}',
    recorded_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE PreferenceEvidenceAlternative (
    evidence_id         TEXT NOT NULL REFERENCES PreferenceEvidence(id) ON DELETE CASCADE,
    candidate_id        TEXT NOT NULL REFERENCES OutfitCandidateSnapshot(id),
    presentation_order  INTEGER,
    was_selected        INTEGER NOT NULL DEFAULT 0 CHECK (was_selected IN (0, 1)),
    was_feasible        INTEGER NOT NULL DEFAULT 1 CHECK (was_feasible IN (0, 1)),
    inclusion_type      TEXT NOT NULL DEFAULT 'PRESENTED' CHECK (
        inclusion_type IN ('PRESENTED', 'FEASIBLE_NOT_PRESENTED', 'INFEASIBLE_REFERENCE')
    ),
    exclusion_reason    TEXT,
    constraint_flags    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (evidence_id, candidate_id),
    CHECK (
        (inclusion_type = 'PRESENTED' AND presentation_order IS NOT NULL)
        OR
        (inclusion_type = 'FEASIBLE_NOT_PRESENTED' AND was_feasible = 1 AND was_selected = 0)
        OR
        (inclusion_type = 'INFEASIBLE_REFERENCE' AND was_feasible = 0 AND was_selected = 0 AND exclusion_reason IS NOT NULL)
    )
);

-- ==========================================================================
-- 14B. Similarity Links & Plan Authorization Reservations
-- ==========================================================================

CREATE TABLE GarmentSimilarityLink (
    garment_id_a   TEXT NOT NULL REFERENCES Garment(id),
    garment_id_b   TEXT NOT NULL REFERENCES Garment(id),
    link_type      TEXT NOT NULL DEFAULT 'OWNER_CONFIRMED_SIMILAR',
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (garment_id_a, garment_id_b),
    CHECK (garment_id_a < garment_id_b)
);

CREATE TABLE PlanWearAuthorizationReservation (
    plan_id             TEXT NOT NULL REFERENCES RecommendationPlan(id),
    session_id          TEXT NOT NULL REFERENCES WearSession(id),
    garment_id          TEXT NOT NULL REFERENCES Garment(id),
    authorization_id    TEXT NOT NULL REFERENCES GarmentWearAuthorization(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (plan_id, session_id, garment_id),
    UNIQUE (authorization_id)
);

-- ==========================================================================
-- 15. Partial unique indexes (correctness invariants)
-- ==========================================================================

-- Only one ACTIVE wear log per session; corrected/voided records coexist
CREATE UNIQUE INDEX idx_one_active_wear_log_per_session
ON OutfitWearLog(session_id)
WHERE record_status = 'ACTIVE';

-- Only one active (non-voided) wear event per session+garment
CREATE UNIQUE INDEX idx_one_active_wear_event_per_session_garment
ON WearEvent(session_id, garment_id)
WHERE is_voided = 0;

-- One wear event can consume at most one authorization
CREATE UNIQUE INDEX idx_one_consumed_authorization_per_wear
ON GarmentWearAuthorization(consumed_by_wear_event_id)
WHERE consumed_by_wear_event_id IS NOT NULL;

-- ==========================================================================
-- 16. Operational & retrieval indexes
-- ==========================================================================

CREATE INDEX idx_outfit_wear_user_local_date ON OutfitWearLog(user_id, local_wear_date DESC);
CREATE INDEX idx_wear_event_garment_time ON WearEvent(garment_id, worn_at DESC);
CREATE INDEX idx_outfit_item_garment ON OutfitWearLogItem(garment_id, outfit_wear_log_id);
CREATE INDEX idx_session_user_date ON WearSession(user_id, session_date DESC);
CREATE INDEX idx_preference_user_recorded ON PreferenceEvidence(user_id, recorded_at DESC);
CREATE INDEX idx_garment_user_state ON Garment(user_id, condition_state, availability_state);
CREATE INDEX idx_garment_duplicate_status ON GarmentDuplicateCandidate(user_id, status);
CREATE INDEX idx_fingerprint_sha256 ON GarmentImageFingerprint(sha256);
CREATE INDEX idx_intake_fingerprint_sha256 ON GarmentIntakeFingerprint(sha256);
CREATE INDEX idx_candidate_snapshot_session ON OutfitCandidateSnapshot(session_id);
CREATE INDEX idx_auth_garment_status ON GarmentWearAuthorization(garment_id, status);

-- ==========================================================================
-- 17. Migration Ledger
-- ==========================================================================

CREATE TABLE IF NOT EXISTS SchemaMigration (
    version     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
