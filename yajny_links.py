"""Resolve competitors' Noon URLs, then create OWN Yajny share-and-earn links.

This is UI automation, not an official API. Fail closed if ambiguous.
"""
import asyncio
import base64
import json
import logging
import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx

LOG = logging.getLogger('noon_forwarder.yajny')
YAJNY_PAGE = 'https://yajny.com/ar-eg/user/share-and-earn'
SHORT_RE = re.compile(r'https?://(?:www\.)?yajny\.com/l/[A-Za-z0-9]+/?', re.I)
URL_RE = re.compile(r'https?://[^\s<>\]\[)"\']+', re.I)
# Discard known referral-only query params. Keep the Noon seller offer selector 'o'.
REFERRAL_KEYS = {'shareid', 'pcl', 'fbclid', 'gclid', 'ref', 'referral', 'affiliate_id', 'aff_id', 'click_id'}


def clean_noon_url(url):
    p = urlparse(url.strip())
    host = (p.hostname or '').lower()
    if not (host == 'noon.com' or host.endswith('.noon.com')):
        raise ValueError('The resolved destination is not noon.com')
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in REFERRAL_KEYS and not k.lower().startswith('utm_')]
    # Preserve original product path and seller offer id; do not guess identifiers.
    return urlunparse(('https', p.netloc, p.path, '', urlencode(kept), ''))


async def resolve_noon_url(url):
    """Follow HTTP redirects, reject any non-Noon final destination."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=22,
                                  headers={'User-Agent': 'Mozilla/5.0 (compatible; NoonDealsBot/1.0)'}) as client:
        response = await client.get(url)
        response.raise_for_status()
        last = str(response.url)
        if (urlparse(last).hostname or '').endswith('noon.com'):
            return clean_noon_url(last)
        # Some shorteners use an HTML or JS redirect; no untrusted extraction.
        raise ValueError('Yajny did not HTTP-redirect to a verified Noon product: ' + last[:140])


def urls_in_text(text):
    return [m.group(0).rstrip('.,،؛!') for m in URL_RE.finditer(text or '')]


def replace_urls(text, mapping):
    # One pass to prevent replacing a URL inside a replacement URL.
    return URL_RE.sub(lambda m: mapping.get(m.group(0).rstrip('.,،؛!'), m.group(0).rstrip('.,،؛!'))
                      + m.group(0)[len(m.group(0).rstrip('.,،؛!')):], text)


class YajnyBrowser:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()

    async def start(self):
        from playwright.async_api import async_playwright
        encoded = os.getenv('YAJNY_STORAGE_STATE_B64', '').strip()
        if not encoded:
            raise RuntimeError('YAJNY_STORAGE_STATE_B64 missing; complete login bootstrap first')
        state = json.loads(base64.b64decode(encoded).decode('utf-8'))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, args=['--no-sandbox'])
        self.context = await self.browser.new_context(storage_state=state, locale='ar-EG')
        self.page = await self.context.new_page()
        await self.page.goto(YAJNY_PAGE, wait_until='domcontentloaded', timeout=35000)
        LOG.info('Yajny browser loaded. URL=%s', self.page.url)

    async def close(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def _history_links(self, page):
        values = await page.evaluate("""() => {
          const links = [...document.querySelectorAll('a[href]')].map(x => x.href);
          const fields = [...document.querySelectorAll('input')].map(x => x.value);
          return [...links, ...fields, document.body.innerText];
        }""")
        found = set()
        for val in values:
            found.update(SHORT_RE.findall(val or ''))
        return {x.rstrip('/') for x in found}

    async def create(self, noon_url, label):
        """Use visible website form and verify a NEW generated link in history.

        If site changes or login expires: throw; publishing is blocked.
        """
        async with self.lock:
            page = self.page
            await page.goto(YAJNY_PAGE, wait_until='domcontentloaded', timeout=35000)
            # History links before the submission. Check changes, never use stale first result.
            before = await self._history_links(page)
            inputs = page.locator('input:visible')
            found = []
            for i in range(await inputs.count()):
                inp = inputs.nth(i)
                place = ((await inp.get_attribute('placeholder')) or '').lower()
                found.append((inp, place))
            link_inputs = [i for i, placeholder in found if ('رابط' in placeholder and 'المتجر' in placeholder) or ('store' in placeholder and 'link' in placeholder)]
            name_inputs = [i for i, placeholder in found if 'اسم' in placeholder or 'العرض' in placeholder or 'name' in placeholder]
            if len(link_inputs) != 1 or len(name_inputs) != 1:
                raise RuntimeError('Yajny form inputs not identified uniquely. Page layout/login may have changed')
            await name_inputs[0].fill(label[:65])
            await link_inputs[0].fill(noon_url)
            btn = page.get_by_role('button', name=re.compile('إنشاء رابط|إنشاء الرابط|Create link', re.I))
            if await btn.count() != 1:
                raise RuntimeError('Yajny create button not identified uniquely')
            await btn.click()
            # Poll the history rather than assuming the first link belongs to this request.
            for _ in range(20):
                await page.wait_for_timeout(1000)
                await page.reload(wait_until='domcontentloaded') if _ == 7 else None
                after = await self._history_links(page)
                new = after - before
                new = [x for x in new if SHORT_RE.fullmatch(x)]
                if len(new) == 1:
                    LOG.info('Yajny created own link %s', new[0])
                    return new[0]
                if len(new) > 1:
                    raise RuntimeError('Several new Yajny links detected; cannot safely match result')
            raise RuntimeError('No verified new Yajny link appeared in history; login or UI needs attention')
