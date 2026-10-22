"""Portable policy and real native filesystem boundary tests, no model calls."""
import os
import pytest
from pydantic import ValidationError
from app.config import Settings
from app.execution import agents
from app.execution.agents import PolicySafety
from app.execution.models import AGENT_ROLES, AgentState
from app.execution.security import ExecutionError, ToolFailure
from app.execution.workspace import Workspace, safe_path
from test_execution import plan


def test_only_five_roles_and_capabilities_belong_to_executor_and_verifier():
    assert AGENT_ROLES == ('Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier')
    for name in ('Research', 'Developer', 'Testing', 'Review'):
        with pytest.raises(ValidationError):
            AgentState(role=name, status='pending', input_summary='', output_summary='')
    assert all(hasattr(agents.WorkspaceExecutor, method) for method in ('research', 'implement', 'test_json', 'execute'))
    assert all(hasattr(agents.OutputVerifier, method) for method in ('review', 'verify'))
    assert not any(hasattr(agents, name) for name in ('SourceResearch', 'JsonImplementation', 'JsonTesting', 'OutputReview'))


@pytest.mark.parametrize('path', ['CON.txt', 'aux.json', 'NUL.csv', 'com1.md', 'lpt9.txt', 'folder./x.json', 'folder /x.txt', 'a.json:stream', 'C:/x.json', '//server/share/x.txt'])
def test_portable_paths_reject_windows_aliases_and_devices(path):
    with pytest.raises(ExecutionError): safe_path(path)


def test_case_aliases_cannot_bypass_writer_dependencies(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path/'state.db')
    value = plan().model_dump()
    value['steps'][1]['tool']['path'] = 'OUTPUT.JSON'
    value['steps'][1]['dependencies'] = []
    from app.execution.models import ExecutionPlan
    with pytest.raises(ExecutionError): PolicySafety(settings).assess(ExecutionPlan.model_validate(value))


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path/'workspace'; root.mkdir()
    settings = Settings(_env_file=None, database_path=tmp_path/'state.db', workspace_path=root)
    return Workspace(settings)


def test_real_nested_read_write_no_overwrite_and_changed_root(workspace, tmp_path):
    (workspace.root/'nested').mkdir()
    assert workspace.create('nested/data.json', '{"budget":25000}') == b'{"budget":25000}'
    assert workspace.read('nested/data.json') == b'{"budget":25000}'
    with pytest.raises(ToolFailure): workspace.create('nested/data.json', 'overwrite')
    workspace.root.rename(tmp_path/'original')
    workspace.root.mkdir()
    with pytest.raises(ExecutionError, match='identity changed'): workspace.create('other.txt', 'blocked')
    assert not (workspace.root/'other.txt').exists()


def make_junction(link, destination):
    """Trusted Windows fixture: create a junction via an explicit control code.

    No shell, admin privilege, or application execution capability is added.
    """
    import ctypes as c
    from ctypes import wintypes as w
    import struct
    from app.execution.windows_workspace import kernel
    link.mkdir(exist_ok=True)
    handle = kernel.CreateFileW(str(link), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
    substitute = ('\\??\\' + str(destination)).encode('utf-16-le')
    display = str(destination).encode('utf-16-le')
    names = substitute + b'\0\0' + display + b'\0\0'
    payload = struct.pack('<LHHHHHH', 0xA0000003, len(names)+8, 0, 0, len(substitute), len(substitute)+2, len(display)) + names
    buffer = c.create_string_buffer(payload)
    transferred = w.DWORD()
    kernel.DeviceIoControl.argtypes = [w.HANDLE,w.DWORD,w.LPVOID,w.DWORD,w.LPVOID,w.DWORD,c.POINTER(w.DWORD),w.LPVOID]
    kernel.DeviceIoControl.restype = w.BOOL
    try:
        if not kernel.DeviceIoControl(handle, 0x900A4, buffer, len(payload), None, 0, c.byref(transferred), None):
            raise c.WinError(c.get_last_error())
    finally: kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != 'nt', reason='Windows-specific native handle tests')
@pytest.mark.parametrize('operation', ['read', 'create'])
def test_junction_cannot_escape_workspace(workspace, tmp_path, operation):
    outside = tmp_path/'outside'; outside.mkdir()
    (outside/'data.txt').write_text('outside', encoding='utf-8')
    make_junction(workspace.root/'alias', outside)
    with pytest.raises((ToolFailure, ExecutionError)):
        if operation == 'read': workspace.read('alias/data.txt')
        else: workspace.create('alias/new.txt', 'must not escape')
    assert not (outside/'new.txt').exists()
    assert (outside/'data.txt').read_text(encoding='utf-8') == 'outside'


@pytest.mark.skipif(os.name != 'nt', reason='Windows-specific native handle tests')
def test_native_parent_handles_prevent_rename_during_operation(workspace):
    directory = workspace.root/'nested'; directory.mkdir()
    with workspace.parent('nested/data.txt'):
        with pytest.raises(OSError): directory.rename(workspace.root/'replacement')
    assert directory.is_dir()


@pytest.mark.skipif(os.name != 'nt', reason='Windows-specific native handle test')
def test_native_parent_cannot_be_converted_to_junction_mid_operation(workspace, tmp_path):
    directory = workspace.root/'nested'; directory.mkdir()
    outside = tmp_path/'outside'; outside.mkdir()
    with workspace.parent('nested/data.txt'):
        with pytest.raises(OSError): make_junction(directory, outside)
    assert not directory.is_junction()


@pytest.mark.skipif(os.name != 'nt', reason='Windows-specific native handle tests')
def test_native_read_handle_prevents_concurrent_writer(workspace):
    workspace.create('data.txt', 'approved')
    with workspace.parent('data.txt') as (parent, name):
        fd = workspace.native.open_fd(parent, name)
        try:
            with pytest.raises(OSError): (workspace.root/'data.txt').write_text('tampered', encoding='utf-8')
        finally: os.close(fd)
    assert workspace.read('data.txt') == b'approved'


@pytest.mark.skipif(os.name != 'nt', reason='Windows-specific native ACL test')
def test_native_creation_uses_protected_owner_only_acl(workspace):
    import ctypes as c
    from ctypes import wintypes as w
    from app.execution.windows_workspace import kernel, security
    workspace.create('private.txt', 'approved')
    descriptor, text = w.LPVOID(), w.LPWSTR()
    security.GetNamedSecurityInfoW.argtypes = [w.LPWSTR,w.DWORD,w.DWORD,w.LPVOID,w.LPVOID,w.LPVOID,w.LPVOID,c.POINTER(w.LPVOID)]
    security.GetNamedSecurityInfoW.restype = w.DWORD
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [w.LPVOID,w.DWORD,w.DWORD,c.POINTER(w.LPWSTR),w.LPVOID]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
    assert security.GetNamedSecurityInfoW(str(workspace.root/'private.txt'), 1, 4, None,None,None,None,c.byref(descriptor)) == 0
    try:
        assert security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, c.byref(text), None)
        assert 'P' in text.value.split('(')[0] and text.value.count('(') == 1
        assert ';;;OW)' in text.value
    finally:
        if text: kernel.LocalFree(c.cast(text, w.LPVOID))
        kernel.LocalFree(descriptor)
