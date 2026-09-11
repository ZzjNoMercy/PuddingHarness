"""Project offline legacy settings into private, inactive Harness staging.

Connection-bearing sections require a separate rebind; this is not an installation
checkpoint or proof that the source writer has been fenced.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path

from harness.session_import import atomic_write, checked, digest, encoded, read_file

FORMAT = 'puddingharness-settings-import/v1'
OWNED = ('compression', 'cache', 'subagents', 'harness', 'write_middleware')
MAX_CONFIG_BYTES = 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate configuration key')
        result[key] = value
    return result


def _shape(value, depth=0):
    if depth > 32:
        raise ValueError('Configuration nesting exceeds budget')
    if isinstance(value, dict):
        for item in value.values(): _shape(item, depth + 1)
    elif isinstance(value, list):
        for item in value: _shape(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Non-finite configuration number')


def project_settings(raw):
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('Configuration exceeds budget')
    value = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError('Configuration must be an object')
    _shape(value)
    revision = value.get('schema_version', 1)
    if type(revision) is not int or revision != 1:
        raise ValueError('Unsupported source config schema')
    selected = {key: value[key] for key in OWNED if key in value}
    if any(not isinstance(item, dict) for item in selected.values()):
        raise ValueError('Generic settings sections must be objects')
    from config import _validate_config_document
    selected = _validate_config_document(selected)
    payload = encoded({'schema_version': 1, **selected})
    # Report only bounded, known section names. Unknown user keys and all values
    # stay out of the manifest and CLI output, including credential-bearing data.
    pending = len(set(value) - set(OWNED) - {'schema_version'})
    return payload, sorted(selected), pending


def prepare_settings_import(source_snapshot, staging, *, _after_copy=None):
    source, stage = checked(source_snapshot), checked(staging)
    if not source.is_dir() or stage == source or stage.is_relative_to(source) or source.is_relative_to(stage):
        raise ValueError('Migration roots must be distinct and disjoint')
    config = checked(source / 'config.json')
    raw = read_file(config)
    payload, sections, pending = project_settings(raw)
    plan = {'format': FORMAT, 'source_identity': digest(str(source).encode()),
            'source_config_digest': digest(raw), 'payload_digest': digest(payload),
            'imported_sections': sections, 'untransferred_section_count': pending}
    expected = {'format': FORMAT, 'plan': plan, 'plan_digest': digest(encoded(plan)),
                'state': 'copying', 'activation_allowed': False,
                'writer_fence_verified': False, 'credential_rebind_required': True}
    if not stage.exists(): stage.mkdir(mode=0o700)
    if not stage.is_dir() or stage.stat().st_mode & 0o077:
        raise ValueError('Staging must be private')
    fd = os.open(checked(stage / '.import.lock'), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        allowed = {'.import.lock', 'manifest.json', 'manifest.json.migration-part',
                   'config.json', 'config.json.migration-part'}
        for entry in stage.iterdir():
            if entry.name not in allowed: raise ValueError('Unowned staging content')
            read_file(entry)
        manifest_path = stage / 'manifest.json'
        already_complete = False
        if manifest_path.exists():
            previous = json.loads(read_file(manifest_path), object_pairs_hook=_object)
            already_complete = previous.get('state') == 'verified_inactive'
            check = {**expected, 'state': 'verified_inactive' if already_complete else 'copying'}
            if encoded(previous) != encoded(check): raise ValueError('Migration source or plan changed')
        elif any(entry.name != '.import.lock' for entry in stage.iterdir()):
            raise ValueError('Unowned staging content')
        else:
            atomic_write(manifest_path, encoded(expected))
        destination = stage / 'config.json'
        if destination.exists():
            if read_file(destination) != payload: raise ValueError('Staged settings integrity mismatch')
        elif already_complete:
            raise ValueError('Verified settings missing')
        else:
            atomic_write(destination, payload)
            if _after_copy: _after_copy()
        if read_file(config) != raw or read_file(destination) != payload:
            raise ValueError('Settings changed during import')
        atomic_write(manifest_path, encoded({**expected, 'state': 'verified_inactive'}))
        return {'format': FORMAT, 'state': 'verified_inactive', 'plan_digest': expected['plan_digest'],
                'imported_sections': sections, 'untransferred_section_count': pending,
                'idempotent': already_complete, 'activation_allowed': False,
                'writer_fence_verified': False, 'credential_rebind_required': True}
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--staging', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare_settings_import(args.source_snapshot, args.staging)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error',
                          'error_code': 'settings_import_rejected', 'activation_allowed': False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__': raise SystemExit(main())
