"""StockNote local launcher. Each installation serves its own HTML and local browser data."""
import sys, webbrowser, json, time
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from threading import Timer

BASE=Path(getattr(sys, '_MEIPASS',Path(__file__).resolve().parent))
FILE=BASE/'재고노트_v7_범용재고관리.html'
PORT=8765
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route=urlparse(self.path).path
        if route in ('/','/index.html'):
            try: content=FILE.read_bytes()
            except OSError: return self.send_error(500,'Missing application HTML')
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif route.startswith('/api/'):
            body=json.dumps({'error':'이 배포판의 스마트스토어/KREAM 자동 API 연결은 아직 지원되지 않습니다. 주문 CSV 업로드를 이용하세요.'},ensure_ascii=False).encode()
            self.send_response(501)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:self.send_error(404)
    def do_POST(self):
        self.send_response(501);self.send_header('Content-Length','0');self.end_headers()
    def log_message(self,*args):pass
if __name__=='__main__':
    try: server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    except OSError as e: raise SystemExit('Port 8765 in use. Close the existing StockNote app first.') from e
    Timer(1,lambda:webbrowser.open('http://127.0.0.1:8765/')).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
