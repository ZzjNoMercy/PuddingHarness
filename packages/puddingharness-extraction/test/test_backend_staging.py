from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "stage_backend.py"
spec = importlib.util.spec_from_file_location("puddingharness_stage_backend", SCRIPT)
assert spec is not None and spec.loader is not None
stage_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_module)


def test_stage_is_flat_independent_and_records_blocked_audit(tmp_path: Path) -> None:
    output = tmp_path / "stage"
    manifest = stage_module.stage_backend(Path(__file__).parents[3], output)

    assert manifest["format"] == "puddingharness-independent-backend-stage/v1"
    assert manifest["releaseable"] is False
    assert manifest["audit_finding_count"] > 0
    assert manifest["python"]["selected_count"] == manifest["python"]["staged_count"]
    assert manifest["python"]["skipped_skill_count"] == 0
    assert (output / "app.py").is_file()
    assert (output / "evaluation" / "schemas" / "protocol-2.0.json").is_file()
    assert (output / "harness" / "docker" / "Dockerfile").is_file()
    assert (output / "skills" / "pdf" / "SKILL.md").is_file()
    assert not (output / "skills" / "database-analysis").exists()
    assert (output / "prompts" / "IDENTITY.md").read_text(encoding="utf-8").find("PuddingHarness") >= 0
    assert (output / "puddingharness_cli.py").is_file()
    assert (output / "uv.lock").is_file()
    assert manifest["packaging"]["pyproject"]["path"] == "pyproject.toml"
    assert manifest["packaging"]["uv_lock"]["path"] == "uv.lock"
    compile((output / "puddingharness_cli.py").read_text(encoding="utf-8"), "puddingharness_cli.py", "exec")
    assert not (output / "backend").exists()
    assert not any(path.suffix == ".pyc" for path in output.rglob("*"))

    manifest_text = (output / stage_module.MANIFEST_NAME).read_text(encoding="utf-8")
    pyproject_text = (output / "pyproject.toml").read_text(encoding="utf-8")
    assert str(Path(__file__).parents[3]) not in manifest_text
    assert str(output) not in manifest_text
    assert str(Path(__file__).parents[3]) not in pyproject_text
    assert "knowledge =" not in pyproject_text.lower()
    assert "analytics =" not in pyproject_text.lower()
    assert "asyncmy" not in pyproject_text.lower()


def test_stage_refuses_to_overwrite_non_empty_directory(tmp_path: Path) -> None:
    output = tmp_path / "stage"
    output.mkdir()
    sentinel = output / "do-not-delete"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty"):
        stage_module.stage_backend(Path(__file__).parents[3], output)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_clean_audit_is_an_explicit_release_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="effective target audit"):
        stage_module.stage_backend(Path(__file__).parents[3], tmp_path / "strict", require_clean_audit=True)


def test_static_clean_does_not_make_stage_releaseable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class CleanAudit:
        @staticmethod
        def audit(repo: Path, overlays: Path) -> dict:
            return {
                "status": "python_static_clean",
                "findings": [],
                "selected": [],
            }

    monkeypatch.setattr(stage_module, "_load_audit", lambda: CleanAudit)
    result = stage_module.stage_backend(Path(__file__).parents[3], tmp_path / "clean")
    assert result["python_static_status"] == "python_static_clean"
    assert result["releaseable"] is False


def test_stage_rejects_output_symlink_and_symlink_ancestor(tmp_path: Path) -> None:
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(ValueError, match="output.*symlink"):
        stage_module.stage_backend(Path(__file__).parents[3], output_link)

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="output.*symlink"):
        stage_module.stage_backend(Path(__file__).parents[3], parent_link / "stage")


def test_stage_rejects_source_overlap_and_safe_copy_symlinks(tmp_path: Path) -> None:
    repo = Path(__file__).parents[3]
    with pytest.raises(ValueError, match="source checkout"):
        stage_module.stage_backend(repo, repo)
    with pytest.raises(ValueError, match="source checkout"):
        stage_module.stage_backend(repo, repo / ".stage-child")

    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    source_link = tmp_path / "source-link"
    source_link.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        stage_module._safe_copy(source_link, tmp_path / "copied.txt", label="source")

    destination_parent = tmp_path / "destination-parent"
    destination_parent_target = tmp_path / "destination-parent-target"
    destination_parent_target.mkdir()
    destination_parent.symlink_to(destination_parent_target, target_is_directory=True)
    with pytest.raises(ValueError, match="destination.*symlink"):
        stage_module._safe_copy(source, destination_parent / "copied.txt", label="source")


def test_prompt_copy_uses_supplied_source_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "backend" / "prompts").mkdir(parents=True)
    (repo / "backend" / "prompts" / "SOUL.md").write_text("supplied source\n", encoding="utf-8")
    output = tmp_path / "stage"
    records = stage_module._copy_prompts(repo, output)
    assert records
    assert (output / "prompts" / "SOUL.md").read_text(encoding="utf-8") == "supplied source\n"


def test_generic_skill_resources_filter_secrets_and_caches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "backend" / "skills" / "pdf"
    (skill / "references").mkdir(parents=True)
    (skill / "cache").mkdir()
    (skill / ".cache").mkdir()
    for relative in (
        "SKILL.md",
        "references/guide.md",
        ".env",
        ".env.example",
        ".npmrc",
        "secret.txt",
        "credentials.json",
        "cache/result.json",
        ".cache/result.json",
        "private.pem",
        "run.log",
        "compiled.pyc",
    ):
        target = skill / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    output = tmp_path / "stage"
    records = stage_module._copy_generic_skill_resources(repo, output)
    copied = {record["path"] for record in records}
    assert copied == {"skills/pdf/SKILL.md", "skills/pdf/references/guide.md"}


def test_selected_hash_drift_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    target = repo / "backend" / "runtime.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    report = {"selected": [{"path": "backend/runtime.py", "sha256": stage_module._sha256(target)}]}
    target.write_text("after\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after audit"):
        stage_module._verify_selected_hashes(repo, report)


def test_required_resource_missing_is_a_hard_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = stage_module._select_file

    def missing_resource(repo: Path, relative: str):
        if relative == "backend/harness/docker/Dockerfile":
            return None, "missing"
        return original(repo, relative)

    monkeypatch.setattr(stage_module, "_select_file", missing_resource)
    with pytest.raises(ValueError, match="required target resource is missing"):
        stage_module.stage_backend(Path(__file__).parents[3], tmp_path / "missing-resource")
