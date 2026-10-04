"""Synthetic end-to-end contracts. No capture worker or native input starts."""
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from gamelens.app import Dispatch, GameLens
from gamelens.arbiter import Action, Rejection
from gamelens.mcp import GameLensClient, ToolError
from gamelens.perception_bridge import PerceptionViews
from gamelens.reviewed_learning import ReviewedLearningStore
from gamelens.run_metrics import RunMetrics
from gamelens.run_store import LocalRunStore
from gamelens.server import Encoded, NO_FRAME, Tokens, create_app
from tests.test_rebind import lens

@pytest.fixture
def runtime(lens):
    lens.port=8777
    lens.tokens=Tokens()
    lens.perception_views=PerceptionViews()
    lens.run_metrics=RunMetrics()
    lens.run_id=lens.run_metrics.begin_run()
    lens.run_store=LocalRunStore()
    lens._metrics_context=threading.local()
    lens.reviewed_learning=ReviewedLearningStore()
    lens.capture.frames.latest_id=lambda: lens.capture.frames.frame.frame_id
    lens.calls=[]
    lens.submit_click=lambda **kw: (lens.calls.append(kw) or Dispatch('ok','dry',action_id=7))
    lens.submit_sequence=lambda **kw: (lens.calls.append(kw) or Dispatch('ok','dry',action_id=8))
    lens.sequence_capacity=lambda: 10
    return lens

def client(runtime, role='agent'):
    c=TestClient(create_app(runtime),base_url='http://127.0.0.1:8777')
    c.headers['X-GameLens-Token']=getattr(runtime.tokens,role)
    return c

def proposal():
    return dict(skill_id='fixture',failure_evidence={'outcome':'failed','evidence':{'observation':'obs-fixture'}},
                candidate={'verification_hint':'Inspect a new result frame'},
                test_evidence={'passed':True,'evidence':{'fixture':'regression-example'}})

def test_crop_http_preserves_parent_and_maps_exactly(runtime):
    c=client(runtime)
    image=c.get('/frame.jpg?crop=minimap')
    assert image.status_code==200
    token=image.headers['X-GameLens-Observation']
    metadata=json.loads(image.headers['X-GameLens-Perception'])
    assert metadata['action_authority']=='none'
    assert runtime.observations.resolve(token) is None
    original={'kind':'click','observation_id':token,'x':2,'y':3,'rebind':True}
    expected=runtime.perception_views.translate(original)
    response=c.post('/act',json=original)
    assert response.status_code==200
    assert runtime.calls[0]['observation_id']==expected['observation_id']
    assert runtime.calls[0]['x']==expected['x'] and runtime.calls[0]['y']==expected['y']
    assert runtime.calls[0]['rebind'] is True

def test_crop_sequence_coordinate_free_click_is_preserved(runtime):
    c=client(runtime)
    token=c.get('/frame.jpg?crop=dialog').headers['X-GameLens-Observation']
    response=c.post('/act',json={'kind':'sequence','observation_id':token,
        'steps':[{'do':'move','x':3,'y':4},{'do':'click'}]})
    assert response.status_code==200
    assert runtime.calls

def test_crop_bounds_and_anchor_fail_before_submission(runtime):
    c=client(runtime)
    token=c.get('/frame.jpg?crop=hud').headers['X-GameLens-Observation']
    for extras in ({'x':-1,'y':0},{'x':0,'y':0,'anchor':{}},{'x':True,'y':0}):
        assert c.post('/act',json={'kind':'click','observation_id':token,**extras}).status_code==400
    assert runtime.calls==[]

def test_crop_registry_eviction_fails_closed(runtime):
    runtime.perception_views.capacity=1
    c=client(runtime)
    old=c.get('/frame.jpg?crop=hud').headers['X-GameLens-Observation']
    c.get('/frame.jpg?crop=dialog')
    assert c.post('/act',json={'kind':'click','observation_id':old,'x':0,'y':0}).status_code==400
    assert runtime.calls==[]

def test_retirement_during_crop_encode_never_issues_image(runtime,monkeypatch):
    import gamelens.perception as p
    original=p.encode_perception
    def retire(*args,**kw):
        result=original(*args,**kw)
        runtime.capture.backend.retired=True
        return result
    monkeypatch.setattr(p,'encode_perception',retire)
    assert runtime.encode_frame(crop='hud') is NO_FRAME

