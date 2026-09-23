"""Promote one CI-tested image; never build, change credentials or reconnect sources."""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = 'cyberstryder/market-compass'
REGISTRY = 'ghcr.io/' + REPOSITORY
PROJECT = '4801dbfb-a7a9-411b-abef-696df393f0b1'
ENVIRONMENT = '711a06df-cbcd-4ee8-8332-b60be4c17723'
SERVICES = (
    ('dashboard', '33e96fc3-e153-4944-a131-b5944c12e902'),
    ('collector', '5a1ad3c7-4090-42d0-954f-a5a55f8e8f9d'),
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


def validate(image, revision):
    if not re.fullmatch(re.escape(REGISTRY) + r'@sha256:[0-9a-f]{64}', image):
        raise ReleaseError('An immutable digest in the Compass GHCR repository is required')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ReleaseError('A full source commit SHA is required')


def request_json(url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={'Accept':'application/json','Content-Type':'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, ValueError) as error:
        # Never print request headers, credentials or response bodies. A failed
        # mutation is not automatically retried: deployment outcome may be unknown.
        raise ReleaseError('API request failed; inspect deployment state before retrying') from None


class Railway:
    def __init__(self, token):
        if not token:raise ReleaseError('Missing RAILWAY_TOKEN GitHub Actions secret')
        self.token = token

    def __call__(self, query, variables):
        result = request_json(API, {'query':query,'variables':variables}, {'Project-Access-Token':self.token})
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
        instance = api(INSTANCE, {'s':service,'e':ENVIRONMENT})['serviceInstance']
        source = instance.get('source') or {}
        if source.get('repo') or not (source.get('image') or '').startswith(REGISTRY+'@sha256:'):
            raise ReleaseError(name+': private image source cutover is not configured')
        if (instance.get('latestDeployment') or {}).get('status') in BUSY:
            raise ReleaseError(name+': another deployment is active')
        if instance.get('healthcheckPath')!='/health':
            raise ReleaseError(name+': expected /health deployment gate is missing')
        if instance.get('numReplicas')!=1:
            raise ReleaseError(name+': topology changed; review before promotion')
        instances[service] = instance
    receipt = dict(image=image,revision=revision,project=PROJECT,environment=ENVIRONMENT,services=[])
    save_receipt(receipt_path, receipt)
    for name, service in SERVICES:
        if head()!=revision:raise ReleaseError('Main changed during rollout; remaining services held')
        before = instances[service]
        item = dict(name=name,service_id=service,previous_image=before['source']['image'],status='updating',verified=False)
        receipt['services'].append(item);save_receipt(receipt_path, receipt)
        # Keep the single-replica topology explicit; do not alter commands,
        # health checks, environment variables, networking or volumes.
        ok = api(UPDATE, {'s':service,'e':ENVIRONMENT,
            'i':{'source':{'image':image},'numReplicas':before['numReplicas']}})['serviceInstanceUpdate']
        if not ok:raise ReleaseError(name+': image update was not accepted')
        configured = api(INSTANCE, {'s':service,'e':ENVIRONMENT})['serviceInstance']['source']
        if configured.get('repo') or configured.get('image')!=image:
            raise ReleaseError(name+': source readback did not match requested digest')
        deployment = api(DEPLOY, {'s':service,'e':ENVIRONMENT})['serviceInstanceDeployV2']
        if not deployment:raise ReleaseError(name+': no deployment receipt returned')
        item.update(deployment_id=deployment,status='deploying');save_receipt(receipt_path,receipt)
        deadline = clock()+timeout
        while clock()<deadline:
            result = api(STATUS, {'id':deployment})['deployment']
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
