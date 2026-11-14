"""Noon deals: mirror source Telegram posts and replace coupon sections.

Python 3.10+, Telethon 1.x. Single running instance only.
"""
import asyncio
import hashlib
import copy
import difflib
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode, urlunparse
import httpx
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.extensions import html
from telethon.tl.types import MessageEntityCode, MessageEntityBold, MessageEntityTextUrl
from yajny_links import YajnyBrowser, SHORT_RE, urls_in_text, replace_urls, resolve_noon_url, clean_noon_url

LOG = logging.getLogger('noon_forwarder')
BUILD_VERSION = 'v20-SOURCE-COUPON-FIREWALL-20261114'
SOURCES = [x.strip().lstrip('@') for x in os.getenv('TELEGRAM_SOURCES', 'Hi_pro1,tawfiry').split(',') if x.strip()]
DESTINATION = os.getenv('TELEGRAM_DESTINATION', 'NoonDealsEgy').strip().lstrip('@')
POLL_SECONDS = max(10, int(os.getenv('POLL_SECONDS', '30')))
BACKFILL_LIMIT = 0  # SAFETY: never replay historical posts on restart, regardless of Railway env
CHECK_RECENT = max(30, int(os.getenv('CHECK_RECENT', '100')))
DATA_DIR = Path(os.getenv('DATA_DIR', '/data' if os.getenv('RAILWAY_ENVIRONMENT') else './data'))
DB_PATH = DATA_DIR / 'noon_mirror.sqlite3'
CODES = ['HZ26', 'HZ27', 'HZ28', 'HZ29', 'HZ30']
COUPON_TEXT = ('👑ماتنساش وانت بتطلب تستخدم واحد من اكوادنا عشان تستفيد بأكبر خصم ممكن ، '
               'دوس على الكوبون هيتاخد كوبى بشكل تلقائى 🛍')
FOOTER_TEXT = '✔️ اضغط على الكود هيتنسخ بشكل تلقائي'
AMAZON_FOOTER_SEPARATOR = '————————————-'
AMAZON_FOOTER_PREFIX = '📱 قناتنا لعروض أمازون ('
AMAZON_FOOTER_LINK_LABEL = 'اضغط هنا للانضمام'
AMAZON_FOOTER_URL = 'https://t.me/EgyptOffersHunter'
AMAZON_FOOTER = AMAZON_FOOTER_PREFIX + AMAZON_FOOTER_LINK_LABEL + ')'
AMAZON_TAG = os.getenv('AMAZON_AFFILIATE_TAG', 'fromnoony-21').strip()


# We keep the whole offer description, including mixed Amazon/Noon descriptions.
# Only source advertising, Gulf coupon blocks and replaced Egyptian coupon blocks go.
AMAZON_SHORT = re.compile(r'https?://(?:link\.amazon|amzn\.to|a\.co)/[A-Za-z0-9/_-]+', re.I)
AMAZON_FULL = re.compile(r'https?://(?:www\.)?amazon\.[a-z.]+/[^\s<>\]\[)"\']+', re.I)
NOON_DIRECT = re.compile(r'https?://(?:www\.)?noon\.com/[^\s<>\]\[)"\']+', re.I)
YAJNY_SHORT = re.compile(r'https?://(?:www\.)?yajny\.com/l/[A-Za-z0-9]+', re.I)
PRODUCT_URLS = re.compile('|'.join('(?:'+x.pattern+')' for x in (AMAZON_SHORT,AMAZON_FULL,NOON_DIRECT,YAJNY_SHORT)), re.I)
ASIN_RE = re.compile(r'^[A-Z0-9]{10}$', re.I)
# Restrict promotion removal to explicit source calls-to-action, not product titles.
PROMO_RE = re.compile(r'(?:شارك\s+القناة\s+مع\s+أصدقائك|رابط\s+جروب\s+الفيسبوك|قناة\s+الواتساب\s*[:：]|قناة\s+التليجرام\s*[:：]|t\.me/la2et3ard|cutt\.ly/iyW3SKrg|cutt\.ly/IyUlMZeU)', re.I)
SEPARATOR_RE = re.compile(r'^[\sـ—–\-_*]{6,}$')
NOON_COUPON_HEADER = re.compile(r'^\s*(?:كوبونات\s+نون|أكواد\s+نون|اكواد\s+نون|أكواد\s+الخصم|اكواد\s+الخصم|كود\s+الخصم)\s*[:：]?\s*$',re.I)
GULF_HEADER = re.compile(r'^\s*(?:ولنون\s+الخليج|نون\s+الخليج|كوبونات\s+نون\s+الخليج)\s*[:：]?\s*$',re.I)
CODE_ONLY = re.compile(r'^[\s`*]*(?:[A-Z]{2,8}\d{1,5}|\d{1,5}[A-Z]{2,8})[\s`*]*$',re.I)
OWN_COUPON_HEADER = re.compile(r'^\s*👑?\s*ماتنساش\s+وانت\s+بتطلب', re.I)
OWN_FOOTER_HEADER = re.compile(r'^\s*✔️?\s*اضغط\s+على\s+الكود', re.I)


def extract_product_urls(text):
    """Recognize link.amazon when glued to Arabic or English prose."""
    found = []
    for match in PRODUCT_URLS.finditer(text or ''):
        url = match.group(0).rstrip('.,،؛!')
        if url not in found:
            found.append(url)
    return found


# Source coupon codes are UNTRUSTED. Never carry a code across from a source
# channel, even if it happens to equal one of our own codes. Our block is
# generated anew after sanitization; source entities are rebuilt accordingly.
COUPON_CUE = re.compile(
    r'(?:ك[وُ]?د(?:\s+ال)?\s*(?:خصم|تخفيض|توفير|نون|امازون|أمازون|العرض)?'
    r'|اكواد|أكواد|كوبون(?:ات)?|قسيم(?:ة|ات)|رمز\s*(?:الخصم|الترويجي)?'
    r'|promo\s*code|discount\s*code|coupon(?:\s*code)?|voucher\s*code'
    r'|استخدم\s+(?:الكود|كود|الرمز)|بالكود|بكود|الكود\s*[:：]|code\s*[:：])', re.I)
