"""재고노트 v6.5 - NAVER Commerce API 로컬 연동 (127.0.0.1 전용)
실제 계정 연동을 위해 판매자 커머스API 센터의 Client ID/Secret, API 호출 IP 등록 필요.
공식 API 엔드포인트: /external/v1/pay-order/seller/product-orders/last-changed-statuses
                      /external/v1/pay-order/seller/product-orders/query
"""
import base64
import hashlib
import json
import os
import sys
import re
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, quote_plus
from threading import Lock, Timer
import stocknote_update
from email.utils import parsedate_to_datetime

try:
    import bcrypt
    import requests
except ImportError:
    print('필요한 모듈이 없습니다. cmd에서 다음을 실행하세요: python -m pip install bcrypt requests')
    raise SystemExit(1)

BASE = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
HTML = BASE / '재고노트_v7_범용재고관리.html'
CONFIG = BASE / '재고노트_API설정.txt'
KST = timezone(timedelta(hours=9))
API = 'https://api.commerce.naver.com/external'
PORT = 8765
session = requests.Session()
token_cache = {'token': None, 'expires': 0}


# UI에서 입력한 앱 ID / 시크릿은 서버 메모리에만 저장됩니다.
# 디스크의 설정 텍스트에는 비밀키를 저장하지 않습니다.
NAVER_CONFIG = {}
NAVER_LOCK = Lock()
# 서로 다른 UI 버튼/브라우저 탭에서 주문 수집이 겹치지 않도록 합니다.
NAVER_JOB_LOCK = Lock()
# 각 네이버 API 주소에 순차적 호출을 적용합니다. 공식 내 스토어 앱 2RPS보다 보수적인 기본값.
NAVER_RATE_LOCK = Lock()
NAVER_NEXT_CALL = {}
NAVER_MIN_INTERVAL_SEC = 1.1
NAVER_MAX_RETRY = 3  # 최초 요청 포함 총 최대 4회 (GW.RATE_LIMIT 한정)


def pace_naver_api(path):
    # 같은 API 엔드포인트는 요청 시작 사이 1.1초 이상 간격을 둡니다.
    with NAVER_RATE_LOCK:
        now = time.monotonic()
        next_at = max(now, NAVER_NEXT_CALL.get(path, 0))
        NAVER_NEXT_CALL[path] = next_at + NAVER_MIN_INTERVAL_SEC
    wait = next_at - now
    if wait > 0:
        time.sleep(wait)


def naver_gateway_code(response):
    try:
        data = response.json()
        return str(data.get('code') or '') if isinstance(data, dict) else ''
    except (ValueError, TypeError):
        return ''


def naver_retry_delay(response, attempt):
    # Retry-After 초 단위 또는 HTTP-date 형식을 반영합니다.
    value = response.headers.get('Retry-After', '').strip()
    if value:
        try:
            wait = float(value)
        except ValueError:
            try:
                wait = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                wait = None
        if wait is not None:
            return max(0.5, wait)
    return (2, 5, 10)[min(attempt, 2)]


def config():
    with NAVER_LOCK:
        values = dict(NAVER_CONFIG)
    if not values.get('client_id') or not values.get('client_secret'):
        raise RuntimeError('스마트스토어 API 설정이 없습니다. 주문 수집 탭에서 앱 ID와 시크릿을 입력하세요.')
    return values


