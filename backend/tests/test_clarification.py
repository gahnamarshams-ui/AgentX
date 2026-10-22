"""Clarification contracts with mocked models and disposable SQLite only.

Conflict recognition is supplied by the mocked Planner, not proven NLP behavior.
"""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from conftest import ProviderResponse as SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app import ai
from app.config import Settings
from app.database import connection
from app.execution.agents import WorkspaceExecutor
from app.execution.docker_supervisor import DockerAPI
from app.execution.sandbox import SupervisorClient
from app.execution.workspace import Workspace
from app.execution.security import ExecutionError
from app.main import create_app
from app.plan_schema import TaskPlan
from app.responses import Answer, ResponseExecutor, ResponseSafety

REQUEST = 'Write a Launch notice in English, at most 80 words. Schedule it exclusively Monday and exclusively Tuesday.'
QUESTION = 'Monday only and Tuesday only conflict. Which day should I use?'
RESOLUTION = 'Use Tuesday only instead of Monday. Keep the other requirements.'
NEXT = 'Restate the final requirements.'
ADDITION = 'Also require plain-text format. Keep the other requirements.'
BASE = ['Title: Launch', 'Language: English', 'At most 80 words']
RESOLVED_TEXT = 'Launch: Tuesday only; English; at most 80 words.'
EXTENDED_TEXT = RESOLVED_TEXT + ' Plain-text format.'
ROLES = ['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier']


def plan(constraints, question=None):
    return TaskPlan(goal='Prepare the Launch notice', assumptions=[], constraints=constraints,
        clarification_needed=bool(question), clarification_questions=[question] if question else [],
        steps=[dict(number=1, action='Clarify schedule' if question else 'Describe the notice',
                    purpose='Respect the requirements', expected_output=question or 'Notice requirements', dependencies=[])])


@pytest.fixture
def chat(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_path=tmp_path/'clarification.sqlite3',
        workspace_path=tmp_path/'untouched', execution_enabled=False, python_execution_enabled=False,
        openai_api_key='test-placeholder-only')
    def forbidden(*args, **kwargs): pytest.fail('No runtime or workspace tools permitted')
    for cls, method in [(Workspace, 'read'), (Workspace, 'create'), (WorkspaceExecutor, 'execute'),
                        (WorkspaceExecutor, 'execute_step'), (DockerAPI, 'request'), (DockerAPI, 'attach'),
                        (SupervisorClient, 'request')]:
        monkeypatch.setattr(cls, method, forbidden)
    state = SimpleNamespace(plans=[plan(BASE+['Monday only', 'Tuesday only'], QUESTION),
                                 plan(BASE+['Tuesday only']), plan(BASE+['Tuesday only', 'Plain-text format'])],
                            answers=[RESOLVED_TEXT, EXTENDED_TEXT], latest=None, pause=False,
                            entered=threading.Event(), release=threading.Event())
    async def parse(**kwargs):
        if kwargs['text']['format']['name'] == 'TaskPlan':
            if state.pause:
                state.entered.set()
                assert await asyncio.to_thread(state.release.wait, 5), 'Test did not release mocked planner'
            result = state.plans.pop(0)
            state.latest = result
        else:
            assert kwargs['text']['format']['name'] == 'Answer'
            # A permissive answer model would silently choose Monday; the app must not use it.
            result = Answer(markdown='I chose Monday without asking.') if state.latest.clarification_needed else Answer(markdown=state.answers.pop(0))
        return SimpleNamespace(status='completed', output_parsed=result)
    sdk = MagicMock()
    sdk.responses.create = AsyncMock(side_effect=parse)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=sdk)
    manager.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(ai, 'AsyncOpenAI', MagicMock(return_value=manager))
    with TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1',1234)) as client:
        yield settings, client, sdk, state
    assert settings.execution_enabled is settings.python_execution_enabled is False
    assert not settings.workspace_path.exists()
    with connection(settings.database_path) as db:
        for table in ['execution_runs','execution_steps','execution_approvals','python_jobs']:
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def turn(client, description, root=None):
    created = client.post('/api/tasks', json={'description':description, **({'conversation_id':root} if root else {})})
    assert created.status_code == 201, created.text
    result = client.post('/api/tasks/'+created.json()['id']+'/respond')
    assert result.status_code == 200, result.text
    return result.json()


