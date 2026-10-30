import hashlib
import io
import json
import os
import platform
import tempfile
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
          {'tag_name':'v7.4.0-preview','draft':False,'assets':[]},
          {'tag_name':'v7.5.1-preview','draft':False,'assets':[]},
          {'tag_name':'v7.9.0-preview','draft':True,'assets':[]}]
        with patch.object(u,'github_json',return_value=releases):
            self.assertEqual(u.newest()['tag_name'],'v7.5.1-preview')
    def test_platform_target_and_asset(self):
        with tempfile.TemporaryDirectory() as folder:
            exe=Path(folder)/'StockNote.exe'
            self.assertEqual(u.app_target(system='Windows',executable=str(exe)),exe.resolve())
            self.assertIsNone(u.app_target(system='Windows',executable=str(exe.with_name('Other.exe'))))
            mac=Path(folder)/'StockNote.app'/'Contents'/'MacOS'/'StockNote'
            self.assertEqual(u.app_target(system='Darwin',executable=str(mac)),(Path(folder)/'StockNote.app').resolve())
        self.assertEqual(u.get_asset({'assets':[{'name':'StockNote_Mac.zip'}]},'Darwin')['name'],'StockNote_Mac.zip')
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
