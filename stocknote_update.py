"""StockNote GitHub Releases update helper. Only checked releases from fixed repository."""
import hashlib, json, os, platform, re, secrets, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path
from urllib.parse import urlparse
import requests

VERSION = '7.4.0-preview'
REPO = 'duehddnr12-spec/stocknote'
API = 'https://api.github.com/repos/' + REPO + '/releases?per_page=30'
TAG = re.compile(r'^v(\d+)\.(\d+)\.(\d+)(-preview)?$')
LOCK = threading.Lock()
PENDING = {}
MAX_BACKUP = 85 * 1024 * 1024
MAX_ARCHIVE = 180 * 1024 * 1024

def app_data():
    p = (Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local') if platform.system()=='Windows'
         else Path.home()/'Library'/'Application Support' if platform.system()=='Darwin'
         else Path.home()/'.local'/'share') / 'StockNote'
    p.mkdir(parents=True, exist_ok=True)
    return p

def parse_version(tag):
    m=TAG.fullmatch(tag)
    return (tuple(map(int,m.groups()[:3])),bool(m.group(4))) if m else None

def app_target(system=None, executable=None):
    system=system or platform.system()
    if executable is None and not getattr(sys,'frozen',False): return None
    exe=Path(executable or sys.executable).resolve()
    if system=='Windows' and exe.name.lower()=='stocknote.exe': return exe
    if system=='Darwin':
        for p in (exe,*exe.parents):
            if p.name=='StockNote.app': return p
    return None

def github_json(url):
    r=requests.get(url,headers={'Accept':'application/vnd.github+json','User-Agent':'StockNote-Updater'},timeout=(8,25),allow_redirects=False)
    r.raise_for_status()
    return r.json()

def newest():
    data=github_json(API)
    if not isinstance(data,list): raise RuntimeError('릴리스 응답을 확인할 수 없습니다.')
    current=parse_version('v'+VERSION)
    candidates=[]
    for rel in data:
        tag=parse_version(str(rel.get('tag_name') or ''))
        if not rel.get('draft') and tag and tag[1]==current[1] and tag[0]>current[0]:
            candidates.append((tag[0],rel))
    return max(candidates,key=lambda x:x[0])[1] if candidates else None

def get_asset(release,system=None):
    name={'Windows':'StockNote_Windows.zip','Darwin':'StockNote_Mac.zip'}.get(system or platform.system())
    if not name: return None
    return next((a for a in release.get('assets',[]) if a.get('name')==name),None)

def check():
    rel=newest()
    asset=get_asset(rel) if rel else None
    return {'current':VERSION,'available':bool(rel and asset),'tag':rel['tag_name'] if rel and asset else None,
            'installable':bool(rel and asset and app_target()),
            'releaseUrl':rel.get('html_url') if rel and asset else None}

def official(url):
    p=urlparse(url)
    if p.scheme!='https' or p.hostname!='github.com' or not p.path.startswith('/'+REPO+'/releases/download/'):
        raise RuntimeError('공식 GitHub 릴리스 URL이 아닙니다.')

def expected_hash(rel, asset):
    digest=str(asset.get('digest') or '')
    if re.fullmatch(r'sha256:[a-fA-F0-9]{64}',digest): return digest[7:].lower()
    sums=next((a for a in rel.get('assets',[]) if a.get('name')=='SHA256SUMS.txt'),None)
    if not sums: raise RuntimeError('릴리스에 SHA-256 검증 정보가 없습니다.')
    url=sums['browser_download_url'];official(url)
    response=requests.get(url,timeout=(8,25))
    response.raise_for_status()
    if len(response.content)>100000: raise RuntimeError('체크섬 파일 크기 초과')
    for line in response.text.splitlines():
        match=re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?(?:.*/)?(StockNote_(?:Windows|Mac)\.zip)',line.strip())
        if match and match.group(2)==asset['name']:return match.group(1).lower()
    raise RuntimeError('업데이트 검증 해시를 찾지 못했습니다.')

def download(url,dest):
    official(url)
    h=hashlib.sha256();n=0
    with requests.get(url,headers={'User-Agent':'StockNote-Updater'},stream=True,timeout=(12,60)) as r:
        r.raise_for_status()
        with open(dest,'wb') as f:
            for block in r.iter_content(chunk_size=262144):
                if not block: continue
                n+=len(block)
                if n>MAX_ARCHIVE:raise RuntimeError('업데이트 크기 초과')
                f.write(block);h.update(block)
    return h.hexdigest()

def unpack(archive, folder, system):
    folder.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        entries=z.infolist()
        if len(entries)>30000: raise RuntimeError('압축 파일 항목 수 초과')
        for ent in entries:
            filename=ent.filename.replace('\\','/')
            parts=Path(filename).parts
            if filename.startswith('/') or '..' in parts or not parts: raise RuntimeError('안전하지 않은 압축파일 경로')
        if system=='Windows':
            if not any(i.filename=='StockNote.exe' for i in entries): raise RuntimeError('실행 파일 없음')
            z.extract('StockNote.exe',folder)
    if system=='Darwin':
        # macOS ditto preserves app bundle symlinks, executable bits and resource forks.
        subprocess.run(['/usr/bin/ditto','-x','-k',str(archive),str(folder)],check=True)
    app=folder/('StockNote.exe' if system=='Windows' else 'StockNote.app')
    if not app.exists():raise RuntimeError('앱 파일이 없습니다.')
    if system=='Darwin' and not (app/'Contents'/'MacOS'/'StockNote').exists():raise RuntimeError('Mac 앱 실행 파일이 없습니다.')
    return app

