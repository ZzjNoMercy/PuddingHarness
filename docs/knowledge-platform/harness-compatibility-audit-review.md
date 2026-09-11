# Compatibility boundary audit disposition

The one remaining business-symbol detector hit is the literal retired field in
`backend/harness/legacy_artifacts.py`. Removing that spelling would prevent exact
rejection of old wire controls; obscuring it in data files would hide the boundary
from review. It is retained and reported as a raw finding.

The module was reviewed as follows:

- Its imports are limited to copy and typing. It has no file, network, provider,
  routing or execution calls.
- The immutable selector set is used to reject schema-owned new controls and to
  remove those controls from copies of historical candidate/session/run objects.
- Candidate/config roots are cleaned without recursively deleting user evidence.
  Session messages, tool results and experiment summaries remain opaque.
- Model and repository acceptance exercises real SQLite reads and writes. Old
  payload bytes remain unchanged; mutated candidate controls are rejected before
  fingerprint reuse and persistence. Cold-start and installed runtime checks pass.

The audit code records an automated adversarial review disposition for this exact
module SHA-256 and single path/line/kind/target tuple. This is repository-local
review evidence, not a cryptographic signature, human approval, or protection
against someone changing the auditor itself. Updating the pinned hash requires a
fresh boundary review and tests; it is not an automatic normalization step.

Raw `findings` remain visible. `blocking_findings` contains unresolved hits and
`reviewed_findings` explains the compatibility disposition. A reviewed source
reports `python_static_reviewed`, not `python_static_clean`. Historical overlay
audits do not inherit the product disposition. Other detectors, including forbidden
imports, remain blocking even at the same location.

`--require-clean-audit` requires no unresolved blocking findings. The staging
manifest retains all three lists and always keeps `releaseable=false`. Static
review does not establish migration, dependency, frontend, signed release or
production activation readiness.

Regression coverage: test_audit.py checks exact source, function-body drift,
renamed path, business import, other detector kinds and historical audit scope.
test_backend_staging.py verifies unresolved findings still block strict staging,
while reviewed findings remain in the non-releaseable manifest.

Adversarial review also reproduced an existing alias-detection gap: importing a
retired symbol from an external module could avoid Name/Attribute scanning. The
scanner now checks both original and bound import aliases. Five alias regression
cases remain blocking and do not receive compatibility dispositions.
