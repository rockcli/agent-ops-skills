import contextlib,importlib.util,io,json,os,stat,subprocess,sys,tempfile,time,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('codex_jobs',Path(__file__).resolve().parents[1]/'codex.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Runtime(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.old=m.STATE;m.STATE=self.root/'state';self.env=patch.dict(os.environ,{'CODEX_JOBS_STATE_DIR':str(m.STATE)});self.env.start()
 def tearDown(self):self.env.stop();m.STATE=self.old;self.tmp.cleanup()
 def terminal(self):
  j={'jobId':'a'*32,'status':'completed','createdAt':m.now(),'repo':str(self.root),'result':{'response':'fixture result'},'error':None,'notifiedAt':None,'target':None,'monitorId':None};m.save(m.jobdir(j['jobId'])/'job.json',j);return j['jobId']
 def fake(self,sleep=False):
  p=self.root/'fake-codex';p.write_text('#!/usr/bin/env python3\nimport json,sys,time\nfrom pathlib import Path\n'+('time.sleep(60)\n' if sleep else '')+'a=sys.argv;Path(a[a.index("-o")+1]).write_text("fixture final")\nprint(json.dumps({"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000000"}))\nprint(json.dumps({"type":"turn.completed"}))\n');p.chmod(0o700);return p
 def wait(self,j):
  for _ in range(160):
   v=m.status(j,include_result=True)
   if v['done']:return v
   time.sleep(.05)
  self.fail('job did not finish')
 def test_internal_codex_guard_before_launch(self):
  with self.assertRaisesRegex(ValueError,'native subagents'):m.start(repo=str(self.root),kind='task',prompt='test',owner_host='codex',owner_thread='x')
  self.assertFalse(m.STATE.exists())
 def test_explicit_owner_required(self):
  with self.assertRaisesRegex(ValueError,'owner-host'):m.start(repo=str(self.root),kind='task',prompt='test')
 def test_command_limits_sandbox_and_ignores_user_config(self):
  with patch('shutil.which',return_value='/bin/codex'):
   j={'jobId':'b'*32,'requestedModel':'requested','effort':'high','serviceTier':'default','cwd':str(self.root),'write':False}
   args=m.command(j);self.assertIn('--ignore-user-config',args);self.assertEqual(args[args.index('--sandbox')+1],'read-only');self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',args);self.assertFalse(any(x.startswith('service_tier=') for x in args))
   j['write']=True;j['serviceTier']='fast';args_fast=m.command(j);self.assertEqual(args_fast[args_fast.index('--sandbox')+1],'workspace-write');self.assertIn('service_tier="fast"',args_fast)
 def test_partial_or_failed_output_is_not_success(self):
  self.assertIsNotNone(m.parse_codex('{"type":"turn.started"}', 'partial')[1])
  raw='{"type":"turn.failed","error":{"message":"failed"}}\n{"type":"turn.completed"}'
  self.assertIsNotNone(m.parse_codex(raw,'some text')[1])
 def test_host_access_requires_authorized_write_task(self):
  for overrides in [{'write':False,'kind':'task','authorization':'allowed'}, {'write':True,'kind':'review','authorization':'allowed'}, {'write':True,'kind':'task','authorization':'  '}]:
   with self.subTest(overrides=overrides),self.assertRaises(ValueError):
    m.start(repo=str(self.root),prompt='test',owner_host='claude',owner_thread='fixture',host_access=True,**overrides)
  self.assertFalse(m.STATE.exists())
  with self.assertRaises(ValueError):m.sandbox_policy(kind='task',write=True,authorization='unused')
 def test_host_access_command_and_persisted_scope(self):
  scope='User authorized local test ports, task Docker and local commit; no push.'
  with patch.dict(os.environ,{'CODEX_BIN':str(self.fake())}):
   j=m.start(repo=str(self.root),kind='task',write=True,host_access=True,authorization=scope,prompt='fixture',owner_host='claude',owner_thread='fixture')
  v=self.wait(j['jobId']);self.assertEqual(v['status'],'completed')
  self.assertTrue(v['hostAccess']);self.assertEqual(v['sandboxMode'],'danger-full-access');self.assertEqual(v['authorization'],scope)
  saved=json.loads((m.jobdir(j['jobId'])/'job.json').read_text())
  with patch('shutil.which',return_value='/bin/codex'):
   args=m.command(saved)
  self.assertEqual(args[args.index('--sandbox')+1],'danger-full-access');self.assertIn('approval_policy="never"',args)
  self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',args)
  self.assertIn(scope,(m.jobdir(j['jobId'])/'prompt.txt').read_text())
  for changes in [{'host_access':False,'authorization':''},{'host_access':True,'authorization':'different scope'}]:
   with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'authority while resuming'):
    m.start(repo=str(self.root),kind='task',write=True,prompt='resume',owner_host='claude',owner_thread='fixture',resume_job=j['jobId'],**changes)
  with patch.dict(os.environ,{'CODEX_BIN':str(self.fake())}):
   resumed=m.start(repo=str(self.root),kind='task',write=True,host_access=True,authorization=scope,prompt='resume',owner_host='claude',owner_thread='fixture',resume_job=j['jobId'])
  self.assertEqual(self.wait(resumed['jobId'])['status'],'completed')
 def test_recovered_transport_warning_and_actual_model(self):
  tid='11111111-1111-1111-1111-111111111111';home=self.root/'home';p=home/'sessions/2026/10/20'/('rollout-'+tid+'.jsonl');p.parent.mkdir(parents=True);p.write_text(json.dumps({'type':'turn_context','payload':{'model':'actual-model'}})+'\n'+json.dumps({'type':'event_msg','payload':{'type':'thread_settings_applied','thread_settings':{'service_tier':'priority'}}})+'\n')
  raw='\n'.join(json.dumps(x) for x in [
   {'type':'thread.started','thread_id':tid},
   {'type':'error','message':'Reconnecting... 1/5'},
   {'type':'item.completed','item':{'type':'command_execution','status':'completed','exit_code':0,'command':'sed -n 1p codex.py','aggregated_output':'r"permission denied|sandbox denied"'}},
   {'type':'turn.completed'}
  ])
  with patch.dict(os.environ,{'CODEX_HOME':str(home)}):result,error,denied=m.parse_codex(raw,'final')
  self.assertIsNone(error);self.assertFalse(denied);self.assertEqual(result['model'],'actual-model');self.assertEqual(result['serviceTier'],'priority');self.assertTrue(result['transportWarnings'])
  raw_denied='\n'.join(json.dumps(x) for x in [
   {'type':'thread.started','thread_id':tid},
   {'type':'item.completed','item':{'type':'command_execution','status':'failed','exit_code':1,'command':'touch /etc/hosts','aggregated_output':'touch: /etc/hosts: Operation not permitted'}},
   {'type':'turn.completed'}
  ])
  with patch.dict(os.environ,{'CODEX_HOME':str(home)}):_,_,denied2=m.parse_codex(raw_denied,'final')
  self.assertTrue(denied2)
 def test_concurrent_claim_ack_and_replay(self):
  j=self.terminal()
  with ThreadPoolExecutor(max_workers=8) as pool:claims=list(pool.map(lambda x:m.claim_notification(j,str(x)),range(16)))
  winners=[r for r in claims if r['notify']];self.assertEqual(len(winners),1)
  with self.assertRaisesRegex(ValueError,'claim token'):m.monitor(j,ack=True,claim_token='wrong')
  m.monitor(j,ack=True,claim_token=winners[0]['claimToken']);self.assertFalse(m.claim_notification(j,'replay')['notify']);self.assertTrue(m.monitor(j)['stopMonitor'])
 def test_expired_lease_recovery(self):
  j=self.terminal();a=m.claim_notification(j,'first',lease=1)
  with m.locked(j) as job:job['notificationClaim']['expiresAt']=time.time()-1;m.commit(job)
  b=m.claim_notification(j,'recovery');self.assertTrue(b['notify']);self.assertNotEqual(a['claimToken'],b['claimToken'])
 def test_watch_quiet_timeout_and_fault(self):
  j=self.terminal()
  with m.locked(j) as job:job['status']='preparing';m.commit(job)
  out=io.StringIO()
  with contextlib.redirect_stdout(out):value,code=m.watch(j,interval=.01,timeout=.03)
  self.assertEqual(code,4);self.assertTrue(value['fallbackRequired']);self.assertEqual(out.getvalue(),'')
  self.assertEqual(m.watch('f'*32,timeout=.01)[1],2)
 def test_real_worker_lifecycle_with_fake_cli(self):
  with patch.dict(os.environ,{'CODEX_BIN':str(self.fake())}):
   j=m.start(repo=str(self.root),kind='task',prompt='literal $(must-not-execute)',owner_host='claude',owner_thread='fixture')
   v=self.wait(j['jobId']);self.assertEqual(v['status'],'completed');self.assertEqual(v['requestedModel'],m.DEFAULT_MODEL);self.assertEqual(v['effort'],'medium');self.assertEqual(v['serviceTier'],'default');self.assertEqual(v['result']['response'],'fixture final');self.assertEqual(v['stateDir'],str(m.STATE))
   j_fast=m.start(repo=str(self.root),kind='task',service_tier='fast',prompt='fixture',owner_host='claude',owner_thread='fixture')
   v_fast=self.wait(j_fast['jobId']);self.assertEqual(v_fast['serviceTier'],'fast')
   j_resume=m.start(repo=str(self.root),kind='task',prompt='resume',owner_host='claude',owner_thread='fixture',resume_job=j['jobId'])
   v_resume=self.wait(j_resume['jobId']);self.assertEqual(v_resume['serviceTier'],'default')
 def test_cancellation_stops_owned_worker_and_cli(self):
  with patch.dict(os.environ,{'CODEX_BIN':str(self.fake(True))}):j=m.start(repo=str(self.root),kind='task',prompt='fixture',owner_host='dsh',owner_thread='fixture')
  for _ in range(100):
   v=m.status(j['jobId'])
   if v.get('codexPid'):break
   time.sleep(.02)
  v=m.cancel(j['jobId']);self.assertEqual(v['status'],'cancelled');self.assertIsNone(m.identity(v.get('codexPid')))
 def test_snapshot_freezes_dirty_code_and_rejects_external_symlink(self):
  repo=self.root/'repo';repo.mkdir();subprocess.run(['git','init','-q',str(repo)],check=True)
  (repo/'a.py').write_text('original');(repo/'real_dir').mkdir();(repo/'real_dir'/'skill.md').write_text('v1')
  (repo/'link_dir').symlink_to('real_dir')
  subprocess.run(['git','-C',str(repo),'add','a.py','real_dir/skill.md','link_dir'],check=True);subprocess.run(['git','-C',str(repo),'-c','user.name=test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
  (repo/'a.py').write_text('dirty');dest=self.root/'snapshot';dest.mkdir();_,cwd,_,target=m.snapshot(repo,dest,'working-tree');(repo/'a.py').write_text('later');self.assertEqual((cwd/'a.py').read_text(),'dirty');self.assertTrue(target['files'])
  self.assertTrue((cwd/'link_dir').is_symlink());self.assertEqual(os.readlink(cwd/'link_dir'),'real_dir');self.assertEqual(target['files']['link_dir'],'link:real_dir')
  (repo/'escape').symlink_to(self.root/'outside');(self.root/'outside').write_text('secret');dest=self.root/'bad';dest.mkdir()
  with self.assertRaisesRegex(ValueError,'external symlink'):m.snapshot(repo,dest,'working-tree')
 def test_state_dir_flag_accepted_before_and_after_subcommand(self):
  j=self.terminal();script=Path(__file__).resolve().parents[1]/'codex.py'
  clean_env={k:v for k,v in os.environ.items() if k!='CODEX_JOBS_STATE_DIR'}
  for argv in [[sys.executable,str(script),'--state-dir',str(m.STATE),'status',j],[sys.executable,str(script),'status',j,'--state-dir',str(m.STATE)]]:
   r=subprocess.run(argv,env=clean_env,text=True,capture_output=True,check=True)
   self.assertEqual(json.loads(r.stdout)['jobId'],j)
if __name__=='__main__':unittest.main(verbosity=2)