def backup(snapshot):
    if not isinstance(snapshot,dict) or snapshot.get('app')!='재고노트':raise ValueError('백업 데이터 형식 오류')
    for key in ('products','movements','orders'):
        if not isinstance(snapshot.get(key),list):raise ValueError('백업 데이터에 '+key+' 항목이 없습니다.')
    if not isinstance(snapshot.get('aliases'),(list,dict)):raise ValueError('별칭 데이터 형식 오류')
    payload=json.dumps(snapshot,ensure_ascii=False).encode('utf-8')
    if len(payload)>MAX_BACKUP:raise ValueError('백업 파일 최대 크기 85MB 초과')
    directory=app_data()/'backups';directory.mkdir(exist_ok=True)
    path=directory/('StockNote_before_update_'+time.strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3)+'.json')
    with open(path,'xb') as f:
        try:os.chmod(path,0o600)
        except OSError:pass
        f.write(payload);f.flush();os.fsync(f.fileno())
    token=secrets.token_urlsafe(32);PENDING.clear();PENDING[token]=time.monotonic()
    return {'token':token,'message':'업데이트 전 재고 백업 완료'}

def helper(src,target,pid,system):
    directory=app_data()
    back=target.with_name(target.name+'.previous-'+time.strftime('%Y%m%d_%H%M%S'))
    if system=='Windows':
        script=directory/'apply_update.ps1'
        script.write_text("""param([int]$OldPid,[string]$Source,[string]$Target,[string]$Backup)
$ErrorActionPreference='Stop'
for($i=0;$i -lt 90;$i++){if(-not (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)){break};Start-Sleep -Seconds 1}
if(Get-Process -Id $OldPid -ErrorAction SilentlyContinue){exit 2}
try{
if(Test-Path -LiteralPath $Target){Move-Item -LiteralPath $Target -Destination $Backup -ErrorAction Stop}
Move-Item -LiteralPath $Source -Destination $Target -ErrorAction Stop
Start-Process -FilePath $Target
}catch{
if(-not (Test-Path -LiteralPath $Target) -and (Test-Path -LiteralPath $Backup)){Move-Item -LiteralPath $Backup -Destination $Target}
exit 1
}
""",encoding='utf-8-sig')
        return ['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-OldPid',str(pid),'-Source',str(src),'-Target',str(target),'-Backup',str(back)]
    script=directory/'apply_update.sh'
    script.write_text("""#!/bin/sh
OLDPID="$1"; SOURCE="$2"; TARGET="$3"; BACKUP="$4"
i=0
while kill -0 "$OLDPID" 2>/dev/null; do i=$((i+1)); if [ "$i" -ge 90 ]; then exit 2; fi; sleep 1; done
if [ -e "$TARGET" ]; then mv "$TARGET" "$BACKUP" || exit 1; fi
if ! mv "$SOURCE" "$TARGET"; then
  if [ -e "$BACKUP" ]; then mv "$BACKUP" "$TARGET"; fi
  exit 1
fi
open "$TARGET" || exit 3
""",encoding='utf-8')
    os.chmod(script,0o700)
    return ['/bin/sh',str(script),str(pid),str(src),str(target),str(back)]

def install(tag,backup_token):
    with LOCK:
        timestamp=PENDING.pop(backup_token,None)
        if timestamp is None or time.monotonic()-timestamp>600:raise RuntimeError('설치 전에 재고 백업이 필요합니다.')
        system=platform.system();target=app_target(system=system)
        if not target or system not in ('Windows','Darwin'):raise RuntimeError('Windows/Mac 설치된 앱에서만 자동 설치가 가능합니다.')
        if not os.access(target.parent,os.W_OK):raise RuntimeError('앱 폴더 쓰기 권한이 없습니다. 쓰기 가능한 폴더에서 실행하세요.')
        rel=newest()
        if not rel or rel.get('tag_name')!=tag:raise RuntimeError('업데이트 버전을 다시 확인하세요.')
        asset=get_asset(rel,system)
        if not asset:raise RuntimeError('운영체제용 앱 파일이 없습니다.')
        expected=expected_hash(rel,asset)
        work=Path(tempfile.mkdtemp(prefix='update-',dir=app_data()))
        archive=work/'download.zip'
        actual=download(asset['browser_download_url'],archive)
        if actual!=expected:raise RuntimeError('SHA-256 검증 실패: 설치하지 않았습니다.')
        source=unpack(archive,work/'stage',system)
        cmd=helper(source,target,os.getpid(),system)
        subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 if system=='Windows' else 0,start_new_session=system!='Windows')
        return {'scheduled':True,'message':tag+' 다운로드·SHA-256 검증 완료. 앱을 종료하고 설치 후 다시 실행합니다.'}
