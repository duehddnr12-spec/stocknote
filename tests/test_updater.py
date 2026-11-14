import hashlib
import io
import json
import os
import platform
import socket
import tempfile
import subprocess
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile
import stocknote_update as u

class UpdateSafetyTests(unittest.TestCase):
    def test_version_and_channel(self):
        self.assertEqual(u.parse_version('v7.4.0-preview'), ((7,4,0),True))
        self.assertEqual(u.parse_version('v7.4.0'), ((7,4,0),False))
        self.assertIsNone(u.parse_version('v7.4.0-preview-extra'))
        self.assertIsNone(u.parse_version('v7.4.0/../../bad'))
    def test_only_newer_preview_release(self):
        releases=[
          {'tag_name':'v7.5.0','draft':False,'assets':[]},
          {'tag_name':'v7.5.2-preview','draft':False,'assets':[]},
          {'tag_name':'v7.5.5-preview','draft':False,'assets':[]},
          {'tag_name':'v7.9.0-preview','draft':True,'assets':[]}]
        with patch.object(u,'VERSION','7.5.4-preview'), patch.object(u,'github_json',return_value=releases):
            self.assertEqual(u.newest()['tag_name'],'v7.5.5-preview')
    def test_bundled_version_agrees_with_ui(self):
        from pathlib import Path
        html=(Path(__file__).resolve().parent.parent/'재고노트_v7_범용재고관리.html').read_text(encoding='utf-8')
        version = u.parse_version('v'+u.VERSION)
        self.assertIsNotNone(version)
        self.assertTrue(version[1])
        self.assertIn('현재 버전 v'+u.VERSION+' ·', html)
        self.assertIn('업데이트 설치 ·', html)

    def test_installed_app_permission_and_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            exe=Path(folder)/'StockNote.exe'
            with patch.object(u.platform,'system',return_value='Windows'),patch.object(u.sys,'frozen',True,create=True),patch.object(u.sys,'executable',str(exe)),patch.object(u.os,'access',return_value=True):
                self.assertIsNone(u.install_preflight())
            with patch.object(u.platform,'system',return_value='Windows'),patch.object(u.sys,'frozen',True,create=True),patch.object(u.sys,'executable',str(exe)),patch.object(u.os,'access',return_value=False):
                self.assertIn('쓰기 권한',u.install_preflight())
            with patch.object(u.platform,'system',return_value='Windows'),patch.object(u.sys,'frozen',False,create=True):
                self.assertIn('앱 실행 파일',u.install_preflight())

    def test_available_update_check_has_actionable_result(self):
        release={'tag_name':'v7.5.5-preview','draft':False,'assets':[{'name':'StockNote_Windows.zip'}]}
        with patch.object(u,'newest',return_value=release),patch.object(u,'get_asset',return_value=release['assets'][0]),patch.object(u,'install_preflight',return_value=None):
            info=u.check()
            self.assertTrue(info['installable'])
            self.assertEqual(info['current'],u.VERSION)
            self.assertEqual(info['tag'],release['tag_name'])
        with patch.object(u,'newest',return_value=release),patch.object(u,'get_asset',return_value=release['assets'][0]),patch.object(u,'install_preflight',return_value='권한 없음'):
            info=u.check()
            self.assertFalse(info['installable'])
            self.assertEqual(info['reason'],'권한 없음')
    def test_platform_target_and_asset(self):
        with tempfile.TemporaryDirectory() as folder:
            exe=Path(folder)/'StockNote.exe'
            self.assertEqual(u.app_target(system='Windows',executable=str(exe)),exe.resolve())
            self.assertIsNone(u.app_target(system='Windows',executable=str(exe.with_name('Other.exe'))))
            mac=Path(folder)/'StockNote.app'/'Contents'/'MacOS'/'StockNote'
            self.assertEqual(u.app_target(system='Darwin',executable=str(mac)),(Path(folder)/'StockNote.app').resolve())
        self.assertEqual(u.get_asset({'assets':[{'name':'StockNote_Mac.zip'}]},'Darwin')['name'],'StockNote_Mac.zip')
    def test_installer_status_and_generated_scripts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(u,'app_data',return_value=Path(folder)):
            u.update_status('ready to install')
            self.assertEqual(u.last_update_status(),'ready to install')
            src=Path(folder)/'stage'/'StockNote.exe'
            target=Path(folder)/'app'/'StockNote.exe'
            src.parent.mkdir()
            target.parent.mkdir()
            cmd=u.helper(src,target,99999999,'Windows','v'+u.VERSION)
            self.assertTrue(cmd[0].endswith('powershell.exe'))
            self.assertIn('-Status',cmd)
            script=Path(cmd[cmd.index('-File')+1]).read_text(encoding='utf-8-sig')
            self.assertIn('Start-Sleep -Milliseconds 750',script)
            self.assertIn('기존 실행파일이 다른 프로세스',script)
            self.assertIn('업데이트 완료:',script)
            self.assertIn('$newInstalled=$true',script)
            self.assertIn("$env:PYINSTALLER_RESET_ENVIRONMENT='1'",script)
            if platform.system()=='Windows':
                parser_command=(
                    "$tokens=$null; $errors=$null; "
                    "[System.Management.Automation.Language.Parser]::ParseFile("
                    +"'"+str(Path(cmd[cmd.index('-File')+1])).replace("'","''")+"'"
                    +",[ref]$tokens,[ref]$errors) | Out-Null; "
                    "if($errors.Count -gt 0){$errors | Out-String | Write-Output; exit 1}"
                )
                subprocess.run(['powershell.exe','-NoProfile','-Command',parser_command],check=True,timeout=30)
                # Invalid Windows EXE forces Start-Process to fail, so we can
                # exercise rollback without leaving a process running or locked.
                src.write_bytes(b'not a Windows executable')
                target.write_bytes(b'old executable')
                run=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=45)
                self.assertNotEqual(run.returncode,0)
                self.assertEqual(target.read_bytes(),b'old executable')
                self.assertIn('업데이트 실패',u.last_update_status())
            else:
                mac=u.helper(Path(folder)/'new.app',Path(folder)/'old.app',99999999,'Darwin','v'+u.VERSION)
                self.assertEqual(mac[0],'/bin/sh')
                subprocess.run(['/bin/sh','-n',mac[1]],check=True,timeout=15)

    @unittest.skipUnless(platform.system()=='Windows','Requires the Windows PyInstaller bootloader')
    def test_frozen_windows_update_restarts_same_path(self):
        # A valid one-file executable exercises the restart path missed by the rollback test.
        with tempfile.TemporaryDirectory(prefix='stocknote upgrade ') as folder:
            root=Path(folder)
            app_dir=root/'재고 앱'
            app_dir.mkdir()
            state_dir=root/'state'
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                port=listener.getsockname()[1]
            source=root/'probe.py'
            source.write_text(f'''import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stocknote_update as updater

state = Path({str(state_dir)!r})
state.mkdir(exist_ok=True)
updater.app_data = lambda: state
port = {port}
if not (state/'first-launch').exists():
    (state/'first-launch').touch()
    command = updater.helper(Path({str(root/'incoming'/'StockNote.exe')!r}), Path(sys.executable), os.getpid(), 'Windows', 'v'+updater.VERSION, port)
    try:
        updater.launch_installer(command, 'Windows')
    except OSError as error:
        updater.update_status('업데이트 실패: '+str(error))
        sys.exit(1)
    os._exit(0)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != '/api/app/version':
            self.send_error(404)
            return
        body = json.dumps({{'version': updater.VERSION}}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        timer = threading.Timer(2, self.server.shutdown)
        timer.daemon = True
        timer.start()

with ThreadingHTTPServer(('127.0.0.1',port),Handler) as server:
    timer = threading.Timer(60,server.shutdown)
    timer.daemon = True
    timer.start()
    server.serve_forever()
''',encoding='utf-8')
            repo=Path(u.__file__).resolve().parent
            subprocess.run([sys.executable,'-m','PyInstaller','--onefile','--windowed','--noconfirm',
                            '--name','StockNote','--paths',str(repo),'--distpath',str(root/'incoming'),
                            '--workpath',str(root/'build'),'--specpath',str(root),str(source)],
                           cwd=root,check=True,timeout=150,capture_output=True)
            target=app_dir/'StockNote.exe'
            shutil.copy2(root/'incoming'/'StockNote.exe',target)
            initial = subprocess.run([str(target)],cwd=app_dir,timeout=30,capture_output=True)
            status_file=state_dir/'update_status.txt'
            log_file=state_dir/'update_installer.log'
            if initial.returncode:
                self.fail(f'Could not start the installer: {initial.returncode}, '
                          f'{status_file.read_bytes() if status_file.exists() else b""!r}, '
                          f'{log_file.read_bytes()[:3000] if log_file.exists() else b""!r}')
            deadline=time.monotonic()+65
            last=''
            while time.monotonic()<deadline:
                if status_file.exists():
                    try:
                        last=status_file.read_text(encoding='utf-8-sig')
                    except (OSError,UnicodeError):
                        last=''
                    if last.startswith(('업데이트 완료:','업데이트 실패:')):
                        break
                time.sleep(.25)
            self.assertTrue(last.startswith('업데이트 완료:'),
                            f'{last!r}, {log_file.read_bytes()[:3000] if log_file.exists() else b""!r}')
            self.assertTrue(target.is_file())
            self.assertEqual(len(list(app_dir.glob('StockNote.exe.previous-*'))),1)
            # The test app exits after answering the health check, then releases its EXE lock.
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                try:
                    target.unlink()
                    break
                except PermissionError:
                    time.sleep(.25)
            self.assertFalse(target.exists(),'The restarted test executable did not exit')

    def test_untrusted_download_rejected(self):
        for url in ['http://github.com/'+u.REPO+'/releases/download/v7.5/a.zip',
                    'https://evil.com/'+u.REPO+'/releases/download/a',
                    'https://github.com/other/repo/releases/download/v7.5/a.zip']:
            with self.assertRaises(RuntimeError):u.official(url)
    def test_checksum_digest(self):
        sha='a'*64
        self.assertEqual(u.expected_hash({}, {'digest':'sha256:'+sha}),sha)
    def test_zip_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'bad.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('../evil.txt','bad')
                z.writestr('StockNote.exe','exe')
            with self.assertRaises(RuntimeError):
                u.unpack(archive,Path(folder)/'out','Windows')
    def test_reliable_backup_and_expiring_token(self):
        data={'app':'재고노트','version':4,'products':[{'sku':'ABC','stock':1}],
              'movements':[],'orders':[],'aliases':{}}
        with tempfile.TemporaryDirectory() as folder,patch.object(u,'app_data',return_value=Path(folder)):
            response=u.backup(data)
            self.assertTrue(response['token'] in u.PENDING)
            files=list((Path(folder)/'backups').glob('*.json'))
            self.assertEqual(len(files),1)
            self.assertEqual(json.loads(files[0].read_text())['products'][0]['sku'],'ABC')
            with self.assertRaises(ValueError):u.backup({'app':'재고노트','products':[]})

if __name__=='__main__':unittest.main()
