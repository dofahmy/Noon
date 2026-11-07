"""Run on local desktop ONLY. Logs in via visible browser, exports encrypted-by-transport Base64 state.
NEVER share printed session or commit it into repository.
"""
import asyncio
import base64
import json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(locale='ar-EG')
        page = await context.new_page()
        await page.goto('https://yajny.com/ar-eg/user/share-and-earn')
        print('Log into your own Yajny account in the browser. Open Share & Earn and ensure your previous links are shown.')
        await asyncio.to_thread(input, 'When finished, return here and press Enter: ')
        if 'yajny.com' not in page.url:
            raise RuntimeError('Not on Yajny website')
        encoded = base64.b64encode(json.dumps(await context.storage_state()).encode('utf-8')).decode('ascii')
        with open('yajny_session_PRIVATE.txt', 'w', encoding='utf8') as f:
            f.write(encoded)
        print('Private browser session saved to yajny_session_PRIVATE.txt. Set its contents as Railway variable YAJNY_STORAGE_STATE_B64.')
        print('Never send the file or its contents to anyone. Do not upload it to GitHub.')
        await browser.close()

asyncio.run(main())
