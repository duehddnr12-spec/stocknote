from pathlib import Path
from importlib.metadata import distribution
import os, sys, platform, subprocess, zipfile
ROOT=Path(__file__).resolve().parent
SYSTEM=platform.system()
if SYSTEM not in ('Windows','Darwin'):
    raise SystemExit('Build on Windows or macOS.')
assets = ('재고노트_v7_범용재고관리.html', 'stocknote_mobile.html', 'stocknote_mobile.js')
for name in ('재고노트_통합API_로컬서버.py', *assets):
    if not (ROOT/name).is_file(): raise SystemExit('Missing '+name)
cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--name','StockNote','--windowed','--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build')]
if SYSTEM=='Windows': cmd.append('--onefile')
else: cmd.extend(['--onedir','--osx-bundle-identifier','com.stocknote.inventory'])
for name in assets:
    cmd.extend(['--add-data', str(ROOT/name)+os.pathsep+'.'])
qr_license = next(file.locate() for file in distribution('qrcode').files if file.name == 'LICENSE')
cmd.extend(['--add-data', str(qr_license)+os.pathsep+'licenses/qrcode'])
cmd.append(str(ROOT/'재고노트_통합API_로컬서버.py'))
subprocess.run(cmd,cwd=ROOT,check=True)
(ROOT/'release').mkdir(exist_ok=True)
if SYSTEM=='Windows':
    with zipfile.ZipFile(ROOT/'release'/'StockNote_Windows.zip','w',zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT/'dist'/'StockNote.exe','StockNote.exe')
        z.write(qr_license, 'qrcode-LICENSE.txt')
else:
    subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(ROOT/'dist'/'StockNote.app'),str(ROOT/'release'/'StockNote_Mac.zip')],check=True)
print('App package created.')
