"""Finite lazy imports must preserve cold start and reject registry-only additions."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def child(code, tmp_path):
    env = dict(os.environ, PUDDINGHARNESS_HOME=str(tmp_path / 'home'))
    if env.get('HARNESS_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None)
    else:
        env['PYTHONPATH'] = str(Path(__file__).parents[1])
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def test_session_cold_start_then_all_harness_exports(tmp_path):
    child('''
        import harness, sys
        assert 'harness.coordinators' not in sys.modules
        from graph.session_manager import SessionManager
        for name in harness.__all__:
            value = getattr(harness, name)
            assert getattr(harness, name) is value
        try:
            getattr(harness, 'knowledge')
        except AttributeError:
            pass
        else:
            raise AssertionError('unknown export accepted')
    ''', tmp_path)


def test_registered_modules_resolve_owned_factories_and_unknowns_do_not_import(tmp_path):
    child('''
        import tools, sys
        from pathlib import Path
        assert 'tools.browser_tool' not in sys.modules
        for name, factory in tools.GENERIC_TOOL_FACTORIES.items():
            module = tools._import_registered_module(name)
            assert module.__name__ == 'tools.' + name
            assert getattr(module, factory).__module__ == module.__name__
        tools.GENERIC_TOOL_FACTORIES['injected_tool'] = 'create_injected_tool'
        assert tools._load_tool_module('injected_tool', Path.cwd()) == []
        assert 'tools.injected_tool' not in sys.modules
        for name in ('../knowledge', 'knowledge', '__init__', 'tools.read_file_tool'):
            try:
                tools._import_registered_module(name)
            except ValueError:
                pass
            else:
                raise AssertionError(name)
    ''', tmp_path)
