"""Reviewed static findings must fail closed when source or decisions change."""
import hashlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('audit_target', ROOT / 'scripts/audit-target-backend.py')
audit_target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_target)


def test_review_is_bound_to_exact_source_kind_target_and_reason(tmp_path):
    source = tmp_path / 'owned.py'
    source.write_text('reviewed code')
    finding = {'path': 'owned.py', 'kind': 'dynamic_import_review', 'target': '<dynamic-import-review>'}
    decision = {**finding, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'rationale': 'Explicit fixed registry'}
    assert audit_target.reviewed_findings(tmp_path, [finding], [decision]) == ([{**finding, 'review': decision}], [])
    for decisions in ([], [decision, decision], [{**decision, 'target': 'different'}], [{**decision, 'rationale': ''}]):
        assert audit_target.reviewed_findings(tmp_path, [finding], decisions) == ([], [finding])
    source.write_text('changed code')
    assert audit_target.reviewed_findings(tmp_path, [finding], [decision]) == ([], [finding])


def test_forbidden_new_domain_cannot_match_review(tmp_path):
    finding = {'path': 'backend/knowledge', 'kind': 'forbidden_domain_present', 'target': 'knowledge'}
    assert audit_target.reviewed_findings(tmp_path, [finding], []) == ([], [finding])
