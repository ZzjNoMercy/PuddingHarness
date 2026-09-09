"""Console entry point for the standalone PuddingHarness API."""

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("PUDDINGHARNESS_HOST", "127.0.0.1"),
        port=int(os.getenv("PUDDINGHARNESS_PORT", "8888")),
    )