# Standalone discount-code forms, including source codes unrelated to AD31/AD32.
STANDALONE_SOURCE_CODE = re.compile(
    r'^[\s`*#✅☑️🔣🎟️🎫💥🔥🎁-]*'
    r'(?=[A-Za-z0-9_-]{3,24}[\s`*#✅☑️🔣🎟️🎫💥🔥🎁-]*$)'
    r'(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{3,24}'
    r'[\s`*#✅☑️🔣🎟️🎫💥🔥🎁-]*$', re.I)
# Outside an explicit coupon block, only discard standalone alphanumeric
# codes with digits, so we don't remove product-brand headings like Samsung.
UNCONTEXTED_CODE = re.compile(
    r'^[\s`*#✅☑️🔣🎟️🎫💥🔥🎁-]*(?=[A-Za-z0-9_-]{3,24}[\s`*#✅☑️🔣🎟️🎫💥🔥🎁-]*$)'
    r'(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])'
    r'[A-Za-z0-9_-]{3,24}[\s`*#✅☑️🔣🎟️🎫💥🔥🎁-]*$', re.I)
SOURCE_COUPON_INSTRUCTION = re.compile(
    r'(?:اضغط\s+على\s+الكود|هيتنسخ|ينسخ\s+تلقائ|دوس\s+على\s+الكوبون|'
    r'ماتنساش\s+وانت\s+بتطلب|استعمل\s+كود|استخدم\s+كود|'
    r'استخدم\s+واحد\s+من\s+(?:الاكواد|الأكواد|اكواد|أكواد|الكوبونات))', re.I)


def _merchant_urls_on_line(line):
    """Keep product links even if a competing coupon appeared on the same line."""
    return extract_product_urls(line)


def remove_source_promos(text, has_noon):
    """Fail-closed source-coupon firewall for new posts AND edited posts.

    Remove all source coupon blocks, standalone coupon codes, and inline coupon
    instructions. Only our own codes can be inserted by format_post(). Works
    identically on Noon-only, Amazon-only and mixed posts.
    """
    lines = (text or '').splitlines()
    result = []
    mode = None
    for line in lines:
        stripped = line.strip()
        if PROMO_RE.search(line):
            mode = 'promo'
            continue
        if (GULF_HEADER.match(stripped) or NOON_COUPON_HEADER.match(stripped)
                or OWN_COUPON_HEADER.match(stripped) or OWN_FOOTER_HEADER.match(stripped)
                or COUPON_CUE.search(stripped) or SOURCE_COUPON_INSTRUCTION.search(stripped)):
            # Even a coupon line containing a product link must not leak the code.
            # Preserve its recognized merchant links so the product is still usable.
            result.extend(_merchant_urls_on_line(line))
            mode = 'coupon'
            continue
        if mode:
            if not stripped or SEPARATOR_RE.match(stripped):
                continue
            if (CODE_ONLY.fullmatch(stripped) or STANDALONE_SOURCE_CODE.fullmatch(stripped)
                    or COUPON_CUE.search(stripped) or SOURCE_COUPON_INSTRUCTION.search(stripped)):
                result.extend(_merchant_urls_on_line(line))
                continue
            if mode == 'promo' and (stripped.startswith('https://') or stripped.startswith('http://')):
                continue
            mode = None
        # Independently remove every source standalone code, not just AD31/AD32.
        # A source may use arbitrary prefixes/suffixes (e.g. ABC10, WINTERSALE).
        if CODE_ONLY.fullmatch(stripped) or UNCONTEXTED_CODE.fullmatch(stripped):
            continue
        if SEPARATOR_RE.match(stripped):
            continue
        result.append(line)
    output = '\n'.join(result).strip()
    # Fail closed if any source coupon instruction survived sanitization.
    # The only code instructions allowed in final posts are generated by us.
    if COUPON_CUE.search(output) or SOURCE_COUPON_INSTRUCTION.search(output):
        raise ValueError('Source coupon reference survived sanitization; refusing publication')
    return output


def format_post(text, mapping, has_noon):
    """Keep the original offer prose, change URLs, add our Noon coupon (if relevant)."""
    cleaned = remove_source_promos(text, has_noon)
    for old, new in sorted(mapping.items(), key=lambda pair: -len(pair[0])):
        cleaned = cleaned.replace(old, new)
    # Check no competitor short links are left in the published text.
    if re.search(r'https?://(?:link\.amazon|amzn\.to|a\.co|yajny\.com/l/)', cleaned, re.I):
        for url in extract_product_urls(cleaned):
            if AMAZON_SHORT.fullmatch(url):
                raise ValueError('Competitor Amazon shortlink still in composed post')
    if has_noon:
        cleaned += '\n\n' + COUPON_TEXT + '\n\n' + '\n'.join(CODES) + '\n\n' + FOOTER_TEXT
    cleaned += '\n' + AMAZON_FOOTER_SEPARATOR + '\n\n' + AMAZON_FOOTER
    return cleaned


def utf16offset(text, idx):
    return len(text[:idx].encode('utf-16-le')) // 2


