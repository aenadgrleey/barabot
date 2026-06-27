"""Interactive helper to mint a Telethon SESSION_STRING. Run once, paste into .env.

    python -m barabot.session_gen

Telethon StringSession is base64-encoded and packs auth_key + DC id into a single
string. After login, copy the printed SESSION_STRING into barabot/.env as
`SESSION_STRING=...`.

Code is requested via `sign_in` → if 2FA is enabled, you'll be prompted for the
cloud password separately.
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


async def main() -> int:
    print("You will be asked for API_ID, API_HASH, then your phone number.")
    print("After login, copy the printed SESSION_STRING into barabot/.env.\n")

    api_id_raw = input("API_ID: ").strip()
    api_hash = input("API_HASH: ").strip()
    if not api_id_raw.isdigit():
        print("ERROR: API_ID must be an integer", file=sys.stderr)
        return 2
    api_id = int(api_id_raw)

    phone = input("Phone (international format, e.g. +79776298495): ").strip()

    # StringSession keeps the in-memory state in `session.session` and lets us
    # export it after login. No .session file is written to disk.
    session = StringSession()
    client = TelegramClient(session, api_id, api_hash)

    await client.connect()
    try:
        # send_code_request asks Telegram to send a code to phone (SMS or app).
        sent = await client.send_code_request(phone)
        code = input(f"Code (sent to {phone}, type={'app' if sent.type == 'app' else 'sms'}): ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            # 2FA enabled → need cloud password
            password = input("2FA password: ").strip()
            await client.sign_in(password=password)
    finally:
        await client.disconnect()

    s = session.save()
    print("\nSESSION_STRING=" + s)
    print(
        "\n# NOTE: this string is the auth_key for account "
        f"{phone}. Treat it as a password; revoke via my.telegram.org "
        "if leaked.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))