def clarification(client):
    first = turn(client, REQUEST)
    assert first['status'] == 'awaiting_user_input'
    assert QUESTION in first['final_result']
    assert 'I chose Monday' not in first['final_result']
    return first


def saved_plan(settings, task):
    with connection(settings.database_path) as db:
        return json.loads(db.execute('SELECT plan_json FROM chat_runs WHERE task_id=?',(task['id'],)).fetchone()[0])


def assert_context(sdk, previous, current):
    expected = [{'user':t['description'], 'assistant':t['final_result'], 'status':t['status']} for t in previous]
    # The latest resolved turn must supply identical saved context to BOTH model calls.
    for call in sdk.responses.create.call_args_list[-2:]:
        payload = json.loads(call.kwargs['input'][0]['content'])
        assert payload['conversation_history'] == expected
        assert payload.get('current_request', payload.get('request')) == current
        assert 'tools' not in call.kwargs


def test_conflicting_plan_requests_clarification_without_silently_selecting_rule(chat):
    settings, client, sdk, _ = chat
    first = clarification(client)
    stored = saved_plan(settings, first)
    assert stored['clarification_needed'] is True and QUESTION in stored['clarification_questions']
    assert len(stored['clarification_questions']) == 2
    assert stored['clarification_questions'][0].endswith('Which should I use?')
    assert all(day in stored['clarification_questions'][0] for day in ['Monday','Tuesday'])
    assert stored['constraints'] == BASE+['Monday only', 'Tuesday only']
    # The Planner already supplied the question; no answer-model call should choose a rule.
    assert sdk.responses.create.await_count == 1
    assert sdk.responses.create.call_args.kwargs['input'][0]['content'] == REQUEST
    assert 'tools' not in sdk.responses.create.call_args.kwargs
    activity = client.get('/api/tasks/'+first['id']+'/activity').json()
    assert [a['role'] for a in activity['agents']] == ROLES
    assert all(a['status']=='succeeded' for a in activity['agents'])
    assert 'clarification' in activity['agents'][3]['output_summary'].lower()
    assert activity['agents'][-1]['text_verification']['factual_correctness_verified'] is False


def test_user_clarification_resolves_conflict_in_same_conversation(chat):
    settings, client, sdk, _ = chat
    first = clarification(client)
    second = turn(client, RESOLUTION, first['id'])
    assert second['conversation_id'] == first['id'] and second['id'] != first['id']
    assert_context(sdk, [first], RESOLUTION)
    stored = saved_plan(settings, second)
    assert stored['clarification_needed'] is False and stored['clarification_questions'] == []
    assert stored['constraints'] == BASE+['Tuesday only']
    assert second['final_result'] == RESOLVED_TEXT
    assert client.get('/api/tasks/'+first['id']).json()['final_result'] == first['final_result']
    assert sdk.responses.create.await_count == 3


def test_resolved_requirement_is_retained_after_reload_in_next_turn(chat):
    settings, client, sdk, state = chat
    first = clarification(client)
    second = turn(client, RESOLUTION, first['id'])
    state.plans[-1] = plan(BASE+['Tuesday only'])
    state.answers[-1] = RESOLVED_TEXT
    with TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1',1234)) as reloaded:
        third = turn(reloaded, NEXT, first['id'])
        assert_context(sdk, [first, second], NEXT)
        assert saved_plan(settings, third)['constraints'] == BASE+['Tuesday only']
        assert third['final_result'] == RESOLVED_TEXT
        restored = reloaded.get('/api/tasks/'+first['id']+'/turns').json()['tasks']
        assert restored == [first, second, third]
        for task in restored:
            activity = reloaded.get('/api/tasks/'+task['id']+'/activity').json()
            assert [a['role'] for a in activity['agents']] == ROLES
            assert all(a['status']=='succeeded' for a in activity['agents'])
            assert activity['agents'][-1]['text_verification']['code_executed'] is False
            assert len(reloaded.get('/api/tasks/'+task['id']+'/history').json()['events']) >= 11
    assert sdk.responses.create.await_count == 5