def build_mixed_entities(message, output, has_noon):
    """Preserve formatting on unchanged spans of source, even when removing middle lines."""
    source = message.message or ''
    ents = []
    # Match only equal pieces; never attach formatting to a different word.
    matches = difflib.SequenceMatcher(None, source, output, autojunk=False).get_matching_blocks()
    for ent in message.entities or []:
        if isinstance(ent, MessageEntityTextUrl):
            # Hidden links could contain competitor tracking: refuse upstream.
            continue
        try:
            start_utf16 = ent.offset
            end_utf16 = ent.offset + ent.length
            start = next(i for i in range(len(source)+1) if utf16offset(source, i) == start_utf16)
            end = next(i for i in range(start, len(source)+1) if utf16offset(source, i) == end_utf16)
        except StopIteration:
            continue
        for m in matches:
            if m.a <= start and end <= m.a + m.size:
                changed = copy.copy(ent)
                pos = m.b + (start - m.a)
                changed.offset = utf16offset(output, pos)
                changed.length = utf16offset(output, pos + end - start) - changed.offset
                ents.append(changed)
                break
    for target in ((COUPON_TEXT, FOOTER_TEXT) if has_noon else ()):
        i = output.rfind(target)
        if i != -1:
            ents.append(MessageEntityBold(offset=utf16offset(output,i),length=utf16offset(output,i+len(target))-utf16offset(output,i)))
    i=output.rfind(AMAZON_FOOTER_LINK_LABEL)
    if i != -1:
        ents.append(MessageEntityTextUrl(offset=utf16offset(output,i),length=utf16offset(output,i+len(AMAZON_FOOTER_LINK_LABEL))-utf16offset(output,i),url=AMAZON_FOOTER_URL))
    if has_noon:
        for code in CODES:
            m=re.search(r'^'+re.escape(code)+r'$',output,flags=re.M)
            if m:
                ents.append(MessageEntityCode(offset=utf16offset(output,m.start()),length=len(code)))
    return sorted(ents,key=lambda e:e.offset)


def media_identity(message) -> str:
    """Stable media identity without Telethon's File.id (broken for some PhotoSize)."""
    photo = getattr(message, 'photo', None)
    document = getattr(message, 'document', None)
    if photo is not None and getattr(photo, 'id', None) is not None:
        return f"photo:{photo.id}"
    if document is not None and getattr(document, 'id', None) is not None:
        return f"document:{document.id}"
    media = getattr(message, 'media', None)
    return repr(media) if media is not None else ''


def digest(text: str, media_fingerprint: str = '') -> str:
    return hashlib.sha256((text + '\u241e' + media_fingerprint).encode('utf-8')).hexdigest()


def build_entities(message, prefix_length: int, output: str, replacements=None):
    """Preserve source formatting/link entities in retained prefix; add clickable code entities."""
    original = message.message or ''
    # Telegram offsets are counted in UTF-16 code units, not Python code points.
    prefix_u16 = len(original[:prefix_length].encode('utf-16-le')) // 2
    ents = []
    replacements = replacements or {}
    # Replacing a short URL changes UTF-16 offsets. Shift text formatting around URLs;
    # never retain a hidden competitor URL entity.
    raw_prefix = original[:prefix_length]
    spans = []
    for m in re.finditer(r'https?://[^\s<>\]\[)"\']+', raw_prefix, re.I):
        old = m.group(0).rstrip('.,،؛!')
        if old in replacements and replacements[old] != old:
            start = len(raw_prefix[:m.start()].encode('utf-16-le')) // 2
            end = start + len(old.encode('utf-16-le')) // 2
            delta = (len(replacements[old].encode('utf-16-le')) - len(old.encode('utf-16-le'))) // 2
            spans.append((start, end, delta))
    for ent in message.entities or []:
        if ent.offset >= prefix_u16 or isinstance(ent, MessageEntityTextUrl):
            continue
        # Entities intersecting replaced URLs cannot be safely retained.
        if any(ent.offset < end and ent.offset + ent.length > start for start, end, _ in spans):
            continue
        limit = min(ent.length, prefix_u16 - ent.offset)
        if limit > 0:
            import copy
            en = copy.copy(ent)
            en.offset += sum(delta for start, end, delta in spans if end <= ent.offset)
            en.length = limit
            ents.append(en)
    for target in (COUPON_TEXT, FOOTER_TEXT):
        idx = output.rfind(target)
        if idx >= 0:
            off = len(output[:idx].encode('utf-16-le')) // 2
            leng = len(target.encode('utf-16-le')) // 2
            ents.append(MessageEntityBold(offset=off, length=leng))
    # Clickable label for the Amazon channel. Telegram entities use UTF-16 offsets.
    label_start = output.rfind(AMAZON_FOOTER_LINK_LABEL)
    if label_start >= 0:
        link_offset = len(output[:label_start].encode('utf-16-le')) // 2
        link_length = len(AMAZON_FOOTER_LINK_LABEL.encode('utf-16-le')) // 2
        ents.append(MessageEntityTextUrl(offset=link_offset, length=link_length,
                                         url=AMAZON_FOOTER_URL))
    for code in CODES:
        # Match only the appended standalone code, not a mention inside prefix.
        m = re.search(r'^' + re.escape(code) + r'$', output, flags=re.M)
        if m:
            offset = len(output[:m.start()].encode('utf-16-le')) // 2
            ents.append(MessageEntityCode(offset=offset, length=len(code)))
    return sorted(ents, key=lambda x: x.offset)


