"""GET /api/capabilities — generic PuddingHarness health status."""

from __future__ import annotations

from fastapi import APIRouter

import capabilities

router = APIRouter()


@router.get("/capabilities")
async def get_capabilities() -> dict:
    """Return Core Catalog, Docker, and optional Harness CLI status."""
    caps = await capabilities.detect_capabilities(force=True)
    return caps.to_dict()
