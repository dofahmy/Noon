"""ONE Yajny Share & Earn submission; no Telegram or auto-retries.
Run locally with YAJNY_STORAGE_STATE_B64 or yajny_session_PRIVATE.txt.
"""
import asyncio
import base64
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright

PAGE = 'https://yajny.com/ar-eg/user/share-and-earn'
LABEL = 'AUTO-TEST-001'
DEFAULT_NOON = 'https://www.noon.com/egypt-ar/ZC5129F17DA323067DE4BZ/p/?o=dcabe5df14aaa55b'
SHORT = re.compile(r'https?://(?:www\.)?yajny\.com/l/[A-Za-z0-9]+/?', re.I)


def get_state():
    value = os.getenv('YAJNY_STORAGE_STATE_B64', '').strip()
    if not value:
        state_file = Path('yajny_session_PRIVATE.txt')
        if state_file.exists():
            value = state_file.read_text(encoding='utf-8').strip()
    if not value:
        raise RuntimeError('Missing Yajny login state. Run bootstrap_yajny.py first, or set YAJNY_STORAGE_STATE_B64.')
    return json.loads(base64.b64decode(value).decode('utf-8'))


async def row_matches(page):
    return await page.evaluate('''(label) => {
      const rows = [...document.querySelectorAll('tr, [role="row"]')];
      const rx = /https?:\\/\\/(?:www\\.)?yajny\\.com\\/l\\/[A-Za-z0-9]+\\/?/g;
      let links=[];
      for (const row of rows) {
        if (!(row.innerText || '').includes(label)) continue;
        const pieces = [row.innerText || '', ...[...row.querySelectorAll('a[href]')].map(x => x.href),
                        ...[...row.querySelectorAll('input')].map(x => x.value || '')];
        for (const part of pieces) links.push(...(part.match(rx) || []));
      }
      return [...new Set(links)];
    }''', LABEL)


async def main():
    url = os.getenv('TEST_NOON_URL', DEFAULT_NOON).strip()
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('noon.com', 'www.noon.com'):
        raise ValueError('TEST_NOON_URL must be an HTTPS noon.com link')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=os.getenv('HEADLESS_TEST', '0') == '1', args=['--no-sandbox'])
        try:
            context = await browser.new_context(storage_state=get_state(), locale='ar-EG')
            page = await context.new_page()
            await page.goto(PAGE, wait_until='domcontentloaded', timeout=35000)
            print('PAGE_PATH:', urlparse(page.url).path)
            if '/user/share-and-earn' not in page.url:
                print('RESULT: LOGIN_REQUIRED — session may have expired')
                return
            before = await row_matches(page)
            if before:
                print('RESULT: EXISTING_TEST_NAME — found existing AUTO-TEST-001; will NOT submit again')
                print('MATCHED_LINK_COUNT:', len(before))
                return
            name_field = page.get_by_placeholder('ادخل عنوان العرض', exact=True)
            link_field = page.get_by_placeholder('ادخل رابط العرض الخاص بالمتجر', exact=True)
            create_btn = page.get_by_role('button', name=re.compile('إنشاء رابط|إنشاء الرابط|Create link', re.I))
            if any([await name_field.count() != 1, await link_field.count() != 1, await create_btn.count() != 1]):
                print('RESULT: FORM_NOT_FOUND — verify form labels; no request submitted')
                return
            await name_field.fill(LABEL)
            await link_field.fill(url)
            if await name_field.input_value() != LABEL or await link_field.input_value() != url:
                print('RESULT: INPUT_MISMATCH — no request submitted')
                return
            print('FILLED: name=TRUE, noon_url=TRUE (values not printed)')
            requests = []
            def on_response(resp):
                try:
                    u = urlparse(resp.url)
                    if u.hostname in ('yajny.com', 'www.yajny.com') and '/createLink' in u.path and resp.request.method == 'POST':
                        requests.append(resp)
                except Exception:
                    pass
            page.on('response', on_response)
            await create_btn.click()
            print('CLICKED_CREATE: once')
            # Only one click; no POST retries. Allow results to populate.
            await page.wait_for_timeout(6000)
            for response in requests:
                redirect = response.headers.get('location', '')
                print('CREATE_POST_STATUS:', response.status)
                print('CREATE_REDIRECT_PATH:', urlparse(urljoin(response.url, redirect)).path if redirect else '<none>')
            after = await row_matches(page)
            if not after:
                await page.reload(wait_until='domcontentloaded')
                await page.wait_for_timeout(2500)
                after = await row_matches(page)
            print('TEST_NAME_FOUND_IN_HISTORY:', bool(after))
            print('HISTORY_MATCH_COUNT:', len(after))
            if len(after) == 1 and SHORT.fullmatch(after[0]):
                print('RESULT: VERIFIED_CREATED_OR_LISTED')
                print('OWN_YAJNY_URL:', after[0])
            elif len(after) > 1:
                print('RESULT: AMBIGUOUS — multiple links matched test name')
            else:
                print('RESULT: NOT_VERIFIED — check Share & Earn history manually for AUTO-TEST-001')
            if os.getenv('HEADLESS_TEST', '0') != '1':
                await asyncio.to_thread(input, 'Inspect the browser if useful, then press Enter to close: ')
        finally:
            await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
