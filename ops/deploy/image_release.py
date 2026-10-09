"""Promote one CI-tested image; never build, change credentials or reconnect sources."""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

REPOSITORY = 'cyberstryder/market-compass'
REGISTRY = 'ghcr.io/' + REPOSITORY
PROJECT = '4801dbfb-a7a9-411b-abef-696df393f0b1'
ENVIRONMENT = '711a06df-cbcd-4ee8-8332-b60be4c17723'
SERVICES = (
    ('dashboard', '33e96fc3-e153-4944-a131-b5944c12e902'),
    ('collector', 'a4d135aa-4e2a-4e0b-b0f3-6777a878418b'),
    ('engine', 'b5a4b496-4359-45a4-b69e-8b0cfe5f2209'),
)
API = 'https://backboard.railway.com/graphql/v2'
INSTANCE = '''query($s:String!,$e:String!){serviceInstance(serviceId:$s,environmentId:$e){
  source{image repo} numReplicas healthcheckPath latestDeployment{id status}
}}'''
UPDATE = '''mutation($s:String!,$e:String!,$i:ServiceInstanceUpdateInput!){
  serviceInstanceUpdate(serviceId:$s,environmentId:$e,input:$i)}'''
DEPLOY = '''mutation($s:String!,$e:String!){
  serviceInstanceDeployV2(serviceId:$s,environmentId:$e)}'''
STATUS = '''query($id:String!){deployment(id:$id){id status serviceId environmentId projectId meta}}'''
BUSY = {'WAITING','QUEUED','INITIALIZING','BUILDING','DEPLOYING','NEEDS_APPROVAL'}
FAILED = {'FAILED','CRASHED','REMOVED','REMOVING','SKIPPED'}


class ReleaseError(RuntimeError):
    pass


class UnknownOutcomeError(ReleaseError):
    """A network/timeout failure where a mutation may or may not have landed.

    Never blindly retried: callers must reconcile (re-read the affected
    object) before deciding whether the operation took effect.
    """


def validate(image, revision):
    if not re.fullmatch(re.escape(REGISTRY) + r'@sha256:[0-9a-f]{64}', image):
        raise ReleaseError('An immutable digest in the Compass GHCR repository is required')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ReleaseError('A full source commit SHA is required')


def request_json(url, body=None, headers=None, timeout=30, retries=0):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={'Accept':'application/json','Content-Type':'application/json',
                 'User-Agent':'market-compass-image-promotion/1.0', **(headers or {})})
    host = urllib.parse.urlsplit(url).hostname
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ReleaseError(f'API HTTP {error.code} from {host}; inspect deployment state before retrying') from None
        except (TimeoutError, OSError, ValueError) as error:
            # TimeoutError (socket read timeouts surface as the builtin, not
            # URLError) and other transport failures: the operation's outcome
            # is unknown. Never print request headers, credentials or
            # response bodies. Idempotent reads may opt into bounded retries;
            # mutations must reconcile instead of retrying blindly.
            if attempt < retries:
                attempt += 1
                time.sleep(min(2 ** attempt, 10))
                continue
            raise UnknownOutcomeError(
                f'API {type(error).__name__} from {host}; outcome unknown; inspect deployment state before retrying') from None


class Railway:
    def __init__(self, token):
        if not token:raise ReleaseError('Missing RAILWAY_TOKEN GitHub Actions secret')
        self.token = token

    def __call__(self, query, variables, timeout=30, retries=0):
        result = request_json(API, {'query':query,'variables':variables}, {'Project-Access-Token':self.token},
                              timeout=timeout, retries=retries)
        if result.get('errors') or 'data' not in result:
            raise ReleaseError('Railway rejected the operation; no automatic retry')
        return result['data']


def current_main():
    token = os.environ.get('GH_TOKEN')
    if not token:raise ReleaseError('Missing GitHub token for source revision verification')
    return request_json('https://api.github.com/repos/'+REPOSITORY+'/git/ref/heads/main',
        headers={'Authorization':'Bearer '+token})['object']['sha']


def save_receipt(path, receipt):
    Path(path).write_text(json.dumps(receipt, indent=2)+'\n')


