"""Build/register the current user's fixed-purpose Chrome native host."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from comic_pipeline.local_launcher import HOST_NAME,allowed_origin


def build(root=ROOT):
    compiler=Path(os.environ.get("WINDIR",r"C:\Windows"))/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        compiler=Path(os.environ.get("WINDIR",r"C:\Windows"))/"Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not compiler.is_file():raise RuntimeError("Windows .NET Framework C# compiler is missing")
    folder=root/".runtime/native-host";folder.mkdir(parents=True,exist_ok=True)
    binary=folder/"comic-reader-host.exe"
    result=subprocess.run([str(compiler),"/nologo","/target:winexe","/optimize+","/out:"+str(binary),str(root/"scripts/native_reader_host.cs")],capture_output=True)
    if result.returncode:raise RuntimeError("Native host compilation failed")
    manifest=folder/(HOST_NAME+".json")
    manifest.write_text(json.dumps(dict(name=HOST_NAME,description="Start the local Comic Reader service and SQLite library",path=str(binary),type="stdio",allowed_origins=[allowed_origin(root)]),ensure_ascii=False,indent=2),encoding="utf-8")
    return manifest


def register(manifest):
    import winreg
    key_path=r"Software\Google\Chrome\NativeMessagingHosts"+"\\"+HOST_NAME
    for view in (winreg.KEY_WOW64_32KEY,winreg.KEY_WOW64_64KEY):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,key_path,0,winreg.KEY_WRITE|view) as key:
            winreg.SetValueEx(key,"",0,winreg.REG_SZ,str(manifest))


def unregister(root=ROOT):
    import winreg
    expected=str(root/".runtime/native-host"/(HOST_NAME+".json"))
    key_path=r"Software\Google\Chrome\NativeMessagingHosts"+"\\"+HOST_NAME
    for view in (winreg.KEY_WOW64_32KEY,winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key_path,0,winreg.KEY_READ|view) as key:
                current=winreg.QueryValueEx(key,"")[0]
            if os.path.normcase(os.path.abspath(current))==os.path.normcase(os.path.abspath(expected)):
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER,key_path,view,0)
        except FileNotFoundError:pass


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--build-only",action="store_true")
    parser.add_argument("--uninstall",action="store_true")
    options=parser.parse_args()
    if os.name!="nt":raise SystemExit("This installer supports Windows Chrome only")
    if options.uninstall:
        unregister();print("Chrome automatic startup bridge unregistered; library data was retained.")
    else:
        manifest=build()
        if not options.build_only:register(manifest)
        print(json.dumps(dict(built=True,registered=not options.build_only,manifest=str(manifest),allowed_origin=allowed_origin()),ensure_ascii=False))


if __name__=="__main__":main()
