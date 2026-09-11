"""Use installed Python outside checkout to verify Candidate persistence boundaries."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile

from pydantic import ValidationError
import evaluation.contracts as contracts
from evaluation.repository import EvaluationRepository


def main():
    assert 'site-packages' in Path(contracts.__file__).parts
    fields = ('analytics_model_id', 'selected_analytics_model', 'sql_generation_ids', 'sql_generation_refs')
    common = dict(name='experiment', dataset_id='data', dataset_version=1,
                  dataset_version_id='revision', dataset_content_hash='hash')
    with tempfile.TemporaryDirectory() as tmp:
        repo = EvaluationRepository(Path(tmp) / 'evaluation.sqlite3')
        raw = {**common, 'protocol_version': '1.0', 'experiment_id': 'historical',
               'candidate': {'name': 'old', 'config': {field: 'old' for field in fields},
                             **{field: 'old' for field in fields}},
               'summary': {field: 'opaque evidence' for field in fields}}
        encoded = json.dumps(raw)
        with sqlite3.connect(repo.db_path) as connection:
            connection.execute('INSERT INTO eval_experiments VALUES (?, ?, ?, ?, ?)',
                               ('historical', encoded, 'completed', 'created', 'updated'))
        projected = repo.get_experiment('historical')
        assert projected.historical_read_only
        assert projected.candidate.config == {}
        assert projected.summary == raw['summary']
        with sqlite3.connect(repo.db_path) as connection:
            assert connection.execute('SELECT payload_json FROM eval_experiments').fetchone()[0] == encoded
        for field in fields:
            candidate = contracts.ExperimentCandidate(name='generic', fingerprint='existing')
            experiment = contracts.EvalExperiment(**common, candidate=candidate)
            experiment.candidate.config[field] = 'retired'
            try:
                repo.create_experiment(experiment)
            except ValueError:
                pass
            else:
                raise AssertionError('mutated candidate persisted')
            try:
                contracts.ExperimentCandidate.model_validate_json(json.dumps({'name': 'new', 'config': {field: 'old'}}))
            except ValidationError:
                pass
            else:
                raise AssertionError('retired control accepted')
        generic = contracts.EvalExperiment(**common, candidate=contracts.ExperimentCandidate(
            name='generic', config={'evidence': copy.deepcopy(raw['summary'])}))
        saved = repo.create_experiment(generic)
        assert repo.get_experiment(saved.experiment_id).candidate.config == generic.candidate.config
        with sqlite3.connect(repo.db_path) as connection:
            assert connection.execute('SELECT COUNT(*) FROM eval_experiments').fetchone()[0] == 2
    print('PASS: installed SQLite history preserved; mutated retired controls rejected before persistence; generic evidence roundtrip')


if __name__ == '__main__':
    main()
