"""StockNote GitHub Releases update helper. Only checked releases from fixed repository."""
import hashlib, json, os, platform, re, secrets, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path
from urllib.parse import urlparse
import requests

VERSION = '7.5.4-preview'
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

def install_preflight():
    """Reasons shown directly inside the app when in-place updating is unavailable."""
    system=platform.system()
    if system not in ('Windows','Darwin'):
        return '자동 설치는 Windows 또는 Mac에서만 가능합니다.'
    target=app_target(system=system)
    if target is None:
        return '앱 실행 파일에서 재고노트를 실행해 주세요. (HTML 파일이나 Python 서버만 실행한 경우 자동 설치 불가)'
    if not os.access(target.parent,os.W_OK):
        return '실행 중인 앱 폴더에 쓰기 권한이 없습니다. 다운로드 폴더 등 본인이 수정할 수 있는 위치로 앱을 이동해 실행하세요.'
    return None

def check():
    rel=newest()
    asset=get_asset(rel) if rel else None
    reason=install_preflight()
    return {'current':VERSION,'available':bool(rel and asset),'tag':rel['tag_name'] if rel and asset else None,
            'installable':bool(rel and asset and not reason),
            'reason':reason or ('' if rel and asset else '설치 가능한 최신 업데이트가 없습니다.'),
            'releaseUrl':rel.get('html_url') if rel and asset else None,
            'lastStatus':last_update_status()}

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

def update_status(message):
    """Store only installer progress; never log personal stock or API credentials."""
    app_data().joinpath('update_status.txt').write_text(str(message)[:600], encoding='utf-8')

def last_update_status():
    try:
        return (app_data()/'update_status.txt').read_text(encoding='utf-8')[:600]
    except (OSError,UnicodeError):
        return ''

def helper(src,target,pid,system):
    directory=app_data()
    back=target.with_name(target.name+'.previous-'+time.strftime('%Y%m%d_%H%M%S'))
    status=directory/'update_status.txt'
    if system=='Windows':
        script=directory/('apply_update_'+secrets.token_hex(4)+'.ps1')
        script.write_text("""param([int]$OldPid,[string]$Source,[string]$Target,[string]$Backup,[string]$Status)
$ErrorActionPreference='Stop'
function State([string]$message) {
  try { [System.IO.File]::WriteAllText($Status,$message,[System.Text.Encoding]::UTF8) } catch {}
}
State '앱 종료 대기 중'
for($i=0;$i -lt 120;$i++){
  if(-not (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)){break}
  Start-Sleep -Seconds 1
}
if(Get-Process -Id $OldPid -ErrorAction SilentlyContinue){
  State '업데이트 실패: 기존 재고노트 프로세스가 종료되지 않았습니다.'
  exit 2
}
$originalMoved=$false
$newInstalled=$false
try {
  State '기존 실행파일 잠금 해제 대기 중'
  # PyInstaller onefile can retain the exe lock in its parent bootloader
  # for a short time even after the HTTP child process exits.
  if(Test-Path -LiteralPath $Target){
    $moved=$false
    for($i=0;$i -lt 90;$i++){
      try {
        Move-Item -LiteralPath $Target -Destination $Backup -ErrorAction Stop
        $moved=$true
        $originalMoved=$true
        break
      } catch {
        Start-Sleep -Milliseconds 750
      }
    }
    if(-not $moved){throw '기존 실행파일이 다른 프로세스에서 사용 중이거나 폴더 쓰기 권한이 없습니다.'}
  }
  State '새 실행파일 설치 중'
  Move-Item -LiteralPath $Source -Destination $Target -ErrorAction Stop
  $newInstalled=$true
  Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target) -ErrorAction Stop
  State '업데이트 완료: 새 재고노트가 실행되었습니다.'
} catch {
  $failure='업데이트 실패: '+$_.Exception.Message
  if($originalMoved -and (Test-Path -LiteralPath $Backup)){
    try {
      if($newInstalled -and (Test-Path -LiteralPath $Target)) {
        Move-Item -LiteralPath $Target -Destination ($Target+'.failed-'+[DateTime]::UtcNow.Ticks) -ErrorAction Stop
      }
      if(-not (Test-Path -LiteralPath $Target)){
        Move-Item -LiteralPath $Backup -Destination $Target -ErrorAction Stop
      }
    } catch { $failure=$failure+' / 이전 버전 복구 실패: '+$_.Exception.Message }
  }
  State $failure
  exit 1
}
""",encoding='utf-8-sig')
        return ['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-OldPid',str(pid),'-Source',str(src),'-Target',str(target),'-Backup',str(back),'-Status',str(status)]
    script=directory/('apply_update_'+secrets.token_hex(4)+'.sh')
    script.write_text("""#!/bin/sh
OLDPID="$1"; SOURCE="$2"; TARGET="$3"; BACKUP="$4"; STATUS="$5"
status(){ printf '%s\\n' "$1" > "$STATUS" 2>/dev/null || true; }
status '앱 종료 대기 중'
i=0
while kill -0 "$OLDPID" 2>/dev/null; do
  i=$((i+1))
  if [ "$i" -ge 120 ]; then status '업데이트 실패: 이전 프로세스가 종료되지 않았습니다.'; exit 2; fi
  sleep 1
done
status '새 앱 설치 중'
if [ -e "$TARGET" ]; then
  if ! mv "$TARGET" "$BACKUP"; then status '업데이트 실패: 기존 앱을 이동할 수 없습니다.'; exit 1; fi
fi
if ! mv "$SOURCE" "$TARGET"; then
  if [ -e "$BACKUP" ]; then mv "$BACKUP" "$TARGET"; fi
  status '업데이트 실패: 새 앱 설치에 실패하여 이전 버전 복구를 시도했습니다.'
  exit 1
fi
if ! open "$TARGET"; then
  status '업데이트 실패: 새 앱 파일은 설치했지만 실행하지 못했습니다.'
  exit 3
fi
status '업데이트 완료: 새 재고노트가 실행되었습니다.'
""",encoding='utf-8')
    os.chmod(script,0o700)
    return ['/bin/sh',str(script),str(pid),str(src),str(target),str(back),str(status)]

def install(tag,backup_token):
    with LOCK:
        timestamp=PENDING.pop(backup_token,None)
        if timestamp is None or time.monotonic()-timestamp>600:raise RuntimeError('설치 전에 재고 백업이 필요합니다.')
        system=platform.system();target=app_target(system=system)
        reason=install_preflight()
        if reason:raise RuntimeError(reason)
        if not target:raise RuntimeError('설치 앱을 찾지 못했습니다.')
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
        update_status('업데이트 다운로드 및 검증 완료 · 프로그램 종료 준비 중')
        subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 if system=='Windows' else 0,start_new_session=system!='Windows')
        return {'scheduled':True,'message':tag+' 다운로드·SHA-256 검증 완료. 앱을 종료하고 설치 후 다시 실행합니다.'}
