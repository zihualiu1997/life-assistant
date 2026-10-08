"""Run on the entrance network; never mount operator files or user volumes."""
import datetime
import io
import json
import urllib.error
import urllib.request
import uuid
import zipfile


def request(name, path, body=None, cookie='', csrf='', origin=None):
    host=name+'.fixture.invalid'
    req=urllib.request.Request('http://caddy:8080'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Host':host,'Origin':origin or 'https://'+host,'Cookie':cookie,'X-CSRF-Token':csrf,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=15) as r:return r.status,r.read(),r.headers
    except urllib.error.HTTPError as r:return r.code,r.read(),r.headers


sessions={}
for i in range(5):
    name=f'fixture{i}'
    assert request(name,'/api/export')[0]==401, 'anonymous export'
    status,raw,headers=request(name,'/api/login',{'password':'fictional-password-only'})
    assert status==200, (name,status,raw)
    cookie=headers['Set-Cookie']
    assert cookie.startswith('__Host-life_session=')
    assert all(value in cookie.lower() for value in ('secure','httponly','samesite=strict','path=/'))
    assert 'domain=' not in cookie.lower()
    sessions[name]=(cookie.split(';',1)[0],json.loads(raw)['csrf'])
for i in range(5):
    name=f'fixture{i}';other=f'fixture{(i+1)%5}'
    cookie,csrf=sessions[name]
    assert request(name,'/api/export',cookie=sessions[other][0])[0]==401, 'cross-user cookie'
    status,raw,headers=request(name,'/api/export',cookie=cookie)
    assert status==200 and 'no-store' in headers['Cache-Control']
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        contents='\n'.join(archive.read(n).decode() for n in archive.namelist())
    assert 'FICTIONAL_OWNER_'+name in contents and 'FICTIONAL_OWNER_'+other not in contents
    body={'message_id':str(uuid.uuid4()),'date':datetime.date.today().isoformat(),'body':'FICTIONAL_INGRESS_'+name}
    assert request(name,'/v1/journal',body,cookie=cookie)[0]==403, 'missing CSRF'
    assert request(name,'/v1/journal',body,cookie=cookie,csrf=csrf,origin='https://'+other+'.fixture.invalid')[0]==403, 'cross-origin write'
    assert request(name,'/v1/journal',body,cookie=cookie,csrf=csrf)[0]==200, 'own write'
    for path in ['/internal/test','/%69nternal/test','/internal%2Ftest','/api/pairing','/api/devices','/api/devices/example','/v1/pair']:
        assert request(name,path,cookie=cookie)[0]==404, ('internal exposed',path)
assert request('unknown','/api/bootstrap')[0]==404
print(json.dumps({'users':5,'host_routing':'passed','host_only_secure_cookie':'passed','cross_user_cookie':'refused','csrf_and_origin':'enforced','export_isolation':'passed','private_routes':'blocked','unknown_host':'blocked','private_response_cache':'no-store','browser_tls_and_public_cloudflare':'not_tested'}))
