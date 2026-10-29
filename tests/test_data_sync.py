"""Run the real sync/release scripts against throwaway repos and fake service boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from _support import ROOT, TimeLimitedTest


class DataSyncTest(TimeLimitedTest):
    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True, env=self.env).strip()

    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp) / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_NOSYSTEM='1', LANG='en_US.UTF-8', LC_ALL='en_US.UTF-8',
                        QUOTES_SYNC_STATUS=str(Path(self.tmp) / 'sync.status'))

    def init_repo(self, root):
        root.mkdir()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.name', 'Synthetic test')
        self.git(root, 'config', 'user.email', 'test@example.invalid')

    def test_sync_commits_new_image_files_before_any_network(self):
        root = Path(self.tmp) / 'data'
        self.init_repo(root)
        (root / 'quotes.json').write_text(json.dumps({'quotes': [], 'updated': 'synthetic'}))
        shutil.copy(Path(ROOT) / 'sync.sh', root / 'sync.sh')
        images = root / 'attachments'
        images.mkdir()
        image = images / ('a' * 64 + '.png')
        image.write_bytes(b'synthetic attachment')
        (images / '.upload-in-progress').write_bytes(b'partial bytes')
        result = subprocess.run(['/bin/bash', str(root / 'sync.sh')], env=self.env,
                                capture_output=True, text=True, errors="replace", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        tracked = self.git(root, 'ls-files').splitlines()
        self.assertIn('attachments/' + image.name, tracked)
        self.assertIn('quotes.json', tracked)
        self.assertNotIn('attachments/.upload-in-progress', tracked)
        self.assertEqual((Path(self.tmp) / 'sync.status').read_text().split(':')[0], 'ok')

    def release_fixture(self, succeeds):
        root = Path(self.tmp) / 'code'
        self.init_repo(root)
        (root / 'scripts').mkdir()
        shutil.copy(Path(ROOT) / 'scripts/release.sh', root / 'scripts/release.sh')
        (root / 'tests').mkdir()
        (root / 'tests/test_success.py').write_text('import unittest\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        (root / 'tests/live_check.py').write_text('raise SystemExit(%d)\n' % (0 if succeeds else 1))
        (root / 'json-merge.py').write_text('# old merge\n')
        (root / 'sync.sh').write_text('# old sync\n')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'previous')
        previous = self.git(root, 'rev-parse', 'HEAD')
        (root / '.git/quotes-release-ok').write_text(previous)
        (root / 'json-merge.py').write_text('# new merge\n')
        (root / 'sync.sh').write_text('# new sync\n')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'new release')
        data = Path(self.tmp) / 'data'
        data.mkdir()
        (data / 'json-merge.py').write_text('# previous installed merge\n')
        (data / 'sync.sh').write_text('# previous installed sync\n')
        binaries = Path(self.tmp) / 'bin'
        binaries.mkdir()
        launch_state = Path(self.tmp) / 'service-pid'
        launch_state.write_text('100')
        (binaries / 'launchctl').write_text('#!/bin/bash\nif [ "$1" = "kickstart" ]; then n=$(cat "$FAKE_PID_FILE"); echo $((n+1)) > "$FAKE_PID_FILE"; else echo "pid = $(cat "$FAKE_PID_FILE")"; fi\n')
        (binaries / 'curl').write_text('#!/bin/bash\nif [[ "$*" == *"http_code"* ]]; then printf 200; else printf \'{"ok":true}\'; fi\n')
        (binaries / 'osascript').write_text('#!/bin/bash\nexit 0\n')
        for executable in binaries.iterdir():
            executable.chmod(0o700)
        env = dict(self.env, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                   QUOTES_DATA_DIR=str(data), FAKE_PID_FILE=str(launch_state))
        result = subprocess.run(['/bin/bash', str(root / 'scripts/release.sh')], env=env,
                                capture_output=True, text=True, errors="replace", timeout=15)
        return root, data, previous, result

    def test_release_installs_both_merge_and_sync_code(self):
        root, data, previous, result = self.release_fixture(True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((data / 'json-merge.py').read_text(), '# new merge\n')
        self.assertEqual((data / 'sync.sh').read_text(), '# new sync\n')
        self.assertNotEqual(self.git(root, 'rev-parse', 'HEAD'), previous)

    def test_failed_release_restores_both_data_scripts_and_code_commit(self):
        root, data, previous, result = self.release_fixture(False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(root, 'rev-parse', 'HEAD'), previous)
        self.assertEqual((data / 'json-merge.py').read_text(), '# previous installed merge\n')
        self.assertEqual((data / 'sync.sh').read_text(), '# previous installed sync\n')
