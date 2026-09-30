import concurrent.futures
import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from life_app.app import create_app
from life_app.config import Settings, service_config
from life_app.store import Store, digest
from life_app import integrations
from life_app.cli import backup, restore
from life_service import mail, model, runner
from life_service.core import receipt_path, write_json

class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'
        self.app = create_app(self.root, background=False, secure=False)
        self.store = self.app.state.store
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        token = self.store.bootstrap()
        r = self.client.post('/api/bootstrap', json={'password':'fictional-test-password','token':token})
        self.assertEqual(r.status_code,200,r.text)
        self.client.headers['x-csrf-token'] = r.json()['csrf']

    def pair(self):
        code = self.client.post('/api/pairing',json={}).json()['code']
        result = self.client.post('/v1/pair',json={'code':code,'name':'test device'})
        self.assertEqual(result.status_code,200,result.text)
        return result.json(), code

    def test_bootstrap_single_use_and_password_not_stored(self):
        self.assertTrue(self.client.get('/api/bootstrap').json()['initialized'])
        self.assertEqual(self.client.post('/api/bootstrap',json={'password':'fictional-test-password','token':'old'}).status_code,403)
        self.assertNotIn('fictional-test-password',self.store.path.read_bytes().decode(errors='ignore'))

    def test_auth_csrf_and_origin(self):
        client=TestClient(self.app)
        self.assertEqual(client.get('/v1/health').status_code,401)
        self.assertEqual(client.get('/api/settings').status_code,401)
        r=self.client.post('/api/pairing',json={},headers={'x-csrf-token':''})
        self.assertEqual(r.status_code,403)
        self.assertEqual(self.client.post('/api/pairing',json={},headers={'origin':'https://attacker.example'}).status_code,403)

    def test_pair_single_use_expiry_and_revocation(self):
        paired,code=self.pair()
        headers={'Authorization':'Bearer '+paired['token']}
        self.assertEqual(self.client.get('/v1/health',headers=headers).status_code,200)
        self.assertEqual(self.client.post('/v1/pair',json={'code':code}).status_code,403)
        self.client.delete('/api/devices/'+paired['device_id'])
        self.assertEqual(self.client.get('/v1/health',headers=headers).status_code,401)
        code=self.client.post('/api/pairing',json={}).json()['code']
        with self.store.db() as db: db.execute('UPDATE tokens SET expires=0 WHERE kind=?',('pair',))
        self.assertEqual(self.client.post('/v1/pair',json={'code':code}).status_code,403)

    def test_device_cannot_manage_server(self):
        paired,_=self.pair()
        client=TestClient(self.app)
        self.assertEqual(client.get('/api/settings',headers={'Authorization':'Bearer '+paired['token']}).status_code,401)

    def test_journal_retry_conflict_and_query(self):
        value={'message_id':str(uuid4()),'date':'2026-12-31','body':'原话\n第二行'}
        first=self.client.post('/v1/journal',json=value)
        self.assertEqual(first.json()['status'],'recorded',first.text)
        self.assertEqual(self.client.post('/v1/journal',json=value).json()['status'],'already_recorded')
        self.assertEqual(self.client.post('/v1/journal',json={**value,'body':'changed'}).status_code,409)
        result=self.client.get('/v1/query',params={'kind':'journal','date':value['date']}).json()
        self.assertIn('原话',result['items'][0]['text'])
        self.assertEqual(result['items'][0]['text'].count('> 原话'),1)

    def test_notes_conflict_snapshot_and_traversal(self):
        value={'path':'04_知识与资源/test.md','text':'original','revision':''}
        r=self.client.put('/v1/note',json=value)
        rev=r.json()['revision']
        self.assertEqual(self.client.put('/v1/note',json={**value,'text':'wrong'}).status_code,409)
        self.assertEqual(self.client.put('/v1/note',json={**value,'text':'updated','revision':rev}).status_code,200)
        self.assertTrue(list((self.store.state/'note-history').rglob('*.md')))
        for name in ['../private.md','.secrets/model','/etc/passwd','04_知识与资源/../../private.md','04_知识与资源/hidden/.secret.md']:
            self.assertEqual(self.client.get('/v1/note',params={'path':name}).status_code,400,name)

    def test_import_refuses_overwrite_and_validates_all_paths(self):
        values=[{'path':'04_知识与资源/import.md','text':'first','revision':''},{'path':'../oops.md','text':'bad','revision':''}]
        self.assertEqual(self.client.post('/api/import',json=values).status_code,400)
        self.assertFalse((self.root/values[0]['path']).exists())
        self.assertEqual(self.client.post('/api/import',json=values[:1]).status_code,200)
        self.assertEqual(self.client.post('/api/import',json=values[:1]).status_code,409)

    def test_secrets_never_returned_and_validation_not_echoed(self):
        s=Settings(model='fixture').model_dump()
        marker='fixture-private-key-value'
        self.assertEqual(self.client.put('/api/settings',json={'settings':s,'model_key':marker}).status_code,200)
        self.assertNotIn(marker,self.client.get('/api/settings').text)
        self.assertNotIn(marker,self.client.put('/api/settings',json={'settings':{'provider':marker},'model_key':marker}).text)

    def test_activation_requires_real_checks(self):
        self.assertEqual(self.client.post('/api/activate',json={'mail_received':True,'previews_approved':True}).status_code,409)
        self.assertFalse(self.store.get('settings',{}).get('enabled',False))

    def test_explicit_record_chat_retry_does_not_call_model(self):
        s=Settings(model_context_approved=True)
        self.store.put('settings',s.model_dump())
        value={'message_id':str(uuid4()),'conversation_id':str(uuid4()),'body':'记一下：午后散步'}
        with patch.object(integrations,'request_json') as upstream:
            first=self.client.post('/v1/chat',json=value)
            self.assertEqual(first.status_code,200,first.text)
            self.assertEqual(first.json(),self.client.post('/v1/chat',json=value).json())
            upstream.assert_not_called()
        self.assertEqual(self.client.post('/v1/chat',json={**value,'body':'modified'}).status_code,409)

    def test_ambiguous_chat_is_not_automatically_repeated(self):
        self.store.put('settings',Settings(model_context_approved=True).model_dump())
        value={'message_id':str(uuid4()),'conversation_id':str(uuid4()),'body':'hello'}
        with patch.object(integrations,'request_json',side_effect=TimeoutError()) as upstream:
            self.assertEqual(self.client.post('/v1/chat',json=value).status_code,503)
            self.assertEqual(self.client.post('/v1/chat',json=value).status_code,503)
            self.assertEqual(upstream.call_count,1)

    def test_archive_replay_preserves_manual_notes(self):
        self.store.put('settings',Settings(model_context_approved=True,archive_enabled=True).model_dump())
        with self.store.db() as db: db.execute('INSERT INTO messages(id,owner,conversation,body,reply,status,created) VALUES (?,?,?,?,?,?,?)',('test-id','test','test','问题','建议','completed',time.time()))
        with patch.object(integrations,'model_text',return_value='## 待核验\n建议，不是用户事实。') as model_call:
            self.assertEqual(integrations.archive_pending(self.store)['count'],1)
            note=self.store.note('04_知识与资源/主题知识/test-id.md')
            note.write_text(note.read_text(encoding='utf-8')+'\n我的备注\n',encoding='utf-8')
            self.assertEqual(integrations.archive_pending(self.store)['count'],0)
            self.assertEqual(model_call.call_count,1)
            self.assertIn('我的备注',note.read_text(encoding='utf-8'))

    def test_backup_restore_and_tar_traversal(self):
        path=Path(self.temp.name)/'backup.tgz'
        backup(self.store,path)
        target=Path(self.temp.name)/'restored'
        restore(target,path)
        self.assertTrue((target/'总览.md').exists())
        self.assertTrue(Store(target).get('admin'))
        bad=Path(self.temp.name)/'bad.tgz'
        with tarfile.open(bad,'w:gz') as tar:
            member=tarfile.TarInfo('../outside');member.size=1;tar.addfile(member,io.BytesIO(b'x'))
        with self.assertRaises(ValueError):restore(Path(self.temp.name)/'reject',bad)
        self.assertFalse((Path(self.temp.name)/'outside').exists())

    def test_smtp_unknown_blocks_restart_and_archive_saved_first(self):
        s=Settings(model='fixture',model_context_approved=True,enabled=True,smtp_host='smtp.example.com',smtp_username='test@example.com',sender='test@example.com',recipient='test@example.com')
        self.store.put('settings',s.model_dump())
        self.store.secret('model','fixture-model');self.store.secret('smtp','fixture-password')
        c=service_config(self.store)
        write_json(self.root/'.local/mail/connection.json',{'smtp_test':'accepted','recipient':s.recipient})
        now=dt.datetime(2026,12,31,8,30,tzinfo=c['tz'])
        with patch.object(model,'generate',return_value='2026-12-31，暂无已确认安排。'),patch.object(mail,'smtp_send',side_effect=TimeoutError('private sentinel')) as send:
            result=runner.Runner(c,lambda:now).run('morning')
            self.assertEqual(result['status'],'needs_review')
            self.assertTrue((self.root/'05_复盘与计划/晨报/2026/2026-12-31.md').exists())
            self.assertEqual(runner.Runner(c,lambda:now).run('morning')['status'],'needs_review')
            self.assertEqual(send.call_count,1)
            self.assertNotIn('private sentinel',receipt_path(c,'morning',now.date()).read_text())

    def test_internal_bridge_requires_separate_secret(self):
        self.assertEqual(self.client.post('/internal/search',json={'query':'test'}).status_code,401)
        paired,_=self.pair()
        self.assertEqual(self.client.post('/internal/search',json={},headers={'Authorization':'Bearer '+paired['token']}).status_code,401)

    def test_empty_plan_is_missing_not_confirmed_activity(self):
        result=self.client.get('/v1/query',params={'kind':'plan','date':'2026-12-31'}).json()
        self.assertEqual(result['status'],'missing')

    def test_concurrent_journal_appends_keep_every_entry_once(self):
        from life_assistant import journal
        def write(i):
            return journal(self.root,'2026-12-31','desktop','fixture-'+str(i),'虚构记录 '+str(i))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(write,[0,1,2,3,0,1,2,3]))
        text=self.store.note('01_日常记录/2026/2026-12-31.md').read_text(encoding='utf-8')
        for i in range(4): self.assertEqual(text.count('> 虚构记录 '+str(i)),1)

    def test_journal_change_rejects_stale_manual_editor(self):
        value={'path':'01_日常记录/2026/2026-12-31.md','text':'# 人工笔记','revision':''}
        rev=self.client.put('/v1/note',json=value).json()['revision']
        self.client.post('/v1/journal',json={'message_id':str(uuid4()),'date':'2026-12-31','body':'新增原话'})
        self.assertEqual(self.client.put('/v1/note',json={**value,'text':'过期编辑','revision':rev}).status_code,409)
        self.assertIn('新增原话',self.store.note(value['path']).read_text(encoding='utf-8'))

    def test_model_http_errors_do_not_echo_credentials(self):
        import urllib.error
        from life_service.core import ServiceError
        for status, expected in [(401,'upstream_key_rejected'),(429,'upstream_rate_limited')]:
            with patch('urllib.request.OpenerDirector.open',side_effect=urllib.error.HTTPError('https://example.com',status,'private sentinel',{},None)):
                with self.assertRaises(ServiceError) as caught: integrations.request_json('https://example.com')
                self.assertEqual(caught.exception.code,expected)

    def test_stale_weather_cannot_be_current(self):
        from life_service.context import weather_context
        self.store.put('settings',Settings(city='Fixture City').model_dump())
        self.store.secret('weather','fixture-weather')
        c=service_config(self.store)
        now=dt.datetime(2026,12,31,8,30,tzinfo=c['tz'])
        write_json(self.root/'.local/openweather/latest.json',{'status':'synced','date':'2026-12-30','fetched_at':'2026-12-30T08:00:00+08:00','city':'Fixture City'})
        self.assertEqual(weather_context(c,now)['status'],'unavailable')

    def test_model_probe_rejects_unsupported_tool_responses(self):
        from life_service.core import ServiceError
        self.store.put('settings',Settings(model='fixture').model_dump())
        self.store.secret('model','fictional-key')
        with patch.object(integrations,'model_text',return_value='OK'), patch.object(integrations,'request_json',return_value={'choices':[{'message':{'content':'no tool support'}}]}):
            with self.assertRaises(ServiceError) as caught: integrations.test_model(self.store)
            self.assertEqual(caught.exception.code,'model_tool_call_incompatible')

    def test_provider_switch_waits_for_request_using_old_key(self):
        import threading
        from life_assistant import lock
        started,release,changed=threading.Event(),threading.Event(),threading.Event()
        self.store.put('settings',Settings(model='fixture',provider='custom',base_url='https://old.example/v1').model_dump())
        self.store.secret('model','old-fictional-key')
        def upstream(url,payload,headers):
            self.assertEqual(url,'https://old.example/v1/chat/completions')
            self.assertEqual(headers['Authorization'],'Bearer old-fictional-key')
            started.set()
            self.assertTrue(release.wait(3))
            return {'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]}
        def change():
            with lock(self.root,'settings'):
                self.store.put('settings',Settings(model='fixture',provider='custom',base_url='https://new.example/v1').model_dump())
                self.store.secret('model','new-fictional-key')
                changed.set()
        with patch.object(integrations,'request_json',side_effect=upstream), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            request=pool.submit(integrations.model_text,self.store,'test','fictional')
            self.assertTrue(started.wait(3))
            update=pool.submit(change)
            try: self.assertFalse(changed.wait(.15))
            finally: release.set()
            self.assertEqual(request.result(),'OK')
            update.result()
        self.assertTrue(changed.is_set())

if __name__=='__main__':unittest.main()
