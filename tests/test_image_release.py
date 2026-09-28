import importlib.util
from pathlib import Path
import pytest

path=Path(__file__).parents[1]/'ops/deploy/image_release.py'
spec=importlib.util.spec_from_file_location('image_release',path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
IMAGE=m.REGISTRY+'@sha256:'+'a'*64
OLD=m.REGISTRY+'@sha256:'+'b'*64
SHA='c'*40

class Fake:
    def __init__(self):
        self.calls=[];self.images={s:OLD for _,s in m.SERVICES};self.repo=None
        self.status='SUCCESS';self.replica=1;self.health='/health';self.busy=False;self.bad_meta=False
    def __call__(self,q,v,**kwargs):
        self.calls.append((q,v))
        if q==m.INSTANCE:return {'serviceInstance':dict(source=dict(image=self.images[v['s']],repo=self.repo),numReplicas=self.replica,healthcheckPath=self.health,latestDeployment=dict(status='BUILDING' if self.busy else 'SUCCESS'))}
        if q==m.UPDATE:
            self.images[v['s']]=v['i']['source']['image'];return {'serviceInstanceUpdate':True}
        if q==m.DEPLOY:return {'serviceInstanceDeployV2':v['s']}
        if q==m.STATUS:return {'deployment':dict(id=v['id'],status=self.status,projectId=m.PROJECT,environmentId=m.ENVIRONMENT,serviceId=v['id'],meta=dict(image=OLD if self.bad_meta else self.images[v['id']]))}
        raise AssertionError(q)

@pytest.mark.parametrize('image',[m.REGISTRY+':latest','ghcr.io/other/repo@sha256:'+'a'*64,m.REGISTRY+'@sha256:short'])
def test_unpinned_or_foreign_images_never_touch_railway(image,tmp_path):
    api=Fake()
    with pytest.raises(m.ReleaseError):m.promote(image,SHA,api,head=lambda:SHA,receipt_path=tmp_path/'receipt.json')
    assert api.calls==[]

def test_superseded_commit_never_touches_railway(tmp_path):
    api=Fake()
    with pytest.raises(m.ReleaseError,match='Superseded'):m.promote(IMAGE,SHA,api,head=lambda:'d'*40,receipt_path=tmp_path/'receipt.json')
    assert not api.calls

@pytest.mark.parametrize('field,value',[('repo',m.REPOSITORY),('health',None),('busy',True)])
def test_all_targets_preflight_before_any_mutation(field,value,tmp_path):
    api=Fake();setattr(api,field,value)
    with pytest.raises(m.ReleaseError):m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=tmp_path/'receipt.json')
    assert not any(q in (m.UPDATE,m.DEPLOY) for q,_ in api.calls)

def test_same_digest_sequential_deployments_and_receipts(tmp_path):
    api=Fake();path=tmp_path/'receipt.json'
    result=m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=path)
    assert [i['name'] for i in result['services']]==['dashboard','collector','engine']
    assert all(i['previous_image']==OLD and i['status']=='SUCCESS' and i['verified'] for i in result['services'])
    changes=[v for q,v in api.calls if q==m.UPDATE]
    assert all(v['i']==dict(source=dict(image=IMAGE)) for v in changes)
    assert path.exists()
    assert [q for q,_ in api.calls].count(m.DEPLOY)==3

@pytest.mark.parametrize('replicas',[None,1,2])
def test_image_promotion_never_rewrites_regional_or_legacy_topology(tmp_path,replicas):
    api=Fake();api.replica=replicas
    result=m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=tmp_path/'receipt.json')
    assert all(item['verified'] for item in result['services'])
    assert all(set(v['i'])=={'source'} for q,v in api.calls if q==m.UPDATE)

@pytest.mark.parametrize('bad_meta',[False,True])
def test_failed_or_unproven_deployment_stops_remaining_services(tmp_path,bad_meta):
    api=Fake();api.bad_meta=bad_meta
    if not bad_meta:api.status='FAILED'
    with pytest.raises(m.ReleaseError):m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=tmp_path/'receipt.json')
    assert len([q for q,_ in api.calls if q==m.UPDATE])==1
    assert api.images[m.SERVICES[1][1]]==OLD

def test_mid_rollout_new_main_does_not_deploy_old_code_to_next_service(tmp_path):
    api=Fake();heads=iter([SHA,SHA,'d'*40])
    with pytest.raises(m.ReleaseError,match='Main changed'):m.promote(IMAGE,SHA,api,head=lambda:next(heads),receipt_path=tmp_path/'receipt.json')
    assert len([q for q,_ in api.calls if q==m.DEPLOY])==1

def test_missing_deployment_token_fails_clearly():
    with pytest.raises(m.ReleaseError,match='RAILWAY_TOKEN'):m.Railway(None)

def test_http_diagnostics_identify_host_and_status_without_credentials(monkeypatch):
    def fail(request,timeout):
        assert request.get_header('User-agent')=='market-compass-image-promotion/1.0'
        raise m.urllib.error.HTTPError(request.full_url,403,'secret-value',{},None)
    monkeypatch.setattr(m.urllib.request,'urlopen',fail)
    with pytest.raises(m.ReleaseError) as caught:
        m.request_json('https://example.com/private?token=secret-value',headers={'Authorization':'secret-value'})
    assert 'HTTP 403 from example.com' in str(caught.value)
    assert 'secret-value' not in str(caught.value)