def test_guarded_dispatch_pending_then_terminal_correlates(runtime):
    parent=runtime.encode_frame()
    observation=runtime.observations.resolve(parent.observation_id)
    action=Action(observation,[])
    runtime._metrics_context.value={'observation_id':parent.observation_id,'kind':'sequence'}
    result=runtime._dispatch(action,None,None,'fixture',wait=0)
    assert result.outcome=='pending'
    sequence=runtime.arbiter._executor.submitted[-1]
    sequence.on_outcome(SimpleNamespace(status='sent',detail='',completed_steps=1,injected_steps=1,
                                       last_completed_step=0,partial=False))
    snapshot=runtime.run_metrics.snapshot(runtime.run_id)
    actions=[e for e in snapshot['events'] if e['kind']=='action']
    outcomes=[e for e in snapshot['events'] if e['kind']=='outcome']
    assert actions[-1]['action_id']==str(action.action_id)
    assert actions[-1]['observation_id']==parent.observation_id
    assert actions[-1]['bound_observation_id']=='action-source-'+str(action.action_id)
    assert actions[-1]['log_id'] is not None
    assert [e['outcome'] for e in outcomes]==['pending','sent']
    assert outcomes[-1]['objective_success'] is None
    assert outcomes[-1]['after_frame']==1 and outcomes[-1]['last_completed_step']==0

def test_metrics_failure_cannot_change_dispatch(runtime):
    parent=runtime.encode_frame()
    action=Action(runtime.observations.resolve(parent.observation_id),[])
    runtime.run_metrics.record_action=lambda *a,**kw: (_ for _ in ()).throw(OSError('fixture failure'))
    assert runtime._dispatch(action,None,None,'fixture',wait=0).outcome=='pending'
    assert len(runtime.arbiter._executor.submitted)==1

def test_metrics_checkpoint_disabled_and_owner_report_is_separate(runtime):
    c=client(runtime)
    assert c.get('/metrics').status_code==200
    assert c.post('/metrics/checkpoint').status_code==403
    owner=client(runtime,'operator')
    assert owner.post('/metrics/checkpoint').json()['saved'] is False
    report={'objective_id':'fixture','success':True,'evidence':['obs-fixture']}
    assert c.post('/metrics/objective',json=report).status_code==403
    assert owner.post('/metrics/objective',json=report).status_code==200
    assert runtime.run_metrics.snapshot(runtime.run_id)['objective_success'] is None
    assert runtime.run_metrics.snapshot(runtime.run_id)['events'][-1]['kind']=='objective'

def test_learning_requires_exact_hash_operator_review_and_is_inert(runtime):
    agent=client(runtime)
    p=agent.post('/learning/proposals',json=proposal())
    assert p.status_code==200
    p=p.json()
    path='/learning/'+p['id']
    review={'candidate_hash':p['candidate_hash'],'decision':'approve'}
    assert agent.post(path+'/review',json=review).status_code==403
    owner=client(runtime,'operator')
    assert owner.post(path+'/activate',json={'candidate_hash':p['candidate_hash']}).status_code==400
    assert owner.post(path+'/review',json={**review,'candidate_hash':'wrong'}).status_code==400
    assert owner.post(path+'/review',json=review).status_code==200
    activated=owner.post(path+'/activate',json={'candidate_hash':p['candidate_hash']})
    assert activated.status_code==200 and activated.json()['runtime_activation'] is False
    assert runtime.calls==[]

def test_learning_body_bound_and_safety_changes_denied(runtime):
    c=client(runtime)
    assert c.post('/learning/proposals',content=b'x'*65537).status_code==413
    p=proposal();p['candidate']={'safety_policy':'disable guard'}
    assert c.post('/learning/proposals',json=p).status_code==400
    assert runtime.reviewed_learning.list_proposals()==[]

@pytest.mark.parametrize('path',['/metrics','/skills','/learning'])
def test_evidence_endpoints_require_auth_and_correct_origin(runtime,path):
    unauth=TestClient(create_app(runtime),base_url='http://127.0.0.1:8777')
    assert unauth.get(path).status_code==401
    assert client(runtime).get(path,headers={'Origin':'https://untrusted.example'}).status_code==403
    assert client(runtime).get(path).status_code==200

