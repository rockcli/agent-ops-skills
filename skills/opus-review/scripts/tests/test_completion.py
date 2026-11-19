import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SKILLS=Path(__file__).resolve().parents[3]
OPUS=SKILLS/'opus-review/scripts/opus.py'
DSH=SKILLS/'dsh-review/scripts/dsh.py'
WATCH=SKILLS/'opus-review/scripts/completion.py'

class Notifications(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.id='a'*32
 def tearDown(self):self.temp.cleanup()
 def seed(self,engine,status='completed'):
  directory=self.root/'jobs'/self.id if engine=='opus' else self.root/self.id
  directory.mkdir(parents=True,exist_ok=True)
  job={'jobId':self.id,'status':status,'createdAt':'test','repo':str(self.root),'result':{'response':'isolated result'},'reply':'isolated result','target':{},'cursor':0}
  (directory/'job.json').write_text(json.dumps(job));return directory/'job.json'
 def command(self,engine,action,*args):
  if engine=='opus':return [sys.executable,str(OPUS),action,self.id,*args]
  return [sys.executable,str(DSH),action,self.id,'--state-dir',str(self.root),*args]
 def env(self):return {**os.environ,'OPUS_REVIEW_STATE_DIR':str(self.root)}
 def run_command(self,engine,action,*args,check=True):
  result=subprocess.run(self.command(engine,action,*args),env=self.env(),text=True,capture_output=True)
  if check:self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  return json.loads(result.stdout),result.returncode
 def test_native_and_fallback_race_has_one_winner_and_late_event_stays_quiet(self):
  for engine in ['opus','dsh']:
   with self.subTest(engine=engine):
    self.seed(engine)
    calls=[subprocess.Popen(self.command(engine,'claim','--source',source),env=self.env(),text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for source in ['native:one','fallback:two']]
    results=[json.loads(p.communicate(timeout=10)[0]) for p in calls]
    self.assertEqual(sum(bool(v['notify']) for v in results),1)
    loser=next(v for v in results if not v['notify']);self.assertFalse(loser['stopMonitor']);self.assertTrue(loser['pendingNotification'])
    winner=next(v for v in results if v['notify'])
    bad,code=self.run_command(engine,'ack','--claim-token','wrong',check=False);self.assertNotEqual(code,0)
    self.run_command(engine,'ack','--claim-token',winner['claimToken'])
    late,_=self.run_command(engine,'claim','--source','native:replayed');self.assertFalse(late['notify']);self.assertTrue(late['stopMonitor'])
 def test_expired_owner_can_be_recovered_and_stale_ack_rejected(self):
  for engine in ['opus','dsh']:
   with self.subTest(engine=engine):
    path=self.seed(engine)
    first,_=self.run_command(engine,'claim','--source','native:lost')
    job=json.loads(path.read_text());job['notificationClaim']['expiresAt']=0;path.write_text(json.dumps(job))
    recovered,_=self.run_command(engine,'claim','--source','fallback:recovery')
    self.assertTrue(recovered['notify']);self.assertNotEqual(first['claimToken'],recovered['claimToken'])
    _,code=self.run_command(engine,'ack','--claim-token',first['claimToken'],check=False);self.assertNotEqual(code,0)
    self.run_command(engine,'ack','--claim-token',recovered['claimToken'])
 def test_observer_is_silent_while_running_and_never_starts_model(self):
  path=self.seed('opus','preparing')
  command=[sys.executable,str(WATCH),'--engine','opus','--job-id',self.id,'--state-dir',str(self.root),'--interval','0.1','--timeout','5']
  p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  time.sleep(.35)
  self.assertIsNone(p.poll())
  import select
  self.assertEqual(select.select([p.stdout,p.stderr],[],[],0)[0],[])
  job=json.loads(path.read_text());job['status']='completed';path.write_text(json.dumps(job))
  stdout,stderr=p.communicate(timeout=10);self.assertEqual(stderr,'');self.assertEqual(p.returncode,0)
  self.assertEqual(json.loads(stdout)['watchStatus'],'terminal');self.assertEqual(len(stdout.splitlines()),1)
 def test_observer_timeout_is_not_a_review_failure(self):
  self.seed('opus','preparing')
  p=subprocess.run([sys.executable,str(WATCH),'--engine','opus','--job-id',self.id,'--state-dir',str(self.root),'--interval','0.05','--timeout','0.1'],text=True,capture_output=True)
  self.assertEqual(p.returncode,4);v=json.loads(p.stdout);self.assertTrue(v['fallbackRequired']);self.assertFalse(v['notify']);self.assertNotIn('status',v)
 def test_missing_job_returns_observer_fault_without_new_job(self):
  p=subprocess.run([sys.executable,str(WATCH),'--engine','opus','--job-id',self.id,'--state-dir',str(self.root)],text=True,capture_output=True)
  self.assertEqual(p.returncode,2);self.assertTrue(json.loads(p.stdout)['fallbackRequired']);self.assertEqual(list(self.root.iterdir()),[])
 def test_pending_claim_waits_for_recovery_without_model_wakes(self):
  path=self.seed('opus')
  self.run_command('opus','claim','--source','native:interrupted')
  p=subprocess.Popen([sys.executable,str(WATCH),'--engine','opus','--job-id',self.id,'--state-dir',str(self.root),'--interval','0.05','--timeout','5'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  time.sleep(.2);self.assertIsNone(p.poll())
  import select
  self.assertEqual(select.select([p.stdout,p.stderr],[],[],0)[0],[])
  job=json.loads(path.read_text());job['notificationClaim']['expiresAt']=0;path.write_text(json.dumps(job))
  stdout,stderr=p.communicate(timeout=10);self.assertEqual(p.returncode,0);self.assertTrue(json.loads(stdout)['notify'])
 def test_killed_dsh_query_does_not_leave_a_lock_that_blocks_recovery(self):
  self.seed('dsh')
  code="import importlib.util,sys,time; from pathlib import Path; s=importlib.util.spec_from_file_location('dsh_jobs',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.STATE=Path(sys.argv[2]); ctx=m.locked(sys.argv[3]); ctx.__enter__(); print('locked',flush=True); time.sleep(10)"
  p=subprocess.Popen([sys.executable,'-c',code,str(DSH),str(self.root),self.id],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  self.assertEqual(p.stdout.readline().strip(),'locked');p.kill();p.communicate(timeout=5)
  recovered,_=self.run_command('dsh','claim','--source','fallback:after-query-death')
  self.assertTrue(recovered['notify'])
 def test_builtin_opus_watch_and_flexible_state_dir(self):
  path=self.seed('opus','preparing')
  p=subprocess.Popen([sys.executable,str(OPUS),'--state-dir',str(self.root),'watch',self.id,'--interval','0.05','--timeout','5'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  time.sleep(.15);self.assertIsNone(p.poll())
  job=json.loads(path.read_text());job['status']='completed';path.write_text(json.dumps(job))
  out,err=p.communicate(timeout=5);self.assertEqual(err,'');self.assertEqual(p.returncode,0)
  v=json.loads(out);self.assertEqual(v['watchStatus'],'terminal');self.assertNotIn('result',v)
  # --state-dir works after subcommand too without OPUS_REVIEW_STATE_DIR env
  clean_env={k:v for k,v in os.environ.items() if k!='OPUS_REVIEW_STATE_DIR'}
  st=subprocess.run([sys.executable,str(OPUS),'status',self.id,'--state-dir',str(self.root)],env=clean_env,text=True,capture_output=True,check=True)
  self.assertEqual(json.loads(st.stdout)['stateDir'],str(self.root.resolve()))
 def test_failed_job_is_a_terminal_receipt_not_observer_fault(self):
  self.seed('opus','failed')
  p=subprocess.run([sys.executable,str(WATCH),'--engine','opus','--job-id',self.id,'--state-dir',str(self.root)],text=True,capture_output=True)
  self.assertEqual(p.returncode,0);v=json.loads(p.stdout);self.assertEqual(v['status'],'failed');self.assertEqual(v['watchStatus'],'terminal')
 def test_snapshot_supports_internal_directory_symlinks(self):
  spec=importlib.util.spec_from_file_location('opus_jobs',OPUS);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  repo=self.root/'repo';repo.mkdir();subprocess.run(['git','init','-q',str(repo)],check=True)
  (repo/'a.py').write_text('v1');(repo/'real_dir').mkdir();(repo/'real_dir'/'skill.md').write_text('doc')
  (repo/'link_dir').symlink_to('real_dir')
  subprocess.run(['git','-C',str(repo),'add','a.py','real_dir/skill.md','link_dir'],check=True)
  subprocess.run(['git','-C',str(repo),'-c','user.name=test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
  (repo/'a.py').write_text('v2');dest=self.root/'snap';dest.mkdir()
  _,cwd,_,target=m.snapshot(repo,dest,'working-tree')
  self.assertTrue((cwd/'link_dir').is_symlink())
  self.assertEqual(os.readlink(cwd/'link_dir'),'real_dir')
  self.assertEqual(target['files']['link_dir'],'link:real_dir')

if __name__=='__main__':unittest.main(verbosity=2)
