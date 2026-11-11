"""本机快捷键设置：持久化、校验及不修改金句数据。"""
import json
import os

from _support import ServerTest, http


class ShortcutSettingsTest(ServerTest):
    def settings(self):
        status, _, body = http('GET', self.srv.base, '/settings/shortcuts')
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def put(self, bindings):
        status, _, body = http('PUT', self.srv.base, '/settings/shortcuts', {'bindings': bindings})
        return status, json.loads(body)

    def test_shortcuts_persist_without_touching_quotes(self):
        original = self.srv.read_file()
        data = self.settings()
        self.assertTrue(data['ok'])
        self.assertEqual(data['bindings']['add'], 'a')
        self.assertEqual(data['bindings']['save'], 'mod+s')
        bindings = dict(data['bindings'], add='b', save='mod+shift+s')
        status, saved = self.put(bindings)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved['bindings'], bindings)
        self.srv.restart()
        self.assertEqual(self.settings()['bindings'], bindings)
        self.assertEqual(self.srv.read_file(), original)
        self.assertFalse(os.listdir(self.srv.backup_dir))

    def test_invalid_and_conflicting_bindings_do_not_replace_settings(self):
        data = self.settings()
        original = data['bindings']
        invalid = [dict(original, add='e'), dict(original, edit='n'),
                   dict(original, add='Escape'), dict(original, save='mod+q'),
                   dict(original, add=None), {'add': 'b'}, dict(original, unknown='z')]
        for bindings in invalid:
            with self.subTest(bindings=bindings):
                status, response = self.put(bindings)
                self.assertEqual(status, 400, response)
                self.assertEqual(self.settings()['bindings'], original)
        status, response = self.put(dict(original, add='B'))
        self.assertEqual(status, 200, response)
        self.assertEqual(response['bindings']['add'], 'b')
