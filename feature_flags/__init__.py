"""Shared feature flag service for all Alea backend services.

Public API:
    FeatureFlagService — the main service class; construct one per tenant
        pool. The 60-second TTL cache is per-instance, so per-tenant
        services never share cached values across tenants.

Cache invalidation:
    Use ``service.clear_cache()`` (instance method).
"""

from __future__ import annotations

from feature_flags.service import FeatureFlagService

__all__ = ["FeatureFlagService"]