def configure_naver(data):
    app_id = data.get('clientId')
    secret = data.get('clientSecret')
    if not isinstance(app_id,str) or not 5 <= len(app_id.strip()) <= 100:
        raise ValueError('네이버 커머스API 애플리케이션 ID를 입력해 주세요.')
    if not isinstance(secret,str) or not re.fullmatch(r'\$2[aby]\$[0-3][0-9]\$[./A-Za-z0-9]{22}',secret.strip()):
        raise ValueError('애플리케이션 시크릿 형식이 올바르지 않습니다. 네이버 커머스API센터에서 발급받은 BCrypt 시크릿을 입력하세요.')
    # bcrypt salt 문법도 확인합니다. 검증 실패 시 키를 변경하지 않습니다.
    try:
        bcrypt.hashpw(b'naver-format-check', secret.strip().encode('ascii'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('애플리케이션 시크릿(BCrypt salt) 형식이 잘못되었습니다.') from exc
    with NAVER_LOCK:
        NAVER_CONFIG.clear()
        NAVER_CONFIG.update(client_id=app_id.strip(), client_secret=secret.strip())
        token_cache.update(token=None,expires=0)


def token():
    with NAVER_LOCK:
        if token_cache['token'] and token_cache['expires'] > time.time() + 90:
            return token_cache['token']
    c = config()
    timestamp = str(int(time.time() * 1000))
    signature = base64.b64encode(bcrypt.hashpw(
        (c['client_id'] + '_' + timestamp).encode('utf-8'),
        c['client_secret'].encode('utf-8')
    )).decode('ascii')
    # 네이버 커머스API 인증 토큰 표준 스펙:
    # 1) POST Content-Type은 form-url-encoded
    # 2) data 딕셔너리로 요청 Body를 form-url-encoded로 직렬화
    # 3) grant_type은 조건에 관계없이 client_credentials로 고정
    response = session.post(
        API + '/v1/oauth2/token',
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
        },
        data={
            'client_id': c['client_id'],
            'timestamp': timestamp,
            'client_secret_sign': signature,
            'grant_type': 'client_credentials',
            'type': 'SELF',
        },
        timeout=25,
        allow_redirects=False,
    )
    check_response(response, '네이버 인증 토큰 발급')
    d = response.json()
    if not d.get('access_token'):raise RuntimeError('네이버 인증 응답에 access_token이 없습니다.')
    with NAVER_LOCK:
        token_cache['token'] = d['access_token']
        token_cache['expires'] = time.time() + max(60,int(d.get('expires_in',10800)))
    return d['access_token']


def check_response(response, operation):
    if response.status_code >= 300:
        try:
            info = response.json()
            info = info if isinstance(info, dict) else {}
            message = str(info.get('message') or '응답 오류')
            code = str(info.get('code') or '')
        except ValueError:
            message, code = 'API 서버 응답 오류', ''
        # API 헤더의 한도를 확인할 수 있도록 표시 (키, 주문, 개인정보는 포함하지 않음)
        extra = []
        if code: extra.append(code)
        if response.status_code == 429:
            rate_left = response.headers.get('GNCP-GW-RateLimit-Remaining')
            quota_left = response.headers.get('GNCP-GW-Quota-Remaining')
            if rate_left is not None: extra.append('초당 잔여 요청 ' + str(rate_left)[:16])
            if quota_left is not None: extra.append('쿼터 잔여 요청 ' + str(quota_left)[:16])
            if code == 'GW.QUOTA_LIMIT':
                extra.append('할당량 제한: 무조건 반복 조회하지 말고 할당량 갱신 후 시도하세요')
            elif code in ('GW.RATE_LIMIT', ''):
                extra.append('자동 재시도 후에도 제한이 지속됨. 잠시 후 재조회하세요')
        trace_id = response.headers.get('GNCP-GW-Trace-ID')
        if trace_id: extra.append('Trace-ID ' + str(trace_id)[:100])
        suffix = ' [' + ' | '.join(extra) + ']' if extra else ''
        raise RuntimeError(f'{operation}: HTTP {response.status_code} / {message[:150]}{suffix}')


def call(method, path, **kwargs):
    refreshed_token = False
    for attempt in range(NAVER_MAX_RETRY + 1):
        auth = token()
        headers = {'Authorization': 'Bearer ' + auth, 'Accept': 'application/json'}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        pace_naver_api(path)
        response = session.request(method, API + path, headers=headers, timeout=30,
                                   allow_redirects=False, **kwargs)
        if response.status_code == 401 and not refreshed_token:
            with NAVER_LOCK:
                token_cache.update(token=None, expires=0)
            refreshed_token = True
            continue
        if response.status_code == 429 and naver_gateway_code(response) != 'GW.QUOTA_LIMIT':
            if attempt < NAVER_MAX_RETRY:
                delay = naver_retry_delay(response, attempt)
                # 긴 Retry-After가 있으면 해당 대기 시간을 사용자에게 알려주고 종료합니다.
                if delay > 30:
                    raise RuntimeError(f'네이버 주문 조회: HTTP 429 / Retry-After {int(delay)}초 뒤 다시 시도해 주세요.')
                time.sleep(delay)
                continue
        check_response(response, '네이버 주문 조회')
        return response.json()
    raise RuntimeError('네이버 주문 조회 재시도 횟수를 초과했습니다.')


def iso(dt):
    return dt.astimezone(KST).isoformat(timespec='milliseconds')


def fetch_changes(days=7):
    # 24시간 제한을 지키고 날짜별 페이지네이션을 끝까지 처리합니다.
    now = datetime.now(KST)
    start = now - timedelta(days=days)
    ids = set()
    while start < now:
        end = min(start + timedelta(hours=23), now)
        point = iso(start)
        more_sequence = None
        seen_page = set()
        for _ in range(100):
            params = {'lastChangedFrom': point, 'lastChangedTo': iso(end), 'limitCount': 300}
            if more_sequence:
                params['moreSequence'] = more_sequence
            payload = call('GET', '/v1/pay-order/seller/product-orders/last-changed-statuses', params=params)
            d = payload.get('data') or {}
            for item in d.get('lastChangeStatuses', []):
                oid = item.get('productOrderId')
                if oid: ids.add(str(oid))
            more = d.get('more') or {}
            if not more.get('moreFrom') or not more.get('moreSequence'):
                break
            pair = (more['moreFrom'], str(more['moreSequence']))
            if pair in seen_page: raise RuntimeError('네이버 페이지네이션 응답이 반복되었습니다.')
            seen_page.add(pair)
            # 간헐적으로 한 자리 소수점으로 응답할 수 있어 밀리초 3자리로 맞춥니다.
            point = iso(datetime.fromisoformat(more['moreFrom'].replace('Z', '+00:00')))
            more_sequence = str(more['moreSequence'])
            if len(ids) > 10000: raise RuntimeError('한 번에 1만 건을 초과해 조회할 수 없습니다. 조회 기간을 줄이세요.')
        else:
            raise RuntimeError('주문 조회 페이지 제한을 초과했습니다.')
        start = end
    return sorted(ids)


def preview_naver(line):
    # 상품주문번호 단위로 주문을 받습니다. 이름/주소/휴대폰 등 개인정보는 사용하지 않습니다.
    po = line.get('productOrder') or {}
    order = line.get('order') or {}
    if not isinstance(po,dict) or not isinstance(order,dict):return None
    line_id = str(po.get('productOrderId') or '').strip()
    if not line_id:return None
    # 옵션 관리코드가 있으면 반드시 상품 공통코드보다 우선합니다.
    option_code = str(po.get('optionManageCode') or '').strip()
    seller_code = str(po.get('sellerProductCode') or '').strip()
    option = str(po.get('productOption') or '').strip()
    if option_code:
        sku,source=option_code,'옵션관리코드'
    elif option:
        # 옵션별 관리코드가 없으면 일반 공통코드를 직접 매칭하지 않습니다.
        # 미연결 SKU로 수집 후 재고노트 주문목록에서 명시적으로 매핑해야 합니다.
        sku='미연결-'+str(po.get('productId') or seller_code or line_id)+'-'+option
        source='옵션코드없음'
    elif seller_code:
        sku,source=seller_code,'판매자상품코드'
    else:
        sku,source='미연결-'+str(po.get('productId') or line_id),'상품코드없음'
    current=po.get('currentClaim') or line.get('currentClaim') or {}
    if not isinstance(current,dict):current={}
    claim=str(po.get('claimStatus') or current.get('claimStatus') or '').strip().upper()
    status=str(po.get('productOrderStatus') or '').strip().upper()
    if claim in ('CANCEL_DONE','ADMIN_CANCEL_DONE','RETURN_DONE'):
        status=claim
    elif claim in ('RETURN_REQUEST','EXCHANGE_REQUEST','CANCEL_REQUEST','CANCELING','ADMIN_CANCELING'):
        # 취소 요청은 기존 order status를 보존하되, 반품/교환 요청은 검토 표시
        if claim in ('RETURN_REQUEST','EXCHANGE_REQUEST'):
            status=claim
    if not status: status='미확인'
    qty=po.get('quantity') or po.get('initialQuantity')
    try:qty=int(qty)
    except (ValueError,TypeError):qty=0
    remaining=po.get('remainQuantity')
    try:partial=(remaining is not None and int(remaining)!=qty and status not in ('CANCEL_DONE','ADMIN_CANCEL_DONE','RETURN_DONE','CANCELED','RETURNED'))
    except (ValueError,TypeError):partial=True
    return {
        'lineId': line_id[:130],
        'orderNo': str(order.get('orderId') or line_id)[:100],
        'sku':sku[:80], 'skuSource':source,
        'name':str(po.get('productName') or '')[:120],
        'option':option[:100], 'qty':qty,
        'status':status,
        'at':str(order.get('orderDate') or datetime.now(KST).isoformat()),
        'partialClaim':partial,
    }


def get_orders(days=7):
    ids = fetch_changes(days)
    results = []
    for begin in range(0,len(ids),300):
        payload=call('POST','/v1/pay-order/seller/product-orders/query',json={
            'productOrderIds':ids[begin:begin+300],
            'quantityClaimCompatibility':True
        })
        d=payload.get('data')
        # 현행 응답은 data가 배열인 경우가 있으며, 구형 래퍼도 수용합니다.
        if isinstance(d,list): items=d
        elif isinstance(d,dict):items=d.get('productOrders') or d.get('productOrdersInfo') or []
        else:items=[]
        if not isinstance(items,list):raise RuntimeError('네이버 주문 상세 응답 형식이 올바르지 않습니다.')
        if not items and ids[begin:begin+300]:
            raise RuntimeError('상품 주문 상세 조회 결과가 비어 있습니다. API 응답 규격과 권한을 확인해 주세요.')
        for record in items:
            if not isinstance(record,dict):continue
            item=preview_naver(record)
            if item:results.append(item)
    return results


def test_naver_connection():
    token()  # 앱 ID/시크릿의 진위를 API 서버에 확인합니다.
    now=datetime.now(KST)
    result=call('GET','/v1/pay-order/seller/product-orders/last-changed-statuses',
       params={'lastChangedFrom':iso(now-timedelta(hours=1)), 'lastChangedTo':iso(now), 'limitCount':1})
    if not isinstance(result,dict):raise RuntimeError('네이버 주문 API 응답 형식 확인 실패')
    return {'connected':True,'authorized':True,'message':'인증 및 주문 조회 권한 확인 완료'}



# KREAM 공식 연동 명세는 공개적으로 확인되지 않아 직접 엔드포인트 설정 방식으로 지원.
# API 키는 이 서버의 메모리에만 존재하며 파일/브라우저에 저장하지 않음.
KREAM_CONFIG = {}
KREAM_LOCK = Lock()
ALLOWED_KREAM_HOSTS = ('kream.co.kr', 'kreamcorp.com')
KREAM_FIELDS = ('lineId','orderNo','sku','name','option','qty','status','at')

def local_request_allowed(handler):
    host = handler.headers.get('Host', '')
    if host not in (f'127.0.0.1:{PORT}', f'localhost:{PORT}'):
        return False
    origin = handler.headers.get('Origin')
    if origin and origin not in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'):
        return False
    return handler.headers.get('Sec-Fetch-Site') in (None, 'same-origin', 'none')

def check_kream_url(raw):
    from urllib.parse import urlsplit
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme != 'https' or not hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
        raise ValueError('KREAM의 공식 HTTPS API URL을 입력하세요. 사용자 정보·특수 포트·#은 허용되지 않습니다.')
    if not any(hostname == base or hostname.endswith('.'+base) for base in ALLOWED_KREAM_HOSTS):
        raise ValueError('안전을 위해 kream.co.kr 또는 kreamcorp.com 공식 도메인의 HTTPS API 주소만 허용합니다. 다른 공식 호스트가 있다면 연결 규격 확인이 필요합니다.')
    return raw

def json_path(data, path):
    if path in ('', '$'):
        return data
    current = data
    for part in path.split('.'):
        if not part or part.startswith('__') or len(part)>70:
            raise ValueError('JSON 경로 형식이 올바르지 않습니다.')
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdecimal():
            i=int(part);current=current[i] if i<len(current) else None
        else:
            return None
    return current

def kream_orders():
    with KREAM_LOCK:
        c=dict(KREAM_CONFIG)
    if not c.get('key') or not c.get('url'):
        raise ValueError('API 설정을 먼저 저장하세요. API 키 및 KREAM 공식 주문 조회 URL이 필요합니다.')
    auth={'bearer': ('Authorization', 'Bearer '+c['key']),
          'authorization': ('Authorization', c['key']),
          'x-api-key':('X-API-Key',c['key']),
          'api-key':('API-KEY',c['key'])}[c['authMode']]
    headers={'Accept':'application/json',auth[0]:auth[1]}
    # 리다이렉트를 금지해 비밀키가 제3자 호스트로 전송되지 않도록 합니다.
    # 환경 DNS의 사설 호스트 경유 및 프록시 사용도 피합니다.
    s = requests.Session();s.trust_env=False
    try:
        r=s.get(c['url'],headers=headers,timeout=25,allow_redirects=False)
        if 300<=r.status_code<400:raise RuntimeError('API 리다이렉트 응답입니다. 실제 공식 URL을 지정하세요.')
        check_response(r,'KREAM API 주문 조회')
        if len(r.content)>8_000_000:raise RuntimeError('응답이 너무 큽니다. API 기간을 줄여 주세요.')
        payload=r.json()
    finally:
        s.close()
    orders=json_path(payload,c['itemsPath'])
    if not isinstance(orders,list):
        raise ValueError('주문 목록 JSON 경로가 배열을 가리키지 않습니다. 공식 API 문서의 응답 예시와 대조하세요.')
    if len(orders)>10000:raise ValueError('주문 목록이 10,000건을 초과했습니다. API 필터 기간을 줄이세요.')
    converted=[]
    for raw in orders:
        if not isinstance(raw,dict):continue
        row={}
        for field,path in c['fields'].items():
            v=json_path(raw,path) if path else None
            row[field]=v if isinstance(v,(str,int,float)) else ''
        converted.append(row)
    return converted

def sanitize_kream_config(data):
    key = data.get('key')
    url = data.get('url')
    mode = data.get('authMode')
    items = data.get('itemsPath')
    fields = data.get('fields')
    if not isinstance(key,str) or not 4<=len(key.strip())<=4096:
        raise ValueError('유효한 API 키를 입력하세요.')
    if not isinstance(url,str) or len(url)>2000:
        raise ValueError('API URL은 2,000자 이하로 입력하세요.')
    check_kream_url(url.strip())
    if mode not in ('bearer','authorization','x-api-key','api-key'):
        raise ValueError('허용되지 않은 인증 방식입니다.')
    if not isinstance(items,str) or len(items)>100:
        raise ValueError('목록 JSON 경로를 확인하세요.')
    if not isinstance(fields,dict) or set(fields.keys())!=set(KREAM_FIELDS):
        raise ValueError('주문 필드 매핑에는 8개 필드가 필요합니다.')
    if any(not isinstance(v,str) or len(v)>120 for v in fields.values()):
        raise ValueError('주문 필드 경로는 짧은 문자열로 입력해 주세요.')
    return {'key':key.strip(),'url':url.strip(),'authMode':mode,'itemsPath':items,'fields':fields}



# POIZON official seller Open Platform (direct seller app). All secrets RAM-only.
POIZON_CONFIG={}
POIZON_LOCK=Lock()
POIZON_JOB_LOCK=Lock()
POIZON_ENDPOINT='https://open.poizon.com/dop/api/v1/pop/api/v1/order/generic_list'

def configure_poizon(d):
    app_key=d.get('appKey');app_secret=d.get('appSecret');access=d.get('accessToken','')
    if not isinstance(app_key,str) or not 8<=len(app_key.strip())<=128:raise ValueError('POIZON App Key를 확인하세요.')
    if not isinstance(app_secret,str) or not 8<=len(app_secret.strip())<=256:raise ValueError('POIZON App Secret을 확인하세요.')
    if not isinstance(access,str) or len(access)>4096:raise ValueError('Access Token이 너무 깁니다.')
    with POIZON_LOCK:
        POIZON_CONFIG.clear();POIZON_CONFIG.update(app_key=app_key.strip(),app_secret=app_secret.strip(),access_token=access.strip())

def poizon_sign(params,secret):
    # https://open.poizon.com/doc/list/documentationDetail/9
    # Sorted URL-encoded nonempty keys/values + raw appSecret => uppercase MD5.
    filtered={str(k):str(v) for k,v in params.items() if k!='sign' and v is not None and str(v)!=''}
    string_a=urlencode(sorted(filtered.items()),quote_via=quote_plus)
    return hashlib.md5((string_a+secret).encode('utf-8')).hexdigest().upper()

def poizon_call(days=1,page=1,page_size=50):
    with POIZON_LOCK:c=dict(POIZON_CONFIG)
    if not c.get('app_key') or not c.get('app_secret'):raise ValueError('POIZON API 키를 먼저 입력하세요.')
    if not 1<=days<=7 or not 1<=page<=200 or not 1<=page_size<=100:raise ValueError('POIZON 주문 조회 기간 또는 페이지 범위 초과')
    now=datetime.now(timezone(timedelta(hours=8)))
    start=now-timedelta(days=days)
    params={'app_key':c['app_key'],'timestamp':int(time.time()*1000),'language':'en',
            'timeZone':'Asia/Shanghai','start_created':start.strftime('%Y-%m-%d %H:%M:%S'),
            'end_created':now.strftime('%Y-%m-%d %H:%M:%S'),'page_no':page,'page_size':page_size}
    if c.get('access_token'):params['access_token']=c['access_token']
    params['sign']=poizon_sign(params,c['app_secret'])
    # Host fixed to official HTTPS. No redirects to prevent credential leakage.
    s=requests.Session();s.trust_env=False
    try:r=s.post(POIZON_ENDPOINT,json=params,headers={'Accept':'application/json','Content-Type':'application/json'},timeout=30,allow_redirects=False)
    finally:s.close()
    if r.status_code!=200:raise RuntimeError(f'POIZON 주문 API: HTTP {r.status_code} / 계정 권한·서명·호출 허용을 확인하세요.')
    if len(r.content)>8_000_000:raise RuntimeError('POIZON 응답이 너무 큽니다. 조회 기간을 줄여 주세요.')
    data=r.json()
    if not isinstance(data,dict) or str(data.get('code','')) not in ('200','0'):
        raise RuntimeError('POIZON API: '+str(data.get('msg') if isinstance(data,dict) else '응답 형식 오류')[:180])
    body=data.get('data') or {}
    if not isinstance(body,dict) or not isinstance(body.get('orders'),list):raise RuntimeError('POIZON 공식 주문 응답의 data.orders 배열을 확인할 수 없습니다.')
    return body

def poizon_convert(raw):
    if not isinstance(raw,dict):return None
    order_no=str(raw.get('order_no') or '').strip()
    if not order_no:return None
    size=str(raw.get('properties') or ((raw.get('trustProductInfo') or {}).get('size') if isinstance(raw.get('trustProductInfo'),dict) else '') or '').strip()
    color=str((raw.get('trustProductInfo') or {}).get('color') or '').strip() if isinstance(raw.get('trustProductInfo'),dict) else ''
    sku=str(raw.get('article_number') or raw.get('merchantSpuId') or raw.get('merchant_sku_code') or '').strip()
    if not sku:sku='미연결-'+order_no
    variant=' / '.join(filter(None,[size,color]))
    try:qty=int(raw.get('qty') or raw.get('quantity') or 0)
    except (ValueError,TypeError):qty=0
    status_code=int(raw.get('order_status') or 0)
    # Unknown statuses remain unrecognized, not silently treated as paid.
    status={1000:'PAYMENT_WAITING',2000:'PAYED',2100:'DELIVERING',2200:'DELIVERING',
            2500:'DELIVERING',2550:'DELIVERING',2600:'DELIVERING',2650:'DELIVERING',
            2700:'DELIVERING',2800:'DELIVERED',3040:'DELIVERED',4000:'PURCHASE_DECIDED',
            7000:'CANCELED',8000:'CANCELED',8010:'CANCELED',8080:'RETURNED'}.get(status_code,'UNKNOWN_POIZON_'+str(status_code))
    return {'lineId':(order_no+'-'+str(raw.get('sku_id') or sku))[:130], 'orderNo':order_no[:100],
            'sku':sku[:80], 'name':str(raw.get('title') or '')[:120], 'option':variant[:100],
            'qty':qty,'status':status,'at':str(raw.get('create_time') or datetime.now(KST).isoformat())}

def poizon_orders(days=7,first_only=False):
    page=1;out=[];seen=set();size=50
    while page<=200:
        body=poizon_call(days,page,size)
        rows=body.get('orders',[])
        for raw in rows:
            row=poizon_convert(raw)
            if row and row['lineId'] not in seen:
                out.append(row);seen.add(row['lineId'])
        if first_only or len(rows)<size:break
        # total_results: documentation says total query result count (not page count).
        total=body.get('total_results')
        try:total=int(total)
        except (ValueError,TypeError):total=0
        if total and page*size>=total:break
        if len(out)>=10000:raise RuntimeError('POIZON 주문 1만 건 초과. 조회 기간을 줄이세요.')
        page+=1;time.sleep(.75)
    else:raise RuntimeError('POIZON 페이지 수 제한에 도달했습니다.')
    return out

class Handler(BaseHTTPRequestHandler):
    def respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path in ('/', '/app'):
            if not HTML.exists():
                return self.respond(404, {'error': 'HTML 파일이 같은 폴더에 없습니다.'})
            body = HTML.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path.path.startswith('/api/') and not local_request_allowed(self):
            return self.respond(403, {'error': '로컬 재고노트 페이지에서만 접근할 수 있습니다.'})
        if path.path == '/api/update/check':
            try:
                return self.respond(200, stocknote_update.check())
            except (RuntimeError, ValueError, requests.RequestException) as exc:
                return self.respond(503, {'error': '업데이트 확인 실패: ' + str(exc)[:220]})
        if path.path == '/api/poizon/status':
            with POIZON_LOCK:ready=bool(POIZON_CONFIG.get('app_key'))
            return self.respond(200,{'configured':ready})
        if path.path in ('/api/poizon/test','/api/poizon/orders'):
            if not POIZON_JOB_LOCK.acquire(blocking=False):return self.respond(409,{'error':'POIZON 주문 조회가 이미 진행 중입니다.'})
            try:
                days=int((parse_qs(path.query).get('days') or ['7'])[0])
                if days<1 or days>7:raise ValueError('POIZON 조회 기간은 최대 7일입니다.')
                rows=poizon_orders(days,first_only=path.path.endswith('/test'))
                if path.path.endswith('/test'):return self.respond(200,{'connected':True,'count':len(rows)})
                return self.respond(200,{'orders':rows,'count':len(rows)})
            except (ValueError,RuntimeError,requests.RequestException,KeyError,TypeError) as exc:
                return self.respond(400,{'error':str(exc)[:320]})
            finally:POIZON_JOB_LOCK.release()
        if path.path == '/api/kream/status':
            with KREAM_LOCK:
                ready = bool(KREAM_CONFIG.get('key'))
            return self.respond(200, {'configured':ready})
        if path.path in ('/api/kream/test','/api/kream/orders'):
            try:
                orders=kream_orders()
                if path.path.endswith('/test'):
                    return self.respond(200,{'connected':True,'count':len(orders)})
                return self.respond(200,{'orders':orders,'count':len(orders)})
            except (ValueError,RuntimeError,requests.RequestException,KeyError,TypeError) as exc:
                return self.respond(400,{'error':str(exc)[:240]})
        if path.path in ('/api/health','/api/naver/status'):
            with NAVER_LOCK:
                configured=bool(NAVER_CONFIG.get('client_id'))
            return self.respond(200, {'ready':configured, 'configured':configured, 'channel':'스마트스토어'})
        if path.path in ('/api/naver/test', '/api/naver/orders'):
            if not NAVER_JOB_LOCK.acquire(blocking=False):
                return self.respond(409, {'error':'스마트스토어 API 요청이 이미 진행 중입니다. 완료될 때까지 기다려 주세요.'})
            try:
                if path.path == '/api/naver/test':
                    return self.respond(200, test_naver_connection())
                # 같은 호스트에서 열린 페이지 요청만 수용하고, 브라우저 CORS 사용을 허용하지 않습니다.
                if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
                    return self.respond(403, {'error':'다른 출처에서는 접근할 수 없습니다.'})
                days = int((parse_qs(path.query).get('days') or ['7'])[0])
                if days < 1 or days > 30:
                    raise ValueError('조회 일수는 1~30일로 선택하세요.')
                return self.respond(200, {'orders': get_orders(days)})
            except (ValueError, RuntimeError, requests.RequestException, KeyError, TypeError) as exc:
                return self.respond(400, {'error':str(exc)[:600]})
            finally:
                NAVER_JOB_LOCK.release()
        return self.respond(404, {'error': '찾을 수 없는 경로입니다.'})

    def do_POST(self):
        if not local_request_allowed(self):
            return self.respond(403, {'error':'로컬 재고노트 페이지에서만 접근할 수 있습니다.'})
        if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
            return self.respond(415,{'error':'JSON 형식만 허용합니다.'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n < 0 or n > (stocknote_update.MAX_BACKUP if urlparse(self.path).path == '/api/update/backup' else 16000):raise ValueError('요청 데이터가 너무 큽니다.')
            data=json.loads(self.rfile.read(n))
            if not isinstance(data,dict):raise ValueError('JSON 객체가 필요합니다.')
            path=urlparse(self.path).path
            if path=='/api/update/backup':
                return self.respond(200, stocknote_update.backup(data))
            if path=='/api/update/install':
                token=data.get('backupToken')
                tag=data.get('tag')
                if not isinstance(token,str) or not isinstance(tag,str):
                    raise ValueError('업데이트 승인 정보가 올바르지 않습니다.')
                result=stocknote_update.install(tag,token)
                exit_timer=Timer(1.0, lambda: os._exit(0))
                exit_timer.daemon=True
                exit_timer.start()
                return self.respond(200,result)
            if path=='/api/poizon/config':
                if POIZON_JOB_LOCK.locked():return self.respond(409,{'error':'POIZON 주문 조회 중에는 키를 변경할 수 없습니다.'})
                configure_poizon(data);return self.respond(200,{'configured':True})
            if path=='/api/poizon/clear':
                if POIZON_JOB_LOCK.locked():return self.respond(409,{'error':'POIZON 주문 조회 중에는 키를 삭제할 수 없습니다.'})
                with POIZON_LOCK:POIZON_CONFIG.clear()
                return self.respond(200,{'configured':False})
            if path=='/api/naver/config':
                if NAVER_JOB_LOCK.locked():return self.respond(409, {'error':'스마트스토어 조회 중에는 키를 수정할 수 없습니다.'})
                configure_naver(data)
                return self.respond(200,{'configured':True,'note':'로컬 메모리에 입력값 저장, 실제 인증 전'})
            if path=='/api/naver/clear':
                if NAVER_JOB_LOCK.locked():return self.respond(409, {'error':'스마트스토어 조회 중에는 키를 삭제할 수 없습니다.'})
                with NAVER_LOCK:
                    NAVER_CONFIG.clear()
                    token_cache.update(token=None,expires=0)
                return self.respond(200,{'configured':False})
            if path=='/api/kream/config':
                setting=sanitize_kream_config(data)
                with KREAM_LOCK:
                    KREAM_CONFIG.clear();KREAM_CONFIG.update(setting)
                return self.respond(200,{'configured':True,'note':'설정 저장 완료. 인증 확인 전입니다.'})
            if path=='/api/kream/clear':
                with KREAM_LOCK:
                    KREAM_CONFIG.clear()
                return self.respond(200,{'configured':False})
            return self.respond(404,{'error':'찾을 수 없는 경로입니다.'})
        except (ValueError,TypeError,KeyError,RuntimeError, OSError, requests.RequestException, json.JSONDecodeError) as exc:
            return self.respond(400,{'error':str(exc)[:230]})

    def log_message(self, format, *args):
        # 주문 정보, API 키 등 민감한 조회 로그를 화면에 출력하지 않습니다.
        pass


if __name__ == '__main__':
    print('\n재고노트 v7.2 · 로컬 서버')
    print(f'브라우저 주소: http://127.0.0.1:{PORT}/')
    print('이 서버는 이 PC에서만 열립니다. 종료하려면 Ctrl+C를 누르세요.\n')
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError:
        raise SystemExit('포트 8765가 사용 중입니다. 기존 재고노트 서버를 종료해 주세요.')
    webbrowser.open(f'http://127.0.0.1:{PORT}/')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n서버를 종료합니다.')
    finally:
        httpd.server_close()