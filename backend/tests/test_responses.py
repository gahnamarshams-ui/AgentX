"""Two mocked model calls; real five-role persistence with zero workspace tools."""
import json
from conftest import ProviderResponse as SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient
from app import ai
from app.config import Settings
from app.database import connection
from app.execution.agents import WorkspaceExecutor
from app.plan_schema import TaskPlan
from app.responses import Answer, ChatOrchestrator, ResponseSafety, ResponseExecutor, ResponseVerifier, TextVerificationError
from app.planning import claim, recover_expired
from app.execution.models import AgentState
from app.main import create_app

PLAN = TaskPlan(goal='Explain addition', assumptions=[], constraints=['Text only'], clarification_needed=False,clarification_questions=[],
                steps=[dict(number=1,action='Explain',purpose='Answer request',expected_output='Python example',dependencies=[])])

@pytest.fixture
def setup(tmp_path,monkeypatch):
    s=Settings(_env_file=None,database_path=tmp_path/'chat.sqlite3',workspace_path=tmp_path/'untouched',execution_enabled=False,openai_api_key='test-placeholder-only')
    sdk=MagicMock(); sdk.responses.create=AsyncMock(side_effect=[SimpleNamespace(status='completed',output_parsed=PLAN),SimpleNamespace(status='completed',output_parsed=Answer(markdown='Use this:\n```python\ndef add(a, b):\n    return a + b\n```'))])
    manager=MagicMock();manager.__aenter__=AsyncMock(return_value=sdk);manager.__aexit__=AsyncMock(return_value=False)
    monkeypatch.setattr(ai,'AsyncOpenAI',MagicMock(return_value=manager))
    def no_tools(*args):pytest.fail('Text chat must not execute tools')
    monkeypatch.setattr(WorkspaceExecutor,'execute',no_tools)
    with TestClient(create_app(s),base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as c:yield s,c,sdk


def test_five_real_roles_answer_code_persistence_and_no_tools(setup,monkeypatch):
    s,c,sdk=setup; called=[]
    for cls,method,role in [(ResponseSafety,'assess','Safety'),(ResponseVerifier,'verify','Verifier')]:
        original=getattr(cls,method)
        def wrapper(self,*args,_original=original,_role=role):called.append(_role);return _original(self,*args)
        monkeypatch.setattr(cls,method,wrapper)
    url='/api/tasks/'+c.post('/api/tasks',json={'description':'Show add function'}).json()['id']
    result=c.post(url+'/respond');assert result.status_code==200,result.text
    saved=result.json();assert saved['status']=='completed' and saved['response_mode']=='chat'
    assert 'def add' in saved['final_result']
    assert sdk.responses.create.await_count==2 and called==['Safety','Verifier']
    activity=c.get(url+'/activity').json()
    assert [a['role'] for a in activity['agents']]==['Orchestrator','Planner','Safety','Executor','Verifier']
    assert all(a['status']=='succeeded' for a in activity['agents'])
    assert 'text_checks_only_code_not_executed' in activity['agents'][-1]['checks']
    assert activity['plan_digest'] is None
    history=' '.join(e['message'] for e in c.get(url+'/history').json()['events'])
    for role in ['Orchestrator','Planner','Safety','Executor','Verifier']:assert '['+role+']' in history
    assert c.get(url).json()==saved
    assert c.post(url+'/respond').status_code==409
    with connection(s.database_path) as db:
        assert db.execute('SELECT count(*) FROM execution_runs').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM execution_steps').fetchone()[0]==0
    assert not s.workspace_path.exists()
    # Deletion cleans the new chat evidence too; isolated disposable fixture only.
    assert c.delete(url).status_code==200
    with connection(s.database_path) as db:assert db.execute('SELECT count(*) FROM chat_runs').fetchone()[0]==0


@pytest.mark.parametrize('failure',['Planner','Safety','Executor','Verifier'])
def test_failures_are_persisted_without_claiming_success(setup,failure,monkeypatch):
    s,c,sdk=setup
    if failure=='Planner':sdk.responses.create.side_effect=[ai.PlanningError('ai_unavailable','Provider unavailable.',503)]
    elif failure=='Safety':
        def reject(*args):raise ai.PlanningError('denied','Text policy rejected.',503)
        monkeypatch.setattr(ResponseSafety,'assess',reject)
    elif failure=='Executor':sdk.responses.create.side_effect=[SimpleNamespace(status='completed',output_parsed=PLAN),ai.PlanningError('ai_unavailable','Provider unavailable.',503)]
    else:sdk.responses.create.side_effect=[SimpleNamespace(status='completed',output_parsed=PLAN),SimpleNamespace(status='completed',output_parsed=Answer(markdown='```python\nunfinished'))]
    url='/api/tasks/'+c.post('/api/tasks',json={'description':'Explain addition'}).json()['id']
    assert c.post(url+'/respond').status_code in [502,503]
    task=c.get(url).json();assert task['status']=='failed' and task['final_result'] is None
    roles={a['role']:a for a in c.get(url+'/activity').json()['agents']}
    assert roles[failure]['status']=='failed'
    assert roles['Orchestrator']['status']=='failed'
    events=[e['message'] for e in c.get(url+'/history').json()['events']]
    for role in roles.values():
        assert any(e.startswith(f"[{role['role']}] {role['status']}:") for e in events)
    assert len(events)==len(set(events))
    for method in sdk.responses.create.call_args_list:
        assert 'tools' not in method.kwargs
    assert not s.workspace_path.exists()


def test_history_tracks_transitions_and_verifier_scope(setup):
    s,c,sdk=setup
    url='/api/tasks/'+c.post('/api/tasks',json={'description':'Calculate 125 + 275'}).json()['id']
    assert c.post(url+'/respond').status_code==200
    messages=[e['message'] for e in c.get(url+'/history').json()['events']]
    assert len(messages)==len(set(messages))
    for role in ['Orchestrator','Planner','Safety','Executor','Verifier']:
        assert sum(m.startswith(f'[{role}] running:') for m in messages)==1
        assert sum(m.startswith(f'[{role}] succeeded:') for m in messages)==1
    assert 'factual_correctness_not_verified' in c.get(url+'/activity').json()['agents'][-1]['checks']
    for call in sdk.responses.create.call_args_list:assert 'tools' not in call.kwargs


def test_stale_initializer_cannot_replace_saved_activity(setup):
    s,c,sdk=setup
    task_id=c.post('/api/tasks',json={'description':'Explain addition'}).json()['id']
    token,_=claim(s,task_id)
    states=[AgentState(role=role,status='pending',input_summary='Waiting.',output_summary='Not run.') for role in ['Orchestrator','Planner','Safety','Executor','Verifier']]
    flow=ChatOrchestrator(s)
    flow.save(task_id,token,states,initialize=True)
    with connection(s.database_path) as db:
        db.execute("UPDATE planning_attempts SET token='replacement' WHERE task_id=?",(task_id,))
        before=dict(db.execute('SELECT * FROM chat_runs WHERE task_id=?',(task_id,)).fetchone())
    with pytest.raises(ai.PlanningError):flow.save(task_id,token,states,initialize=True)
    with connection(s.database_path) as db:
        assert dict(db.execute('SELECT * FROM chat_runs WHERE task_id=?',(task_id,)).fetchone())==before
    assert sdk.responses.create.await_count==0 and not s.workspace_path.exists()


def test_interruption_persists_failed_and_skipped_roles_without_replay(setup):
    s,c,sdk=setup
    task_id=c.post('/api/tasks',json={'description':'Explain addition'}).json()['id']
    token,_=claim(s,task_id)
    states=[AgentState(role=role,status='running' if role in ['Orchestrator','Planner'] else 'pending',input_summary='Saved request.',output_summary='Waiting.') for role in ['Orchestrator','Planner','Safety','Executor','Verifier']]
    ChatOrchestrator(s).save(task_id,token,states,initialize=True)
    with connection(s.database_path) as db:
        db.execute("UPDATE planning_attempts SET expires_at='2000-01-01' WHERE task_id=?",(task_id,))
    recover_expired(s.database_path)
    activity=c.get(f'/api/tasks/{task_id}/activity').json()
    assert [a['status'] for a in activity['agents']]==['failed','failed','skipped','skipped','skipped']
    with connection(s.database_path) as db:
        assert json.loads(db.execute('SELECT agents_json FROM chat_runs WHERE task_id=?',(task_id,)).fetchone()[0])==activity['agents']
    messages=[e['message'] for e in c.get(f'/api/tasks/{task_id}/history').json()['events']]
    for a in activity['agents']:assert f"[{a['role']}] {a['status']}: {a['output_summary']}" in messages
    assert sdk.responses.create.await_count==0 and not s.workspace_path.exists()


def test_running_response_cannot_be_deleted_and_chat_rows_do_not_leak(setup):
    s,c,sdk=setup
    url='/api/tasks/'+c.post('/api/tasks',json={'description':'Explain addition'}).json()['id']
    with connection(s.database_path) as db:db.execute("UPDATE tasks SET status='planning' WHERE id=?",(url.split('/')[-1],))
    assert c.delete(url).status_code==409


def statuses(report):
    return {c.name:c.status for c in report.checks}


def test_static_checks_parse_only_and_never_establish_correctness(setup):
    s,c,sdk=setup
    # Parsing must not import this module, raise the exception, or call the function.
    source='import agentx_nonexistent_generated_module\nraise AssertionError("must never run")\ndef add(a, b):\n    return a / 0'
    report=ResponseVerifier(s).verify(Answer(markdown='```python\n'+source+'\n```'),'Show Python function add(a, b)')
    checks=statuses(report)
    assert checks['python_syntax']==checks['requested_function']=='passed'
    assert checks['semantic_correctness']==checks['factual_correctness']=='not_checked'
    assert report.code_executed is False and report.factual_correctness_verified is False
    assert not s.workspace_path.exists() and sdk.responses.create.await_count==0


@pytest.mark.parametrize('source',[
    'def subtract(a, b):\n    return a - b',
    '# def add(a, b): pass\nname = "def add(a, b): pass"',
    'def outer():\n    def add(a, b):\n        return a + b',
])
def test_requested_function_requires_actual_top_level_definition(setup,source):
    s,_,_=setup
    with pytest.raises(TextVerificationError) as exc:
        ResponseVerifier(s).verify(Answer(markdown='```python\n'+source+'\n```'),'Write a Python function named add')
    checks=statuses(exc.value.report)
    assert checks['python_syntax']=='passed' and checks['requested_function']=='failed'
    assert checks['semantic_correctness']=='not_checked'


@pytest.mark.parametrize('text,failed',[
    ('```python\ndef add(:\n    pass\n```','python_syntax'),
    ('```python\n\n```','python_syntax'),
    ('```python\ndef add(a, b):\n    return a+b','code_fences_balanced'),
    ('    ```python\nx = 1','code_fences_balanced'),
    ('~~~python\nx = 1\n```','code_fences_balanced'),
    ('  \n  ','nonempty_answer'),
    ('text\x00','nonempty_answer'),
])
def test_failed_checks_are_explicit_and_later_checks_are_not_claimed(setup,text,failed):
    s,_,_=setup
    with pytest.raises(TextVerificationError) as exc:ResponseVerifier(s).verify(Answer(markdown=text))
    report=exc.value.report
    assert statuses(report)[failed]=='failed'
    assert statuses(report)['requested_function']=='not_checked'
    assert report.code_executed is False and report.factual_correctness_verified is False
    if failed=='nonempty_answer':assert statuses(report)['secret_detection']=='not_checked'


def test_empty_model_and_credentials_do_not_claim_checks_passed_or_leak(setup):
    s,_,_=setup
    with pytest.raises(TextVerificationError) as exc:ResponseVerifier(s).verify(Answer.model_construct(markdown=''))
    assert statuses(exc.value.report)['answer_schema']=='failed'
    assert statuses(exc.value.report)['secret_detection']=='not_checked'
    secret='password=do-not-persist-this'
    with pytest.raises(TextVerificationError) as exc:ResponseVerifier(s).verify(Answer(markdown=secret))
    assert statuses(exc.value.report)['secret_detection']=='failed'
    assert statuses(exc.value.report)['code_fences_balanced']=='not_checked'
    assert exc.value.report.answer_sha256 is None
    assert secret not in exc.value.report.model_dump_json()


@pytest.mark.parametrize('text',['400','```javascript\nconst x = 1;\n```','```\ndef add(a, b): return a+b\n```'])
def test_unknown_language_or_plain_text_remains_unchecked(setup,text):
    s,_,_=setup
    report=ResponseVerifier(s).verify(Answer(markdown=text))
    checks=statuses(report)
    assert checks['python_syntax']==checks['requested_function']=='not_checked'
    assert checks['semantic_correctness']==checks['factual_correctness']=='not_checked'


@pytest.mark.parametrize('source,expected',[
    ('def add(a, b):\n    return a+b','passed'),
    ('def other(a, b):\n    return a+b','failed'),
    ('def add(:\n    pass','failed'),
])
def test_static_report_persists_in_activity_and_task_failures(setup,source,expected):
    s,c,sdk=setup
    sdk.responses.create.side_effect=[SimpleNamespace(status='completed',output_parsed=PLAN),SimpleNamespace(status='completed',output_parsed=Answer(markdown='```python\n'+source+'\n```'))]
    url='/api/tasks/'+c.post('/api/tasks',json={'description':'Show Python function add(a, b)'}).json()['id']
    response=c.post(url+'/respond')
    assert response.status_code==(200 if expected=='passed' else 502)
    activity=c.get(url+'/activity').json()
    verifier=activity['agents'][-1]
    assert verifier['status']==('succeeded' if expected=='passed' else 'failed')
    report=verifier['text_verification']
    assert report['code_executed'] is False and report['factual_correctness_verified'] is False
    assert all(check['status']=='not_checked' for check in report['checks'] if check['category']=='correctness')
    with connection(s.database_path) as db:
        persisted=json.loads(db.execute('SELECT agents_json FROM chat_runs WHERE task_id=?',(url.split('/')[-1],)).fetchone()[0])
        assert persisted[-1]['text_verification']==report
        for table in ['execution_runs','execution_steps','execution_approvals','python_jobs']:
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0
    if expected=='failed':
        assert c.get(url).json()['final_result'] is None
        assert activity['agents'][0]['status']=='failed'
    assert not s.workspace_path.exists()


def test_two_turns_use_saved_context_and_survive_backend_restart(setup):
    s,c,sdk=setup
    sdk.responses.create.side_effect=[SimpleNamespace(status='completed',output_parsed=PLAN),SimpleNamespace(status='completed',output_parsed=Answer(markdown='I will remember blue.')),
                                    SimpleNamespace(status='completed',output_parsed=PLAN),SimpleNamespace(status='completed',output_parsed=Answer(markdown='Blue.'))]
    root=c.post('/api/tasks',json={'description':'My favorite color is blue.'}).json()['id']
    assert c.post(f'/api/tasks/{root}/respond').status_code==200
    second=c.post('/api/tasks',json={'description':'What is my favorite color?','conversation_id':root})
    assert second.status_code==201
    child=second.json()['id']
    assert child!=root and second.json()['conversation_id']==root
    assert c.post(f'/api/tasks/{child}/respond').json()['final_result']=='Blue.'
    for call in sdk.responses.create.call_args_list[2:]:
        payload=json.loads(call.kwargs['input'][0]['content'])
        assert payload['conversation_history']==[{'user':'My favorite color is blue.','assistant':'I will remember blue.','status':'completed'}]
        assert 'tools' not in call.kwargs
    # API read after a new app instance uses the same DB, not browser state.
    with TestClient(create_app(s),base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as restarted:
        turns=restarted.get(f'/api/tasks/{root}/turns').json()['tasks']
        assert [t['id'] for t in turns]==[root,child]
        assert [t['final_result'] for t in turns]==['I will remember blue.','Blue.']
        assert [t['id'] for t in restarted.get('/api/tasks?conversations_only=true&q=favorite').json()['tasks']]==[root]
        for task_id in [root,child]:
            activity=restarted.get(f'/api/tasks/{task_id}/activity').json()
            assert [a['role'] for a in activity['agents']]==['Orchestrator','Planner','Safety','Executor','Verifier']
            assert all(a['status']=='succeeded' for a in activity['agents'])
            assert activity['agents'][-1]['text_verification']['code_executed'] is False
            assert len(restarted.get(f'/api/tasks/{task_id}/history').json()['events'])>=11
    assert not s.workspace_path.exists()
    with connection(s.database_path) as db:
        for table in ['execution_runs','execution_steps','execution_approvals','python_jobs']:
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def test_followup_claim_is_serialized_and_duplicate_respond_never_calls_model(setup):
    from concurrent.futures import ThreadPoolExecutor
    s,c,sdk=setup
    root=c.post('/api/tasks',json={'description':'First turn'}).json()['id']
    assert c.post(f'/api/tasks/{root}/respond').status_code==200
    assert sdk.responses.create.await_count==2
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:c.post('/api/tasks',json={'description':'Follow-up','conversation_id':root}),range(2)))
    assert sorted(r.status_code for r in responses)==[201,409]
    child=next(r.json()['id'] for r in responses if r.status_code==201)
    assert len(c.get(f'/api/tasks/{root}/turns').json()['tasks'])==2
    assert c.post(f'/api/tasks/{root}/respond').status_code==409
    # An older failed turn cannot start alongside (or later overwrite context for) a follow-up.
    with connection(s.database_path) as db:
        db.execute("UPDATE tasks SET status='failed' WHERE id=?",(root,))
        db.execute("UPDATE planning_attempts SET outcome='failed' WHERE task_id=?",(root,))
    assert c.post(f'/api/tasks/{root}/respond').status_code==409
    claim(s,child)  # Model a running request without invoking a real model.
    assert c.post(f'/api/tasks/{child}/respond').status_code==409
    assert c.post('/api/tasks',json={'description':'Duplicate','conversation_id':root}).status_code==409
    assert sdk.responses.create.await_count==2
    assert not s.workspace_path.exists()