def test_catalog_has_references_and_no_remote_execution(runtime):
    c=client(runtime)
    catalog=c.get('/skills').json()
    assert len(catalog['skills'])==3 and all(s['reference_only'] for s in catalog['skills'])
    assert c.post('/skills/run',json={}).status_code==404
    assert runtime.calls==[]

def test_mcp_cropped_frame_mapping_roundtrips_and_old_server_falls_back():
    gl=GameLensClient('http://127.0.0.1:8777',None)
    requests=[]
    def request(path,body=None):
        requests.append(path)
        return 200,b'jpeg',{'X-GameLens-Observation':'full-obs','X-GameLens-Frame':'1'}
    gl.request=request
    content,error=gl.tool_see({'crop':'hud'})
    assert not error and gl.shown=='full-obs'
    assert len(requests)==2 and 'crop=hud' in requests[0] and 'crop=' not in requests[1]
    assert 'full-frame fallback' in content[0]['text']
    gl.request=lambda path,body=None:(200,b'jpeg',{'X-GameLens-Observation':'view-fixture',
                         'X-GameLens-Perception':'not-json'})
    with pytest.raises(ToolError):gl.tool_see({'crop':'hud'})
    assert gl.shown is None


def test_constructor_default_and_opt_in_do_not_start_or_write(monkeypatch,tmp_path):
    import gamelens.app as app
    import gamelens.recording as recording
    monkeypatch.setattr(app,'find_window',lambda target:SimpleNamespace(hwnd=1234,title='fixture'))
    for name in ('CaptureSupervisor','GeometryTracker','SafetySupervisor','InputExecutor','Arbiter'):
        monkeypatch.setattr(app,name,lambda *a,**kw:SimpleNamespace())
    monkeypatch.setattr(recording,'Recorder',lambda *a,**kw:SimpleNamespace())
    default=GameLens('fixture')
    opted=GameLens('fixture',metrics_directory=tmp_path)
    assert not default.run_store.enabled and opted.run_store.enabled
    assert not (tmp_path/'run-metrics').exists()
    assert opted.run_metrics.snapshot(opted.run_id)['status']=='running'


def test_opt_in_http_checkpoint_retains_closed_schema(runtime,tmp_path):
    runtime.run_store=LocalRunStore(tmp_path,enabled=True)
    runtime.encode_frame(crop='hud')
    result=client(runtime,'operator').post('/metrics/checkpoint')
    assert result.status_code==200 and result.json()['saved'] is True
    stored=runtime.run_store.snapshots()
    assert len(stored)==1 and stored[0]['run_id']==runtime.run_id
    assert all(e['kind']=='observation' for e in stored[0]['events'])


@pytest.mark.parametrize('tool,path',[('tool_metrics','/metrics'),('tool_skills','/skills'),('tool_learning','/learning')])
def test_mcp_evidence_never_dispatches_or_changes_observation(tool,path):
    gl=GameLensClient('http://127.0.0.1:8777',None)
    gl.shown='original-obs'
    requests=[]
    gl.request=lambda p,body=None:(requests.append((p,body)) or (200,b'{}',{}))
    result,error=getattr(gl,tool)({})
    assert not error and requests==[(path,None)] and gl.shown=='original-obs'
    with pytest.raises(ToolError):getattr(gl,tool)({'owner':'fake'})


def test_expired_runtime_run_rotates_before_resumed_evidence(runtime):
    import time
    clock=[time.monotonic()]
    runtime.run_metrics=RunMetrics(max_age_seconds=1,monotonic=lambda:clock[0])
    runtime.run_id=runtime.run_metrics.begin_run()
    old=runtime.run_id
    clock[0]+=2
    assert client(runtime).get('/metrics').json()['run_id']!=old
    resumed=runtime.run_id
    runtime.encode_frame()
    snapshot=runtime.metrics_snapshot()
    assert snapshot['run_id']==resumed and snapshot['events'][-1]['kind']=='observation'
    assert not runtime.run_metrics.has_run(old)


