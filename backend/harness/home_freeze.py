"""Persistent cooperative Harness Home freeze; no activation or thaw authority.

Only participating Harness entry points use this admission protocol. Old binaries, external writers
and Home/lock replacement are outside this protocol. An incomplete publication
also denies startup; it must never be interpreted as an unfrozen installation.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from harness.installation_guard import InstallationGuard, FREEZE_NAME

FORMAT = 'puddingharness-home-freeze/v1'
MAX_BYTES = 4096


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _root(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Freeze Home must be absolute and unlinked')
    if not path.is_dir():
        raise ValueError('Freeze Home must already exist')
    return path.resolve()


def _read(path, *, links=1):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != links or info.st_mode & 0o077 or info.st_size > MAX_BYTES:
            raise ValueError('Freeze record must be private and bounded')
        data = os.read(fd, MAX_BYTES + 1)
        current = path.stat(follow_symlinks=False)
        if len(data) > MAX_BYTES or (info.st_dev, info.st_ino, info.st_size) != (current.st_dev, current.st_ino, current.st_size):
            raise ValueError('Freeze record changed')
        return data, info
    finally:
        os.close(fd)


def _sync_directory(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


@contextmanager
def _cli_freeze_gate(root):
    gate = root / '.installation-cli-admission'
    try:
        gate.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ValueError('CLI admission is busy or unresolved') from error
    try:
        leases = root / '.installation-cli-leases'
        if leases.exists() or leases.is_symlink():
            info = leases.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ValueError('CLI admission tickets are invalid')
            if next(leases.iterdir(), None) is not None:
                raise ValueError('CLI write operation is active or unresolved')
        _sync_directory(root)
        yield
    finally:
        gate.rmdir()
        _sync_directory(root)


@contextmanager
def _legacy_backend_exclusion(root):
    # Refuse a known already-running backend even if it predates this protocol.
    # This cannot stop a nonparticipating old binary from starting later.
    state = root / 'state'
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        raise ValueError('Invalid existing backend state directory')
    path = state / 'backend.lease'
    if not path.exists() and not path.is_symlink():
        yield
        return
    import fcntl
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise ValueError('Invalid existing backend lease')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def freeze_home(home, operation_id, *, _after_part=None, _after_link=None):
    if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', operation_id):
        raise ValueError('Invalid freeze operation ID')
    root = _root(home)
    with InstallationGuard(root, exclusive=True, allow_frozen=True) as guard, _cli_freeze_gate(root), _legacy_backend_exclusion(root):
        identity = root.stat()
        value = {'format': FORMAT, 'operation_id': operation_id, 'state': 'home_frozen',
                 'home_identity': hashlib.sha256(str(root).encode()).hexdigest(),
                 'directory_identity': {'device': identity.st_dev, 'inode': identity.st_ino},
                 'installation_cutover_performed': False, 'external_writers_fenced': False,
                 'covered_protocols': ['python-process-admission/v1','cli-admission-ticket/v1']}
        data = _encoded(value)
        target = root / FREEZE_NAME; part = root / (FREEZE_NAME + '.part')
        if target.exists() or target.is_symlink():
            has_part = part.exists() or part.is_symlink()
            recorded, target_info = _read(target, links=2 if has_part else 1)
            if recorded != data:
                raise ValueError('Freeze operation or Home identity changed')
            if has_part:
                staged, part_info = _read(part, links=2)
                if staged != data or (target_info.st_dev, target_info.st_ino) != (part_info.st_dev, part_info.st_ino):
                    raise ValueError('Freeze publication part mismatch')
                part.unlink()
        else:
            if part.exists() or part.is_symlink():
                staged, _ = _read(part)
                if staged != data:
                    raise ValueError('Incomplete or conflicting freeze publication; writers remain denied')
            else:
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    offset = 0
                    while offset < len(data): offset += os.write(fd, data[offset:])
                    os.fsync(fd)
                finally: os.close(fd)
            _sync_directory(root)
            if _after_part: _after_part()
            os.link(part, target)
            _sync_directory(root)
            if _after_link: _after_link()
            part.unlink()
        _sync_directory(root)
        guard.verify()
        if _read(target)[0] != data:
            raise ValueError('Freeze publication changed')
        return {**value, 'receipt_sha256': hashlib.sha256(data).hexdigest(), 'activation_allowed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--operation-id', required=True)
    args = parser.parse_args()
    try:
        result = freeze_home(args.home, args.operation_id)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'home_freeze_rejected', 'activation_allowed': False}))
        return 1
    print(json.dumps({'status': 'home_frozen', **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