def test_new_requirement_preserves_earlier_resolved_requirements(chat):
    settings, client, sdk, _ = chat
    first = clarification(client)
    first_plan = saved_plan(settings, first)
    second = turn(client, RESOLUTION, first['id'])
    third = turn(client, ADDITION, first['id'])
    assert_context(sdk, [first, second], ADDITION)
    assert saved_plan(settings, third)['constraints'] == BASE+['Tuesday only', 'Plain-text format']
    assert third['final_result'] == EXTENDED_TEXT
    assert saved_plan(settings, second)['constraints'] == BASE+['Tuesday only']
    assert saved_plan(settings, first) == first_plan
    assert QUESTION in first_plan['clarification_questions']


def test_duplicate_clarification_submissions_do_not_repeat_model_calls_or_history(chat):
    _, client, sdk, state = chat
    first = clarification(client)
    root = first['id']
    original_history = client.get(f'/api/tasks/{root}/history').json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(lambda _:client.post('/api/tasks',json={'description':RESOLUTION,'conversation_id':root}),range(2)))
    assert sorted(r.status_code for r in created) == [201,409]
    child = next(r.json()['id'] for r in created if r.status_code == 201)
    state.pause = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f'/api/tasks/{child}/respond')
        try:
            assert state.entered.wait(5)
            before = client.get(f'/api/tasks/{child}/history').json()
            assert sdk.responses.create.await_count == 2
            assert client.post(f'/api/tasks/{child}/respond').status_code == 409
            assert client.post('/api/tasks',json={'description':RESOLUTION,'conversation_id':root}).status_code == 409
            assert client.get(f'/api/tasks/{child}/history').json() == before
            assert sdk.responses.create.await_count == 2
        finally:
            state.release.set()
        assert pending.result(timeout=5).status_code == 200
    assert sdk.responses.create.await_count == 3
    completed_history = client.get(f'/api/tasks/{child}/history').json()
    assert client.post(f'/api/tasks/{child}/respond').status_code == 409
    assert sdk.responses.create.await_count == 3
    assert client.get(f'/api/tasks/{child}/history').json() == completed_history
    assert client.get(f'/api/tasks/{root}/history').json() == original_history
    tasks = client.get(f'/api/tasks/{root}/turns').json()['tasks']
    assert [t['id'] for t in tasks] == [root,child]
    for task in tasks:
        messages = [e['message'] for e in client.get('/api/tasks/'+task['id']+'/history').json()['events']]
        for role in ROLES:
            assert sum(m.startswith(f'[{role}] running:') for m in messages) == 1
            assert sum(m.startswith(f'[{role}] succeeded:') for m in messages) == 1


@pytest.mark.parametrize('kind', ['secret', 'malformed_fence'])
def test_clarification_output_still_passes_secret_and_verifier_checks(chat, kind):
    settings, client, sdk, state = chat
    question = 'password=do-not-persist-this' if kind == 'secret' else '```python\nunfinished'
    state.plans[0] = plan(BASE+['Monday only', 'Tuesday only'], question)
    task_id = client.post('/api/tasks',json={'description':REQUEST}).json()['id']
    url = '/api/tasks/'+task_id
    result = client.post(url+'/respond')
    assert result.status_code == (422 if kind == 'secret' else 502)
    task = client.get(url).json()
    assert task['status'] == 'failed' and task['final_result'] is None
    assert sdk.responses.create.await_count == 1
    agents = client.get(url+'/activity').json()['agents']
    assert agents[0]['status'] == 'failed'
    if kind == 'secret':
        assert agents[1]['status'] == 'failed'
        with connection(settings.database_path) as db:
            assert db.execute('SELECT plan_json FROM chat_runs WHERE task_id=?',(task_id,)).fetchone()[0] is None
        assert question not in json.dumps(task)+result.text+client.get(url+'/history').text
    else:
        assert agents[-1]['status'] == 'failed'
        checks = {c['name']:c['status'] for c in agents[-1]['text_verification']['checks']}
        assert checks['code_fences_balanced'] == 'failed' and checks['factual_correctness'] == 'not_checked'