def test_stalled_durable_learning_cannot_block_stop_or_unbound_admission(runtime,tmp_path):
    import asyncio
    import httpx
    import time
    store=ReviewedLearningStore(tmp_path/'learning.json')
    original=store._commit
    entered,release=threading.Event(),threading.Event()
    def stalled(records):
        entered.set()
        if not release.wait(2):raise RuntimeError('fixture deadline')
        original(records)
    store._commit=stalled
    runtime.reviewed_learning=store
    killed=[]
    runtime.safety=SimpleNamespace(kill=lambda reason:killed.append(reason))
    runtime.state=lambda:{'safety':{'killed':bool(killed)}}
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url='http://127.0.0.1:8777',headers={'X-GameLens-Token':runtime.tokens.operator}) as c:
            start=time.monotonic()
            mutation=asyncio.create_task(c.post('/learning/proposals',json=proposal()))
            async def stop_probe():
                await asyncio.sleep(.02)
                answer=await c.post('/stop')
                return answer,time.monotonic()-start
            reads=[asyncio.create_task(c.get("/learning")) for _ in range(64)]
            stop_task=asyncio.create_task(stop_probe())
            try:
                answer,elapsed=await asyncio.wait_for(stop_task,timeout=.75)
                assert entered.is_set() and not mutation.done()
                assert answer.status_code==200 and elapsed<.75 and killed
                assert all(reply.status_code==429 for reply in await asyncio.gather(*reads))
                assert (await c.post('/learning/proposals',json=proposal())).status_code==429
                assert (await c.post('/metrics/objective',json={'objective_id':'o','success':True,'evidence':[]})).status_code==429
            finally:
                release.set()
            assert (await mutation).status_code==200
    asyncio.run(exercise())


def test_cancelled_evidence_request_retains_worker_admission_until_completion(runtime):
    import asyncio
    import httpx
    entered,release=threading.Event(),threading.Event()
    original=runtime.reviewed_learning.propose
    def stalled(**kw):
        entered.set()
        if not release.wait(2):raise RuntimeError('fixture deadline')
        return original(**kw)
    runtime.reviewed_learning.propose=stalled
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url='http://127.0.0.1:8777',headers={'X-GameLens-Token':runtime.tokens.agent}) as c:
            task=asyncio.create_task(c.post('/learning/proposals',json=proposal()))
            try:
                for _ in range(100):
                    if entered.is_set():break
                    await asyncio.sleep(.005)
                assert entered.is_set()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
                assert (await c.post('/learning/proposals',json=proposal())).status_code==429
            finally:release.set()
            for _ in range(100):
                if runtime.reviewed_learning.list_proposals():break
                await asyncio.sleep(.005)
            assert runtime.reviewed_learning.list_proposals()
    asyncio.run(exercise())


@pytest.mark.parametrize('operation',[' MOVE ','Move',' CLICK ','Click'])
def test_crop_sequence_canonical_operations_map_nonzero_origin(runtime,operation):
    from gamelens.arbiter import PointAt
    c=client(runtime)
    image=c.get('/frame.jpg?crop=dialog')
    view=json.loads(image.headers['X-GameLens-Perception'])
    left,top,right,bottom=view['rect']
    assert left>0 and top>0
    px=(left+3*(right-left)/view['image_width'])*view['parent']['scale']
    py=(top+4*(bottom-top)/view['image_height'])*view['parent']['scale']
    response=c.post('/act',json={'kind':'sequence','observation_id':image.headers['X-GameLens-Observation'],
                                'steps':[{'do':operation,'x':3,'y':4}]})
    assert response.status_code==200
    points=[step for step in runtime.calls[-1]['steps'] if isinstance(step,PointAt)]
    assert len(points)==1 and points[0].x==pytest.approx(px) and points[0].y==pytest.approx(py)


@pytest.mark.parametrize('warning', ['', 'Capture session or size changed; recording stopped.'])
def test_orderly_shutdown_final_clip_is_deduplicated_and_persisted(runtime,tmp_path,warning):
    runtime.run_store=LocalRunStore(tmp_path,enabled=True)
    clip={'active':False,'error':warning,'file':str(tmp_path/'fixture.mp4'),'frames':60,'seconds':2.0}
    events=[]
    runtime.recorder=SimpleNamespace(stop=lambda:(events.append('recorder') or dict(clip)))
    runtime.safety=SimpleNamespace(kill=lambda reason:events.append('kill'),shutdown=lambda:None)
    runtime._stop=SimpleNamespace(set=lambda:None)
    runtime._poller=None;runtime.agent=None
    runtime.executor=SimpleNamespace(shutdown=lambda:None)
    runtime.capture.stop=lambda:None
    assert runtime.link_recording(clip) is True
    runtime.stop()
    assert events[:2]==['kill','recorder']
    saved=runtime.run_store.snapshots()
    assert len(saved)==1 and saved[0]['status']=='completed'
    videos=[e for e in saved[0]['events'] if e['kind']=='video']
    assert len(videos)==1 and videos[0]['reference']=='fixture.mp4' and videos[0]['end_seconds']==2.0
    assert videos[0].get('interrupted', False) is bool(warning)


