"""Interactive login of a Telegram account; writes its StringSession to a 0600 file.

    python -m app.telegram.login --session-file PATH [--test-dc]

Reads TG_API_ID and TG_API_HASH from the environment, asks for the phone number, the
login code and the 2FA password, and never prints the session. Move the file's content
into the server's TG_SESSION_<NAME> variable, then delete the file.
"""

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from telethon.sessions import StringSession

from app.telegram.telethon_account import TEST_DC, make_client

ClientFactory = Callable[[StringSession, int, str], Any]


def write_secret_file(path: Path, content: str) -> None:
    """Create or replace `path` with mode 0600; the content exists nowhere else."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)  # an existing file keeps its old mode otherwise
        os.write(fd, content.encode("ascii"))
    finally:
        os.close(fd)


async def login(
    session_file: Path,
    *,
    test_dc: bool = False,
    environ: dict[str, str] | None = None,
    client_factory: ClientFactory = make_client,
    phone: Callable[[], str] = lambda: input("Phone number (international format): "),
    code: Callable[[], str] = lambda: input("Login code: "),
    password: Callable[[], str] = lambda: getpass.getpass("2FA password (if set): "),
) -> None:
    env = os.environ if environ is None else environ
    api_id, api_hash = env.get("TG_API_ID"), env.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TG_API_ID and TG_API_HASH must be set in the environment")
    session = StringSession()
    if test_dc:
        session.set_dc(*TEST_DC)
    client = client_factory(session, int(api_id), api_hash)
    await client.start(phone=phone, code_callback=code, password=password)
    try:
        write_secret_file(session_file, client.session.save())
    finally:
        await client.disconnect()
    print(f"Session saved to {session_file} (mode 0600). Do not commit or share it.")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.telegram.login", description=__doc__)
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--test-dc", action="store_true", help="use Telegram's test servers")
    args = parser.parse_args(argv)
    if args.session_file.exists() and not args.session_file.is_file():
        sys.exit(f"{args.session_file} is not a regular file")
    asyncio.run(login(args.session_file, test_dc=args.test_dc))


if __name__ == "__main__":
    main()
