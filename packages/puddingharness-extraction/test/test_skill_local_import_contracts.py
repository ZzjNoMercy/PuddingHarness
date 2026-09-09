import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).parents[3]
SKILL_PYTHON = REPOSITORY_ROOT / "backend/.venv/bin/python"


def run_isolated(skill_name, source):
    if not SKILL_PYTHON.exists():
        pytest.skip("development backend/.venv is not installed")
    skill_root = REPOSITORY_ROOT / "backend/skills" / skill_name
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(skill_root)
    return subprocess.run(
        [str(SKILL_PYTHON), "-c", source],
        cwd=skill_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def assert_started(skill_name, source):
    result = run_isolated(skill_name, source)
    assert result.returncode == 0, f"{skill_name} startup failed:\n{result.stdout}\n{result.stderr}"


def test_dialogue_summarizer_uses_skill_root_for_scripts_imports():
    assert_started(
        "dialogue-summarizer",
        "from scripts.summarizer import DialogueSummarizer; "
        "from scripts.context_handler import ContextHandler; "
        "assert DialogueSummarizer and ContextHandler",
    )


def test_get_date_uses_skill_root_for_scripts_imports():
    assert_started(
        "get-date",
        "from scripts.get_datetime import detect_intent, format_by_intent, get_current_datetime; "
        "assert detect_intent('现在几点了') == 'time'; assert get_current_datetime()",
    )


def test_skill_creator_scripts_start_in_isolated_skill_root():
    assert_started(
        "skill-creator",
        "from scripts.utils import parse_skill_md; "
        "from scripts.quick_validate import validate_skill; "
        "from scripts.generate_report import generate_html; "
        "from scripts.improve_description import improve_description; "
        "from scripts.package_skill import package_skill; "
        "from scripts.run_eval import find_project_root; "
        "from scripts.run_loop import run_eval; "
        "assert all((parse_skill_md, validate_skill, generate_html, improve_description, "
        "package_skill, find_project_root, run_eval))",
    )


def test_generic_skill_skeletons_have_no_builtin_knowledge_runtime_dependency():
    fetch = (REPOSITORY_ROOT / "backend/skills/github-monitor/scripts/fetch_github.py").read_text()
    store = (REPOSITORY_ROOT / "backend/skills/github-monitor/scripts/store_kb.py").read_text()
    hv = (REPOSITORY_ROOT / "backend/skills/hv-analysis/scripts/md_to_pdf.py").read_text()

    for source in (fetch, store, hv):
        assert "PUDDINGCLAW_HOME" not in source
        assert "from knowledge" not in source
        assert "import knowledge" not in source
        assert "gbrain" not in source.lower()

    assert 'parser.add_argument("--output-dir", "--kb-path", dest="output_dir", required=True' in store
    assert 'parser.add_argument("input"' in hv
    assert 'parser.add_argument("output"' in hv
