"""Pinned, offline secret scans. Only installation accesses the network.

Reports contain locations/rule IDs, never matched source or secret values.
The tool uses Python's standard library, not the application's model stack.
"""
import argparse
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
POLICY=ROOT/'security/gitleaks-config.toml'
RELEASE=ROOT/'security/gitleaks-release.json'


class ScanError(Exception):
    pass


def git_env(repo):
    env=dict(os.environ)
    count=int(env.get('GIT_CONFIG_COUNT','0'))
    env.update(GIT_CONFIG_COUNT=str(count+1))
    env[f'GIT_CONFIG_KEY_{count}']='safe.directory'
    env[f'GIT_CONFIG_VALUE_{count}']=repo.as_posix()
    # Never let an environment override the repository's explicit scan policy.
    env.pop('GITLEAKS_CONFIG_TOML',None)
    env.pop('GITLEAKS_CONFIG',None)
    return env


def git(repo,*args):
    result=subprocess.run(['git',*args],cwd=repo,env=git_env(repo),capture_output=True,timeout=90)
    if result.returncode:raise ScanError('Git operation failed; command output was withheld.')
    return result.stdout


def tool_spec():
    release=json.loads(RELEASE.read_text(encoding='utf-8'))
    machine=platform.machine().lower()
    target=platform.system().lower()+'_'+('x64' if machine in ('amd64','x86_64') else machine)
    spec=release['platforms'].get(target)
    if not spec:raise ScanError('Pinned scanner supports Windows x64 and Linux x64; add verified release hashes for other platforms.')
    name='gitleaks.exe' if target.startswith('windows') else 'gitleaks'
    return release,spec,ROOT/'.runtime/security-tools'/release['version']/target/name


def digest(data):
    return hashlib.sha256(data).hexdigest()


def scanner():
    _,spec,path=tool_spec()
    if not path.is_file() or digest(path.read_bytes())!=spec['binary_sha256']:
        raise ScanError('Pinned Gitleaks is missing or has changed. Run: python scripts/security_scan.py install')
    return path


def install():
    release,spec,path=tool_spec()
    if path.is_file() and digest(path.read_bytes())==spec['binary_sha256']:
        print('Verified Gitleaks '+release['version']);return
    url=f"https://github.com/gitleaks/gitleaks/releases/download/v{release['version']}/{spec['archive']}"
    request=urllib.request.Request(url,headers={'User-Agent':'comic-translator-secret-scan'})
    with urllib.request.urlopen(request,timeout=45) as response:data=response.read(32*1024*1024+1)
    if len(data)>32*1024*1024 or digest(data)!=spec['sha256']:raise ScanError('Scanner archive checksum mismatch; nothing installed.')
    # Read one known archive member; do not extract arbitrary archive paths.
    if spec['archive'].endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:binary=archive.read(path.name)
    else:
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:binary=archive.extractfile(path.name).read()
    if digest(binary)!=spec['binary_sha256']:raise ScanError('Scanner executable checksum mismatch; nothing installed.')
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as temporary:
        temporary.write(binary);temporary_path=Path(temporary.name)
    try:
        temporary_path.chmod(0o755);os.replace(temporary_path,path)
    finally:temporary_path.unlink(missing_ok=True)
    print('Installed verified Gitleaks '+release['version'])


def forbidden_path(value):
    path=PurePosixPath(value.replace('\\','/'))
    name=path.name.lower()
    if any(part.lower() in ('.runtime','.aws','.azure','.gcloud') for part in path.parts):return True
    if name=='.env.example' or name.startswith('.env.') and name.endswith('.example'):return False
    if name=='.env' or name.startswith('.env.'):return True
    if name in ('application_default_credentials.json','credentials.db','access_tokens.db','.git-credentials','id_rsa','id_ed25519'):return True
    if any(fnmatch.fnmatch(name,pattern) for pattern in ('*service-account*.json','*service_account*.json','credentials*.json','*.key','*.pem','*.pfx','*.p12')):return True
    return False


def paths_for(repo,scope):
    if scope=='history':
        if git(repo,'rev-parse','--is-shallow-repository').strip()!=b'false':raise ScanError('Full history scan needs a non-shallow checkout (fetch-depth: 0).')
        output=git(repo,'log','--all','--format=','--name-only','--diff-filter=ACMR','-z')
    elif scope=='staged':output=git(repo,'diff','--cached','--name-only','--diff-filter=ACMR','-z')
    else:output=git(repo,'ls-files','--cached','--others','--exclude-standard','-z')
    return sorted({path.decode('utf-8',errors='surrogateescape').lstrip('\r\n') for path in output.split(b'\0') if path.strip(b'\r\n')})


