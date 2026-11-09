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
        """Return links only from a HISTORY TABLE ROW containing this exact title.

        Do not compare whole-page link counts: lazy loading may reveal old links
        between snapshots, and a successful form post may return many rows.
        """
        return await page.evaluate(r"""(label) => {
          const rgx = /https?:\/\/(?:www\.)?yajny\.com\/l\/[A-Za-z0-9]+\/?/ig;
          const rows = [...document.querySelectorAll('tr, [role="row"]')];
          const found = new Set();
          for (const row of rows) {
            // Exact cell contents, not substring across unrelated rows.
            const cells = [...row.querySelectorAll('td, th, [role="cell"]')];
            const matches = cells.some(c => (c.innerText || c.textContent || '').trim() === label);
            if (!matches) continue;
            const values = [row.innerText || '', ...[...row.querySelectorAll('a[href]')].map(a => a.href),
                            ...[...row.querySelectorAll('input')].map(i => i.value || '')];
            for (const value of values) {
              for (const m of (value.match(rgx) || [])) found.add(m.replace(/\/$/, ''));
            }
          }
          return [...found];
        }""", label)

    async def _find_own_link(self, page, label):
        matches = await self._links_for_label(page, label)
        valid = [x for x in matches if SHORT_RE.fullmatch(x)]
        if len(valid) == 1:
            return valid[0]
        if len(valid) > 1:
            raise RuntimeError('Several links associated with the SAME Yajny offer name; manual review needed')
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

    async def create(self, noon_url, label):
        """Use visible website form and verify a NEW generated link in history.

        If site changes or login expires: throw; publishing is blocked.
        """
        async with self.lock:
            page = self.page
            await page.goto(YAJNY_PAGE, wait_until='domcontentloaded', timeout=35000)
            # Search by exact offer title so old rows appearing asynchronously
            # cannot be confused with the result. Reuse an already-created link
            # on retry to avoid duplicate links during deployment failures.
            stable_label = (label[:42] + '-' + hashlib.sha256(noon_url.encode()).hexdigest()[:12])[:65]
            for _ in range(5):
                existing = await self._find_own_link(page, stable_label)
                if existing:
                    LOG.info('Yajny reused existing own link %s', existing)
                    return existing
                await page.wait_for_timeout(500)
            # Identify the two inputs from their distinct purpose, not the shared
            # word "العرض": the URL placeholder also contains that word.
            # Keep the lookup restricted to the visible Share & Earn form.
            inputs = page.locator('input:visible')
            candidates = []
            for i in range(await inputs.count()):
                inp = inputs.nth(i)
                placeholder = ((await inp.get_attribute('placeholder')) or '').strip().lower()
                typ = ((await inp.get_attribute('type')) or 'text').lower()
                candidates.append((inp, placeholder, typ))

            def is_link_placeholder(s):
                return ('رابط' in s and ('متجر' in s or 'المتجر' in s or 'العرض' in s)) or ('link' in s and ('store' in s or 'product' in s))

            def is_name_placeholder(s):
                # Never classify link field as offer name.
                if 'رابط' in s or 'link' in s or 'url' in s:
                    return False
                return ('عنوان العرض' in s or 'اسم العرض' in s or 'اسم' in s or 'offer name' in s or 'offer title' in s)

            link_inputs = [inp for inp, p, typ in candidates if is_link_placeholder(p) and typ not in ('hidden', 'search')]
            name_inputs = [inp for inp, p, typ in candidates if is_name_placeholder(p) and typ not in ('hidden', 'search')]
            if len(link_inputs) != 1 or len(name_inputs) != 1:
                # Diagnostic reports only field metadata; never log cookies,
                # storage state, field VALUES, or any account secret.
                diagnostics = [{'placeholder': p[:100], 'type': typ} for _, p, typ in candidates]
                LOG.warning('Yajny form check: url=%s; link_fields=%s; name_fields=%s; visible_inputs=%s',
                            page.url, len(link_inputs), len(name_inputs), diagnostics)
                if '/user/share-and-earn' not in page.url.lower():
                    raise RuntimeError('Yajny login/session expired or redirected; regenerate YAJNY_STORAGE_STATE_B64')
                raise RuntimeError('Yajny form inputs not identified uniquely; see sanitized Yajny form check log')
            await name_inputs[0].fill(stable_label)
            await link_inputs[0].fill(noon_url)
            btn = page.get_by_role('button', name=re.compile('إنشاء رابط|إنشاء الرابط|Create link', re.I))
            if await btn.count() != 1:
                raise RuntimeError('Yajny create button not identified uniquely')
            # Capture responses triggered by THIS creation click. Unlike the
            # history table, these responses belong to the current operation.
            responses = []
            def on_response(response):
                try:
                    if (response.request.method.upper() in ('POST', 'PUT') and \
                       (urlparse(response.url).hostname or '').lower() in ('yajny.com', 'www.yajny.com')):
                        responses.append(response)
                except Exception:
                    pass
            page.on('response', on_response)
            try:
                await btn.click()
                collected = set()
                inspected = set()
                # Extract one newly generated link from creation response OR
                # a visible success dialog, not unrelated history entries.
                for attempt in range(20):
                    await page.wait_for_timeout(1000)
                    for response in list(responses):
                        if id(response) in inspected:
                            continue
                        inspected.add(id(response))
                        try:
                            if not response.ok:
                                continue
                            request_body = (response.request.post_data or '')
                            from urllib.parse import unquote
                            if (noon_url not in unquote(request_body) and stable_label not in unquote(request_body)):
                                continue
                            ctype = (response.headers.get('content-type') or '').lower()
                            if not any(x in ctype for x in ('json', 'text', 'html')):
                                continue
                            body = (await response.text())[:250000]
                            for match in SHORT_RE.findall(body.replace('\\/', '/')):
                                collected.add(match.rstrip('/'))
                        except Exception:
                            continue
                    # A generated link is generally shown in a success popup.
                    # Never scan the whole page: history results may load late.
                    for link in await self._visible_created_links(page):
                        if SHORT_RE.fullmatch(link):
                            collected.add(link.rstrip('/'))
                    if len(collected) == 1:
                        own = next(iter(collected))
                        LOG.info('Yajny created own link %s (creation response/result)', own)
                        return own
                    if len(collected) > 1:
                        # Exact-title history can disambiguate multiple links.
                        own = await self._find_own_link(page, stable_label)
                        if own and own in collected:
                            LOG.info('Yajny created own link %s (offer-specific result)', own)
                            return own
                        raise RuntimeError('Yajny creation returned multiple candidate links; cannot safely select one')
                    if attempt in (7, 14):
                        own = await self._find_own_link(page, stable_label)
                        if own:
                            LOG.info('Yajny created own link %s (exact offer history)', own)
                            return own
                raise RuntimeError('Yajny create click completed but no unique new link appeared in response or result; inspect Yajny interface')
            finally:
                page.remove_listener('response', on_response)
