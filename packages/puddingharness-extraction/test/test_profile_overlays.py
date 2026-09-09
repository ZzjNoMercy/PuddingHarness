"""Behavior checks for the target Harness profile and verification overlays."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
BACKEND = ROOT.parents[1] / "backend"


def _load_target(relative: str, module_name: str, monkeypatch):
    if str(BACKEND) not in sys.path:
        monkeypatch.syspath_prepend(str(BACKEND))
    package_name = f"harness.{module_name}"
    path = ROOT / "overlays/backend/harness" / relative
    spec = importlib.util.spec_from_file_location(package_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package_name, module)
    spec.loader.exec_module(module)
    return module


def test_task_profile_keeps_generic_classification_and_explicit_skill(monkeypatch) -> None:
    profiles = _load_target("task_profiles.py", "task_profiles", monkeypatch)

    profile = profiles.TaskProfileClassifier.classify(
        message="请修改 Python 代码，并使用 pdf skill",
        skill_catalog=[{"skill_id": "pdf"}],
    )

    assert "code" in profile.intents
    assert profile.available_context_refs == []
    assert all("analytics" not in item for item in profile.intents)
    assert [(item.skill_id, item.explicit) for item in profile.skill_candidates] == [("pdf", True)]


def test_task_profile_negation_does_not_activate_a_skill(monkeypatch) -> None:
    profiles = _load_target("task_profiles.py", "task_profiles_negation", monkeypatch)

    profile = profiles.TaskProfileClassifier.classify(
        message="不要使用 pdf skill，直接回答问题",
        skill_catalog=[{"skill_id": "pdf"}],
    )

    assert not profile.skill_candidates
    assert not profile.missing_explicit_skill_ids
    assert profile.available_context_refs == []


def test_ai_news_routes_to_installed_general_news_skill(monkeypatch) -> None:
    profiles = _load_target("task_profiles.py", "task_profiles_ai_news", monkeypatch)

    profile = profiles.TaskProfileClassifier.classify(
        message="搜索最近 AI 新闻并附来源",
        skill_catalog=[{"skill_id": "aihot"}],
    )

    assert "ai_insights" in profile.intents
    assert "web_research" not in profile.intents
    assert [(item.skill_id, item.explicit) for item in profile.skill_candidates] == [("aihot", False)]
    assert profile.available_context_refs == []


def test_rubric_compiler_has_only_generic_packs_and_no_business_context(monkeypatch) -> None:
    _load_target("task_profiles.py", "task_profiles_rubric", monkeypatch)
    rubric = _load_target("rubric_compiler.py", "rubric_compiler", monkeypatch)

    context = rubric.RubricBuildContext(user_message="请修改 Python 代码")
    assert not hasattr(context, "analytics_model_id")
    contract = rubric.RunRubricCompiler.compile(context)

    assert contract is not None
    assert "code" in contract.verification_packs
    assert "analytics" not in contract.verification_packs
    assert all("analytics" not in criterion.id for criterion in contract.criteria)


def test_deterministic_checks_keep_generic_todo_and_reject_removed_business_criterion(monkeypatch) -> None:
    _load_target("task_profiles.py", "task_profiles_checks", monkeypatch)
    checks = _load_target("deterministic_checks.py", "deterministic_checks", monkeypatch)

    from harness.models import (
        CriterionSource,
        RunVerificationContract,
        VerificationCriterion,
        VerifierKind,
    )

    generic = RunVerificationContract(
        contract_id="contract-generic",
        task_type="core",
        criteria=[
            VerificationCriterion(
                id="todo_reconciliation",
                statement="Todos complete",
                source=CriterionSource.MANAGED,
                verifier=VerifierKind.DETERMINISTIC,
            )
        ],
    )
    result = checks.evaluate_deterministic_criteria(
        generic,
        {"_harness_context": {"run_id": "run-1", "todos": []}},
    )
    assert result[0].passed is True

    removed = generic.model_copy(
        update={
            "criteria": [
                VerificationCriterion(
                    id="analytics_model_invariants",
                    statement="removed",
                    source=CriterionSource.SYSTEM,
                    verifier=VerifierKind.DETERMINISTIC,
                )
            ]
        }
    )
    removed_result = checks.evaluate_deterministic_criteria(removed, {})
    assert removed_result[0].passed is False
    assert "没有注册" in (removed_result[0].gap or "")


def test_profile_overlays_contain_no_analytics_import_or_symbol(monkeypatch) -> None:
    for name in ("task_profiles.py", "rubric_compiler.py", "deterministic_checks.py"):
        text = (ROOT / "overlays/backend/harness" / name).read_text()
        assert "analytics_model_id" not in text
        assert "harness.analytics_invariants" not in text
        assert "\"analytics\"" not in text
