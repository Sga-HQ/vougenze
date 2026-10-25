"""
Hypothesis Domain Strategies for Vougenze Lifecycle Entities.
Provides generative strategies for garments, wardrobe sessions, authorizations,
laundry batches, and wear logging arguments.
"""
from __future__ import annotations
import hypothesis.strategies as st
from core.models import (
    ConditionState,
    AvailabilityState,
    ConditionGrade,
    PriorityLevel,
    CleaningScope,
    PostWearChoice,
    LaundryItemStatus,
)

# Core Identifiers
user_ids = st.sampled_from(["u_stateful_1", "u_stateful_2"])
garment_categories = st.sampled_from(["top", "bottom", "footwear", "outerwear", "one_piece"])
formality_levels = st.integers(min_value=1, max_value=5)
hard_limits = st.integers(min_value=1, max_value=10)

# Lifecycle Enums
condition_states = st.sampled_from([
    ConditionState.CLEAN,
    ConditionState.WORN_REUSABLE,
    ConditionState.WORN_AWAITING_ASSESSMENT,
    ConditionState.AIRING,
    ConditionState.NEEDS_WASH,
    ConditionState.UNKNOWN_CONDITION,
])

availability_states = st.sampled_from([
    AvailabilityState.AVAILABLE,
    AvailabilityState.IN_LAUNDRY,
    AvailabilityState.UNAVAILABLE,
    AvailabilityState.REPAIR,
    AvailabilityState.RETIRED,
])

post_wear_choices = st.sampled_from([
    PostWearChoice.READY_TO_REUSE,
    PostWearChoice.NEEDS_AIRING,
    PostWearChoice.NEEDS_WASH,
    PostWearChoice.STAINED,
    PostWearChoice.WET,
    PostWearChoice.NOT_SURE,
])

laundry_item_statuses = st.sampled_from([
    LaundryItemStatus.RETURNED_CLEAN,
    LaundryItemStatus.RETURNED_DAMAGED,
    LaundryItemStatus.RETURNED_UNWASHED,
    LaundryItemStatus.MISSING_RESOLVED,
])

cleaning_scopes = st.sampled_from([
    CleaningScope.FULL_CLEAN,
    CleaningScope.PARTIAL_CLEAN,
])

# Session Parameters
session_types = st.sampled_from(["casual", "office", "formal", "exercise", "home_casual"])
session_importances = st.sampled_from(["LOW", "NORMAL", "HIGH"])
dates_2026 = st.dates(min_value=st.just(2026).map(lambda y: __import__('datetime').date(2026, 3, 1)),
                      max_value=st.just(2026).map(lambda y: __import__('datetime').date(2026, 3, 31)))

# Wear Modification Inputs
modification_reasons = st.one_of(
    st.none(),
    st.just(""),
    st.just("   "),
    st.sampled_from([
        "Thermal discomfort (too cold)",
        "Stain noticed before leaving",
        "Formality changed last minute",
        "Owner aesthetic preference",
    ]),
)
