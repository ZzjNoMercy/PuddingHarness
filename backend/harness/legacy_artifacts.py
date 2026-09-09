"""Read-only projection of legacy artifact selectors into generic Harness data.

Only schema-owned control fields are removed. User messages, arbitrary tool
results and external resource payloads are opaque and must remain unchanged.
These helpers never read or write files, import a Platform package, or route a
legacy identifier to a provider.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

_REMOVED_SELECTORS = frozenset({'analytics_model_id', 'selected_analytics_model',
                                'sql_generation_ids', 'sql_generation_refs'})


def reject_legacy_selectors(value: Any) -> Any:
    """New control-plane objects must not silently accept retired selectors."""
    if isinstance(value, dict):
        removed = _REMOVED_SELECTORS.intersection(value)
        if removed:
            raise ValueError('Retired control fields are not accepted: ' + ', '.join(sorted(removed)))
    return value


def project_legacy_run(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    for field in _REMOVED_SELECTORS:
        result.pop(field, None)
    profile = result.get('task_profile')
    if isinstance(profile, dict):
        refs = profile.get('available_context_refs')
        if isinstance(refs, list):
            profile['available_context_refs'] = [ref for ref in refs
                if not (isinstance(ref, str) and ref.startswith('analytics_model:'))]
    for contract in result.get('delegation_contracts', []) if isinstance(result.get('delegation_contracts'), list) else []:
        if isinstance(contract, dict):
            for field in _REMOVED_SELECTORS:
                contract.pop(field, None)
    # Only project schema-owned control objects, never arbitrary evidence payloads.
    for envelope in result.get('delegation_results', []) if isinstance(result.get('delegation_results'), list) else []:
        if isinstance(envelope, dict):
            envelope.pop('sql_generation_ids', None)
    handoff = result.get('handoff_summary')
    if isinstance(handoff, dict):
        handoff.pop('sql_generation_refs', None)
    return result


def project_legacy_session(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    for field in _REMOVED_SELECTORS:
        result.pop(field, None)
    harness = result.get('harness')
    runs = harness.get('runs') if isinstance(harness, dict) else None
    if isinstance(runs, dict):
        harness['runs'] = {key: project_legacy_run(run) if isinstance(run, dict) else run
                           for key, run in runs.items()}
    return result


def project_legacy_trace(value: dict[str, Any]) -> dict[str, Any]:
    """Expose embedded traces without lazily rewriting a historical artifact."""
    traces = value.get('traces')
    traces = deepcopy(traces) if isinstance(traces, dict) else {}
    latest = value.get('trace')
    latest_query = value.get('latest_query_id')
    if isinstance(latest, dict):
        latest_query = latest_query or latest.get('query_id') or value.get('latest_trace_id')
        if isinstance(latest_query, str) and latest_query not in traces:
            traces[latest_query] = deepcopy(latest)
    result = {'traces': traces}
    if isinstance(latest_query, str):
        result['latest_query_id'] = latest_query
    if isinstance(value.get('latest_trace_id'), str):
        result['latest_trace_id'] = value['latest_trace_id']
    return result if traces else {}
