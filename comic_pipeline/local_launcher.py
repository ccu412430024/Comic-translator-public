"""Fixed-purpose Chrome native host: start/check the loopback reader only."""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
HOST_NAME = "com.comic_translator.reader"
SERVER = "http://127.0.0.1:8765"
MAX_MESSAGE = 8192


def allowed_origin(root=ROOT):
    public = json.loads((root/"chrome-extension/manifest.json").read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(public)).digest()[:16].hex()
    return "chrome-extension://"+"".join(chr(ord("a")+int(n,16)) for n in digest)+"/"


def read_exact(stream, length):
    data = bytearray()
    while len(data)<length:
        chunk=stream.read(length-len(data))
        if not chunk:raise ValueError("Truncated native message")
        data.extend(chunk)
    return bytes(data)


def read_message(stream):
    size=struct.unpack("<I",read_exact(stream,4))[0]
    if not 0<size<=MAX_MESSAGE:raise ValueError("Invalid native message size")
    return json.loads(read_exact(stream,size).decode("utf-8"))


def write_message(stream, value):
    data=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    if len(data)>MAX_MESSAGE:raise ValueError("Native response too large")
    stream.write(struct.pack("<I",len(data))+data)
    stream.flush()


class LaunchError(RuntimeError):
    pass


def health():
    # Ignore proxy environment variables for the fixed loopback destination.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(SERVER+"/api/health",timeout=1) as response:
            data=json.loads(response.read(8192).decode("utf-8"))
    except urllib.error.HTTPError:
        raise LaunchError("port_occupied") from None
    except (urllib.error.URLError,TimeoutError,ConnectionError,OSError):
        return None
    except (ValueError,UnicodeError):
        raise LaunchError("port_occupied") from None
    if not isinstance(data,dict) or data.get("app")!="comic-reader" or data.get("ready") is not True:
        raise LaunchError("port_occupied")
    return dict(ready=True,version=str(data.get("version",""))[:30],database_ready=data.get("database_ready",True))


@contextmanager
def startup_lock(root=ROOT, timeout=40):
    # A process-wide lock also coalesces separate sendNativeMessage processes.
    folder=root/".runtime/native-host"
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/"start.lock").open("a+b") as file:
        if os.name=="nt":
            import msvcrt
            if file.seek(0,2)==0:file.write(b"0");file.flush()
            deadline=time.monotonic()+timeout
            while True:
                try:
                    file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                    break
                except OSError:
                    if time.monotonic()>=deadline:raise LaunchError("startup_busy") from None
                    time.sleep(.15)
            try:yield
            finally:
                file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(file,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(file,fcntl.LOCK_UN)


def launch_reader(root=ROOT):
    python=root/".venv/Scripts/python.exe"
    script=root/"scripts/serve_reader.py"
    if not python.is_file() or not script.is_file():raise LaunchError("installation_missing")
    options=dict(cwd=str(root),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
    if os.name=="nt":
        # Do not attach the persistent server to Chrome's short-lived host job.
        options["creationflags"]=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_BREAKAWAY_FROM_JOB
    else:options["start_new_session"]=True
    try:return subprocess.Popen([str(python),"-u",str(script)],**options)
    except OSError:raise LaunchError("launch_denied") from None


def ensure_started(root=ROOT, probe=health, launch=launch_reader):
    ready=probe()
    if ready:return dict(ok=True,started=False,url=SERVER,**ready)
    with startup_lock(root):
        ready=probe()
        if ready:return dict(ok=True,started=False,url=SERVER,**ready)
        process=launch(root)
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            ready=probe()
            if ready:return dict(ok=True,started=True,url=SERVER,**ready)
            if process.poll() is not None:raise LaunchError("server_exited")
            time.sleep(.2)
        raise LaunchError("startup_timeout")


def handle(message, origin, root=ROOT):
    if origin!=allowed_origin(root):return dict(ok=False,reason="origin_denied")
    if not isinstance(message,dict) or set(message)!={"action"} or not isinstance(message["action"],str) or message["action"] not in {"ensure_started","status"}:
        return dict(ok=False,reason="invalid_request")
    try:
        if message["action"]=="status":
            ready=health()
            return dict(ok=True,started=False,url=SERVER,**(ready or dict(ready=False,database_ready=False)))
        return ensure_started(root)
    except LaunchError as error:return dict(ok=False,reason=str(error))
    except Exception:return dict(ok=False,reason="startup_failed")


def main(argv, stdin, stdout):
    try:
        if not argv or argv[0]!=allowed_origin():
            value=dict(ok=False,reason="origin_denied")
        else:value=handle(read_message(stdin),argv[0])
    except Exception:value=dict(ok=False,reason="invalid_request")
    write_message(stdout,value)