@pytest.mark.parametrize('prompt,left,right',[
    ('Write an English notice titled Launch, maximum 80 words, scheduled exclusively Monday and exclusively Tuesday.', 'Monday', 'Tuesday'),
    ('The output format must be exclusively JSON and exclusively YAML.', 'JSON', 'YAML'),
    ('Set the color to only red and only blue.', 'red', 'blue'),
    ('The delivery mode is exclusively email and exclusively SMS.', 'email', 'SMS'),
])
@pytest.mark.parametrize('planner_clarifies',[False,True])
def test_explicit_exclusive_conflict_is_caught_even_when_planner_misses_it(chat,prompt,left,right,planner_clarifies):
    settings,client,sdk,state=chat
    # Reproduce both a false negative and the actual live flag-with-unrelated-questions case.
    state.plans[0]=plan(BASE,'What is the nature of the launch?' if planner_clarifies else None)
    result=turn(client,prompt)
    assert left.lower() in result['final_result'].lower() and right.lower() in result['final_result'].lower()
    assert '?' in result['final_result']
    assert sdk.responses.create.await_count==1, 'Final artifact model must not run for an unresolved conflict'
    stored=saved_plan(settings,result)
    assert stored['clarification_needed'] is True
    safety=client.get('/api/tasks/'+result['id']+'/activity').json()['agents'][2]
    assert any('exclusive_conflict' in c for c in safety['checks'])
    assert 'clarification' in safety['output_summary'].lower()


def test_missed_conflict_stays_blocked_until_explicit_user_resolution(chat):
    settings,client,sdk,state=chat
    state.plans=[plan(BASE) for _ in range(3)]+[plan(BASE+['Tuesday only']),plan(BASE+['Tuesday only','Plain-text format'])]
    first=turn(client,'Write a Launch notice in English, at most 80 words, scheduled exclusively Monday and exclusively Tuesday.')
    root=first['id']
    assert saved_plan(settings,first)['clarification_needed'] is True
    second=turn(client,'Also require plain-text format.',root)
    assert saved_plan(settings,second)['clarification_needed'] is True
    third=turn(client,'Do not use Tuesday.',root)  # A rejection alone must not be mistaken for positive selection.
    assert saved_plan(settings,third)['clarification_needed'] is True
    assert sdk.responses.create.await_count==3
    fourth=turn(client,RESOLUTION,root)
    assert saved_plan(settings,fourth)['clarification_needed'] is False
    assert_context(sdk,[first,second,third],RESOLUTION)
    fifth=turn(client,ADDITION,root)
    assert saved_plan(settings,fifth)['constraints']==BASE+['Tuesday only','Plain-text format']
    assert_context(sdk,[first,second,third,fourth],ADDITION)
    assert sdk.responses.create.await_count==7


@pytest.mark.parametrize('prompt',[
    'The schedule is exclusively Monday or exclusively Tuesday.',
    'The schedule is exclusively Monday and exclusively Monday.',
    'The language is exclusively English and the format is exclusively JSON.',
    'Do not require the schedule to be exclusively Monday and exclusively Tuesday.',
    'Explain the phrase "scheduled exclusively Monday and exclusively Tuesday".',
    'Use only JSON and only English.',  # No explicit shared attribute; do not infer one.
])
def test_exclusivity_guard_does_not_invent_conflicts_from_ambiguous_or_nonconflicting_text(chat,prompt):
    settings,client,sdk,state=chat
    state.plans[0]=plan(BASE)
    result=turn(client,prompt)
    assert saved_plan(settings,result)['clarification_needed'] is False
    assert sdk.responses.create.await_count==2


def test_unrelated_planner_clarification_does_not_hide_detected_conflict(chat):
    settings,client,sdk,state=chat
    state.plans[0]=plan(BASE,'What tone should the notice use?')
    result=turn(client,'The schedule is exclusively Monday and exclusively Tuesday.')
    questions=saved_plan(settings,result)['clarification_questions']
    assert any('Monday' in q and 'Tuesday' in q for q in questions)
    assert 'What tone should the notice use?' in questions
    assert sdk.responses.create.await_count==1


@pytest.mark.parametrize('reply',[
    'Use Tuesday or Monday.',
    'Use Tuesday, or Monday.',
    'Use Tuesday. Use Monday.',
    'If possible, use Tuesday.',
    'Use Tuesday?',
    'Use Tuesday. Do not use Tuesday.',
])
def test_ambiguous_user_choice_does_not_resolve_an_exclusive_conflict(chat,reply):
    settings,client,sdk,state=chat
    state.plans=[plan(BASE),plan(BASE)]
    first=turn(client,'The schedule is exclusively Monday and exclusively Tuesday.')
    second=turn(client,reply,first['id'])
    assert saved_plan(settings,second)['clarification_needed'] is True
    assert sdk.responses.create.await_count==2


