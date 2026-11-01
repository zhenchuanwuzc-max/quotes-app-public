#!/usr/bin/env python3
"""Drive the real browser against a disposable quotes server and keep evidence."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'tests'))
from _support import ServerHandle, http, load_example
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    evidence = args.evidence.expanduser().resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    scratch = tempfile.mkdtemp(prefix='quotes-pstack-')
    server = None
    try:
        server = ServerHandle(scratch)
        server.start()
        assert server.proc.poll() is None
        assert ('data:    ' + server.data_file) in server.log_text()
        assert not (Path(server.data_dir) / 'sync.sh').exists()
        status, _, health = http('GET', server.base, '/health')
        assert status == 200 and json.loads(health)['total'] == len(load_example()['quotes'])
        records = ['doctor: isolated process, data path, no sync script and health verified']
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={'width': 1280, 'height': 860})
            blocked = []
            def filter_request(route):
                url = urlparse(route.request.url)
                if (url.scheme == 'http' and url.hostname == '127.0.0.1'
                        and url.port == server.port and url.path not in
                        ('/wallpaper', '/sync-now', '/install-update', '/check-update')):
                    route.continue_()
                else:
                    blocked.append(url.path)
                    route.abort()
            context.route('**/*', filter_request)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(server.base + '/', wait_until='domcontentloaded')
            page.wait_for_function('n => document.querySelectorAll("#list .row").length === n',
                                   arg=len(load_example()['quotes']))
            page.screenshot(path=str(evidence / '01-list.png'), full_page=True)
            page.locator('#q').fill('Drucker')
            page.wait_for_function('() => document.querySelectorAll("#list .row").length === 1')
            assert 'measured' in page.locator('#list .row').first.text_content()
            page.screenshot(path=str(evidence / '02-search.png'), full_page=True)
            records.append('browse-search: example list, Drucker filter and matching row observed')
            page.locator('#q').fill('')
            page.locator('#btnNew').click()
            editor = page.locator('#cedit .vditor-ir .vditor-reset')
            editor.wait_for(state='visible')
            editor.click()
            page.keyboard.type('Pstack disposable verification quote')
            page.locator('#cSrc').fill('Pstack sample')
            page.screenshot(path=str(evidence / '03-draft.png'), full_page=True)
            page.locator('#cSave').click()
            page.locator('#compose').wait_for(state='hidden')
            row = page.locator('#list .row', has_text='Pstack disposable verification quote')
            row.wait_for(state='visible')
            page.screenshot(path=str(evidence / '04-saved.png'), full_page=True)
            saved = [q for q in server.read_file()['quotes']
                     if q['text'] == 'Pstack disposable verification quote']
            assert len(saved) == 1 and saved[0]['source'] == 'Pstack sample'
            assert not errors, errors
            records.append('create: browser draft and saved list visible; isolated quotes.json contains text and source')
            records.append('blocked requests: ' + (', '.join(blocked) if blocked else 'none'))
            context.close()
            browser.close()
        (evidence / 'result.txt').write_text('\n'.join(records) + '\n', encoding='utf-8')
        print('\n'.join(records))
    finally:
        if server:
            server.stop()
        shutil.rmtree(scratch)
    assert (evidence / 'result.txt').is_file()
    assert (evidence / '04-saved.png').is_file()
    print('cleanup: disposable server stopped and scratch removed; evidence retained at ' + str(evidence))


if __name__ == '__main__':
    main()
