#!/usr/bin/env python3
"""Verify editing journeys on disposable data and retain screenshots."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'tests'))
from test_browser import BrowserUsageTest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    evidence = args.evidence.expanduser().resolve()
    evidence.mkdir(parents=True, exist_ok=False)

    class EditingProof(BrowserUsageTest):
        def tearDown(self):
            try:
                self.page.screenshot(path=str(evidence / (self._testMethodName + '.png')), full_page=True)
                if 'shortcut_menu' in self._testMethodName:
                    if self.page.locator('#compose').is_visible():
                        self.page.locator('#cCancel').click()
                    button = self.page.locator('#btnHelpA') if self.page.locator('#index').is_visible() else self.page.locator('#btnHelpB')
                    if self.page.locator('#help').is_hidden():
                        button.click()
                    self.page.get_by_label('新增快捷键', exact=True).wait_for()
                    self.page.screenshot(path=str(evidence / (self._testMethodName + '-menu.png')), full_page=True)
                    self.page.locator('#help .hcard').evaluate('(el) => el.scrollTop = el.scrollHeight')
                    self.page.screenshot(path=str(evidence / (self._testMethodName + '-menu-bottom.png')), full_page=True)
            finally:
                super().tearDown()

    names = [
        'test_last_saved_metadata_defaults_survive_edit_cancel_and_restart',
        'test_failed_save_does_not_replace_defaults_and_empty_save_clears_them',
        'test_initial_load_blocks_new_until_metadata_defaults_are_ready',
        'test_bold_toolbar_handles_punctuation_without_added_spaces',
        'test_cjk_bold_punctuation_can_touch_following_text',
        'test_add_edit_save_shortcuts_respect_typing_fields',
        'test_shortcuts_preserve_button_activation_and_ime_search',
        'test_shortcut_menu_change_cancel_restore_and_restart',
        'test_shortcut_menu_conflict_failed_save_and_escape',
    ]
    suite = unittest.TestSuite(EditingProof(name) for name in names)
    with (evidence / 'tests.txt').open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print((evidence / 'tests.txt').read_text(encoding='utf-8'))
    print('Evidence retained at ' + str(evidence))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