@pytest.mark.parametrize('reply',[
    'Use Tuesday only, not Monday.',
    'Use Tuesday only. Do not change the title.',
    'Tuesday.',
])
def test_explicit_positive_choice_resolves_conflict_without_dropping_other_requirements(chat,reply):
    settings,client,sdk,state=chat
    state.plans[0]=plan(BASE)
    first=turn(client,'The schedule is exclusively Monday and exclusively Tuesday.')
    second=turn(client,reply,first['id'])
    assert saved_plan(settings,second)['clarification_needed'] is False
    assert saved_plan(settings,second)['constraints']==BASE+['Tuesday only']
    assert_context(sdk,[first],reply)
    assert sdk.responses.create.await_count==3


def test_executor_refuses_exact_unresolved_conflict_even_if_safety_was_bypassed(chat):
    settings,_,sdk,_=chat
    prompt='Write an English notice titled Launch, maximum 80 words, scheduled exclusively Monday and exclusively Tuesday.'
    sdk.responses.create.side_effect=lambda **kwargs:SimpleNamespace(status='completed',output_parsed=Answer(markdown='Launch on Monday and Tuesday.'))
    with pytest.raises(ExecutionError,match='Unresolved exclusive requirements'):
        asyncio.run(ResponseExecutor(settings).answer(prompt,plan(BASE),[]))
    assert sdk.responses.create.await_count==0


def test_executor_allows_explicit_resolution_and_retains_original_context(chat):
    settings,_,sdk,_=chat
    prompt='Write an English notice titled Launch, maximum 80 words, scheduled exclusively Monday and exclusively Tuesday.'
    context=[{'user':prompt,'assistant':'Which day should I use?','status':'completed'}]
    sdk.responses.create.side_effect=lambda **kwargs:SimpleNamespace(status='completed',output_parsed=Answer(markdown=RESOLVED_TEXT))
    answer=asyncio.run(ResponseExecutor(settings).answer(RESOLUTION,plan(BASE+['Tuesday only']),context))
    assert answer.markdown==RESOLVED_TEXT and sdk.responses.create.await_count==1
    payload=json.loads(sdk.responses.create.call_args.kwargs['input'][0]['content'])
    assert payload['conversation_history']==context and payload['request']==RESOLUTION
    assert payload['internal_plan']['constraints']==BASE+['Tuesday only']


def test_http_request_cannot_generate_artifact_if_safety_conflict_review_regresses(chat,monkeypatch):
    _,client,sdk,state=chat
    state.plans[0]=plan(BASE)
    monkeypatch.setattr(ResponseSafety,'review_conflicts',lambda self,description,proposal,context:(proposal,[]))
    prompt='Write an English notice titled Launch, maximum 80 words, scheduled exclusively Monday and exclusively Tuesday.'
    task_id=client.post('/api/tasks',json={'description':prompt}).json()['id']
    response=client.post('/api/tasks/'+task_id+'/respond')
    assert response.status_code==409 and response.json()['error']['code']=='unresolved_conflict'
    assert sdk.responses.create.await_count==1
    saved=client.get('/api/tasks/'+task_id).json()
    assert saved['status']=='failed' and saved['final_result'] is None
    agents=client.get('/api/tasks/'+task_id+'/activity').json()['agents']
    assert agents[3]['status']=='failed' and agents[4]['status']=='skipped'
    assert 'Unresolved exclusive requirements' in agents[3]['error']


def test_dates_question_mentioning_both_values_cannot_replace_required_choice(chat):
    settings,client,sdk,state=chat
    dates_question='What are the exact dates or times for the launch on Monday and Tuesday?'
    state.plans[0]=plan(BASE,dates_question)
    prompt='Write an English notice titled Launch, maximum 80 words, scheduled exclusively Monday and exclusively Tuesday.'
    result=turn(client,prompt)
    questions=saved_plan(settings,result)['clarification_questions']
    assert 'Monday' in questions[0] and 'Tuesday' in questions[0]
    assert 'Which should I use?' in questions[0]
    assert dates_question in questions  # Do not discard other Planner questions silently.
    assert result['final_result'].startswith(questions[0])
    assert sdk.responses.create.await_count==1
