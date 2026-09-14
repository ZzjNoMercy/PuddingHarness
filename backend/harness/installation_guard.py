"""Process admission for the independent Harness runtime.

All participating writers keep a shared lease until process exit. An offline
migration coordinator takes the exclusive lease before reading the Home. This
is a cooperative POSIX protocol, not a fence for old binaries, arbitrary
scripts, external databases, or children that do not retain admission.
Managed child call sites inherit the lease; remote/container execution still
requires separate writer authority.
The lock inode must never be removed or replaced.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import stat

try:
    import fcntl
except ImportError:
    fcntl = None

LOCK_NAME = '.installation-gate-v1.lock'
FREEZE_NAME = '.installation-freeze-v1.json'
_process_guard = None


class AdmissionUnavailable(RuntimeError):
    pass


class InstallationGuard:
    def __init__(self, home: Path, *, exclusive: bool = False, allow_frozen: bool = False):
        if not home.is_absolute():
            raise ValueError('Installation Home must be absolute')
        self.home = home.resolve()
        self.exclusive = exclusive
        self.allow_frozen = allow_frozen
        if allow_frozen and not exclusive:
            raise ValueError("Frozen Home inspection requires exclusive admission")
        self.fd = None
        self.authority_fd = None
        self._identity = None

    def acquire(self):
        if self.fd is not None:
            raise RuntimeError('Installation guard is already acquired')
        if fcntl is None:
            raise AdmissionUnavailable('Installation admission requires POSIX flock')
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.home / LOCK_NAME
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AdmissionUnavailable('Installation admission lock must be a private owned regular file')
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AdmissionUnavailable('Installation is busy: stop participating writers before migration; retry startup after migration') from exc
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise AdmissionUnavailable('Installation admission lock changed')
            if not self.allow_frozen:
                self._check_not_frozen()
                from harness.installation_authority import acquire_writer
                self.authority_fd = acquire_writer(self.home)
            self.fd = fd
            self._identity = (info.st_dev, info.st_ino)
            return self
        except BaseException:
            if self.authority_fd is not None:
                os.close(self.authority_fd)
                self.authority_fd = None
            os.close(fd)
            raise

    def _check_not_frozen(self):
        # Existence, including malformed/partial/symlink records, denies writers.
        for name in (FREEZE_NAME, FREEZE_NAME + ".part"):
            try:
                (self.home / name).lstat()
            except FileNotFoundError:
                continue
            raise AdmissionUnavailable("Installation is persistently frozen; startup cannot write")

    def verify(self):
        if self.fd is None:
            raise AdmissionUnavailable('Installation admission is not held')
        current = (self.home / LOCK_NAME).stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != self._identity or current.st_nlink != 1:
            raise AdmissionUnavailable('Installation admission lock changed')
        if not self.allow_frozen:
            self._check_not_frozen()

    def close(self):
        if self.authority_fd is not None:
            os.close(self.authority_fd)
            self.authority_fd = None
        if self.fd is not None:
            # close, rather than LOCK_UN, preserves the lease in forked copies.
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.close()


def installation_home():
    # Keep admission independent of runtime_identity.__init__ and its adapters.
    value = os.environ.get('PUDDINGHARNESS_HOME', '').strip()
    home = Path(value).expanduser() if value else Path.home() / '.puddingharness'
    if not home.is_absolute():
        raise ValueError('PUDDINGHARNESS_HOME must be absolute')
    return home.resolve()


def bound_installation_home():
    """Keep business path factories on the Home admitted for this process."""
    requested = installation_home()
    if _process_guard is not None:
        if requested != _process_guard.home:
            raise AdmissionUnavailable('Cannot change admitted Home through the environment')
        _process_guard.verify()
        return _process_guard.home
    return requested


def inherited_guard_fds():
    """Retain process admission across managed POSIX child startup/parent death."""
    if _process_guard is None:return ()
    _process_guard.verify()
    return tuple(fd for fd in (_process_guard.fd, _process_guard.authority_fd) if fd is not None)


def admit_backend_process():
    """Called before app imports any configuration/registry/worker modules."""
    if fcntl is None:
        guard=InstallationGuard(installation_home())
        guard._check_not_frozen()
        for name in (".installation-authority-v1.json", ".installation-authority-v1.json.part"):
            if (guard.home/name).exists() or (guard.home/name).is_symlink():
                raise AdmissionUnavailable("Enrolled writer authority requires POSIX admission")
        return None
    return _admit_process(exclusive=False)


def _admit_process(*, exclusive):
    global _process_guard
    if _process_guard is not None:
        if _process_guard.home != installation_home() or _process_guard.exclusive != exclusive:
            raise AdmissionUnavailable('Cannot change Home or admission mode within an admitted process')
        _process_guard.verify()
        return _process_guard
    guard = InstallationGuard(installation_home(), exclusive=exclusive).acquire()
    _process_guard = guard
    atexit.register(guard.close)
    return guard