def test_poll_timeout_preserves_receipt_and_stops_rollout(tmp_path):
    api=Fake();api.status='DEPLOYING';clock=iter([0,0,901])
    receipt=tmp_path/'receipt.json'
    with pytest.raises(m.ReleaseError,match='timed out'):
        m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=receipt,
                  clock=lambda:next(clock),sleep=lambda _:None)
    assert receipt.exists()
    assert len([q for q,_ in api.calls if q==m.DEPLOY])==1


def test_socket_read_timeout_is_a_clean_release_error(monkeypatch):
    def fail(request,timeout=30):
        raise TimeoutError('The read operation timed out')
    monkeypatch.setattr(m.urllib.request,'urlopen',fail)
    with pytest.raises(m.UnknownOutcomeError,match='outcome unknown'):
        m.request_json('https://example.com/graphql',body={'query':'{x}'})
    # UnknownOutcomeError is still a ReleaseError for the top-level handler.
    with pytest.raises(m.ReleaseError):
        m.request_json('https://example.com/graphql',body={'query':'{x}'})


def test_socket_timeout_never_leaks_credentials(monkeypatch):
    def fail(request,timeout=30):
        raise TimeoutError('The read operation timed out')
    monkeypatch.setattr(m.urllib.request,'urlopen',fail)
    with pytest.raises(m.ReleaseError) as caught:
        m.request_json('https://example.com/private?token=secret-value',
                       headers={'Authorization':'secret-value'})
    assert 'secret-value' not in str(caught.value)
    assert 'example.com' in str(caught.value)


def test_idempotent_reads_retry_transient_timeouts(monkeypatch):
    attempts={'n':0}
    class Resp:
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def read(self):return b'{"data":{}}'
    def flaky(request,timeout=30):
        attempts['n']+=1
        if attempts['n']<3:raise TimeoutError('The read operation timed out')
        return Resp()
    monkeypatch.setattr(m.urllib.request,'urlopen',flaky)
    monkeypatch.setattr(m.time,'sleep',lambda _:None)
    assert m.request_json('https://example.com/graphql',retries=3)=={'data':{}}
    assert attempts['n']==3


class DeployTimeoutFake(Fake):
    """DEPLOY raises UnknownOutcomeError; latestDeployment id shows whether it landed."""
    def __init__(self,landed):
        super().__init__();self.landed=landed;self.new_id='dep-new-dashboard';self.deploy_attempted=set()
    def __call__(self,q,v,**kwargs):
        if q==m.INSTANCE:
            self.calls.append((q,v))
            sid=v['s']
            if self.landed and sid==m.SERVICES[0][1] and sid in self.deploy_attempted:
                latest=dict(id=self.new_id,status='SUCCESS')
            else:
                latest=dict(id='dep-old',status='SUCCESS')
            return {'serviceInstance':dict(source=dict(image=self.images[sid],repo=self.repo),
                    numReplicas=self.replica,healthcheckPath=self.health,latestDeployment=latest)}
        if q==m.DEPLOY:
            if v['s']==m.SERVICES[0][1]:
                self.calls.append((q,v))
                self.deploy_attempted.add(v['s'])
                raise m.UnknownOutcomeError('The read operation timed out')
            return super().__call__(q,v,**kwargs)
        if q==m.STATUS:
            self.calls.append((q,v))
            sid=m.SERVICES[0][1] if v['id']==self.new_id else v['id']
            return {'deployment':dict(id=v['id'],status='SUCCESS',projectId=m.PROJECT,
                    environmentId=m.ENVIRONMENT,serviceId=sid,meta=dict(image=self.images[sid]))}
        return super().__call__(q,v,**kwargs)


def test_deploy_timeout_that_landed_is_adopted_and_verified(tmp_path):
    api=DeployTimeoutFake(landed=True)
    result=m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=tmp_path/'receipt.json')
    first=result['services'][0]
    assert first['verified'] and first['status']=='SUCCESS'
    assert first['deployment_id']=='dep-new-dashboard'
    # Rollout continued to the remaining services.
    assert len([q for q,_ in api.calls if q==m.DEPLOY])==3


def test_deploy_timeout_with_no_deployment_is_safe_to_retry(tmp_path):
    api=DeployTimeoutFake(landed=False)
    with pytest.raises(m.ReleaseError,match='safe to retry'):
        m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=tmp_path/'receipt.json')
    # Remaining services held: only one UPDATE was attempted.
    assert len([q for q,_ in api.calls if q==m.UPDATE])==1


class UpdateTimeoutFake(Fake):
    """UPDATE raises UnknownOutcomeError after the image update actually landed."""
    def __call__(self,q,v,**kwargs):
        if q==m.UPDATE:
            self.calls.append((q,v))
            self.images[v['s']]=v['i']['source']['image']
            raise m.UnknownOutcomeError('The read operation timed out')
        return super().__call__(q,v,**kwargs)


def test_update_timeout_with_confirmed_image_continues_rollout(tmp_path):
    api=UpdateTimeoutFake()
    result=m.promote(IMAGE,SHA,api,head=lambda:SHA,receipt_path=tmp_path/'receipt.json')
    assert all(i['verified'] for i in result['services'])
    assert all(img==IMAGE for img in api.images.values())
