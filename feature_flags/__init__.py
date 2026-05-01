"""Shared feature flag service for all Alea backend services.

Public API:
    FeatureFlagService  — the main service class; construct once at startup
    clear_flag_cache    — test helper; invalidates the in-process cache
"""

from __future__ import annotations

from feature_flags.service import FeatureFlagService, clear_flag_cache

__all__ = ["FeatureFlagService", "clear_flag_cache"]
