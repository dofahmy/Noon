"""One-time local generation of Telethon string session; keep output private."""
import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(os.environ['TELEGRAM_API_ID'])
    api_hash = os.environ['TELEGRAM_API_HASH']
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        session = client.session.save()
        print('\nTELEGRAM_SESSION_STRING (SECRET; paste into Railway variable):\n' + session)

asyncio.run(main())
