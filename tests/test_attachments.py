"""Image persistence, safe serving, quote editing, and retry behavior on isolated data."""
import base64
import concurrent.futures
import hashlib
from http.client import HTTPConnection
import json
import os
import urllib.request
import unittest

from _support import ServerTest, http

# Complete, valid one-pixel PNG; never includes personal data.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jY5kAAAAASUVORK5CYII=')
DATA_URL = 'data:image/png;base64,' + base64.b64encode(PNG).decode('ascii')
PATH = 'attachments/' + hashlib.sha256(PNG).hexdigest() + '.png'


class AttachmentTest(ServerTest):
    def post(self, path, body):
        status, _, text = http('POST', self.srv.base, path, body)
        return status, json.loads(text)

    def upload(self):
        status, body = self.post('/attachments', {'data_url': DATA_URL, 'name': '视频截图.png'})
        self.assertEqual(status, 200, body)
        return body['attachment']

    def test_uploaded_image_is_content_addressed_served_and_survives_restart(self):
        attachment = self.upload()
        self.assertEqual(attachment, {'path': PATH, 'mime': 'image/png', 'alt': '视频截图.png'})
        self.assertEqual(self.upload()['path'], PATH)
        self.srv.restart()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.srv.base + '/' + PATH) as response:
            self.assertEqual(response.headers.get_content_type(), 'image/png')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response.read(), PNG)
        self.assertEqual(os.listdir(os.path.join(self.srv.data_dir, 'attachments')), [PATH.split('/')[1]])

    def test_jpeg_and_webp_uploads_have_matching_content_types(self):
        fixtures = [
            ('image/jpeg', 'jpg', '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDiqKKKZ4B//9k='),
            ('image/webp', 'webp', 'UklGRjAAAABXRUJQVlA4ICQAAABQAQCdASoCAAIAAUAmJaAABDOAAP7zaTf/+QY/3Zf6ScRgAAA=')]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for mime, ext, encoded in fixtures:
            status, body = self.post('/attachments', {'data_url': 'data:' + mime + ';base64,' + encoded})
            self.assertEqual(status, 200, body)
            self.assertEqual(body['attachment']['mime'], mime)
            self.assertTrue(body['attachment']['path'].endswith('.' + ext))
            with opener.open(self.srv.base + '/' + body['attachment']['path']) as response:
                self.assertEqual(response.headers.get_content_type(), mime)
                self.assertEqual(response.read(), base64.b64decode(encoded))

    def test_single_image_over_limit_does_not_write_an_attachment(self):
        encoded = base64.b64encode(b'x' * (4 * 1024 * 1024 + 1)).decode()
        status, body = self.post('/attachments', {'data_url': 'data:image/png;base64,' + encoded})
        self.assertEqual(status, 400, body)
        self.assertFalse(os.path.exists(os.path.join(self.srv.data_dir, 'attachments')))

    def test_image_quote_edit_preserves_and_explicitly_removes_attachments(self):
        attachment = self.upload()
        status, body = self.post('/quotes/add', {'text': '', 'attachments': [attachment],
            'source_url': 'https://www.youtube.com/watch?v=abc&t=23s', 'capture_id': 'yt:abc:23:sample'})
        self.assertEqual(status, 200, body)
        quote = body['quote']
        status, _, raw = http('PATCH', self.srv.base, '/quotes/' + quote['id'], {'text': '补充原文'})
        self.assertEqual(status, 200, raw)
        got = json.loads(raw)['quote']
        self.assertEqual(got['attachments'], [attachment])
        self.assertEqual(got['source_url'], 'https://www.youtube.com/watch?v=abc&t=23s')
        self.assertEqual(got['capture_id'], 'yt:abc:23:sample')
        status, _, raw = http('PATCH', self.srv.base, '/quotes/' + quote['id'],
                              {'text': '正文保留', 'attachments': [], 'source_url': ''})
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)['quote']['attachments'], [])
        self.assertEqual(json.loads(raw)['quote']['source_url'], '')
        self.assertTrue(os.path.exists(os.path.join(self.srv.data_dir, PATH)), 'removing a reference must not race with cross-device sync')

    def test_concurrent_capture_retries_make_one_persistent_quote(self):
        payload = {'text': 'synthetic video note', 'capture_id': 'yt:abc:42:retry'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.post('/quotes/add', payload), range(5)))
        self.assertEqual([s for s, _ in results], [200] * 5)
        self.assertEqual(len({body['quote']['id'] for _, body in results}), 1)
        self.assertEqual(sum(body['deduplicated'] for _, body in results), 4)
        self.srv.restart()
        status, body = self.post('/quotes/add', payload)
        self.assertEqual(status, 200)
        self.assertTrue(body['deduplicated'])
        self.assertEqual(len(self.list_all()), self.srv.seed_count + 1)

    def test_unsafe_uploads_rejected_without_writing_files(self):
        for data in ['data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=',
                     'data:image/png;base64,not!base64',
                     'data:image/jpeg;base64,' + base64.b64encode(PNG).decode(),
                     'data:image/png;base64,' + base64.b64encode(b'not an image').decode()]:
            status, body = self.post('/attachments', {'data_url': data})
            self.assertEqual(status, 400, body)
        status, _ = self.post('/attachments', ['unexpected array'])
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(os.path.join(self.srv.data_dir, 'attachments')))

    def test_unknown_or_unsafe_reference_rejected_without_quote_write(self):
        attachment = {'path': PATH, 'mime': 'image/png', 'alt': 'not uploaded'}
        for patch in [{'attachments': [attachment]}, {'source_url': 'javascript:alert(1)'},
                      {'attachments': [{'path': '../quotes.json', 'mime': 'image/png'}]},
                      {'capture_id': ['bad']}]:
            status, body = self.post('/quotes/add', dict(text='example', **patch))
            self.assertEqual(status, 400, body)
        attachment = self.upload()
        status, body = self.post('/quotes/add', {'text': 'example', 'attachments': [attachment] * 5})
        self.assertEqual(status, 400, body)
        self.assertEqual(len(self.list_all()), self.srv.seed_count)

    def test_symlinked_attachment_file_and_directory_are_never_followed(self):
        image_dir = os.path.join(self.srv.data_dir, 'attachments')
        os.mkdir(image_dir)
        outside = os.path.join(self.tmp, 'outside.png')
        with open(outside, 'wb') as f:
            f.write(PNG)
        os.symlink(outside, os.path.join(self.srv.data_dir, PATH))
        status, _, _ = http('GET', self.srv.base, '/' + PATH)
        self.assertEqual(status, 404)
        status, _ = self.post('/attachments', {'data_url': DATA_URL})
        self.assertEqual(status, 400)
        os.unlink(os.path.join(self.srv.data_dir, PATH))
        os.rmdir(image_dir)
        os.symlink(self.tmp, image_dir)
        status, _ = self.post('/attachments', {'data_url': DATA_URL})
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, PATH.split('/')[1])))

    def test_request_size_limit_refuses_before_reading_body(self):
        conn = HTTPConnection('127.0.0.1', self.srv.port, timeout=3)
        try:
            conn.request('POST', '/attachments', body=b'', headers={'Content-Length': str(25 * 1024 * 1024)})
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        finally:
            conn.close()

    def test_capture_id_cannot_be_changed_by_edit(self):
        _, body = self.post('/quotes/add', {'text': 'first', 'capture_id': 'immutable'})
        qid = body['quote']['id']
        status, _, raw = http('PATCH', self.srv.base, '/quotes/' + qid,
                              {'text': 'second', 'capture_id': 'different'})
        self.assertEqual(status, 400, raw)
        self.assertEqual(self.find(qid)['text'], 'first')


if __name__ == '__main__':
    unittest.main()