def db_connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS mirror (
      source TEXT NOT NULL, source_id INTEGER NOT NULL,
      target_ids TEXT NOT NULL, content_hash TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (source, source_id)
    )''')
    conn.execute('CREATE TABLE IF NOT EXISTS watermarks (source TEXT PRIMARY KEY, last_id INTEGER NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS uneditable_targets (
      source TEXT NOT NULL, source_id INTEGER NOT NULL, content_hash TEXT NOT NULL,
      reason TEXT NOT NULL, PRIMARY KEY(source, source_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS skipped_duplicates (
      source TEXT NOT NULL, source_id INTEGER NOT NULL,
      target_id INTEGER, reason TEXT NOT NULL,
      PRIMARY KEY(source, source_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS media_state (
      source TEXT NOT NULL, source_id INTEGER NOT NULL, slot INTEGER NOT NULL,
      fingerprint TEXT NOT NULL, PRIMARY KEY(source, source_id, slot)
    )''')
    conn.execute("""CREATE TABLE IF NOT EXISTS own_links (
      clean_url TEXT PRIMARY KEY, yajny_url TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    conn.commit()
    return conn


class TargetEditUnavailable(Exception):
    """Never create a replacement message for an invalid destination edit target."""


def blocked_edit(db, source, source_id, content_hash):
    row = db.execute('SELECT content_hash FROM uneditable_targets WHERE source=? AND source_id=?',
                     (source, source_id)).fetchone()
    return bool(row and row[0] == content_hash)


def mark_uneditable(db, source, source_id, content_hash, reason):
    db.execute("""INSERT INTO uneditable_targets VALUES (?,?,?,?)
       ON CONFLICT(source, source_id) DO UPDATE SET
       content_hash=excluded.content_hash, reason=excluded.reason""",
       (source, source_id, content_hash, str(reason)[:200]))
    db.commit()


async def assert_destination_editable(client, dest, ids):
    """Verify Telegram destination messages exist before trying to edit.

    Does NOT delete DB mappings or send replacement posts when a target is missing.
    """
    if not ids:
        raise TargetEditUnavailable('No saved destination IDs')
    found = await client.get_messages(dest, ids=ids)
    if not isinstance(found, list):
        found = [found]
    actual = {m.id for m in found if m is not None and not getattr(m, 'action', None)}
    missing = [i for i in ids if i not in actual]
    if missing:
        raise TargetEditUnavailable('Missing destination message IDs: ' + ','.join(map(str, missing)))


def fetch_row(db, source, msg_id):
    return db.execute('SELECT target_ids,content_hash FROM mirror WHERE source=? AND source_id=?', (source, msg_id)).fetchone()


def save_row(db, source, msg_id, ids, hashval):
    db.execute('''INSERT INTO mirror(source,source_id,target_ids,content_hash,updated_at)
      VALUES(?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET
      target_ids=excluded.target_ids,content_hash=excluded.content_hash,updated_at=excluded.updated_at''',
      (source, msg_id, ','.join(map(str, ids)), hashval, datetime.now(timezone.utc).isoformat()))
    db.commit()


# Guard against a second copy when another source shared the same product,
# or a previous deployment lost part of its local source->target mapping.
# The *destination* itself is consulted, not only the source message ID.
DEDUP_HOURS = 24
DEST_LINK_CACHE = {}  # URL -> most recent destination timestamp (UTC)
DEST_TEXT_CACHE = {}  # normalized offer text -> most recent timestamp (UTC)
DEST_LINK_CACHE_AT = 0.0
DEST_LINK_CACHE_DEST = None


def skipped_duplicate(db, source, source_id):
    return db.execute('SELECT 1 FROM skipped_duplicates WHERE source=? AND source_id=?',
                      (source, source_id)).fetchone() is not None


def mark_duplicate(db, source, source_id, target_id, reason):
    db.execute('INSERT OR IGNORE INTO skipped_duplicates(source,source_id,target_id,reason) VALUES (?,?,?,?)',
               (source, source_id, target_id, reason))
    db.commit()


def offer_signature(text):
    """Conservative near-exact duplicate guard for a product post.

    Strip URLs and appended coupon instructions; keep price/product title.
    """
    prefix = (text or '').split(COUPON_TEXT)[0]
    prefix = re.sub(r'https?://\S+', '', prefix, flags=re.I)
    prefix = re.sub(r'\s+', ' ', prefix).strip().casefold()
    return prefix if len(prefix) >= 30 else ''


def _within_dedup_window(published_at, now=None):
    """Only destination messages posted in the previous 24 hours block a NEW offer."""
    if published_at is None:
        return False
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return timedelta(0) <= now - published_at < timedelta(hours=DEDUP_HOURS)


async def destination_duplicate(client, dest, own_urls, post_text=''):
    """Prevent same product within 24h; original source message IDs remain permanent."""
    global DEST_LINK_CACHE, DEST_TEXT_CACHE, DEST_LINK_CACHE_AT, DEST_LINK_CACHE_DEST
    if not own_urls:
        return False
    now_monotonic = asyncio.get_running_loop().time()
    current_utc = datetime.now(timezone.utc)
    if DEST_LINK_CACHE_DEST != str(dest) or now_monotonic - DEST_LINK_CACHE_AT > 20:
        recent_urls = {}
        recent_texts = {}
        # Read by date, not by count. Stop after the 24h window (newest first).
        async for old in client.iter_messages(dest):
            if not _within_dedup_window(old.date, current_utc):
                break
            if old.message:
                for url in urls_in_text(old.message):
                    recent_urls.setdefault(url, old.date)
                sig = offer_signature(old.message)
                if sig:
                    recent_texts.setdefault(sig, old.date)
        DEST_LINK_CACHE = recent_urls
        DEST_TEXT_CACHE = recent_texts
        DEST_LINK_CACHE_AT = now_monotonic
        DEST_LINK_CACHE_DEST = str(dest)
    sig = offer_signature(post_text)
    return (any(_within_dedup_window(DEST_LINK_CACHE.get(url), current_utc) for url in own_urls)
            or bool(sig and _within_dedup_window(DEST_TEXT_CACHE.get(sig), current_utc)))


def remember_published_urls(mapping, post_text=''):
    """Update in-process cache immediately so consecutive posts cannot duplicate."""
    published_at = datetime.now(timezone.utc)
    for url in mapping.values():
        DEST_LINK_CACHE[url] = published_at
    sig = offer_signature(post_text)
    if sig:
        DEST_TEXT_CACHE[sig] = published_at


async def resolve_amazon_product(url):
    """Expand Amazon's short URLs, verify ASIN + marketplace; never retain source tag."""
    original = urlparse(url)
    host = (original.hostname or '').lower()
    allowed_short = {'link.amazon', 'amzn.to', 'a.co'}
    if host in allowed_short:
        current = url
        async with httpx.AsyncClient(follow_redirects=False, timeout=18,
                                     headers={'User-Agent':'Mozilla/5.0'}) as client:
            for _ in range(7):
                r = await client.get(current)
                if r.status_code not in (301, 302, 303, 307, 308):
                    raise ValueError('Amazon shortcut did not return a usable redirect')
                loc = r.headers.get('location')
                if not loc:
                    raise ValueError('Amazon shortlink has no redirect Location')
                current = urljoin(current, loc)
                target_host = (urlparse(current).hostname or '').lower()
                if target_host in allowed_short:
                    continue
                if target_host == 'amazon.eg' or target_host == 'www.amazon.eg':
                    break
                raise ValueError('Amazon shortcut did not resolve to amazon.eg')
            else:
                raise ValueError('Too many Amazon redirects')
    else:
        if host not in ('amazon.eg','www.amazon.eg'):
            raise ValueError('Only amazon.eg supported for Egyptian affiliate tag')
        current = url
    dest = urlparse(current)
    asin_match = re.search(r'/(?:dp/|gp/product/|gp/aw/d/)([A-Za-z0-9]{10})(?:/|$|\?)',dest.path,re.I)
    if asin_match:
        asin = asin_match.group(1).upper()
        if not ASIN_RE.fullmatch(asin):
            raise ValueError('Invalid Amazon ASIN')
        return f'https://www.amazon.eg/dp/{asin}?tag={AMAZON_TAG}'
    # Genuine Amazon campaign/category page: keep its path, remove competitor referral data.
    if not dest.path or dest.path == '/':
        raise ValueError('Amazon redirect reached home page instead of an offer')
    blocked = {'tag', 'ascsubtag', 'linkcode', 'linkid', 'ref', 'ref_', 'camp', 'creative'}
    params = [(k,v) for k,v in parse_qsl(dest.query, keep_blank_values=True)
              if k.lower() not in blocked and not k.lower().startswith('utm_')]
    params.append(('tag', AMAZON_TAG))
    return urlunparse(('https', 'www.amazon.eg', dest.path, '', urlencode(params), ''))


async def convert_source_links(db, creator, message):
    """Generate own affiliate URLs for both Amazon and Noon; return mapping and kind."""
    raw = message.message or ''
    raw_urls = extract_product_urls(raw)
    # Handle hidden links strictly; otherwise source tracking can leak through entities.
    hidden = [e for e in (message.entities or []) if isinstance(e, MessageEntityTextUrl)
              and ('yajny.com/' in (e.url or '').lower() or 'amazon.' in (e.url or '').lower()
                   or 'amzn.to/' in (e.url or '').lower())]
    if hidden:
        raise ValueError('Hidden affiliate link: refusing to publish unconverted source tracking')
    mapping = {}
    has_noon = False
    for url in raw_urls:
        u = urlparse(url)
        host = (u.hostname or '').lower()
        if host in ('link.amazon','amzn.to','a.co') or host.startswith('amazon.') or host.startswith('www.amazon.'):
            mapping[url] = await resolve_amazon_product(url)
        elif SHORT_RE.fullmatch(url) or host in ('noon.com','www.noon.com'):
            has_noon = True
            clean = await resolve_noon_url(url) if SHORT_RE.fullmatch(url) else clean_noon_url(url)
            cached = db.execute('SELECT yajny_url FROM own_links WHERE clean_url=?',(clean,)).fetchone()
            if cached:
                new = cached[0]
            else:
                new = await creator.create(clean,'نون مصر - '+str(message.id))
                if not SHORT_RE.fullmatch(new):
                    raise RuntimeError('Yajny returned invalid own link')
                db.execute('INSERT OR IGNORE INTO own_links VALUES (?,?,?)',
                           (clean,new,datetime.now(timezone.utc).isoformat()))
                db.commit()
            mapping[url] = new
    if not mapping:
        raise ValueError('Post has no recognized Amazon/Noon product URL')
    return mapping, has_noon


def no_product_link(message):
    """Only ignore messages with no shop/product links at all.

    Malformed merchant URLs or hidden product links must remain pending for review,
    rather than being silently discarded.
    """
    raw = message.message or ''
    if extract_product_urls(raw):
        return False
    # Includes malformed merchant URLs and markdown-escaped https\:// variants.
    if re.search(r'(?:noon\.com|yajny\.com|link\.amazon|amzn\.to|a\.co/|amazon\.eg)', raw, re.I):
        return False
    for ent in message.entities or []:
        url = getattr(ent, 'url', None)
        if url and re.search(r'(?:noon\.com|yajny\.com|link\.amazon|amzn\.to|amazon\.eg|a\.co/)', url, re.I):
            return False
    return True


def is_post(message):
    return bool(message and not message.action and (message.message or message.media))


def is_album_member(message):
    return bool(message.grouped_id)


async def send_post(client, dest, msg, output, ents):
    """Copy (not forward) a message. Returns all target message ids."""
    media = msg.media if msg.media and not msg.web_preview else None
    if media:
        # Telegram limits media caption to 1024 chars, so put long text separately.
        if len(output.encode('utf-16-le')) // 2 > 1024:
            posted = await client.send_file(dest, media, caption='')
            txt = await client.send_message(dest, output, formatting_entities=ents, link_preview=False)
            return [posted.id, txt.id]
        sent = await client.send_file(dest, media, caption=output, formatting_entities=ents)
        return [sent.id]
    sent = await client.send_message(dest, output, formatting_entities=ents, link_preview=False)
    return [sent.id]


async def edit_post(client, db, source, source_id, dest, ids, msg, output, ents):
    """Edit text AND source photo in place, keeping target IDs and affiliate URL."""
    await assert_destination_editable(client, dest, ids)
    if msg.media and not msg.web_preview:
        change_photo = await needs_media_edit(client, dest, ids[0], msg, db, source, source_id, 0)
    else:
        change_photo = False
        existing = await client.get_messages(dest, ids=ids[0])
        if existing and existing.media and not existing.web_preview:
            raise RuntimeError('Source media removed; cannot safely remove target media in place')
    long_caption = len(ids) > 1
    if change_photo:
        await edit_media(client, dest, ids[0], msg.media,
                         '' if long_caption else output, None if long_caption else ents)
        LOG.info('MEDIA EDIT %s/%s destination=%s', source, source_id, ids[0])
    # Read actual destination before issuing EditMessageRequest. A no-op request
    # still hits Telegram limits; keep the original message IDs if unchanged.
    text_target = await client.get_messages(dest, ids=ids[-1])
    if not text_target:
        raise TargetEditUnavailable(f'Missing destination message {ids[-1]}')
    text_changed = (text_target.message or '') != output
    # The last message contains long caption separately; otherwise image caption.
    if (not change_photo or long_caption) and text_changed:
        try:
            await client.edit_message(dest, ids[-1], output, formatting_entities=ents, link_preview=False)
        except errors.MessageNotModifiedError:
            pass
    put_media_states(db, source, source_id, [msg])
    return ids


def get_media_state(db, source, source_id, slot):
    row = db.execute('SELECT fingerprint FROM media_state WHERE source=? AND source_id=? AND slot=?',
                     (source, source_id, slot)).fetchone()
    return row[0] if row else None


def put_media_states(db, source, source_id, messages):
    # Write the state only after Telegram has confirmed the edit.
    for slot, msg in enumerate(messages):
        db.execute('''INSERT INTO media_state(source,source_id,slot,fingerprint) VALUES(?,?,?,?)
          ON CONFLICT(source,source_id,slot) DO UPDATE SET fingerprint=excluded.fingerprint''',
          (source, source_id, slot, media_identity(msg)))
    db.commit()


async def needs_media_edit(client, dest, target_id, source_msg, db, source, source_id, slot):
    new = media_identity(source_msg)
    previous = get_media_state(db, source, source_id, slot)
    if previous is not None:
        return previous != new
    # Upgrading from v5: compare against the actual existing destination photo.
    # Do not assume v5 mirrored changed photos correctly.
    current = await client.get_messages(dest, ids=target_id)
    if not current:
        raise RuntimeError(f'Destination media message {target_id} missing; manual repair needed')
    return media_identity(current) != new


async def edit_media(client, dest, target_id, media, caption, ents):
    if not media:
        raise RuntimeError('Source photo disappeared; refusing to delete existing image automatically')
    # Telegram EditMessage supports replacing photo/document in-place, preserving message ID.
    await client.edit_message(dest, target_id, text=caption, file=media,
                              formatting_entities=ents if caption else None, link_preview=False)


def source_has_new_edit(db, source, key, messages):
    """True only when Telegram reports a source edit AFTER its last sync.

    A new deploy or changed post formatter must not rewrite old destination posts.
    Existing DB mappings are retained; never repost previously mirrored messages.
    """
    rec = db.execute('SELECT updated_at FROM mirror WHERE source=? AND source_id=?',
                     (source, key)).fetchone()
    if not rec:
        return False
    last_sync = datetime.fromisoformat(rec[0])
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    for msg in messages:
        edited = getattr(msg, 'edit_date', None)
        if edited:
            if edited.tzinfo is None:
                edited = edited.replace(tzinfo=timezone.utc)
            if edited > last_sync:
                return True
    return False


def unchanged_recorded_post(db, source, key, messages):
    """Skip previously mirrored source messages unless genuinely edited since sync.

    This avoids mass edits when deploying a new version or discovering missing
    historical media_state rows. Telegram assigns edit_date to media edits too.
    """
    if not fetch_row(db, source, key):
        return False
    return not source_has_new_edit(db, source, key, messages)


async def sync_one(client, db, creator, entity, dest, source, msg):
    if not is_post(msg) or is_album_member(msg):
        return
    if skipped_duplicate(db, source, msg.id):
        return
    if unchanged_recorded_post(db, source, msg.id, [msg]):
        return
    if no_product_link(msg) and not fetch_row(db, source, msg.id):
        mark_duplicate(db, source, msg.id, None, 'ignored: no Amazon/Noon product link')
        LOG.info('IGNORED NON-PRODUCT %s/%s; advancing watermark', source, msg.id)
        return
    raw = msg.message or ''
    try:
        mapping, has_noon = await convert_source_links(db, creator, msg)
    except Exception as exc:
        LOG.error('NO PUBLISH %s/%s: own link conversion failed: %s', source, msg.id, exc)
        return
    output = format_post(raw, mapping, has_noon)
    ents = build_mixed_entities(msg, output, has_noon)
    media_fingerprint = media_identity(msg)
    hv = digest(output, media_fingerprint)
    row = fetch_row(db, source, msg.id)
    if row and blocked_edit(db, source, msg.id, hv):
        return
    if row and row[1] == hv:
        # A source edit can leave the composed offer unchanged. Acknowledge that
        # edit in the DB so future polls don't process it again.
        if msg.media and get_media_state(db, source, msg.id, 0) is not None:
            if get_media_state(db, source, msg.id, 0) != media_identity(msg):
                pass  # Real media change: continue to edit_post.
            else:
                save_row(db, source, msg.id, [int(x) for x in row[0].split(',')], hv)
                return
        elif not msg.media:
            save_row(db, source, msg.id, [int(x) for x in row[0].split(',')], hv)
            return
        # Missing old media_state is checked by edit_post; not automatically
        # replaced unless the source was edited and the destination differs.
    try:
        if row:
            ids = [int(s) for s in row[0].split(',')]
            ids = await edit_post(client, db, source, msg.id, dest, ids, msg, output, ents)
            LOG.info('EDIT %s/%s -> %s', source, msg.id, ids)
        else:
            if await destination_duplicate(client, dest, mapping.values(), output):
                mark_duplicate(db, source, msg.id, None, 'same product published within 24h in destination')
                LOG.warning('DUPLICATE SKIPPED %s/%s; same product posted within last 24h', source, msg.id)
                return
            ids = await send_post(client, dest, msg, output, ents)
            remember_published_urls(mapping, output)
            LOG.info('NEW %s/%s -> %s', source, msg.id, ids)
        save_row(db, source, msg.id, ids, hv)
        if not row:
            put_media_states(db, source, msg.id, [msg])
    except (TargetEditUnavailable, errors.MessageIdInvalidError, errors.MessageAuthorRequiredError) as exc:
        mark_uneditable(db, source, msg.id, hv, exc)
        LOG.error('TARGET EDIT BLOCKED %s/%s: %s; preserving mapping, NO REPOST', source, msg.id, exc)
    except errors.FloodWaitError as e:
        LOG.warning('FloodWait %ss', e.seconds)
        await asyncio.sleep(e.seconds + 2)
    except Exception:
        LOG.exception('Failed source %s/%s; will retry', source, msg.id)


async def sync_album(client, db, creator, dest, source, messages):
    """Sync multi-photo album caption and changed photos, retaining original message IDs."""
    messages = sorted(messages, key=lambda m: m.id)
    root = messages[0]
    album_key = root.grouped_id
    if skipped_duplicate(db, source, album_key):
        return
    if unchanged_recorded_post(db, source, album_key, [m for m in messages if m.media]):
        return
    caption_msg = next((m for m in messages if m.message), root)
    if no_product_link(caption_msg) and not fetch_row(db, source, album_key):
        mark_duplicate(db, source, album_key, None, 'ignored: no Amazon/Noon product link')
        LOG.info('IGNORED NON-PRODUCT album %s/%s; advancing watermark', source, root.id)
        return
    try:
        mapping, has_noon = await convert_source_links(db, creator, caption_msg)
    except Exception as exc:
        LOG.error('NO PUBLISH album %s/%s: own link conversion failed: %s', source, root.id, exc)
        return
    output = format_post(caption_msg.message or '', mapping, has_noon)
    ents = build_mixed_entities(caption_msg, output, has_noon)
    media_msgs = [m for m in messages if m.media]
    if not media_msgs:
        return
    hashval = digest(output, '|'.join(media_identity(m) for m in media_msgs))
    row = fetch_row(db, source, album_key)
    if row and blocked_edit(db, source, album_key, hashval):
        return
    if row and row[1] == hashval:
        if all(get_media_state(db, source, album_key, i) == media_identity(msg)
               for i, msg in enumerate(media_msgs)):
            save_row(db, source, album_key, [int(x) for x in row[0].split(',')], hashval)
            return
        # Missing media fingerprints or actual photo changes require checking
        # the destination when the source edit_date advanced.
    long_caption = len(output.encode('utf-16-le')) // 2 > 1024
    try:
        if row:
            ids = [int(s) for s in row[0].split(',')]
            expected = len(media_msgs) + (1 if long_caption else 0)
            if len(ids) != expected:
                # Telegram albums cannot safely gain/lose items in-place. Never
                # delete/repost silently: that would duplicate posts or lose comments.
                LOG.warning('ALBUM MEDIA COUNT CHANGED %s/%s source=%s target=%s; manual repair needed',
                            source, root.id, len(media_msgs), len(ids))
                return
            await assert_destination_editable(client, dest, ids)
            changed = []
            for slot, (src, target_id) in enumerate(zip(media_msgs, ids)):
                if await needs_media_edit(client, dest, target_id, src, db, source, album_key, slot):
                    changed.append((slot, src, target_id))
            for slot, src, target_id in changed:
                await edit_media(client, dest, target_id, src.media,
                                 output if slot == 0 and not long_caption else '',
                                 ents if slot == 0 and not long_caption else None)
                LOG.info('ALBUM MEDIA EDIT %s/%s slot=%s destination=%s',
                         source, root.id, slot, target_id)
            caption_id = ids[-1] if long_caption else ids[0]
            if long_caption or not any(slot == 0 for slot, _, _ in changed):
                caption_target = await client.get_messages(dest, ids=caption_id)
                if not caption_target:
                    raise TargetEditUnavailable(f'Missing destination message {caption_id}')
                if (caption_target.message or '') != output:
                    try:
                        await client.edit_message(dest, caption_id, output, formatting_entities=ents,
                                                  link_preview=False)
                    except errors.MessageNotModifiedError:
                        pass
            # If media changed partway but subsequent edits failed, this is not reached;
            # next poll retries based on the actual destination and last saved state.
        else:
            if await destination_duplicate(client, dest, mapping.values(), output):
                mark_duplicate(db, source, album_key, None, 'same product published within 24h in destination')
                LOG.warning('DUPLICATE SKIPPED album %s/%s; same product posted within last 24h', source, root.id)
                return
            sent = await client.send_file(dest, [m.media for m in media_msgs],
                                          caption='' if long_caption else output,
                                          formatting_entities=None if long_caption else ents)
            ids = [s.id for s in (sent if isinstance(sent, list) else [sent])]
            if long_caption:
                txt = await client.send_message(dest, output, formatting_entities=ents, link_preview=False)
                ids.append(txt.id)
            remember_published_urls(mapping, output)
        put_media_states(db, source, album_key, media_msgs)
        save_row(db, source, album_key, ids, hashval)
        LOG.info('ALBUM %s/%s -> %s', source, root.id, ids)
    except (TargetEditUnavailable, errors.MessageIdInvalidError, errors.MessageAuthorRequiredError) as exc:
        mark_uneditable(db, source, album_key, hashval, exc)
        LOG.error('TARGET ALBUM EDIT BLOCKED %s/%s: %s; preserving mapping, NO REPOST', source, root.id, exc)
    except errors.FloodWaitError as e:
        LOG.warning('FloodWait on album %s/%s: %ss', source, root.id, e.seconds)
        await asyncio.sleep(e.seconds + 2)
    except Exception:
        LOG.exception('Failed album %s/%s; will retry', source, root.id)


async def run():
    api_id = int(os.environ['TELEGRAM_API_ID'])
    api_hash = os.environ['TELEGRAM_API_HASH']
    token = os.getenv('TELEGRAM_SESSION_STRING') or os.getenv('TELEGRAM_STRING_SESSION') or os.getenv('TELEGRAM_SESSION')
    if not token and os.getenv('RAILWAY_ENVIRONMENT'):
        raise RuntimeError('Set TELEGRAM_SESSION_STRING on Railway (never commit it to ZIP).')
    session = StringSession(token) if token else str(DATA_DIR / 'noon_user')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session, api_id, api_hash)
    await client.start()  # For local login only; cloud must have StringSession.
    db = db_connect()
    dest = await client.get_entity(DESTINATION)
    source_entities = {}
    for source in SOURCES:
        source_entities[source] = await client.get_entity(source)
    LOG.info('VERSION %s; executing=%s; no-product posts skipped', BUILD_VERSION, Path(__file__).resolve())
    LOG.info('READY: sources=%s target=%s; BACKFILL DISABLED; duplicate_window=%sh; DB=%s', SOURCES, DESTINATION, DEDUP_HOURS, DB_PATH)
    if os.getenv('BACKFILL_LIMIT', '0').strip() not in ('', '0'):
        LOG.warning('Safety: BACKFILL_LIMIT environment variable ignored. No historical reposts permitted.')
    creator = YajnyBrowser()
    await creator.start()  # Cannot run if authenticated Yajny browser unavailable.
    initial = True
    try:
        while True:
            for source, entity in source_entities.items():
                try:
                    LOG.info('SCAN START %s version=%s', source, BUILD_VERSION)
                    watermark = db.execute('SELECT last_id FROM watermarks WHERE source=?', (source,)).fetchone()
                    if not watermark and BACKFILL_LIMIT == 0:
                        # On deployment start from NOW, not old history. Persist across redeploys.
                        most_recent = await client.get_messages(entity, limit=1)
                        high = most_recent[0].id if most_recent else 0
                        db.execute('INSERT OR IGNORE INTO watermarks VALUES (?,?)', (source, high))
                        db.commit()
                        LOG.info('BASELINE %s last_id=%s (no historical repost)', source, high)
                        continue
                    if initial and not watermark:
                        msgs = list(await client.get_messages(entity, limit=BACKFILL_LIMIT))
                    else:
                        # Fetch all unprocessed messages after watermark; preserve backpressure.
                        last_id = watermark[0] if watermark else 0
                        new_messages = [m async for m in client.iter_messages(
                            entity, min_id=last_id, reverse=True, limit=100)]
                        recent = list(await client.get_messages(entity, limit=CHECK_RECENT))
                        # Examine only actual source edits newer than our last sync.
                        # Never mass-edit older posts solely because the formatter
                        # or affiliate/footer rules changed in a new deployment.
                        old_edits = [m for m in recent if m.id <= last_id and
                                     source_has_new_edit(db, source,
                                         m.grouped_id if m.grouped_id else m.id, [m])]
                        msgs = list({m.id: m for m in new_messages + old_edits}.values())
                        LOG.info('SCAN QUEUE %s new=%d old_to_check=%d total=%d',
                                 source, len(new_messages), len(old_edits), len(msgs))
                    # Gather full albums at polling boundary from source if needed.
                    groups = {}
                    singles = []
                    for msg in msgs:
                        if not is_post(msg):
                            continue
                        if msg.grouped_id:
                            groups.setdefault(msg.grouped_id, []).append(msg)
                        else:
                            singles.append(msg)
                    for msg in sorted(singles, key=lambda m: m.id):
                        await sync_one(client, db, creator, entity, dest, source, msg)
                    for group in sorted(groups.values(), key=lambda g: min(m.id for m in g)):
                        # Refresh around the album to avoid accidentally treating a partial
                        # 2-photo group as a 1-photo album near CHECK_RECENT boundaries.
                        lo, hi = min(m.id for m in group), max(m.id for m in group)
                        surrounding = [m async for m in client.iter_messages(
                            entity, min_id=max(0, lo - 16), max_id=hi + 16)]
                        full = sorted([m for m in surrounding if m.grouped_id == group[0].grouped_id],
                                      key=lambda m: m.id)
                        await sync_album(client, db, creator, dest, source, full or group)
                    # Watermark only after processing; if link creation fails, keep it for retry.
                    # Failing individual posts should not advance beyond them.
                    # No watermark advancement for failed conversions detected via own-links hash.
                    # Failed posts are intentionally retried on the next poll.
                    if msgs:
                        old_mark = watermark[0] if watermark else 0
                        # Only advance through completely processed NEW messages.
                        # Albums use grouped_id as DB key; skipped non-posts count as handled.
                        advance = old_mark
                        for m in sorted((m for m in msgs if m.id > old_mark), key=lambda x: x.id):
                            key = m.grouped_id if m.grouped_id else m.id
                            if is_post(m) and not fetch_row(db, source, key) and not skipped_duplicate(db, source, key):
                                LOG.warning('PENDING %s/%s; watermark held for retry', source, m.id)
                                break
                            advance = m.id
                        if advance > old_mark:
                            db.execute('INSERT INTO watermarks VALUES (?,?) ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id',
                                       (source, advance))
                            db.commit()
                    LOG.info('SCAN END %s', source)
                except Exception:
                    LOG.exception('Source scan failed: %s', source)
            initial = False
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await creator.close()
        db.close()
        await client.disconnect()


if __name__ == '__main__':
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run())
