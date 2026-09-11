"""Prepare a unified inactive Harness Home from an offline source snapshot."""
import argparse
import fcntl
import json
import os
from pathlib import Path

from harness.session_import import (
    _check_stage, atomic_write, checked, digest, encoded, inventory, read_file,
)
from harness.settings_import import project_settings

FORMAT = 'puddingharness-home-import/v1'


def _snapshot(source):
    config = checked(source / 'config.json')
    raw = read_file(config) if config.exists() else None
    payload, sections, pending = project_settings(raw if raw is not None else b'{}')
    files = inventory(source, require_sessions=False)
    return {'source_config_digest': digest(raw) if raw is not None else None,
            'session_files': files, 'settings_sections': sections,
            'untransferred_section_count': pending}, payload


def prepare_home_import(source_snapshot, staging, *, _after_copy=None):
    source, stage = checked(source_snapshot), checked(staging)
    if not source.is_dir() or stage == source or stage.is_relative_to(source) or source.is_relative_to(stage):
        raise ValueError('Migration roots must be distinct and disjoint')
    snapshot, settings = _snapshot(source)
    files = {**snapshot['session_files'], 'config.json': {'digest': digest(settings), 'size': len(settings)}}
    plan = {'format': FORMAT, 'source_identity': digest(str(source).encode()),
            'snapshot': snapshot, 'files': files}
    expected = {'format': FORMAT, 'plan': plan, 'plan_digest': digest(encoded(plan)),
                'state': 'copying', 'activation_allowed': False,
                'writer_fence_verified': False, 'credential_rebind_required': True}
    if not stage.exists(): stage.mkdir(mode=0o700)
    if not stage.is_dir() or stage.stat().st_mode & 0o077:
        raise ValueError('Staging must be private')
    fd = os.open(checked(stage / '.import.lock'), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _check_stage(stage, files)
        manifest_path = stage / 'manifest.json'
        complete = False
        if manifest_path.exists():
            previous = json.loads(read_file(manifest_path))
            complete = previous.get('state') == 'verified_inactive'
            if encoded(previous) != encoded({**expected, 'state': 'verified_inactive' if complete else 'copying'}):
                raise ValueError('Migration source or plan changed')
        elif any(entry.name != '.import.lock' for entry in stage.iterdir()):
            raise ValueError('Unowned staging content')
        else:
            atomic_write(manifest_path, encoded(expected))
        for relative, fact in files.items():
            destination = checked(stage / 'payload' / relative)
            if destination.exists():
                if digest(read_file(destination)) != fact['digest']:
                    raise ValueError('Staged Home integrity mismatch')
                continue
            if complete: raise ValueError('Verified Home file missing')
            data = settings if relative == 'config.json' else read_file(source / relative)
            if digest(data) != fact['digest']: raise ValueError('Source changed during import')
            atomic_write(destination, data)
            if _after_copy: _after_copy(relative)
        current, current_settings = _snapshot(source)
        if current != snapshot or current_settings != settings:
            raise ValueError('Source changed during combined import')
        _check_stage(stage, files)
        for relative, fact in files.items():
            if digest(read_file(stage / 'payload' / relative)) != fact['digest']:
                raise ValueError('Staged Home integrity mismatch')
        atomic_write(manifest_path, encoded({**expected, 'state': 'verified_inactive'}))
        return {'format': FORMAT, 'state': 'verified_inactive', 'plan_digest': expected['plan_digest'],
                'file_count': len(files), 'session_file_count': len(snapshot['session_files']),
                'settings_sections': snapshot['settings_sections'],
                'untransferred_section_count': snapshot['untransferred_section_count'],
                'idempotent': complete, 'activation_allowed': False,
                'writer_fence_verified': False, 'credential_rebind_required': True}
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--staging', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare_home_import(args.source_snapshot, args.staging)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'home_import_rejected', 'activation_allowed': False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__': raise SystemExit(main())