def safe_findings(raw,base):
    result=[]
    for finding in raw:
        file=str(finding.get('File','')).replace('\\','/')
        prefix=base.as_posix().rstrip('/')+'/'
        if file.startswith(prefix):file=file[len(prefix):]
        result.append(dict(rule=str(finding.get('RuleID','unknown')),file=file,
            line=int(finding.get('StartLine',0)),commit=str(finding.get('Commit',''))))
    return result


def run_scan(repo,scope):
    binary=scanner()
    paths=paths_for(repo,scope)
    findings=[dict(rule='sensitive-file-path',file=path,line=0,commit='') for path in paths if forbidden_path(path)]
    reports=ROOT/'.runtime/security-scans';reports.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='scan-',dir=reports) as directory:
        temporary=Path(directory);raw_report=temporary/'raw.json';ignore=temporary/'empty-ignore'
        ignore.write_text('',encoding='utf-8')
        cwd=repo;target=repo
        if scope=='worktree':
            # Scan tracked + non-ignored working files, including unstaged edits.
            # This avoids walking model downloads, ADC data or ignored runtime files.
            target=temporary/'snapshot';target.mkdir();cwd=target
            for value in paths:
                relative=PurePosixPath(value)
                if relative.is_absolute() or '..' in relative.parts:raise ScanError('Invalid repository path.')
                source=repo/value;destination=target/value
                if not source.exists() and not source.is_symlink():continue
                if not source.is_symlink() and not source.resolve().is_relative_to(repo):raise ScanError('Working file resolves outside the repository.')
                destination.parent.mkdir(parents=True,exist_ok=True)
                if source.is_symlink():destination.write_text(os.readlink(source),encoding='utf-8')
                elif source.is_file():shutil.copyfile(source,destination)
        command=[str(binary),'dir' if scope=='worktree' else 'git',
            '--config='+str(POLICY),'--redact=100','--no-banner','--no-color','--log-level=error',
            '--ignore-gitleaks-allow','--gitleaks-ignore-path='+str(ignore),
            '--report-format=json','--report-path='+str(raw_report)]
        command+=['--log-opts=--all'] if scope=='history' else ['--pre-commit','--staged'] if scope=='staged' else []
        command.append('.')
        completed=subprocess.run(command,cwd=cwd,env=git_env(repo),capture_output=True,timeout=180)
        if completed.returncode not in (0,1) or not raw_report.is_file():
            raise ScanError('Gitleaks did not complete. Raw diagnostics are withheld to avoid exposing credentials.')
        raw=json.loads(raw_report.read_text(encoding='utf-8'))
        if not isinstance(raw,list):raise ScanError('Invalid scanner report.')
        if completed.returncode==1 and not raw:raise ScanError('Scanner failed without a finding; scan is incomplete.')
        findings+=safe_findings(raw,target)
    report=dict(scope=scope,files_checked=len(paths),findings=findings)
    (reports/(scope+'.json')).write_text(json.dumps(report,ensure_ascii=True,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    return 1 if findings else 0


def install_hooks(repo):
    scanner()
    configured=subprocess.run(['git','config','--get','core.hooksPath'],cwd=repo,env=git_env(repo),capture_output=True)
    if configured.returncode not in (0,1):raise ScanError('Cannot read repository hook settings.')
    path=configured.stdout.decode().strip()
    if path and path!='.githooks':raise ScanError('Existing hook directory is configured; integrate it before replacing any hooks.')
    if not path:
        for name in ('pre-commit','pre-push'):
            active=git(repo,'rev-parse','--git-path','hooks/'+name).decode().strip()
            if (repo/active).exists():raise ScanError('An existing active hook must be preserved and integrated first.')
    for name in ('pre-commit','pre-push','run-security'):
        hook=repo/'.githooks'/name
        if not hook.is_file():raise ScanError('Tracked security hooks are missing.')
        hook.chmod(hook.stat().st_mode|0o111)
    git(repo,'config','--local','core.hooksPath','.githooks')
    print('Enabled repository pre-commit and pre-push secret scans.')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('install','install-hooks','history','staged','worktree'))
    parser.add_argument('--repo',type=Path,default=ROOT)
    args=parser.parse_args(argv);repo=args.repo.resolve()
    try:
        if args.command=='install':install()
        elif args.command=='install-hooks':install_hooks(repo)
        else:return run_scan(repo,args.command)
        return 0
    except Exception as error:
        # Never print arbitrary subprocess, HTTP or file contents on failure.
        message=str(error) if isinstance(error,ScanError) else type(error).__name__+'; diagnostic contents withheld.'
        print('Secret scan failed: '+message,file=sys.stderr)
        return 2


if __name__=='__main__':sys.exit(main())
