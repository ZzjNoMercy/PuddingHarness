"""Product-level Harness control plane for PuddingHarness Agent runs.

The package deliberately exposes its compatibility API lazily. Core storage
modules import lightweight Harness submodules during application cold start;
eagerly importing coordinators here would import ``graph.session_manager``
again while it is still being initialized.
"""

from __future__ import annotations

from typing import Any

__all__ = ['CompletionVerificationCoordinator', 'evaluate_deterministic_criteria', 'EvidenceScope', 'GoalCoordinator', 'GoalRecord', 'GoalStatus', 'HarnessRunCoordinator', 'RubricEvaluationReport', 'RunOutcome', 'RunRecord', 'RunStatus', 'RunVerificationContract', 'VerificationStatus']


def __getattr__(name: str) -> Any:
    if name == 'CompletionVerificationCoordinator':
        from harness.coordinators import CompletionVerificationCoordinator as value
    elif name == 'evaluate_deterministic_criteria':
        from harness.deterministic_checks import evaluate_deterministic_criteria as value
    elif name == 'EvidenceScope':
        from harness.models import EvidenceScope as value
    elif name == 'GoalCoordinator':
        from harness.coordinators import GoalCoordinator as value
    elif name == 'GoalRecord':
        from harness.models import GoalRecord as value
    elif name == 'GoalStatus':
        from harness.models import GoalStatus as value
    elif name == 'HarnessRunCoordinator':
        from harness.coordinators import HarnessRunCoordinator as value
    elif name == 'RubricEvaluationReport':
        from harness.models import RubricEvaluationReport as value
    elif name == 'RunOutcome':
        from harness.models import RunOutcome as value
    elif name == 'RunRecord':
        from harness.models import RunRecord as value
    elif name == 'RunStatus':
        from harness.models import RunStatus as value
    elif name == 'RunVerificationContract':
        from harness.models import RunVerificationContract as value
    elif name == 'VerificationStatus':
        from harness.models import VerificationStatus as value
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
