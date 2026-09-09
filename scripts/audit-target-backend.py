"""Audit the extracted backend with explicit, content-pinned review decisions."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def reviewed_findings(root, findings, decisions):
    reviewed, unresolved = [], []
    for finding in findings:
        key = (finding['path'], finding['kind'], finding['target'])
        matching = [item for item in decisions if
                    (item['path'], item['kind'], item['target']) == key]
        source = root / finding['path']
        digest = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
        if len(matching) == 1 and digest == matching[0]['sha256'] and matching[0].get('rationale'):
            reviewed.append({**finding, 'review': matching[0]})
        else:
            unresolved.append(finding)
    return reviewed, unresolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('target_audit', ROOT / 'packages/puddingharness-extraction/audit.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.audit(ROOT, None)
    for name in ('knowledge', 'knowledge_platform', 'knowledge_contracts', 'analytics', 'vanna'):
        if (ROOT / 'backend' / name).exists():
            result['findings'].append({'path': 'backend/' + name, 'line': 1,
                                      'kind': 'forbidden_domain_present', 'target': name})
    decisions = json.loads((ROOT / 'provenance/target-boundary-reviews.json').read_text())
    reviewed, unresolved = reviewed_findings(ROOT, result['findings'], decisions)
    result.update(reviewed_findings=reviewed, unresolved_findings=unresolved,
                  status='blocked' if unresolved else 'python_static_reviewed',
                  scope='Actual backend static check; does not certify runtime, frontend, packaging or production')
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({'status': result['status'], 'selected': len(result['selected']),
                      'findings': len(result['findings']), 'reviewed': len(reviewed),
                      'unresolved': len(unresolved)}))
    return int(bool(unresolved))


if __name__ == '__main__':
    raise SystemExit(main())