def promote(image, revision, api, head=current_main, sleep=time.sleep,
            clock=time.monotonic, receipt_path='deployment-receipt.json', timeout=900):
    validate(image, revision)
    if head()!=revision:raise ReleaseError('Superseded commit: refusing to replace newer main')
    instances = {}
    # Inspect every target before changing any. Initial source migration and
    # registry credentials must be configured explicitly in Railway first.
    for name, service in SERVICES:
        instance = api(INSTANCE, {'s':service,'e':ENVIRONMENT}, retries=3)['serviceInstance']
        source = instance.get('source') or {}
        if source.get('repo') or not (source.get('image') or '').startswith(REGISTRY+'@sha256:'):
            raise ReleaseError(name+': private image source cutover is not configured')
        if (instance.get('latestDeployment') or {}).get('status') in BUSY:
            raise ReleaseError(name+': another deployment is active')
        if instance.get('healthcheckPath')!='/health':
            raise ReleaseError(name+': expected /health deployment gate is missing')
        instances[service] = instance
    receipt = dict(image=image,revision=revision,project=PROJECT,environment=ENVIRONMENT,services=[])
    save_receipt(receipt_path, receipt)
    for name, service in SERVICES:
        if head()!=revision:raise ReleaseError('Main changed during rollout; remaining services held')
        before = instances[service]
        before_deploy_id = (before.get('latestDeployment') or {}).get('id')
        item = dict(name=name,service_id=service,previous_image=before['source']['image'],status='updating',verified=False)
        receipt['services'].append(item);save_receipt(receipt_path, receipt)
        # Update only the image. Railway may store replicas in regional config
        # while the legacy numReplicas field is null; never rewrite topology.
        try:
            ok = api(UPDATE, {'s':service,'e':ENVIRONMENT,
                'i':{'source':{'image':image}}})['serviceInstanceUpdate']
        except UnknownOutcomeError:
            # The update may have landed despite the timeout: read back the
            # source instead of re-issuing the mutation blindly.
            configured = api(INSTANCE, {'s':service,'e':ENVIRONMENT}, retries=3)['serviceInstance']['source']
            ok = configured.get('image') == image and not configured.get('repo')
        if not ok:raise ReleaseError(name+': image update was not accepted or is unconfirmed; remaining services held')
        configured = api(INSTANCE, {'s':service,'e':ENVIRONMENT}, retries=3)['serviceInstance']['source']
        if configured.get('repo') or configured.get('image')!=image:
            raise ReleaseError(name+': source readback did not match requested digest')
        try:
            # The deploy mutation can take a while to return a deployment id;
            # give it a longer read window than the idempotent queries.
            deployment = api(DEPLOY, {'s':service,'e':ENVIRONMENT}, timeout=120)['serviceInstanceDeployV2']
        except UnknownOutcomeError:
            # The deploy may have been created server-side despite the
            # timeout. Adopt the new deployment if one appeared; otherwise
            # fail cleanly so the rollout can be retried without risking a
            # duplicate deployment.
            latest = api(INSTANCE, {'s':service,'e':ENVIRONMENT}, retries=3)['serviceInstance'].get('latestDeployment') or {}
            if latest.get('id') and latest.get('id') != before_deploy_id:
                deployment = latest['id']
            else:
                raise ReleaseError(name+': deploy request timed out with no new deployment; safe to retry') from None
        if not deployment:raise ReleaseError(name+': no deployment receipt returned')
        item.update(deployment_id=deployment,status='deploying');save_receipt(receipt_path,receipt)
        deadline = clock()+timeout
        while clock()<deadline:
            result = api(STATUS, {'id':deployment}, retries=3)['deployment']
            if (result.get('projectId'),result.get('environmentId'),result.get('serviceId')) != (PROJECT,ENVIRONMENT,service):
                raise ReleaseError('Deployment receipt does not belong to the expected target')
            item['status'] = result['status'];save_receipt(receipt_path,receipt)
            if result['status']=='SUCCESS':
                # Railway's configured /health check gates SUCCESS. Verify the
                # deployment itself names the digest, not merely current config.
                meta = result.get('meta') or {}
                if meta.get('image')!=image:
                    raise ReleaseError(name+': running deployment image evidence is missing or different')
                item['verified']=True;save_receipt(receipt_path,receipt)
                print(name+': healthy at '+image, flush=True)
                break
            if result['status'] in FAILED:
                raise ReleaseError(name+': deployment '+result['status']+'; remaining services held')
            sleep(5)
        else:raise ReleaseError(name+': health verification timed out; remaining services held')
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['manifest','promote'])
    parser.add_argument('--image',required=True)
    parser.add_argument('--revision',required=True)
    args=parser.parse_args();validate(args.image,args.revision)
    if args.mode=='manifest':
        save_receipt('image-release.json',dict(image=args.image,revision=args.revision,
            repository=REPOSITORY,run_id=os.environ.get('GITHUB_RUN_ID'),run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT')))
    else:
        promote(args.image,args.revision,Railway(os.environ.get('RAILWAY_TOKEN')))


if __name__=='__main__':
    try:main()
    except ReleaseError as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
