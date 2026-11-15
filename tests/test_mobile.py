import importlib
import json
import sys
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

import stocknote_mobile as mobile


class MobileViewTests(unittest.TestCase):
    def setUp(self):
        desktop = importlib.import_module('재고노트_통합API_로컬서버')
        self.share = mobile.MobileShare(Path(__file__).resolve().parents[1])
        self.server = desktop.ThreadingHTTPServer(('127.0.0.1', 0), desktop.Handler)
        self.addCleanup(self.server.server_close)
        Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.share.stop)
        self.enterContext(patch.object(desktop, 'PORT', self.server.server_port))
        self.enterContext(patch.object(desktop, 'MOBILE_VIEW', self.share))
        # Use loopback for repeatable HTTP tests on runners without private IPv4 interfaces.
        self.enterContext(patch.object(mobile, 'desktop_addresses', return_value=['127.0.0.1']))
        self.enterContext(patch.object(mobile.InventoryServer, 'verify_request', return_value=True))

    def request(self, port, path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', port, timeout=4)
        try:
            headers = dict(headers or {})
            if body is not None:
                headers['Content-Type'] = 'application/json'
            connection.request('GET' if body is None else 'POST', path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            payload = response.read()
            if response.headers.get_content_type() == 'application/json':
                payload = json.loads(payload)
            return response.status, payload, response.headers
        finally:
            connection.close()

    def desktop(self, route, body=None, headers=None):
        return self.request(self.server.server_port, '/api/mobile/' + route, body, headers)[:2]

    def test_phone_queries_only_its_read_only_inventory_and_updates(self):
        self.assertEqual(self.desktop('status'), (200, {'enabled': False}))
        saved = ['NK-001', '운동화', '270', 5, 2, '켤레']
        rows = [saved] + [[f'SKU{i}', '양말', '기본', 1, 1, '개'] for i in range(30)]
        code, connection = self.desktop('start', {'revision': 100, 'rows': rows})
        self.assertEqual(code, 200)
        link = urlsplit(connection['url'])
        key = connection['key']
        token = {'Authorization': 'Bearer ' + link.fragment}
        self.assertNotEqual(key, link.fragment)
        self.assertTrue(any(any(row) for row in connection['qr']))
        self.assertEqual(self.desktop('status'), (200, {'enabled': True}))
        self.assertEqual(self.desktop('status', headers={'X-StockNote-Mobile-Key': key})[1]['url'], connection['url'])

        def phone(path, body=None, headers=None):
            return self.request(link.port, path, body, headers)

        self.assertIn(b'/mobile.js', phone('/')[1])
        self.assertEqual(phone('/mobile.js')[0], 200)
        self.assertEqual(phone('/api/inventory')[0], 401)
        self.assertEqual(phone('/api/inventory', headers={**token, 'Origin': 'https://elsewhere.invalid'})[0], 403)
        self.assertEqual(phone('/api/inventory', headers={**token, 'Host': 'elsewhere.invalid'})[0], 403)
        self.assertEqual(phone('/api/mobile/status', headers=token)[0], 404)
        self.assertEqual(phone('/api/app/quit', {}, token)[0], 405)
        self.assertEqual(phone('/api/naver/orders', headers=token)[0], 404)
        self.assertEqual(phone('/api/inventory?page=2', headers=token)[1]['total'], 31)
        self.assertEqual(len(phone('/api/inventory?page=2', headers=token)[1]['rows']), 1)
        code, listed, headers = phone('/api/inventory?q=nk-001', headers=token)
        self.assertEqual((code, listed['rows']), (200, [saved]))
        self.assertEqual(headers['Cache-Control'], 'no-store')

        updated = ['NK-001', '운동화', '270', 4, 1, '켤레']
        self.assertEqual(self.desktop('snapshot', {'key': link.fragment, 'revision': 110, 'rows': [updated]})[0], 409)
        self.assertEqual(self.desktop('snapshot', {'key': key, 'revision': 110, 'rows': [updated]}), (200, {'ok': True}))
        self.desktop('snapshot', {'key': key, 'revision': 109, 'rows': rows})
        self.assertEqual(phone('/api/inventory?q=270', headers=token)[1]['rows'], [updated])

        self.assertEqual(self.desktop('stop', {}), (200, {'enabled': False}))
        with self.assertRaises(OSError):
            phone('/api/inventory', headers=token)
        new_link = urlsplit(self.desktop('start', {'revision': 112, 'rows': [updated]})[1]['url'])
        self.assertEqual(self.request(new_link.port, '/api/inventory', headers=token)[0], 401)

    def test_desktop_endpoints_require_local_origin_and_valid_snapshot(self):
        self.assertEqual(self.desktop('start', {'revision': 100, 'rows': []}, {'Origin': 'https://elsewhere.invalid'})[0], 403)
        self.assertEqual(self.desktop('start', {'revision': 100, 'rows': [['SKU', '상품', '', 1, 2, '개']]})[0], 400)
        self.assertEqual(self.desktop('status'), (200, {'enabled': False}))
        self.assertTrue(mobile.private_address('192.168.1.30'))
        self.assertFalse(mobile.private_address('1.2.3.4'))


@unittest.skipUnless(sys.platform == 'win32', 'Requires a Windows host with a private IPv4 adapter')
class MobileNetworkTests(unittest.TestCase):
    def test_private_adapter_accepts_the_mobile_connection(self):
        if not mobile.desktop_addresses():
            self.skipTest('Windows host has no private IPv4 address')
        share = mobile.MobileShare(Path(__file__).resolve().parents[1])
        self.addCleanup(share.stop)
        row = ['SKU-100', '운동화', '260', 6, 4, '켤레']
        link = urlsplit(share.start({'revision': 100, 'rows': [row]})['url'])
        self.assertTrue(mobile.private_address(link.hostname))
        connection = HTTPConnection(link.hostname, link.port, timeout=5)
        try:
            connection.request('GET', '/api/inventory?q=sku-100', headers={'Authorization': 'Bearer ' + link.fragment})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)['rows'], [row])
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
