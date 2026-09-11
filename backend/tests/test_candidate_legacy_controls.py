import copy
import json

import pytest
from pydantic import ValidationError
from evaluation.contracts import ExperimentCandidate, EvalExperiment

FIELDS = ('analytics_model_id', 'selected_analytics_model', 'sql_generation_ids', 'sql_generation_refs')


def experiment(candidate, **kwargs):
    return EvalExperiment(name='experiment', dataset_id='data', dataset_version=1,
                          dataset_version_id='revision', dataset_content_hash='hash',
                          candidate=candidate, **kwargs)


@pytest.mark.parametrize('field', FIELDS)
def test_new_candidate_rejects_retired_root_and_config_controls(field):
    for payload in ({'name': 'test', field: 'old'}, {'name': 'test', 'config': {field: 'old'}}):
        with pytest.raises(ValidationError):
            ExperimentCandidate.model_validate(payload)
        with pytest.raises(ValidationError):
            ExperimentCandidate.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize('field', FIELDS)
def test_mutated_candidate_revalidated_before_experiment_or_fingerprint(field):
    candidate = ExperimentCandidate(name='test', fingerprint='already-set')
    candidate.config[field] = 'old'
    with pytest.raises(ValidationError):
        experiment(candidate)
    with pytest.raises(ValueError):
        candidate.with_fingerprint()


def test_historical_projection_removes_only_owned_controls_and_preserves_input():
    opaque = {field: 'evidence value' for field in FIELDS}
    candidate = {'name': 'historical', 'protocol_version': '1.0', **opaque,
                 'config': {**opaque, 'evidence': copy.deepcopy(opaque), 'generic': True}}
    raw = {'protocol_version': '1.0', 'name': 'experiment', 'dataset_id': 'data',
           'dataset_version': 1, 'dataset_version_id': 'revision', 'dataset_content_hash': 'hash',
           'candidate': candidate, 'summary': copy.deepcopy(opaque)}
    before = copy.deepcopy(raw)
    projected = EvalExperiment.model_validate(raw)
    assert projected.historical_read_only is True
    assert raw == before
    assert projected.candidate.config == {'evidence': opaque, 'generic': True}
    assert projected.summary == opaque
    assert not set(FIELDS).intersection(projected.candidate.model_dump())
    assert not set(FIELDS).intersection(projected.candidate.config)


def test_generic_candidate_still_accepts_opaque_nested_evidence():
    candidate = ExperimentCandidate(name='generic', config={'evidence': {FIELDS[0]: 'opaque'}})
    assert experiment(candidate).candidate.config == candidate.config
    assert candidate.with_fingerprint().fingerprint
