"""Row factories for tests. They flush but never commit; the caller owns the transaction."""

from itertools import count
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account
from app.domain.states import AccountKind

_seq = count(1)


async def create_account(
    session: AsyncSession, *, kind: AccountKind | str = AccountKind.OPERATOR, **overrides: Any
) -> Account:
    kind = AccountKind(kind)
    overrides.setdefault("display_name", f"{kind.value}-{next(_seq)}")
    account = Account(kind=kind.value, **overrides)
    session.add(account)
    await session.flush()
    return account


async def create_operators(session: AsyncSession, n: int, **overrides: Any) -> list[Account]:
    return [await create_account(session, kind=AccountKind.OPERATOR, **overrides) for _ in range(n)]
