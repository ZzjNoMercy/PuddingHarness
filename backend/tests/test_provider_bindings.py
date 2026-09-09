"""Owned Provider control data cannot reactivate retired business workloads."""
import copy
import json

import pytest

from provider_registry import ProviderRegistry, _default_registry


def test_registry_projects_retired_bindings_without_rewriting_file(tmp_path):
    registry = ProviderRegistry(tmp_path)
    payload = _default_registry()
    assert not {'vanna_llm', 'vanna_embedding'} & payload['bindings'].keys()
    payload['bindings']['vanna_llm'] = payload['bindings']['agent']
    payload['bindings']['vanna_embedding'] = payload['bindings']['text_embedding']
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(payload).encode()
    registry.path.write_bytes(original)
    projected = registry._payload()
    assert not {'vanna_llm', 'vanna_embedding'} & projected['bindings'].keys()
    assert registry.path.read_bytes() == original
    assert projected['providers'] == payload['providers']
    for binding in ('vanna_llm', 'vanna_embedding', 'unknown_workload'):
        with pytest.raises(ValueError, match='Unsupported Harness binding'):
            registry.set_binding(binding, payload['bindings']['agent'])
    assert registry.path.read_bytes() == original


def test_registry_preserves_generic_binding_update(tmp_path):
    registry = ProviderRegistry(tmp_path)
    original = copy.deepcopy(registry._payload())
    registry.set_binding('agent', original['bindings']['agent'])
    assert registry._payload()['bindings'] == original['bindings']
