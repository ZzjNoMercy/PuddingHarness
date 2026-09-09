"""Stable runtime-profile contract for the PuddingHarness frontend."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/runtime-profile", tags=["runtime"])

# The frontend currently uses these keys to hide product-specific navigation.
# They are a fixed target-repository contract, not extension switches.
_HARNESS_RUNTIME_PROFILE = {
    "schema_version": 1,
    "profile": "harness",
    "extensions": {
        "knowledge": False,
        "analytics": False,
        "headless_worker": True,
    },
}


@router.get("")
async def get_runtime_profile() -> dict[str, object]:
    return {
        **_HARNESS_RUNTIME_PROFILE,
        "extensions": dict(_HARNESS_RUNTIME_PROFILE["extensions"]),
    }