def test_conversation_search_archive_and_atomic_group_deletion(setup):
    s,c,sdk=setup
    root=c.post('/api/tasks',json={'description':'Root prompt'}).json()['id']
    assert c.post(f'/api/tasks/{root}/respond').status_code==200
    child=c.post('/api/tasks',json={'description':'Unique followup phrase','conversation_id':root}).json()['id']
    # No model needed here: a failed child is still a saved, searchable turn.
    with connection(s.database_path) as db:db.execute("UPDATE tasks SET status='failed',error='Interrupted' WHERE id=?",(child,))
    assert c.get('/api/tasks?conversations_only=true&q=Unique%20followup').json()['tasks'][0]['id']==root
    assert c.post(f'/api/tasks/{root}/conversation',json={'archived':True}).status_code==200
    assert c.post('/api/tasks',json={'description':'Blocked','conversation_id':root}).status_code==409
    assert c.post(f'/api/tasks/{root}/conversation',json={'archived':False}).status_code==200
    assert c.delete(f'/api/tasks/{child}').status_code==409
    with connection(s.database_path) as db:db.execute("UPDATE tasks SET status='planning' WHERE id=?",(child,))
    assert c.delete(f'/api/tasks/{root}').status_code==409
    with connection(s.database_path) as db:
        db.execute("UPDATE tasks SET status='failed' WHERE id=?",(child,))
        db.execute("CREATE TRIGGER block_root_delete BEFORE DELETE ON tasks WHEN OLD.conversation_id IS NULL BEGIN SELECT RAISE(ABORT,'test'); END")
    assert c.delete(f'/api/tasks/{root}').status_code==503
    assert len(c.get(f'/api/tasks/{root}/turns').json()['tasks'])==2
    with connection(s.database_path) as db:db.execute('DROP TRIGGER block_root_delete')
    assert c.delete(f'/api/tasks/{root}').status_code==200
    for identifier in [root,child]:
        assert c.get(f'/api/tasks/{identifier}').status_code==404
        assert c.get(f'/api/tasks/{identifier}/history').status_code==404
    assert not s.workspace_path.exists()
