"""User-data writes stay separate from executable/runtime writes."""
import datetime as dt
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from life_proactive.core import Store, load_config
from life_service.core import ServiceError


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = json.loads((Path(__file__).parents[2] / 'server/life_proactive/config.example.json').read_text())
        cfg.update(workspace='.', account_id='test', owner_id='owner')
        path = self.root / 'config.json'
        path.write_text(json.dumps(cfg))
        self.now = dt.datetime.fromisoformat('2026-10-08T14:30:00+08:00')
        self.store = Store(load_config(path), lambda: self.now)
        self.addCleanup(self.store.close)
        self.mid = self.store.ingest({'channel':'openclaw-weixin','account':'test','sender':'owner',
            'message_id':'source','session':'session','text':'帮我建立同事档案：测试同事喜欢茶饮。',
            'at':self.now.isoformat(),'media':[]})['message_id']
        self.k = importlib.import_module('life_proactive.knowledge')
        self.path = '02_生活领域/08_关系与情感/同事与朋友/测试同事.md'

    def request(self, **extra):
        value = dict(action='create',path=self.path,content='# 测试同事\n\n喜欢茶饮。',
                     message_id=self.mid,session='session',quote='帮我建立同事档案：测试同事喜欢茶饮。')
        value.update(extra)
        return value

    def test_create_colleague_file(self):
        result = self.k.manage(self.store,self.request())
        self.assertEqual(result['status'],'created')
        self.assertIn('喜欢茶饮', (self.root/self.path).read_text(encoding='utf-8'))
        self.assertFalse((self.root/'01_日常记录').exists())

    def test_knowledge_notes_and_index_are_allowed(self):
        for name in ['04_知识与资源/知识笔记/读书方法.md','04_知识与资源/知识笔记/README.md',
                     '00_收件箱/想法.md','03_目标与项目/2026-10-练习/计划.md','05_复盘与计划/周计划与复盘/本周.md','总览.md']:
            with self.subTest(path=name):
                self.assertEqual(self.k.manage(self.store,self.request(path=name))['status'],'created')

    def test_software_runtime_journals_and_reports_are_denied(self):
        for name in ['app.py','99_系统与模板/工具/a.py','.local/openclaw/openclaw.json','.runtime/a.md',
                     '02_生活领域/a.py','04_知识与资源/x.json','AGENTS.md','SOUL.md',
                     '03_目标与项目/demo/AGENTS.md','03_目标与项目/demo/AGENTS.override.md','04_知识与资源/SKILL.md','04_知识与资源/tools/test.md',
                     '01_日常记录/2026/2026-10-08.md','05_复盘与计划/晨报/2026/a.md',
                     '00_收件箱/设备数据/WHOOP/a.md','02_生活领域/03_健康与医疗/设备记录/a.md']:
            with self.subTest(path=name), self.assertRaises(ServiceError):
                self.k.manage(self.store,self.request(path=name))

    def test_windows_paths_and_aliases_are_denied(self):
        for name in ['../out.md','C:/out.md','\\\\host\\share\\x.md','04_知识与资源/../x.md',
                     '04_知识与资源/a.md:evil','04_知识与资源/a.md.','04_知识与资源/a.md ',
                     '04_知识与资源/CON.md','04_知识与资源/.git/x.md','04_知识与资源/a.py.md',
                     '04_知识与资源/a.md::$DATA','04_知识与资源/a\x00.md']:
            with self.subTest(path=repr(name)), self.assertRaises(ServiceError):
                self.k.manage(self.store,self.request(path=name))

    def test_forged_or_old_message_is_rejected(self):
        for change in [dict(session='other'),dict(message_id='forged'),dict(quote='伪造')]:
            with self.subTest(change=change), self.assertRaises(ServiceError):
                self.k.manage(self.store,self.request(**change))
        self.now += dt.timedelta(minutes=11)
        with self.assertRaises(ServiceError): self.k.manage(self.store,self.request())

    def test_create_never_overwrites(self):
        self.k.manage(self.store,self.request())
        with self.assertRaises(ServiceError): self.k.manage(self.store,self.request(content='覆盖'))

    def test_retry_does_not_duplicate_append(self):
        self.k.manage(self.store,self.request())
        data=self.request(action='append',content='\n补充信息。')
        self.k.manage(self.store,data)
        result=self.k.manage(self.store,data)
        self.assertEqual(result['status'],'already_saved')
        self.assertEqual((self.root/self.path).read_text(encoding='utf-8').count('补充信息'),1)

    def test_replace_requires_current_revision_and_keeps_backup(self):
        first=self.k.manage(self.store,self.request())
        with self.assertRaises(ServiceError):
            self.k.manage(self.store,self.request(action='replace',content='修订'))
        self.k.manage(self.store,self.request(action='replace',content='已修订的资料',expected_sha256=first['sha256']))
        self.assertTrue(list((self.root/'.local/knowledge/backups').glob('*.md')))
        with self.assertRaises(ServiceError):
            self.k.manage(self.store,self.request(action='replace',content='旧版本覆盖',expected_sha256=first['sha256']))

    def test_read_returns_revision_for_existing_notes(self):
        first=self.k.manage(self.store,self.request())
        read=self.k.manage(self.store,dict(action='read',path=self.path,session='session'))
        self.assertEqual(first['sha256'],read['sha256'])
        self.assertIn('喜欢茶饮',read['content'])

    def test_directory_listing_discovers_notes_but_hides_protected_items(self):
        self.k.manage(self.store,self.request())
        folder=(self.root/self.path).parent
        (folder/'AGENTS.md').write_text('protected')
        (folder/'app.py').write_text('code')
        result=self.k.manage(self.store,dict(action='list',path=folder.relative_to(self.root).as_posix()))
        self.assertEqual([x['path'] for x in result['entries']],[self.path])
        for forbidden in ['.local','99_系统与模板','04_知识与资源/../..']:
            with self.assertRaises(ServiceError): self.k.manage(self.store,dict(action='list',path=forbidden))

    def test_symlink_and_junction_are_denied(self):
        for method in ['is_symlink','is_junction']:
            with patch.object(Path,method,return_value=True), self.assertRaises(ServiceError):
                self.k.manage(self.store,self.request())

    def test_hardlink_target_is_denied(self):
        import os
        p=self.root/self.path
        p.parent.mkdir(parents=True)
        original=self.root/'outside.md'
        original.write_text('original')
        os.link(original,p)
        with self.assertRaises(ServiceError):
            self.k.manage(self.store,self.request(action='append'))
        self.assertEqual(original.read_text(),'original')

    def test_unknown_fields_and_delete_are_denied(self):
        for extra in [dict(command='echo bad'),dict(action='delete'),dict(content=''),dict(content='x'*100001)]:
            with self.subTest(extra=list(extra)), self.assertRaises(ServiceError):
                self.k.manage(self.store,self.request(**extra))

    def test_backup_and_source_receipt_preserve_evidence(self):
        self.k.manage(self.store,self.request())
        receipt=json.loads(next((self.root/'.local/knowledge/receipts').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(receipt['quote'],self.request()['quote'])
        self.assertEqual(receipt['message_id'],self.mid)

    def test_write_failure_cannot_report_success(self):
        with patch.object(self.k,'atomic_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.k.manage(self.store,self.request())
        self.assertFalse((self.root/self.path).exists())

    def test_crash_after_write_recovers_without_duplicate_append(self):
        self.k.manage(self.store,self.request())
        data=self.request(action='append',content='仅一次的补充')
        real=self.k.atomic_write
        def crash(path, text):
            if path.parent.name=='receipts' and json.loads(text)['status']=='saved':
                raise OSError('simulated crash after data write')
            return real(path,text)
        with patch.object(self.k,'atomic_write',side_effect=crash), self.assertRaises(OSError):
            self.k.manage(self.store,data)
        self.assertEqual(self.k.manage(self.store,data)['status'],'already_saved')
        self.assertEqual((self.root/self.path).read_text(encoding='utf-8').count('仅一次的补充'),1)

    def test_explicit_do_not_save_is_respected(self):
        self.store.db.execute('UPDATE messages SET text=? WHERE id=?',('只聊不记，测试同事喜欢茶饮。',self.mid))
        with self.assertRaises(ServiceError):
            self.k.manage(self.store,self.request(quote='测试同事喜欢茶饮。'))
        self.assertFalse((self.root/self.path).exists())
