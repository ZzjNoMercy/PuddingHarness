# Harness lazy import boundary

The independent Harness retains lazy imports to avoid the session-storage and
coordinator cold-start cycle. Its public exports and generic tool modules use
literal import branches, so every possible import target can be checked by the
existing source dependency audit. The audit policy has not been relaxed.

Registering a string in GENERIC_TOOL_FACTORIES alone cannot introduce a module.
A new tool requires a literal import branch and an owned callable factory.
Unknown module names are rejected without package scanning or import evaluation.
This bounds runtime discovery; it is not a sandbox against arbitrary Python code
already executing inside the process.

Validate cold start, complete exports, factory ownership and unknown-module
rejection with backend/tests/test_static_lazy_imports.py. Set
HARNESS_TEST_INSTALLED=1 and use the noneditable installation Python from outside
the checkout for installed acceptance. The integration suite additionally uses
HARNESS_TEST_PYTHON to select the staged installation.

The source audit still reports two retired selector strings used by legacy
artifact projection/rejection. These findings remain blocked pending a separate
boundary review. Runtime tests do not imply production activation readiness.
