"""Handle-relative, no-follow access to bounded UTF-8 workspace files.

No pathname is passed to a shell. Symlinks, hardlinks, devices, hidden paths,
credential files and unsupported file types are rejected. Existing files are
never overwritten, and directories must already exist.
"""
import errno
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath
from app.config import Settings
from app.execution.security import ExecutionError, ToolFailure, check_secrets

MAX_BYTES = 65536


def workspace_supported() -> bool:
    return os.name == 'nt' or (hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW')
            and os.open in os.supports_dir_fd)


def safe_path(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    parts = value.split('/')
    if (path.is_absolute() or len(parts) > 8 or any(
        not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. -]{0,79}', p) or p in {'.', '..'}
        or p.endswith((' ', '.'))
        or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}
        or any(word in p.lower() for word in ('credential', 'secret', 'password', 'token', 'api_key'))
        for p in parts) or path.suffix.lower() not in {'.txt', '.md', '.json', '.csv'}):
        raise ExecutionError('unsafe_path', 'Only ordinary .txt, .md, .json and .csv files inside the configured workspace are allowed.', 422)
    return tuple(parts)


class Workspace:
    def __init__(self, settings: Settings):
        if not workspace_supported():
            raise ValueError('Safe workspace execution requires native handle-relative no-follow file access.')
        self.settings = settings
        self.root = settings.workspace_path
        self.native = None
        if os.name == 'nt':
            from app.execution.windows_workspace import WindowsAccess
            self.native = WindowsAccess(self.root)
            self.device, self.inode = self.native.device, self.native.inode
            self.identity = hashlib.sha256(f'{self.root}:{self.device}:{self.inode}'.encode()).hexdigest()
            return
        # Provisioning is an explicit operator action, never a planning side effect.
        # Opening the existing directory also rejects a substituted final symlink.
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        self.identity = hashlib.sha256(f'{self.root}:{info.st_dev}:{info.st_ino}'.encode()).hexdigest()
        self.device, self.inode = info.st_dev, info.st_ino

    @contextmanager
    def parent(self, value: str):
        parts = safe_path(value)
        descriptors = []
        try:
            if self.native is not None:
                with self.native.parent(parts) as parent:
                    yield parent
                return
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (self.device, self.inode):
                raise ExecutionError('workspace_changed', 'Workspace identity changed. Start a new task.', 409)
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
            yield fd, parts[-1]
        except OSError as exc:
            raise ToolFailure('Workspace access failed. Check that directories exist and paths are ordinary files.',
                              retryable=exc.errno in {errno.EAGAIN, errno.EBUSY, errno.EINTR}) from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def read(self, path: str) -> bytes:
        with self.parent(path) as (parent, name):
            fd = (self.native.open_fd(parent, name) if self.native else
                  os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                    raise ToolFailure('Only bounded ordinary files without hardlinks can be read.')
                data = b''
                while len(data) <= MAX_BYTES:
                    chunk = os.read(fd, min(8192, MAX_BYTES + 1 - len(data)))
                    if not chunk: break
                    data += chunk
                if len(data) > MAX_BYTES:
                    raise ToolFailure('Workspace file exceeds the 64 KiB limit.')
                try: text = data.decode('utf-8')
                except UnicodeDecodeError:
                    raise ToolFailure('Workspace file must contain UTF-8 text.') from None
                check_secrets(self.settings, text)
                return data
            finally:
                os.close(fd)

    def create(self, path: str, content: str) -> bytes:
        check_secrets(self.settings, content)
        data = content.encode('utf-8')
        if len(data) > MAX_BYTES:
            raise ToolFailure('Content exceeds the workspace file limit.')
        with self.parent(path) as (parent, name):
            # Exclusive creation: approval never authorizes overwriting an existing file.
            try:
                fd = (self.native.open_fd(parent, name, create=True) if self.native else
                      os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent))
            except FileExistsError:
                raise ToolFailure('Target already exists. Existing files are never overwritten.') from None
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written == 0: raise OSError('Short write')
                    view = view[written:]
                os.fsync(fd)
            except OSError:
                # A file may exist partially. Never retry this mutation automatically.
                raise ToolFailure('File creation was interrupted. Inspect the workspace before creating a new task.') from None
            finally: os.close(fd)
        return data
