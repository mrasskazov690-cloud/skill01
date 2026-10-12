"""The single active sender: a session-level PostgreSQL advisory lock.

The lock lives as long as its connection, so a crashed worker frees it at once and
nobody else can release it; a live connection therefore still holds it.
"""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

SENDER_LOCK_KEY = 0x7464_7370_0001  # arbitrary, fixed: "tdsp" + 1


class SenderLock:
    def __init__(self, engine: AsyncEngine, key: int = SENDER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: AsyncConnection | None = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    async def acquire(self) -> bool:
        """Take the lock or confirm it is still ours; False if another worker has it."""
        if self._conn is not None:
            if await self._alive(self._conn):
                return True
            await self._drop()
        conn = await self._engine.connect()
        try:
            got = (await conn.execute(select(func.pg_try_advisory_lock(self._key)))).scalar_one()
            await conn.commit()
        except BaseException:
            await conn.invalidate()
            await conn.close()
            raise
        if not got:
            await conn.close()
            return False
        self._conn = conn
        return True

    async def release(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        try:
            await conn.execute(select(func.pg_advisory_unlock(self._key)))
            await conn.commit()
        except Exception:
            await conn.invalidate()  # closing the backend frees the lock as well
        finally:
            await conn.close()

    async def _alive(self, conn: AsyncConnection) -> bool:
        try:
            await conn.execute(text("SELECT 1"))
            await conn.commit()
        except Exception:
            return False
        return True

    async def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.invalidate()
                await conn.close()
            except Exception:
                pass
