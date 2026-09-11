# Historical candidate controls

New ExperimentCandidate objects reject retired selectors at both candidate and
config roots. Candidate instances are revalidated when embedded in an experiment,
and with_fingerprint rejects mutated retired config controls before its cached
fingerprint early return. Repository creation calls this boundary before writing.

Protocol-1.0 experiment reading uses a shared in-memory candidate projection.
Only schema-owned candidate/config roots are cleaned. Nested user evidence and
experiment summaries remain opaque; original SQLite payload bytes are preserved.
This is not permission to execute or rewrite a historical experiment, and existing
read-only runner/repository protections remain in place.

The one remaining static business-symbol finding is in the shared compatibility
helper. It stays blocked pending explicit review; no audit rule was relaxed.

Run backend/tests/test_candidate_legacy_controls.py for model boundary tests.
Use installed Python outside the checkout with
scripts/acceptance/installed_candidate_controls.py for actual SQLite read/write
acceptance without development-only dependencies.
