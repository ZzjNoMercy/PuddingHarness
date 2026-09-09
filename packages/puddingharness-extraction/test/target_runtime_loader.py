"""Resolve effective target Python files while refusing excluded legacy modules.

This is an integration-test loader, not a distribution. Dependencies still come
from the development environment; independent installation remains required.
"""
import importlib.abc
import importlib.util
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[1]
spec = importlib.util.spec_from_file_location('target_audit_rules', PACKAGE / 'audit.py')
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


class TargetFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        stem = Path(*fullname.split('.'))
        for rel in (stem.with_suffix('.py'), stem / '__init__.py'):
            name = 'backend/' + rel.as_posix()
            if name in rules.EXCLUDED_FILES or any(name.startswith(p) for p in rules.EXCLUDED_PREFIXES):
                raise ImportError('Excluded target module: ' + fullname)
            candidate = PACKAGE / 'overlays/backend' / rel
            if not candidate.is_file():
                candidate = REPO / 'backend' / rel
            if candidate.is_file():
                return importlib.util.spec_from_file_location(fullname, candidate)
        return None


sys.meta_path.insert(0, TargetFinder())
