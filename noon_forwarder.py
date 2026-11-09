"""Noon deals: mirror source Telegram posts and replace coupon sections.

Python 3.10+, Telethon 1.x. Single running instance only.
"""
import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.extensions import html
from telethon.tl.types import MessageEntityCode, MessageEntityBold, MessageEntityTextUrl
from yajny_links import YajnyBrowser, SHORT_RE, urls_in_text, replace_urls, resolve_noon_url, clean_noon_url

LOG = logging.getLogger('noon_forwarder')
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

# Source-specific coupon headings. Does not remove standalone product details.
COUPON_START = re.compile(
    r'(?:ماتنساش\s+وانت\s+بتطلب|ما\s*تنساش\s+وانت\s+بتطلب|'
    r'دوس\s+على\s+الكوبون|اضغط\s+على\s+الكود|'
    r'^(?:[⚡👑🔥🎟🎫🔣✅✔️\s]*)?(?:اكواد\s+الخصم|أكواد\s+الخصم|'
    r'كود\s+الخصم|كوبونات\s+الخصم|كوبونات\s+نون|الكوبونات\s*[:：]))', re.I | re.M)
CODE_LINE = re.compile(r'^\s*(?:[`\s/|,،\-–+]|(?:[A-Z0-9]{3,15}))*\s*$', re.I)


def section_start(text: str) -> Optional[int]:
    """Return start of recognized coupon paragraph, preserving product description."""
    lines = text.splitlines(keepends=True)
    pos = 0
    for line in lines:
        if COUPON_START.search(line):
            return pos
        pos += len(line)
    return None


def replace_coupon(text: str) -> tuple[str, int]:
    """Replace the coupon section at the end; return original prefix char length."""
    start = section_start(text)
    if start is None:
        # Only remove a terminal block of isolated code lines if there is a clear
        # coupon marker elsewhere. Otherwise append without deleting unrelated text.
        prefix = text.rstrip()
    else:
        prefix = text[:start].rstrip()
    out = prefix + ('\n\n' if prefix else '') + COUPON_TEXT + '\n\n' + '\n'.join(CODES) + '\n\n' + FOOTER_TEXT
    return out, len(prefix)


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
DEST_LINK_CACHE = set()
DEST_TEXT_CACHE = set()
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


async def destination_duplicate(client, dest, own_urls, post_text=''):
    """Check the last 500 destination posts for any own Yajny link before a NEW send."""
    global DEST_LINK_CACHE, DEST_TEXT_CACHE, DEST_LINK_CACHE_AT, DEST_LINK_CACHE_DEST
    if not own_urls:
        return False
    now = asyncio.get_running_loop().time()
    if DEST_LINK_CACHE_DEST != str(dest) or now - DEST_LINK_CACHE_AT > 20:
        recent_urls = set()
        recent_texts = set()
        async for old in client.iter_messages(dest, limit=1000):
            if old.message:
                recent_urls.update(urls_in_text(old.message))
                sig = offer_signature(old.message)
                if sig:
                    recent_texts.add(sig)
        DEST_LINK_CACHE = recent_urls
        DEST_TEXT_CACHE = recent_texts
        DEST_LINK_CACHE_AT = now
        DEST_LINK_CACHE_DEST = str(dest)
    sig = offer_signature(post_text)
    return bool(set(own_urls) & DEST_LINK_CACHE) or bool(sig and sig in DEST_TEXT_CACHE)


def remember_published_urls(mapping, post_text=''):
    DEST_LINK_CACHE.update(mapping.values())
    sig = offer_signature(post_text)
    if sig:
        DEST_TEXT_CACHE.add(sig)


async def convert_source_links(db, creator, message):
    """Resolve source referral links, create our own links, never pass competitor links."""
    raw = message.message or ''
    raw_urls = urls_in_text(raw)
    hidden = [e for e in (message.entities or []) if isinstance(e, MessageEntityTextUrl)
              and ('yajny.com/l/' in (e.url or '').lower())]
    if hidden:
        raise ValueError('Hidden Yajny hyperlink found; refusing to publish competitor tracking URL')
    mapping = {}
    for url in dict.fromkeys(raw_urls):
        if not SHORT_RE.fullmatch(url) and 'noon.com/' not in url.lower():
            # Keep unrelated links unchanged, including Telegram channels.
            continue
        clean = await resolve_noon_url(url) if SHORT_RE.fullmatch(url) else clean_noon_url(url)
        cached = db.execute('SELECT yajny_url FROM own_links WHERE clean_url=?', (clean,)).fetchone()
        if cached:
            new = cached[0]
        else:
            # If no verified browser creation, error propagates and NO post is sent.
            new = await creator.create(clean, 'نون مصر - ' + str(message.id))
            if not SHORT_RE.fullmatch(new):
                raise RuntimeError('Yajny returned invalid own link')
            db.execute('INSERT OR IGNORE INTO own_links VALUES (?,?,?)',
                       (clean, new, datetime.now(timezone.utc).isoformat()))
            db.commit()
        mapping[url] = new
    if not mapping:
        raise ValueError('Post has no recognized Noon/Yajny product URL; refuse publish')
    return mapping


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
    if not ids:
        raise RuntimeError('No stored destination IDs')
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
    # The last message contains long caption separately; otherwise image caption.
    if not change_photo or long_caption:
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


