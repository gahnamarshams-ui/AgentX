"""Windows NTFS access: single-component, directory-handle-relative opens.

Never resolve a checked string and then reopen it by pathname. Hold directory
handles without delete sharing through each operation, open reparse points
themselves and reject them before consuming data. No shell or generated code.
Imported only on Windows; POSIX continues using openat/O_NOFOLLOW.
"""
import ctypes as c
from ctypes import wintypes as w
from contextlib import contextmanager
import msvcrt
import os
from pathlib import Path


class UnicodeString(c.Structure):
    _fields_ = [('Length', w.USHORT), ('MaximumLength', w.USHORT), ('Buffer', w.LPWSTR)]


class ObjectAttributes(c.Structure):
    _fields_ = [('Length', w.ULONG), ('RootDirectory', w.HANDLE),
                ('ObjectName', c.POINTER(UnicodeString)), ('Attributes', w.ULONG),
                ('SecurityDescriptor', w.LPVOID), ('SecurityQualityOfService', w.LPVOID)]


class IOStatus(c.Structure):
    _fields_ = [('Status', c.c_void_p), ('Information', c.c_size_t)]


class FileInfo(c.Structure):
    _fields_ = [('attributes', w.DWORD), ('creation', w.FILETIME),
                ('access', w.FILETIME), ('write', w.FILETIME), ('volume', w.DWORD),
                ('size_high', w.DWORD), ('size_low', w.DWORD), ('links', w.DWORD),
                ('index_high', w.DWORD), ('index_low', w.DWORD)]


kernel = c.WinDLL('kernel32', use_last_error=True)
nt = c.WinDLL('ntdll')
security = c.WinDLL('advapi32', use_last_error=True)
security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(w.LPVOID), c.POINTER(w.ULONG)]
security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
kernel.LocalFree.argtypes = [w.LPVOID]
kernel.LocalFree.restype = w.LPVOID
kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
kernel.CreateFileW.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.CloseHandle.restype = w.BOOL
kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, c.POINTER(FileInfo)]
kernel.GetFileInformationByHandle.restype = w.BOOL
kernel.GetFileType.argtypes = [w.HANDLE]
kernel.GetFileType.restype = w.DWORD
kernel.GetDriveTypeW.argtypes = [w.LPCWSTR]
kernel.GetDriveTypeW.restype = w.UINT
kernel.GetVolumeInformationByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD,
    c.POINTER(w.DWORD), c.POINTER(w.DWORD), c.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
kernel.GetVolumeInformationByHandleW.restype = w.BOOL
nt.NtCreateFile.argtypes = [c.POINTER(w.HANDLE), w.ULONG, c.POINTER(ObjectAttributes),
    c.POINTER(IOStatus), w.LPVOID, w.ULONG, w.ULONG, w.ULONG, w.ULONG, w.LPVOID, w.ULONG]
nt.NtCreateFile.restype = w.LONG
nt.RtlNtStatusToDosError.argtypes = [w.LONG]
nt.RtlNtStatusToDosError.restype = w.ULONG

READ_ATTRIBUTES = 0x80
SYNCHRONIZE = 0x100000
OPEN_REPARSE_POINT = 0x200000
SYNCHRONOUS_NONALERT = 0x20
DIRECTORY_FILE = 1
NON_DIRECTORY_FILE = 0x40


def info(handle, *, directory):
    value = FileInfo()
    if not kernel.GetFileInformationByHandle(handle, c.byref(value)):
        raise c.WinError(c.get_last_error())
    if (kernel.GetFileType(handle) != 1 or value.attributes & (0x400 | 0x40)
            or bool(value.attributes & 0x10) != directory):
        raise OSError('Only ordinary directories/files without reparse points are allowed')
    if not directory and value.links != 1:
        raise OSError('Hardlinked files are not permitted')
    return value


def identity(handle):
    value = info(handle, directory=True)
    return value.volume, (value.index_high << 32) | value.index_low


