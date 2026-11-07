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
from telethon.tl.types import MessageEntityCode, MessageEntityBold

LOG = logging.getLogger('noon_forwarder')
SOURCES = [x.strip().lstrip('@') for x in os.getenv('TELEGRAM_SOURCES', 'Hi_pro1,tawfiry').split(',') if x.strip()]
DESTINATION = os.getenv('TELEGRAM_DESTINATION', 'NoonDealsEgy').strip().lstrip('@')
POLL_SECONDS = max(10, int(os.getenv('POLL_SECONDS', '30')))
BACKFILL_LIMIT = max(0, int(os.getenv('BACKFILL_LIMIT', '20')))
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


def build_entities(message, prefix_length: int, output: str):
    """Preserve source formatting/link entities in retained prefix; add clickable code entities."""
    original = message.message or ''
    # Telegram offsets are counted in UTF-16 code units, not Python code points.
    prefix_u16 = len(original[:prefix_length].encode('utf-16-le')) // 2
    ents = []
    for ent in message.entities or []:
        if ent.offset >= prefix_u16:
            continue
        limit = min(ent.length, prefix_u16 - ent.offset)
        if limit > 0:
            import copy
            en = copy.copy(ent)
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


async def edit_post(client, dest, ids, msg, output, ents):
    """Update existing copy; maintain ID mapping and do not create duplicates."""
    try:
        target_id = ids[-1]  # for long captions, the separate text message is last
        await client.edit_message(dest, target_id, output, formatting_entities=ents, link_preview=False)
        return ids
    except errors.MessageNotModifiedError:
        return ids
    except (errors.MessageIdInvalidError, errors.MessageAuthorRequiredError):
        LOG.warning('Cannot edit target message %s; preserving mapping', ids)
        return ids


async def sync_one(client, db, entity, dest, source, msg):
    if not is_post(msg) or is_album_member(msg):
        return
    raw = msg.message or ''
    output, prefix_len = replace_coupon(raw)
    ents = build_entities(msg, prefix_len, output)
    media_fingerprint = media_identity(msg)
    hv = digest(output, media_fingerprint)
    row = fetch_row(db, source, msg.id)
    if row and row[1] == hv:
        return
    try:
        if row:
            ids = [int(s) for s in row[0].split(',')]
            ids = await edit_post(client, dest, ids, msg, output, ents)
            LOG.info('EDIT %s/%s -> %s', source, msg.id, ids)
        else:
            ids = await send_post(client, dest, msg, output, ents)
            LOG.info('NEW %s/%s -> %s', source, msg.id, ids)
        save_row(db, source, msg.id, ids, hv)
    except errors.FloodWaitError as e:
        LOG.warning('FloodWait %ss', e.seconds)
        await asyncio.sleep(e.seconds + 2)
    except Exception:
        LOG.exception('Failed source %s/%s; will retry', source, msg.id)


async def sync_album(client, db, dest, source, messages):
    """Copy a multi-image Telegram album as an album with one coupon caption."""
    messages = sorted(messages, key=lambda m: m.id)
    root = messages[0]
    album_key = root.grouped_id  # stable even if scan cuts across an album
    # Use whichever member contains the original caption.
    caption_msg = next((m for m in messages if m.message), root)
    output, pref = replace_coupon(caption_msg.message or '')
    ents = build_entities(caption_msg, pref, output)
    media = [m.media for m in messages if m.media]
    if not media:
        return
    hashval = digest(output, '|'.join(media_identity(m) for m in messages))
    row = fetch_row(db, source, album_key)
    if row and row[1] == hashval:
        return
    try:
        if row:
            ids = [int(s) for s in row[0].split(',')]
            caption_id = ids[-1] if len(ids) > len(media) else ids[0]
            try:
                await client.edit_message(dest, caption_id, output, formatting_entities=ents, link_preview=False)
            except errors.MessageNotModifiedError:
                pass
        else:
            long = len(output.encode('utf-16-le')) // 2 > 1024
            sent = await client.send_file(dest, media, caption='' if long else output,
                                          formatting_entities=None if long else ents)
            ids = [s.id for s in (sent if isinstance(sent, list) else [sent])]
            if long:
                text_msg = await client.send_message(dest, output, formatting_entities=ents, link_preview=False)
                ids.append(text_msg.id)
        save_row(db, source, album_key, ids, hashval)
        LOG.info('ALBUM %s/%s -> %s', source, root.id, ids)
    except errors.FloodWaitError as e:
        await asyncio.sleep(e.seconds + 2)
    except Exception:
        LOG.exception('Failed album %s/%s', source, root.id)


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
    LOG.info('READY: sources=%s target=%s', SOURCES, DESTINATION)
    initial = True
    try:
        while True:
            for source, entity in source_entities.items():
                try:
                    # First run: limited backfill. Subsequent polls: detect edits in recent history.
                    limit = CHECK_RECENT if not initial else (BACKFILL_LIMIT or 1)
                    msgs = list(await client.get_messages(entity, limit=limit))
                    if initial and BACKFILL_LIMIT == 0:
                        # Remember latest source ID and process messages newer than this
                        # on subsequent polls (not historical content).
                        db.execute('CREATE TABLE IF NOT EXISTS watermarks (source TEXT PRIMARY KEY, last_id INTEGER)')
                        db.execute('INSERT OR IGNORE INTO watermarks VALUES (?,?)', (source, msgs[0].id if msgs else 0))
                        db.commit()
                        continue
                    watermark_row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='watermarks'").fetchone()
                    if watermark_row:
                        last = db.execute('SELECT last_id FROM watermarks WHERE source=?', (source,)).fetchone()
                        if last:
                            msgs = [m for m in msgs if m.id > last[0]]
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
                        await sync_one(client, db, entity, dest, source, msg)
                    for group in sorted(groups.values(), key=lambda g: min(m.id for m in g)):
                        # Albums not fully loaded at a page edge will be picked up in subsequent polls.
                        await sync_album(client, db, dest, source, group)
                except Exception:
                    LOG.exception('Source scan failed: %s', source)
            initial = False
            await asyncio.sleep(POLL_SECONDS)
    finally:
        db.close()
        await client.disconnect()


if __name__ == '__main__':
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run())