def unchanged_recorded_post(db, source, key, messages):
    """Avoid expensive URL resolution for already-synced, unedited messages.

    Source edits later than the last successful sync always require a check.
    Compare media fingerprints so edits to images are not missed.
    Unknown historical media_state is deliberately rechecked once.
    """
    rec = db.execute('SELECT updated_at FROM mirror WHERE source=? AND source_id=?', (source, key)).fetchone()
    if not rec:
        return False
    last_sync = datetime.fromisoformat(rec[0])
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    for slot, msg in enumerate(messages):
        edited = getattr(msg, 'edit_date', None)
        if edited and edited > last_sync:
            return False
        if msg.media:
            old_media = get_media_state(db, source, key, slot)
            if old_media is None or old_media != media_identity(msg):
                return False
    return True


async def sync_one(client, db, creator, entity, dest, source, msg):
    if not is_post(msg) or is_album_member(msg):
        return
    if skipped_duplicate(db, source, msg.id):
        return
    if unchanged_recorded_post(db, source, msg.id, [msg]):
        return
    raw = msg.message or ''
    try:
        mapping = await convert_source_links(db, creator, msg)
    except Exception as exc:
        LOG.error('NO PUBLISH %s/%s: own link conversion failed: %s', source, msg.id, exc)
        return
    output, prefix_len = replace_coupon(raw)
    output = replace_urls(output, mapping)
    ents = build_entities(msg, prefix_len, output, mapping)
    media_fingerprint = media_identity(msg)
    hv = digest(output, media_fingerprint)
    row = fetch_row(db, source, msg.id)
    if row and row[1] == hv:
        # v5 saved hashes even when it had only edited the text. On upgrade,
        # inspect the destination once to find old photos that v5 missed.
        if not msg.media or get_media_state(db, source, msg.id, 0) is not None:
            return
        old_ids = [int(x) for x in row[0].split(',')]
        if not await needs_media_edit(client, dest, old_ids[0], msg, db, source, msg.id, 0):
            put_media_states(db, source, msg.id, [msg])
            return
    try:
        if row:
            ids = [int(s) for s in row[0].split(',')]
            ids = await edit_post(client, db, source, msg.id, dest, ids, msg, output, ents)
            LOG.info('EDIT %s/%s -> %s', source, msg.id, ids)
        else:
            if await destination_duplicate(client, dest, mapping.values(), output):
                mark_duplicate(db, source, msg.id, None, 'own Yajny link already published in destination')
                LOG.warning('DUPLICATE SKIPPED %s/%s; matching own link already in destination', source, msg.id)
                return
            ids = await send_post(client, dest, msg, output, ents)
            remember_published_urls(mapping, output)
            LOG.info('NEW %s/%s -> %s', source, msg.id, ids)
        save_row(db, source, msg.id, ids, hv)
        if not row:
            put_media_states(db, source, msg.id, [msg])
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
    try:
        mapping = await convert_source_links(db, creator, caption_msg)
    except Exception as exc:
        LOG.error('NO PUBLISH album %s/%s: own link conversion failed: %s', source, root.id, exc)
        return
    output, pref = replace_coupon(caption_msg.message or '')
    output = replace_urls(output, mapping)
    ents = build_entities(caption_msg, pref, output, mapping)
    media_msgs = [m for m in messages if m.media]
    if not media_msgs:
        return
    hashval = digest(output, '|'.join(media_identity(m) for m in media_msgs))
    row = fetch_row(db, source, album_key)
    if row and row[1] == hashval:
        if all(get_media_state(db, source, album_key, i) is not None
               for i in range(len(media_msgs))):
            return
        old_ids = [int(x) for x in row[0].split(',')]
        if len(old_ids) >= len(media_msgs):
            mismatches = [await needs_media_edit(client, dest, old_ids[i], msg,
                                                  db, source, album_key, i)
                          for i, msg in enumerate(media_msgs)]
            if not any(mismatches):
                put_media_states(db, source, album_key, media_msgs)
                return
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
                try:
                    await client.edit_message(dest, caption_id, output, formatting_entities=ents,
                                              link_preview=False)
                except errors.MessageNotModifiedError:
                    pass
            # If media changed partway but subsequent edits failed, this is not reached;
            # next poll retries based on the actual destination and last saved state.
        else:
            if await destination_duplicate(client, dest, mapping.values(), output):
                mark_duplicate(db, source, album_key, None, 'own Yajny link already published in destination')
                LOG.warning('DUPLICATE SKIPPED album %s/%s; matching own link already in destination', source, root.id)
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
    LOG.info('READY: sources=%s target=%s; BACKFILL DISABLED; DB=%s', SOURCES, DESTINATION, DB_PATH)
    if os.getenv('BACKFILL_LIMIT', '0').strip() not in ('', '0'):
        LOG.warning('Safety: BACKFILL_LIMIT environment variable ignored. No historical reposts permitted.')
    creator = YajnyBrowser()
    await creator.start()  # Cannot run if authenticated Yajny browser unavailable.
    initial = True
    try:
        while True:
            for source, entity in source_entities.items():
                try:
                    LOG.info('SCAN START %s', source)
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
                        # Edit existing copied posts too; only those with known mappings.
                        old_edits = [m for m in recent if m.id <= last_id and (
                            bool(m.grouped_id) or fetch_row(db, source, m.id))]
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
