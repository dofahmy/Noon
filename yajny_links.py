"""Resolve competitors' Noon URLs, then create OWN Yajny share-and-earn links.

This is UI automation, not an official API. Fail closed if ambiguous.
"""
import asyncio
import base64
import json
import hashlib
import logging
import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, urljoin

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
    """Read Yajny HTTP redirect headers without requesting the Noon landing page.

    Noon may block server-side requests with HTTP 403 even though the redirect
    destination itself is valid. A Yajny 302 Location is sufficient here.
    Reject redirects outside Yajny/Noon and avoid following an external URL.
    """
    first = urlparse(url)
    if (first.hostname or '').lower() not in ('yajny.com', 'www.yajny.com'):
        raise ValueError('Expected a Yajny URL')
    current = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=22,
                                  headers={'User-Agent': 'Mozilla/5.0'}) as client:
        for _ in range(8):
            response = await client.get(current)
            if response.status_code not in (301, 302, 303, 307, 308):
                response.raise_for_status()
                raise ValueError('Yajny did not provide a usable HTTP redirect')
            location = response.headers.get('location')
            if not location:
                raise ValueError('Yajny redirect was missing Location header')
            target = urljoin(current, location)
            parsed = urlparse(target)
            if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password:
                raise ValueError('Unsafe redirect target')
            host = (parsed.hostname or '').lower()
            if host == 'noon.com' or host.endswith('.noon.com'):
                cleaned = clean_noon_url(target)
                LOG.info('Resolved Yajny redirect to Noon without requesting Noon page')
                return cleaned
            if host not in ('yajny.com', 'www.yajny.com'):
                raise ValueError('Unexpected intermediate redirect host: ' + host)
            current = target
    raise ValueError('Too many Yajny redirects')


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

    async def _links_for_label(self, page, label):
        """Same history-row lookup used in the successful one-link test.

        Exact offer *identifier* within row, with uniqueness enforced on output.
        A row may contain both title and URL in nonstandard cells/inputs.
        """
        return await page.evaluate(r"""(label) => {
          const rows = [...document.querySelectorAll('tr, [role="row"]')];
          const rx = /https?:\/\/(?:www\.)?yajny\.com\/l\/[A-Za-z0-9]+\/?/g;
          const found = new Set();
          for (const row of rows) {
            if (!(row.innerText || '').includes(label)) continue;
            const pieces = [row.innerText || '',
              ...[...row.querySelectorAll('a[href]')].map(x => x.href),
              ...[...row.querySelectorAll('input')].map(x => x.value || '')];
            for (const part of pieces)
              for (const hit of (part.match(rx) || [])) found.add(hit.replace(/\/$/, ''));
          }
          return [...found];
        }""", label)

    async def _find_own_link(self, page, label):
        matches = [x for x in await self._links_for_label(page, label)
                   if SHORT_RE.fullmatch(x)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError('Several links for the same offer label; refusing ambiguous link')
        return None

    async def _visible_created_links(self, page):
        """Inspect only visible result/dialog surfaces, not the old links table."""
        return await page.evaluate(r"""() => {
          const re = /https?:\/\/(?:www\.)?yajny\.com\/l\/[A-Za-z0-9]+\/?/ig;
          const boxes = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [role="alert"], .modal.show, .swal2-popup, .toast, .notification')]
            .filter(e => e.getClientRects().length > 0);
          const found = new Set();
          for (const box of boxes) {
            for (const text of [box.innerText || '', ...[...box.querySelectorAll('a[href]')].map(a => a.href), ...[...box.querySelectorAll('input')].map(i => i.value || '')]) {
              for (const m of text.match(re) || []) found.add(m.replace(/\/$/, ''));
            }
          }
          return [...found];
        }""")

    async def _form_diagnostics(self, page):
        """Sanitized UI diagnostics; no session tokens, customer data or field values."""
        return await page.evaluate(r"""() => {
          const visible = e => !!e.getClientRects().length;
          const selectors = '[role="alert"], [role="status"], .invalid-feedback, .field-error, .error-message, .alert, .toast, .notification, .swal2-html-container';
          const notices = [...document.querySelectorAll(selectors)]
            .filter(visible).map(e => (e.innerText || '').trim().slice(0,180))
            .filter(Boolean).slice(0,8);
          const inputs = [...document.querySelectorAll('input')].filter(visible)
            .map(e => ({type: e.type, placeholder: (e.placeholder||'').slice(0,70), invalid: e.getAttribute('aria-invalid') || ''}))
            .filter(x => x.placeholder || x.invalid).slice(0,8);
          return {path: location.pathname, notices, inputs};
        }""")

    async def create(self, noon_url, label):
        """Create exactly once using the form sequence proven by AUTO-TEST-001.

        The redirect itself is not proof of success. Verify by offer-specific
        link in own history. Never choose a link belonging to another row.
        """
        async with self.lock:
            page = self.page
            stable_label = (label[:42] + '-' + hashlib.sha256(noon_url.encode()).hexdigest()[:12])[:65]
            await page.goto(YAJNY_PAGE, wait_until='domcontentloaded', timeout=35000)
            if '/user/share-and-earn' not in urlparse(page.url).path:
                raise RuntimeError('Yajny session may have expired; cannot access Share & Earn')

            # Reuse an identical verified offer if an earlier attempt submitted
            # successfully but failed to return to the caller (e.g. redeploy).
            for _ in range(3):
                existing = await self._find_own_link(page, stable_label)
                if existing:
                    LOG.info('Yajny reused existing own link %s', existing)
                    return existing
                await page.wait_for_timeout(500)

            # Precisely the selectors and verification from the successful test.
            name_field = page.get_by_placeholder('ادخل عنوان العرض', exact=True)
            link_field = page.get_by_placeholder('ادخل رابط العرض الخاص بالمتجر', exact=True)
            btn = page.get_by_role('button', name=re.compile('إنشاء رابط|إنشاء الرابط|Create link', re.I))
            # Await each Playwright locator count explicitly: using await inside
            # a generator expression creates an async_generator, which cannot
            # be passed to synchronous any().
            name_count = await name_field.count()
            link_count = await link_field.count()
            button_count = await btn.count()
            if name_count != 1 or link_count != 1 or button_count != 1:
                LOG.warning('Yajny form check: %s', await self._form_diagnostics(page))
                raise RuntimeError('Yajny form selectors not unique; no request submitted')
            await name_field.fill(stable_label)
            await link_field.fill(noon_url)
            if await name_field.input_value() != stable_label or await link_field.input_value() != noon_url:
                raise RuntimeError('Yajny input values did not match; no request submitted')
            LOG.info('Yajny create form filled: exact field labels; product_ref=%s',
                     hashlib.sha256(noon_url.encode()).hexdigest()[:12])

            # Capture only creation POST metadata. No cookie, form body or token logging.
            responses = []
            def on_response(resp):
                try:
                    u = urlparse(resp.url)
                    if (u.hostname in ('yajny.com', 'www.yajny.com')
                            and '/createLink' in u.path and resp.request.method.upper() == 'POST'):
                        responses.append(resp)
                except Exception:
                    pass
            page.on('response', on_response)
            try:
                await btn.click()   # exactly one submission; no automatic re-click
                LOG.info('Yajny CREATE_CLICKED once; label_ref=%s', stable_label)
                await page.wait_for_timeout(6000)
                for resp in responses:
                    loc = resp.headers.get('location', '')
                    LOG.info('Yajny CREATE_POST_STATUS=%s redirect_path=%s',
                             resp.status,
                             urlparse(urljoin(resp.url, loc)).path if loc else '<none>')
                # As in the successful test, use the row's name, not global links.
                own = await self._find_own_link(page, stable_label)
                if not own:
                    await page.reload(wait_until='domcontentloaded', timeout=35000)
                    await page.wait_for_timeout(2500)
                    own = await self._find_own_link(page, stable_label)
                if own:
                    LOG.info('Yajny created own link %s (confirmed in own history)', own)
                    return own
                # Some accounts load the history asynchronously after redirect.
                # A few reads are safe; another POST is not.
                for _ in range(3):
                    await page.wait_for_timeout(1500)
                    own = await self._find_own_link(page, stable_label)
                    if own:
                        LOG.info('Yajny created own link %s (confirmed after history refresh)', own)
                        return own
                LOG.warning('Yajny CREATE_NOT_VERIFIED: post_count=%s; %s',
                            len(responses), await self._form_diagnostics(page))
                raise RuntimeError('Yajny link not verified after one create click; do NOT publish. Check own Yajny history for label ' + stable_label)
            finally:
                page.remove_listener('response', on_response)
