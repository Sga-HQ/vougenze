from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any


# ==============================================================================
# Two Orthogonal State Dimensions (Frozen Spec 1.2)
# ==============================================================================

class ConditionState(str, Enum):
    """Hygiene & cleanliness dimension (No READY_FOR_REVIEW; review is a UI prompt)."""
    CLEAN = "CLEAN"
    WORN_AWAITING_ASSESSMENT = "WORN_AWAITING_ASSESSMENT"
    UNKNOWN_CONDITION = "UNKNOWN_CONDITION"
    WORN_REUSABLE = "WORN_REUSABLE"
    AIRING = "AIRING"
    NEEDS_WASH = "NEEDS_WASH"


class AvailabilityState(str, Enum):
    """Physical access dimension."""
    AVAILABLE = "AVAILABLE"
    IN_LAUNDRY = "IN_LAUNDRY"
    UNAVAILABLE = "UNAVAILABLE"
    REPAIR = "REPAIR"
    RETIRED = "RETIRED"


class ConditionGrade(str, Enum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    WORN_SERVICEABLE = "WORN_SERVICEABLE"
    POOR = "POOR"
    NEEDS_REPAIR = "NEEDS_REPAIR"


class PriorityLevel(str, Enum):
    FAVOURITE = "FAVOURITE"
    NORMAL = "NORMAL"
    FALLBACK_ONLY = "FALLBACK_ONLY"
    DO_NOT_RECOMMEND = "DO_NOT_RECOMMEND"


class PlanStatus(str, Enum):
    PROPOSED = "PROPOSED"
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    SUPERSEDED = "SUPERSEDED"


class AssignmentStatus(str, Enum):
    PROPOSED = "PROPOSED"
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    UNASSIGNED = "UNASSIGNED"


class InvalidationReason(str, Enum):
    LAUNDRY_DELAYED = "LAUNDRY_DELAYED"
    ITEM_MARKED_DIRTY = "ITEM_MARKED_DIRTY"
    EVENT_ADDED_OR_MODIFIED = "EVENT_ADDED_OR_MODIFIED"
    USER_OVERRIDE = "USER_OVERRIDE"
    RETIREMENT = "RETIREMENT"
    MANUAL_TRIGGER = "MANUAL_TRIGGER"


class StrategyMode(str, Enum):
    SAFE = "SAFE"
    BALANCED = "BALANCED"
    EXPLORATORY = "EXPLORATORY"


class LaundryBatchStatus(str, Enum):
    IN_LAUNDRY = "IN_LAUNDRY"
    RETURNED_CONFIRMED = "RETURNED_CONFIRMED"
    PARTIAL_RETURN = "PARTIAL_RETURN"
    DELAYED = "DELAYED"


class LaundryItemStatus(str, Enum):
    IN_LAUNDRY = "IN_LAUNDRY"
    RETURNED_CLEAN = "RETURNED_CLEAN"
    RETURNED_DAMAGED = "RETURNED_DAMAGED"
    RETURNED_UNWASHED = "RETURNED_UNWASHED"
    MISSING_RESOLVED = "MISSING_RESOLVED"


class CleaningScope(str, Enum):
    FULL_CLEAN = "FULL_CLEAN"
    PARTIAL_CLEAN = "PARTIAL_CLEAN"


class PostWearChoice(str, Enum):
    READY_TO_REUSE = "ready_to_reuse"
    NEEDS_AIRING = "needs_airing"
    NEEDS_WASH = "needs_wash"
    STAINED = "stained"
    WET = "wet"
    NOT_SURE = "not_sure"


class AuthorizationStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    CONSUMED = "CONSUMED"
    CANCELLED = "CANCELLED"


class InclusionType(str, Enum):
    PRESENTED = "PRESENTED"
    FEASIBLE_NOT_PRESENTED = "FEASIBLE_NOT_PRESENTED"
    INFEASIBLE_REFERENCE = "INFEASIBLE_REFERENCE"


class RecordStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CORRECTED = "CORRECTED"
    VOIDED = "VOIDED"


class IntakeDraftStatus(str, Enum):
    PENDING = "PENDING"
    ATTACHED_TO_EXISTING = "ATTACHED_TO_EXISTING"
    CREATED_NEW_GARMENT = "CREATED_NEW_GARMENT"
    CANCELLED = "CANCELLED"


class DuplicateCandidateStatus(str, Enum):
    PENDING = "PENDING"
    SAME_GARMENT = "SAME_GARMENT"
    DIFFERENT_GARMENT = "DIFFERENT_GARMENT"
    LINKED_SIMILAR = "LINKED_SIMILAR"
    DISMISSED = "DISMISSED"


class FallbackPolicy(str, Enum):
    EXCLUDE = "EXCLUDE"
    IF_REQUIRED = "IF_REQUIRED"
    ALLOW = "ALLOW"


class RejectionReason(str, Enum):
    AESTHETIC = "AESTHETIC"
    FIT = "FIT"
    COMFORT = "COMFORT"
    REPETITION = "REPETITION"
    ITEM_IN_LAUNDRY = "ITEM_IN_LAUNDRY"
    ITEM_UNAVAILABLE = "ITEM_UNAVAILABLE"
    OCCASION_MISMATCH = "OCCASION_MISMATCH"
    WEATHER = "WEATHER"
    OTHER = "OTHER"


# ==============================================================================
# Domain Entities
# ==============================================================================

@dataclass
class User:
    id: str
    display_name: str
    climate_zone: Optional[str] = None
    timezone_id: str = "UTC"
    active_history_window: int = 100
    created_at: Optional[str] = None


@dataclass
class ReusePolicy:
    research_review_after_wears: int = 1
    owner_review_after_wears: Optional[int] = None
    hard_limit_wears: Optional[int] = None
    consecutive_wear_tolerance: str = "avoid"  # "avoid" or "acceptable"
    context_wear_adjustments: Dict[str, int] = field(default_factory=dict)
    usually_worn_over_inner_layer: bool = False
    expected_airing_hours: float = 8.0
    airing_review_due_at: Optional[str] = None

    def get_review_threshold(self, context: Optional[str] = None) -> int:
        """Threshold where post-wear review prompt gains prominence."""
        if context and context in self.context_wear_adjustments:
            return self.context_wear_adjustments[context]
        if self.owner_review_after_wears is not None:
            return self.owner_review_after_wears
        return self.research_review_after_wears

    def review_due(self, current_wears: int, context: Optional[str] = None) -> bool:
        """Determines if review prompt should be shown."""
        return current_wears >= self.get_review_threshold(context)

    def hard_limit_reached(self, current_wears: int) -> bool:
        """Evaluates whether the physical hard wear limit has been reached."""
        return (
            self.hard_limit_wears is not None
            and current_wears >= self.hard_limit_wears
        )


@dataclass
class Garment:
    id: str
    user_id: str
    name: str

    # Physical Classification
    physical_category: str
    subcategory: str
    cut_and_fit: str
    primary_color: str
    fills_roles: List[str] = field(default_factory=list)

    # Aesthetics
    secondary_colors: List[str] = field(default_factory=list)
    pattern_type: str = "solid"
    formality: int = 2
    style_tags: List[str] = field(default_factory=list)
    layering_role: Optional[str] = None

    # Usage & Cultural Context
    usage_contexts: List[str] = field(default_factory=list)
    cultural_context: Optional[str] = None
    weather_suitability: List[str] = field(default_factory=list)

    # Reuse Policy
    reuse_policy: ReusePolicy = field(default_factory=ReusePolicy)

    # Personal Desirability
    like_rating: int = 5
    priority_level: PriorityLevel = PriorityLevel.NORMAL
    context_priority_overrides: Dict[str, PriorityLevel] = field(default_factory=dict)

    # Two Orthogonal Dimensions
    condition_state: ConditionState = ConditionState.CLEAN
    availability_state: AvailabilityState = AvailabilityState.AVAILABLE
    unavailability_reason: Optional[str] = None

    # Wear & Laundry Tracking
    wears_since_confirmed_wash: int = 0
    last_worn_at: Optional[str] = None
    last_confirmed_wash_at: Optional[str] = None
    current_laundry_batch_id: Optional[str] = None
    current_condition_assessment_id: Optional[str] = None

    owner_verified: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def get_effective_priority(self, context: Optional[str] = None) -> PriorityLevel:
        if context and context in self.context_priority_overrides:
            return self.context_priority_overrides[context]
        return self.priority_level

    def is_physically_available(self) -> bool:
        """Physical accessibility check."""
        return self.availability_state == AvailabilityState.AVAILABLE

    def is_clean_or_reusable(self) -> bool:
        """Hygiene eligibility check for planning."""
        return self.condition_state in {
            ConditionState.CLEAN,
            ConditionState.WORN_REUSABLE
        }

    def is_eligible_for_candidate(self) -> bool:
        """Must be both physically accessible and hygienic/reusable."""
        return self.is_physically_available() and self.is_clean_or_reusable()


@dataclass
class WearSession:
    id: str
    user_id: str
    session_date: str  # YYYY-MM-DD
    session_type: str
    importance: str = "NORMAL"  # LOW, NORMAL, HIGH
    required_roles: List[str] = field(default_factory=lambda: ["top", "bottom", "footwear"])
    weather_context: Optional[str] = None
    start_time: Optional[str] = None
    expected_duration_hours: Optional[float] = None
    generated_from_template_id: Optional[str] = None
    status: str = "PLANNED"
    created_at: Optional[str] = None


@dataclass
class OutfitCandidate:
    id: str
    garment_ids: List[str]
    roles: Dict[str, str] = field(default_factory=dict)  # role -> garment_id
    compatibility_score: float = 0.0
    occasion_score: float = 0.0
    preference_score: float = 0.0
    repetition_penalty: float = 0.0
    quality_score: float = 0.0
    explanation: List[str] = field(default_factory=list)
    is_fallback: bool = False
    fallback_count: int = 0


@dataclass
class RecommendationPlan:
    id: str
    user_id: str
    version: int = 1
    strategy_mode: StrategyMode = StrategyMode.BALANCED
    horizon_start_date: str = ""
    horizon_end_date: str = ""
    status: PlanStatus = PlanStatus.PROPOSED
    invalidation_reason: Optional[InvalidationReason] = None
    superseded_by_id: Optional[str] = None
    plan_input_fingerprint: Optional[str] = None
    diversity_target: int = 0
    diversity_achieved: int = 0
    diversity_notes: Optional[str] = None
    feasibility_summary: Dict[str, Any] = field(default_factory=dict)
    assignments: Dict[str, OutfitCandidate] = field(default_factory=dict)  # session_id -> candidate
    created_at: Optional[str] = None
