#!/usr/bin/env python3
"""Run heading journeys on disposable data and retain screenshots and test output."""
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

    class HeadingProof(BrowserUsageTest):
        def tearDown(self):
            try:
                self.page.evaluate('() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {})))')
                self.page.screenshot(path=str(evidence / (self._testMethodName + '.png')), full_page=True)
            finally:
                super().tearDown()

    names = [
        'test_plain_paragraphs_are_not_promoted_to_a_title',
        'test_markdown_heading_levels_survive_edit_save_and_reload',
        'test_heading_sizes_are_distinct_from_body_in_editor_menu_and_display',
        'test_heading_syntax_matches_editor_and_does_not_activate_html',
        'test_long_body_stays_readable_after_window_resize',
        'test_choose_heading_level_in_toolbar_and_change_it',
        'test_wallpaper_keeps_complete_body_and_heading_hierarchy',
        'test_task_status_and_wallpaper_preserve_all_visible_content',
    ]
    suite = unittest.TestSuite(HeadingProof(name) for name in names)
    with (evidence / 'tests.txt').open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print((evidence / 'tests.txt').read_text(encoding='utf-8'))
    print('Evidence retained at ' + str(evidence))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
