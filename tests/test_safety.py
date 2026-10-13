import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
TEMP = tempfile.TemporaryDirectory()
ROOT = Path(TEMP.name)
CONFIG = ROOT/'config.json'
CONFIG.write_text(json.dumps({'categories': {'anime': {'quark_root': '/shows', 'library': str(ROOT/'media')}},
                             'default_category': 'anime', 'notify': [], 'tmdb': {}, 'rename': {'enabled': False}}), encoding='utf-8')
os.environ['QUARK_TRACKER_CONFIG'] = str(CONFIG)
import link_audit as audit
import download_guard as guard
import quark_ctl as ctl
import quark_sync as sync

NAME = '[Group] Demo - 01 [1080p].mkv'
FOLDER = {'fid': 'folder', 'path': '/Demo/Season 01', 'kind': 'video', 'episodes': [1],
          'video_count': 1, 'archive_count': 0, 'files': [{'name': NAME, 'fid': 'file', 'kind': 'video', 'size': 100}]}

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ROOT/'download_policy.json'
        self.policy.unlink(missing_ok=True)
        sync.ACTIVE_SHOWS_CACHE.clear()
        sync.ERRORS.clear()

    def test_tracking_future_and_reject_unrelated(self):
        pattern, fixed = audit.tracking_pattern(FOLDER, 1)
        self.assertFalse(fixed)
        for ep in ['02', '12', '100']:
            self.assertRegex(NAME.replace('01', ep), pattern)
        for name in [NAME.replace('Demo', 'Other'), NAME.replace('01', '00'), NAME.replace('01', '1000'),
                     NAME.replace('01', '02 OVA'), NAME.replace('Demo', 'Demo S2'), NAME+'.part', NAME+'.ass']:
            self.assertIsNone(re.fullmatch(pattern, name))

    def test_season_metadata_cannot_override_explicit_files(self):
        f=dict(FOLDER, files=[{'name':'Demo.S02E01.mkv','fid':'s2','kind':'video'}])
        report=audit.audit({'scan_complete':True,'folders':[f]},'Demo',{'seasons':[{'season_number':1}]},requested_season=2)
        self.assertEqual(report['_units'][0]['season'],2)
        plan=audit.select_plan(report,ctl._select_folder,requested_season=2)
        pattern,_=audit.tracking_pattern(plan[0][1],2)
        self.assertRegex('Demo.S02E02.mkv',pattern)
        self.assertIsNone(re.fullmatch(pattern,'Demo.S01E02.mkv'))
        with self.assertRaises(ValueError): audit.select_plan(report,ctl._select_folder,requested_season=3)

    def test_special_folders_never_win(self):
        special=dict(FOLDER,path='/Demo/SPs',episodes=list(range(30)))
        self.assertIs(ctl._select_folder([special,FOLDER])[1],FOLDER)

    def test_reseed_note_not_season(self):
        self.assertEqual(audit.season_hints('S1 Reseed + S2'),{2})

    def test_unknown_filename_stays_exact(self):
        pattern,fixed=audit.tracking_pattern({'files':[{'name':'Demo.1080p.mkv','kind':'video'}]},1)
        self.assertEqual(fixed,['Demo.1080p.mkv'])
        self.assertIsNone(re.fullmatch(pattern,'Other.1080p.mkv'))

    def test_local_has_requires_complete_video(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            for ext in ['.nfo','.ass','.mkv.part','.mkv.part.aria2']:
                (p/('Demo - S01E01'+ext)).write_bytes(b'x')
            self.assertFalse(sync.local_has(directory,1,1))
            (p/'Demo - S01E01.mkv').write_bytes(b'x')
            self.assertTrue(sync.local_has(directory,1,1))

    def test_policy_cancel_and_season_block(self):
        self.policy.write_text(json.dumps({'shows':[{'name':'Demo','paused':False,'blocked_seasons':[1]}]}),encoding='utf-8')
        with self.assertRaises(ValueError): guard.ensure_allowed('Demo',1)
        guard.ensure_allowed('Demo',2)
        self.policy.write_text(json.dumps({'shows':[{'name':'Demo','paused':True}]}),encoding='utf-8')
        with self.assertRaises(ValueError): guard.ensure_allowed('Demo',2)

    def test_addtask_rejects_relabeling(self):
        probe={'scan_complete':True,'folders':[dict(FOLDER, files=[{'name':'Demo.S01E01.mkv','kind':'video'}])]}
        with self.assertRaises(ValueError): guard.validate_task(probe,2,r'\.mkv$')

    def test_exact_transfer_check_survives_recurring_pattern(self):
        accounts=[{'files':[{'name':'old-unrelated.mkv'}]}, {'files':[{'name':NAME}]}]
        with patch.object(ctl,'_catalog_entry',return_value={}), patch.object(ctl,'_tmdb_search_id',return_value=None), \
             patch.object(ctl,'dexec',return_value='{}') as de, patch.object(ctl,'_acct_season_files',side_effect=accounts), \
             patch.object(ctl.subprocess,'run'), patch.object(ctl.time,'sleep'), patch.object(ctl,'run_sync') as run, \
             patch.object(ctl,'push'), patch.object(ctl,'_fetch_subs',return_value=0):
            ctl._dl_season(ctl.category('anime'),'Demo','https://example.invalid',[FOLDER],[],1,True)
            task=json.loads(de.call_args_list[0].args[0][1])
            self.assertRegex(NAME.replace('01','02'),task['pattern'])
            run.assert_called_once()
            self.assertEqual(task['savepath'],'/shows/Demo/Season 01')

    def test_failed_transfer_does_not_download(self):
        with patch.object(ctl,'_catalog_entry',return_value={}), patch.object(ctl,'_tmdb_search_id',return_value=None), \
             patch.object(ctl,'dexec',return_value='{}'), patch.object(ctl,'_acct_season_files',return_value={'files':[{'name':'old.mkv'}]}), \
             patch.object(ctl.subprocess,'run'), patch.object(ctl.time,'sleep'), patch.object(ctl,'run_sync') as run, patch.object(ctl,'push'):
            ctl._dl_season(ctl.category('anime'),'Demo','https://example.invalid',[FOLDER],[],1,True)
            run.assert_not_called()

    def test_archive_password_sidecar_remains_allowed(self):
        arc=dict(FOLDER, kind='archive',video_count=0,archive_count=1,has_password=True,
                 files=[{'name':'Demo [01-12].7z','kind':'archive','fid':'archive'}])
        with patch.object(ctl,'dexec',return_value='{}') as de, patch.object(ctl,'_write_pw'), \
             patch.object(ctl,'_acct_season_files',return_value={'files':[{'name':'Demo [01-12].7z'}]}), \
             patch.object(ctl.subprocess,'run'), patch.object(ctl,'run_sync'), patch.object(ctl,'push'), patch.object(ctl,'out'):
            ctl._dl_season(ctl.category('anime'),'Demo','https://example.invalid',[arc],[],1,True)
            pattern=json.loads(de.call_args_list[0].args[0][1])['pattern']
            self.assertRegex('password.txt',pattern)
            self.assertIsNone(re.search(pattern,'Other.mkv'))

    def test_ledger_prevents_redownload_after_folder_rename(self):
        tree={'/shows':{'exists':True,'shows':[{'name':'Renamed','seasons':[{'name':'Season 01','files':[{'name':NAME,'fid':'already','size':100}]}]}]}}
        with patch.object(sync,'DRY',True), patch.object(sync,'api',return_value=tree), \
             patch.object(sync,'ledger_load',return_value={'already':{}}), patch.object(sync,'verify_new_season',return_value=1), \
             patch.object(sync,'download') as dl, patch.object(sync,'push') as push, contextlib.redirect_stdout(io.StringIO()):
            sync.main()
            dl.assert_not_called(); push.assert_not_called()

    def test_download_retries_end_without_slow_guard(self):
        with tempfile.TemporaryDirectory() as library:
            tree={'/shows':{'exists':True,'shows':[{'name':'Demo','seasons':[{'name':'Season 01','files':[{'name':NAME,'fid':'new','size':100}]}]}]}}
            def api(action,*args):
                return tree if action=='tree' else {'urls':{'new':'https://example.invalid'},'cookie':'test','dlcookies':{'new':'temporary'}}
            with patch.object(sync,'DRY',False), patch.object(sync,'LIB',{'anime':library}), patch.object(sync,'api',side_effect=api), \
                 patch.object(sync,'ledger_load',return_value={}), patch.object(sync,'verify_new_season',return_value=1), \
                 patch.object(sync,'download',side_effect=[False,False,True]) as dl, patch.object(sync,'ledger_mark') as mark, \
                 patch.object(sync,'push'),patch.object(sync,'save_speed_samples'),patch.object(sync.time,'sleep'), \
                 patch.object(sync,'rename_show'),patch.object(sync,'log'):
                sync.main()
                self.assertEqual([c.kwargs['slowguard'] for c in dl.call_args_list],[True,True,False])
                self.assertTrue(all(c.args[1]=='test' and c.args[3]=='temporary' for c in dl.call_args_list))
                mark.assert_called_once()

    def test_ai_disabled_falls_back_and_dry_writes_nothing(self):
        with patch.object(sync,'DRY',True),patch.object(sync,'ai_dedup_cache_load',return_value={}),patch.object(sync,'log'):
            selected,_=sync.ai_pick_best('Demo',1,1,[{'name':'small','size':10},{'name':'large','size':20}])
        self.assertEqual(selected,'large')

    def test_explicit_season_beats_stale_cache(self):
        with patch.object(sync,'season_verify_cache_load',return_value={'Demo|2':1}):
            self.assertEqual(sync.verify_new_season('Demo',2,['Demo.S02E01.mkv'],{}),2)

    def test_numbering_and_start_message(self):
        self.assertEqual(sync.parse_ep('Demo Season 2 [03].mkv'),3)
        text=sync.build_start_msg('anime','Demo',[{'snum':1,'want':[(1,{'size':100})],'archives':[]}],[])
        text.encode('utf-8')

    def test_incomplete_scan_and_ambiguous_season_do_not_download(self):
        args=['--link','https://example.invalid','--name','Demo','--season','2']
        for complete in [False,True]:
            probe={'scan_complete':complete,'folders':[FOLDER]}
            report=audit.audit(probe,'Demo',{'seasons':[{'season_number':1}]},requested_season=2)
            with patch.object(ctl,'dexec',return_value=json.dumps(probe)),patch.object(ctl,'_audit_link',return_value=report), \
                 patch.object(ctl,'_save_link_audit'),patch.object(ctl,'push'),patch.object(ctl,'out'),patch.object(ctl,'_dl_season') as dl:
                if complete:
                    with self.assertRaises(ValueError): ctl.cmd_autodl(args)
                else: ctl.cmd_autodl(args)
                dl.assert_not_called()

    def test_autodl_specific_season_keeps_category_and_skips_extras(self):
        f=dict(FOLDER,files=[{'name':'Demo.S02E01.mkv','kind':'video','fid':'s2','size':100}])
        probe={'scan_complete':True,'folders':[f]}
        report=audit.audit(probe,'Demo',{'seasons':[{'season_number':1}]},requested_season=2)
        with patch.object(ctl,'dexec',return_value=json.dumps(probe)),patch.object(ctl,'_audit_link',return_value=report), \
             patch.object(ctl,'_save_link_audit'),patch.object(ctl,'push'),patch.object(ctl,'out'),patch.object(ctl,'restart'), \
             patch.object(ctl,'_dl_season') as dl,patch.object(ctl,'_fetch_movie') as movie,patch.object(ctl,'_place_subs_from_scan') as subs:
            ctl.cmd_autodl(['--link','https://example.invalid','--name','Demo','--season','2'])
            self.assertEqual(dl.call_args.args[0],ctl.category('anime'))
            self.assertEqual(dl.call_args.args[5],2)
            movie.assert_not_called(); subs.assert_not_called()

class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old=sys.stdout
        try:
            with patch.dict(sys.modules,{'quark_auto_save':types.SimpleNamespace()}):
                cls.ns=runpy.run_path(str(SRC/'qsync_api.py'),run_name='test_helper')
        finally: sys.stdout=old
        cls.ns=cls.ns['main'].__globals__

    def probe(self,depth,readonly=True):
        ns=self.ns; result=[]
        account=Mock()
        account.extract_url.return_value=('id','','0',None)
        account.get_stoken.return_value={'status':200,'data':{'stoken':'test'}}
        def detail(acc,pwd,stoken,fid):
            n=int(fid)
            if n<depth: return [{'dir':True,'file_name':'folder','fid':str(n+1)}]
            return [{'file_name':'Demo.S01E01.mkv','fid':'video','size':100}, {'file_name':'password.txt','fid':'pw'}]
        with patch.dict(ns,{'get_acc':lambda:(account,'test'),'share_detail':detail,'emit':result.append,'_read_share_txt':Mock(side_effect=AssertionError('probe must not transfer'))}), \
             patch.object(sys,'argv',['api','probe','https://example.invalid']+(['--readonly'] if readonly else [])):
            ns['main']()
        return result[0]

    def test_deep_readonly_probe(self):
        report=self.probe(6)
        self.assertTrue(report['scan_complete'])
        self.assertEqual(report['folders'][0]['files'][0]['name'],'Demo.S01E01.mkv')

    def test_scan_limit_is_explicit(self):
        report=self.probe(34)
        self.assertFalse(report['scan_complete'])
        self.assertTrue(report['scan_errors'])

    def test_client_timeout_default_and_override(self):
        config=ROOT/'quark-test-config.json'
        config.write_text(json.dumps({'cookie':['test']}),encoding='utf-8')
        client=Mock()
        request=Mock(return_value='response')
        client._send_request=request
        with patch.dict(self.ns,{'CONF':str(config),'q':types.SimpleNamespace(Quark=lambda *args:client)}):
            acc,_=self.ns['get_acc']()
            acc._send_request('GET','https://example.invalid')
            self.assertEqual(request.call_args.kwargs['timeout'],(8,30))
            acc._send_request('GET','https://example.invalid',timeout=2)
            self.assertEqual(request.call_args.kwargs['timeout'],2)

if __name__ == '__main__': unittest.main()
