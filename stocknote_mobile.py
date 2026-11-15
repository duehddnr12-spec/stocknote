"""An opt-in, read-only inventory view on the desktop's private network."""
import ipaddress
import json
import secrets
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from unicodedata import normalize
from urllib.parse import parse_qs, urlsplit

import qrcode


MAX_SNAPSHOT_BYTES = 85 * 1024 * 1024
PAGE_SIZE = 30
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def private_address(address):
    ip = ipaddress.IPv4Address(address)
    return any(ip in network for network in PRIVATE_NETWORKS)


def desktop_addresses():
    addresses = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # A UDP connect selects the default interface without sending a datagram.
            probe.connect(('192.0.2.1', 9))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        records = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
        addresses.extend(record[4][0] for record in records)
    except OSError:
        pass
    return [address for address in dict.fromkeys(addresses) if private_address(address)]


def search_text(value):
    return normalize('NFKC', value).casefold()


def parse_snapshot(data):
    revision, rows = data.get('revision'), data.get('rows')
    if type(revision) not in (int, float) or not 0 < revision < 10**14:
        raise ValueError('재고 저장 시점이 올바르지 않습니다.')
    if not isinstance(rows, list) or len(rows) > 100000:
        raise ValueError('휴대폰 조회용 재고 목록이 올바르지 않습니다.')
    inventory = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError('재고 항목 형식이 올바르지 않습니다.')
        sku, name, option, stock, available, unit = row
        if (not all(isinstance(value, str) and len(value) <= limit for value, limit in
                    ((sku, 40), (name, 90), (option, 90), (unit, 12)))
                or not sku.strip() or not name.strip() or not unit.strip()
                or type(stock) is not int or type(available) is not int
                or not 0 <= available <= stock <= 999999999):
            raise ValueError('품번, 상품명 또는 재고 수량을 확인하세요.')
        inventory.append((search_text(f'{sku} {name} {option}'), tuple(row)))
    return revision, inventory, datetime.now(timezone.utc).isoformat(timespec='seconds')


def matches(supplied, expected):
    return isinstance(supplied, str) and supplied.isascii() and secrets.compare_digest(supplied, expected)


class InventoryHandler(BaseHTTPRequestHandler):
    timeout = 8
    server_version = 'StockNote'
    sys_version = ''

    def send_content(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def respond(self, status, data):
        self.send_content(status, 'application/json; charset=utf-8', json.dumps(data, separators=(',', ':')).encode('utf-8'))

    def do_GET(self):
        server = self.server
        if self.headers.get('Host') != server.host:
            return self.respond(403, {'error': 'PC에 표시된 연결 주소를 열어 주세요.'})
        try:
            request = urlsplit(self.path)
        except ValueError:
            return self.respond(400, {'error': '주소가 올바르지 않습니다.'})
        asset = server.assets.get(request.path)
        if asset:
            return self.send_content(200, *asset)
        if request.path != '/api/inventory':
            return self.respond(404, {'error': '페이지를 찾을 수 없습니다.'})
        if (self.headers.get('Origin') not in (None, server.origin)
                or self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none')):
            return self.respond(403, {'error': '연결 주소에서 재고 조회를 열어 주세요.'})
        if not server.active or not matches(self.headers.get('Authorization'), f'Bearer {server.access_key}'):
            return self.respond(401, {'error': 'PC에서 새 연결 QR을 스캔해 주세요.'})
        try:
            params = parse_qs(request.query, max_num_fields=2)
            query = search_text(params.get('q', [''])[0][:120].strip())
            page = max(1, int(params.get('page', ['1'])[0]))
        except ValueError:
            return self.respond(400, {'error': '검색 조건을 확인해 주세요.'})
        _, inventory, updated_at = server.snapshot
        matching = [entry for entry in inventory if query in entry[0]] if query else inventory
        total = len(matching)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        start = (page - 1) * PAGE_SIZE
        rows = [row for _, row in matching[start:start + PAGE_SIZE]]
        return self.respond(200, {'rows': rows, 'total': total, 'page': page, 'pages': pages, 'updatedAt': updated_at})

    def do_POST(self):
        self.respond(405, {'error': '휴대폰에서는 재고를 조회할 수 있어요.'})

    def log_message(self, *_):
        pass


class InventoryServer(ThreadingHTTPServer):
    allow_reuse_address = False
    request_queue_size = 12

    def __init__(self, address, assets, snapshot):
        super().__init__((address, 0), InventoryHandler)
        self.host = f'{address}:{self.server_port}'
        self.origin = f'http://{self.host}'
        self.assets = assets
        self.snapshot = snapshot
        self.active = True
        self.access_key = secrets.token_urlsafe(24)
        self.control_key = secrets.token_urlsafe(24)
        self.link = f'{self.origin}/#{self.access_key}'

    def verify_request(self, _request, peer):
        return self.active and private_address(peer[0])

    def handle_error(self, _request, _peer):
        # A phone may disconnect during a request. Keep inventory and tokens out of logs.
        pass


class MobileShare:
    def __init__(self, asset_dir):
        self.asset_dir = asset_dir
        self.lock = Lock()
        self.server = None

    def status(self, key):
        with self.lock:
            if not self.server:
                return {'enabled': False}
            if not matches(key, self.server.control_key):
                return {'enabled': True}
            return {'enabled': True, 'url': self.server.link, 'qr': self.server.qr}

    def start(self, data):
        snapshot = parse_snapshot(data)
        with self.lock:
            if self.server:
                raise ValueError('휴대폰 조회가 이미 켜져 있어요. 연결을 끈 다음 다시 켜 주세요.')
            assets = {
                '/': ('text/html; charset=utf-8', (self.asset_dir / 'stocknote_mobile.html').read_bytes()),
                '/mobile.js': ('text/javascript; charset=utf-8', (self.asset_dir / 'stocknote_mobile.js').read_bytes()),
            }
            for address in desktop_addresses():
                try:
                    server = InventoryServer(address, assets, snapshot)
                except OSError:
                    continue
                try:
                    qr = qrcode.QRCode(box_size=1, border=4)
                    qr.add_data(server.link)
                    server.qr = qr.get_matrix()
                    Thread(target=server.serve_forever, daemon=True).start()
                except Exception:
                    server.server_close()
                    raise
                self.server = server
                return {'enabled': True, 'key': server.control_key, 'url': server.link, 'qr': server.qr}
        raise ValueError('PC의 사설 네트워크 주소를 열 수 없어요. Wi-Fi나 유선 네트워크 연결을 확인해 주세요.')

    def publish(self, data):
        key = data.get('key')
        with self.lock:
            server = self.server
            if not server or not matches(key, server.control_key):
                return False
        snapshot = parse_snapshot(data)
        with self.lock:
            if self.server is not server:
                return False
            if snapshot[0] >= server.snapshot[0]:
                server.snapshot = snapshot
            return True

    def stop(self):
        with self.lock:
            server = self.server
            if not server:
                return
            server.active = False
            self.server = None
        server.shutdown()
        server.server_close()