@pytest.mark.parametrize('warning', ['', 'No fresh capture frame; recording stopped.'])
def test_shutdown_links_new_clip_before_finishing_run(runtime,tmp_path,warning):
    runtime.run_store=LocalRunStore(tmp_path,enabled=True)
    clip={'active':False,'error':warning,'file':str(tmp_path/'new.mp4'),'frames':30,'seconds':1.0}
    runtime.recorder=SimpleNamespace(stop=lambda:clip)
    runtime.safety=SimpleNamespace(kill=lambda reason:None,shutdown=lambda:None)
    runtime._stop=SimpleNamespace(set=lambda:None);runtime._poller=None;runtime.agent=None
    runtime.executor=SimpleNamespace(shutdown=lambda:None);runtime.capture.stop=lambda:None
    runtime.stop()
    kinds=[e['kind'] for e in runtime.run_store.snapshots()[0]['events']]
    assert kinds.index('video')<kinds.index('terminal')


@pytest.mark.parametrize('warning', ['', 'Capture session or size changed; recording stopped.'])
def test_http_stop_links_published_interrupted_clip(runtime,tmp_path,warning):
    clip={'active':False,'error':warning,'file':str(tmp_path/'http.mp4'),'frames':30,'seconds':1.0}
    runtime.recorder=SimpleNamespace(stop=lambda:dict(clip))
    with client(runtime) as c:
        result=c.post('/recording/stop')
        assert result.status_code==200 and result.json()['error']==warning
        assert c.post('/recording/stop').status_code==200
    videos=[event for event in runtime.metrics_snapshot()['events'] if event['kind']=='video']
    assert len(videos)==1 and videos[0]['reference']=='http.mp4'
    assert videos[0].get('interrupted', False) is bool(warning)
    assert warning not in json.dumps(videos) if warning else True


@pytest.mark.parametrize('invalid', [{'active':True}, {'file':None}, {'frames':0}, {'frames':-1}, {'frames':True}])
def test_clip_link_refuses_unpublished_or_active_snapshots(runtime,invalid):
    clip={'active':False,'error':'Interrupted or invalid finalization','file':'fixture.mp4','frames':30,'seconds':1.0}
    clip.update(invalid)
    assert runtime.link_recording(clip) is False
    assert not [event for event in runtime.metrics_snapshot()['events'] if event['kind']=='video']


def test_http_multireview_byte_capacity_allows_rejection(runtime,tmp_path):
    path=tmp_path/'reviewed.json'
    runtime.reviewed_learning=ReviewedLearningStore(path)
    body=dict(skill_id='demo',failure_evidence={'outcome':'failed','evidence':'x'*60079},
              candidate={'description':'Observe'},test_evidence={'passed':True,'evidence':'fixture'})
    assert len(json.dumps(body,separators=(',',':')).encode())<65536
    with client(runtime,'operator') as c:
        result=c.post('/learning/proposals',json=body)
        assert result.status_code==200
        original=result.json()
        url='/learning/'+original['id']
        review={'candidate_hash':original['candidate_hash'],'decision':'approve'}
        for notes, count in [('n'*4096,46),('',34)]:
            for _ in range(count):
                result=c.post(url+'/review',json={**review,'notes':notes})
                assert result.status_code==200
        result=c.post(url+'/activate',json={'candidate_hash':original['candidate_hash']})
        assert result.status_code==200 and result.json()['proposal']['active'] is True
        result=c.post(url+'/review',json={**review,'decision':'reject'})
        assert result.status_code==200
        rejected=result.json()
    assert rejected['active'] is False and rejected['history'][-1]['decision']=='reject'
    assert rejected['proposal']==original['proposal'] and rejected['candidate_hash']==original['candidate_hash']
    persisted=json.loads(path.read_text())[original['id']]
    assert persisted['active'] is False and persisted['review']['decision']=='reject'
    assert ReviewedLearningStore(path).get(original['id'])['proposal']==original['proposal']
