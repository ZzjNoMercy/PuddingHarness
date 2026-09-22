import hashlib
import json
from pathlib import Path

import pytest

from harness.cutover_domain_inventory import DomainInventoryError, produce_inventory
from harness.home_import import prepare_home_import


def _source(root: Path) -> Path:
    root.mkdir(mode=0o700)
    (root / "sessions").mkdir(mode=0o700)
    (root / "sessions/a.json").write_bytes(b'{"messages":[]}')
    (root / "data/attachments/a/b").mkdir(parents=True, mode=0o700)
    (root / "data/attachments/a/b/body.bin").write_bytes(b"body")
    return root


def test_inventory_is_derived_from_verified_staging_and_idempotent(tmp_path: Path) -> None:
    source = _source(tmp_path / "source")
    stage = tmp_path / "stage"
    prepare_home_import(source, stage)
    output = tmp_path / "inventory.json"
    result = produce_inventory(staging=stage, output=output)

    assert result["producer"] == "puddingharness"
    assert len(result["inventory"]) == 2
    assert result["inventory"] == sorted(result["inventory"])
    assert all(item.startswith("file:") for item in result["inventory"])
    assert result["inventory_sha256"] == "sha256:" + hashlib.sha256(
        json.dumps(result["inventory"], separators=(",", ":")).encode()
    ).hexdigest()
    assert json.loads(output.read_text()) == result
    assert produce_inventory(staging=stage, output=output) == result


def test_target_bytes_and_forged_manifest_are_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path / "source")
    stage = tmp_path / "stage"
    prepare_home_import(source, stage)
    (stage / "payload/sessions/a.json").write_text("{}")
    with pytest.raises(DomainInventoryError, match="payload"):
        produce_inventory(staging=stage, output=tmp_path / "bad.json")

    stage2 = tmp_path / "stage2"
    prepare_home_import(source, stage2)
    manifest = json.loads((stage2 / "manifest.json").read_text())
    manifest["activation_allowed"] = True
    (stage2 / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DomainInventoryError, match="verified inactive"):
        produce_inventory(staging=stage2, output=tmp_path / "forged.json")


def test_output_collision_and_public_output_parent_fail_closed(tmp_path: Path) -> None:
    source = _source(tmp_path / "source")
    stage = tmp_path / "stage"
    prepare_home_import(source, stage)
    output = tmp_path / "inventory.json"
    output.write_text("{}")
    output.chmod(0o600)
    with pytest.raises(DomainInventoryError, match="disagrees"):
        produce_inventory(staging=stage, output=output)
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(DomainInventoryError, match="private"):
        produce_inventory(staging=stage, output=public / "inventory.json")
