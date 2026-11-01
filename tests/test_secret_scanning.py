"""Exercise real scans with generated, nonfunctional credentials in temp repos."""
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import shutil
import string
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('project_secret_scan',ROOT/'scripts/security_scan.py')
scan=importlib.util.module_from_spec(spec);spec.loader.exec_module(scan)


def synthetic(prefix,length=35):
    rng=random.Random(71021)
    return prefix+''.join(rng.choices(string.ascii_letters+string.digits+'_-',k=length))


class PolicyTests(unittest.TestCase):
    def test_sensitive_files_cannot_be_force_added_but_templates_are_allowed(self):
        for path in ('.env','settings/.env.production','.runtime/work-memory.sqlite3','.runtime/.env.example','application_default_credentials.json',
                     'config/my-service-account.json','config/service_account_test.json','credentials-dev.json','id_ed25519','auth/private.pem'):
            with self.subTest(path=path):self.assertTrue(scan.forbidden_path(path))
        for path in ('.env.example','.env.local.example','tests/test_vertex.py','reports/vertex-priority-diagnosis.md','chrome-extension/manifest.json'):
            with self.subTest(path=path):self.assertFalse(scan.forbidden_path(path))

    def test_report_never_copies_match_line_secret_or_arbitrary_description(self):
        value=synthetic('AQ.',48)
        raw=[dict(RuleID='rule',File='tests/fake.py',StartLine=4,Commit='c',Secret=value,Match=value,Line=value,Description=value)]
        sanitized=scan.safe_findings(raw,ROOT)
        self.assertNotIn(value,json.dumps(sanitized));self.assertEqual(set(sanitized[0]),{'rule','file','line','commit'})

    def test_changed_binary_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'gitleaks';path.write_bytes(b'changed')
            with patch.object(scan,'tool_spec',return_value=({},dict(binary_sha256='0'*64),path)),self.assertRaises(scan.ScanError):
                scan.scanner()

    def test_download_checksum_failure_does_not_replace_existing_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'gitleaks';path.write_bytes(b'old')
            manifest=dict(version='0',platforms={})
            binary=dict(archive='fixed.zip',sha256='0'*64,binary_sha256='1'*64)
            with patch.object(scan,'tool_spec',return_value=(manifest,binary,path)),patch.object(scan.urllib.request,'urlopen',return_value=io.BytesIO(b'bad')):
                with self.assertRaises(scan.ScanError):scan.install()
            self.assertEqual(path.read_bytes(),b'old')


class RealScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:scan.scanner()
        except scan.ScanError:raise unittest.SkipTest('Install pinned Gitleaks to run security integration tests.')

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.repo=Path(self.temporary.name)/'repo';self.repo.mkdir()
        self.env=scan.git_env(self.repo)
        self.env['PATH']=str(Path(sys.executable).parent)+os.pathsep+self.env.get('PATH','')
        self.git('init','--initial-branch=main')
        self.git('config','user.name','Security scan test')
        self.git('config','user.email','security-test@example.invalid')
        self.git('config','commit.gpgsign','false')
        self.git('config','core.hooksPath','.disabled-test-hooks')
        self.write('README.md','Synthetic security tests only.\n')
        self.git('add','README.md');self.git('commit','-m','Clean initial fixture')

    def git(self,*args,ok=True):
        result=subprocess.run(['git',*args],cwd=self.repo,env=self.env,capture_output=True,timeout=30)
        if ok:self.assertEqual(result.returncode,0,'Fixture Git operation failed: '+args[0])
        return result

    def write(self,name,text):
        path=self.repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')

    def run_scan(self,scope,script=None):
        result=subprocess.run([sys.executable,str(script or ROOT/'scripts/security_scan.py'),scope,'--repo',str(self.repo)],env=self.env,capture_output=True,text=True,timeout=90)
        self.assertIn(result.returncode,(0,1),result.stderr)
        return result,json.loads(result.stdout)

    def test_all_history_includes_a_deleted_branch_kept_only_by_a_tag(self):
        value=synthetic('AI'+'za')
        self.git('checkout','-b','old-branch');self.write('old-secret.txt','key='+value+'\n')
        self.git('add','.');self.git('commit','-m','Synthetic old fixture');self.git('tag','old-fixture')
        self.git('checkout','main');self.git('branch','-D','old-branch')
        self.assertFalse((self.repo/'old-secret.txt').exists())
        result,report=self.run_scan('history')
        self.assertEqual(result.returncode,1);self.assertTrue(any(f['file']=='old-secret.txt' and f['commit'] for f in report['findings']))
        self.assertNotIn(value,result.stdout+result.stderr)

    def test_staged_scan_checks_index_instead_of_unstaged_working_copy(self):
        value=synthetic('AI'+'za')
        self.write('pending.txt','clean\n');self.git('add','pending.txt')
        self.write('pending.txt','token='+value+'\n')
        self.assertEqual(self.run_scan('staged')[0].returncode,0)
        self.git('add','pending.txt');self.write('pending.txt','clean again\n')
        result,_=self.run_scan('staged');self.assertEqual(result.returncode,1)
        self.assertNotIn(value,result.stdout+result.stderr)

    def test_worktree_catches_untracked_test_and_report_files_and_custom_google_tokens(self):
        first=synthetic('AQ.',48);second=synthetic('1//',54)
        self.write('tests/untracked-fixture.txt','key='+first+'\n')
        self.write('reports/untracked-fixture.json',json.dumps(dict(refresh_token=second)))
        result,report=self.run_scan('worktree')
        self.assertEqual(result.returncode,1)
        self.assertTrue({'project-google-aq-token','project-oauth-refresh-token'}<={f['rule'] for f in report['findings']})
        self.assertNotIn(first,result.stdout+result.stderr);self.assertNotIn(second,result.stdout+result.stderr)

    def test_force_added_credentials_path_fails_even_when_file_is_empty(self):
        self.write('.gitignore','.env\n');self.write('.env','')
        self.git('add','-f','.env')
        result,report=self.run_scan('staged')
        self.assertEqual(result.returncode,1)
        self.assertTrue(any(f['rule']=='sensitive-file-path' and f['file']=='.env' for f in report['findings']))

    def test_inline_allow_comment_cannot_silence_a_credential(self):
        value=synthetic('AI'+'za')
        self.write('reports/inline.txt','key='+value+' # gitleaks:allow\n')
        result,_=self.run_scan('worktree')
        self.assertEqual(result.returncode,1);self.assertNotIn(value,result.stdout+result.stderr)

    @unittest.skipIf(os.name=='nt','Directory symlink creation requires Windows privileges; exercised on Linux CI.')
    def test_worktree_rejects_tracked_paths_redirected_outside_repository(self):
        self.write('sub/file.txt','clean\n');self.git('add','sub/file.txt')
        shutil.rmtree(self.repo/'sub')
        outside=self.repo.parent/'outside';outside.mkdir();(outside/'file.txt').write_text('outside data',encoding='utf-8')
        (self.repo/'sub').symlink_to(outside,target_is_directory=True)
        result=subprocess.run([sys.executable,str(ROOT/'scripts/security_scan.py'),'worktree','--repo',str(self.repo)],env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,2);self.assertNotIn('outside data',result.stdout+result.stderr)

    def test_exact_manifest_public_key_exception_does_not_hide_real_looking_tokens(self):
        manifest=json.loads((ROOT/'chrome-extension/manifest.json').read_text(encoding='utf-8'))
        self.write('chrome-extension/manifest.json',json.dumps(manifest))
        self.assertEqual(self.run_scan('worktree')[0].returncode,0)
        value=synthetic('AI'+'za');manifest['test_api_key']=value
        self.write('chrome-extension/manifest.json',json.dumps(manifest))
        result,_=self.run_scan('worktree');self.assertEqual(result.returncode,1)
        self.assertNotIn(value,result.stdout+result.stderr)

    def test_real_git_commit_hook_blocks_credentials_without_exposing_the_value(self):
        for relative in ('scripts/security_scan.py','security/gitleaks-config.toml','security/gitleaks-release.json',
                         '.githooks/pre-commit','.githooks/pre-push','.githooks/run-security'):
            destination=self.repo/relative;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/relative,destination)
        binary=scan.scanner();destination=self.repo/binary.relative_to(ROOT)
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(binary,destination)
        self.git('config','--unset','core.hooksPath')
        script=self.repo/'scripts/security_scan.py'
        installed=subprocess.run([sys.executable,str(script),'install-hooks','--repo',str(self.repo)],env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(installed.returncode,0,installed.stderr)
        value=synthetic('AI'+'za');self.write('blocked.txt','key='+value+'\n');self.git('add','blocked.txt')
        result=self.git('commit','-m','Must be blocked',ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'findings',result.stdout+result.stderr)
        self.assertNotIn(value.encode(),result.stdout+result.stderr)