def open_relative(parent, name, *, directory=False, create=False):
    # All names are a SINGLE component. The kernel never traverses an untrusted
    # intermediate path; each directory has already been opened and inspected.
    if not name or any(ch in name for ch in '/\\:\x00') or name in {'.', '..'}:
        raise OSError('Invalid relative component')
    buffer = c.create_unicode_buffer(name)
    length = len(name.encode('utf-16-le'))
    text = UnicodeString(length, length + 2, c.cast(buffer, w.LPWSTR))
    attrs = ObjectAttributes(c.sizeof(ObjectAttributes), parent, c.pointer(text), 0x40, None, None)
    handle, status = w.HANDLE(), IOStatus()
    access = READ_ATTRIBUTES | SYNCHRONIZE | (0x20 if directory else 2 if create else 1)
    options = OPEN_REPARSE_POINT | SYNCHRONOUS_NONALERT | (DIRECTORY_FILE if directory else NON_DIRECTORY_FILE)
    descriptor = w.LPVOID()
    try:
        if create:
            # Equivalent to POSIX owner-only creation: do not inherit a more
            # permissive parent DACL. OW grants access to the file owner only.
            if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    'D:P(A;;FA;;;OW)', 1, c.byref(descriptor), None):
                raise c.WinError(c.get_last_error())
            attrs.SecurityDescriptor = descriptor
        result = nt.NtCreateFile(c.byref(handle), access, c.byref(attrs), c.byref(status),
            None, 0x80, 0 if create else 1,
            2 if create else 1, options, None, 0)
    finally:
        if descriptor: kernel.LocalFree(descriptor)
    if result < 0:
        raise c.WinError(nt.RtlNtStatusToDosError(result))
    try:
        info(handle, directory=directory)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return handle.value


class WindowsAccess:
    def __init__(self, root: Path):
        self.root = root
        # Remote/UNC, device namespaces and filesystems without stable NTFS file
        # identity are unsupported, rather than falling back to pathname checks.
        if (not root.is_absolute() or not root.drive or len(root.drive) != 2
                or kernel.GetDriveTypeW(root.anchor) != 3):
            raise ValueError('Windows workspace requires a local fixed NTFS drive')
        with self.parent(()) as (handle, _):
            fs = c.create_unicode_buffer(64)
            if not kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, fs, len(fs)):
                raise c.WinError(c.get_last_error())
            if fs.value.upper() != 'NTFS':
                raise ValueError('Windows workspace requires NTFS')
            self.device, self.inode = identity(handle)

    @contextmanager
    def parent(self, parts):
        handles = []
        try:
            handle = kernel.CreateFileW(self.root.anchor, READ_ATTRIBUTES | SYNCHRONIZE | 0x20,
                1, None, 3, 0x02000000 | OPEN_REPARSE_POINT, None)
            if handle == c.c_void_p(-1).value:
                raise c.WinError(c.get_last_error())
            handles.append(handle)
            info(handle, directory=True)
            for component in self.root.parts[1:]:
                handle = open_relative(handle, component, directory=True)
                handles.append(handle)
            # Compare the provisioned root before opening any task-controlled name.
            if hasattr(self, 'device') and identity(handle) != (self.device, self.inode):
                from app.execution.security import ExecutionError
                raise ExecutionError('workspace_changed', 'Workspace identity changed. Start a new task.', 409)
            for component in parts[:-1]:
                handle = open_relative(handle, component, directory=True)
                handles.append(handle)
            yield handle, parts[-1] if parts else None
        finally:
            for handle in reversed(handles): kernel.CloseHandle(handle)

    @staticmethod
    def open_fd(parent, name, *, create=False):
        handle = open_relative(parent, name, create=create)
        try:
            # Ownership of the verified native handle transfers to the CRT fd.
            return msvcrt.open_osfhandle(handle, (os.O_WRONLY if create else os.O_RDONLY) | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
