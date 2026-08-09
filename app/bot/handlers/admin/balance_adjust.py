from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.is_admin import IsAdmin
from app.core.config import get_settings
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.user_repo import UserRepo
from app.services import wallet_service
from app.utils.errors import UserError
from app.utils.money import format_minor, parse_to_minor

router = Router(name="admin.balance_adjust")
router.message.filter(IsAdmin())


@router.message(Command("adjust_balance"))
async def adjust_balance(message: Message, command: CommandObject, session: AsyncSession, user) -> None:
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/adjust_balance &lt;telegram_id&gt; &lt;+amount|-amount&gt; [reason]</code>\n"
            "Example: <code>/adjust_balance 123456789 +10.00 goodwill credit</code>"
        )
        return

    telegram_id_text, amount_text, *rest = parts
    reason = rest[0] if rest else "Manual admin adjustment"

    if not telegram_id_text.isdigit():
        await message.answer("telegram_id must be numeric.")
        return

    target = await UserRepo(session).get_by_telegram_id(int(telegram_id_text))
    if target is None:
        await message.answer("No such user (they must have messaged the bot at least once).")
        return

    try:
        amount_minor = parse_to_minor(amount_text)
    except ValueError:
        await message.answer("Invalid amount. Use e.g. +10.00 or -5.00.")
        return
    if amount_minor == 0:
        await message.answer("Amount can't be zero.")
        return

    currency = get_settings().default_currency

    try:
        # Shared with the 💳 button on a user's admin profile, so both write the same transaction
        # type and the same audit-visible ref.
        txn = await wallet_service.admin_adjust(
            session,
            user_id=target.id,
            amount_minor=amount_minor,
            currency=currency,
            reason=reason,
        )
    except UserError:
        await message.answer("That user's balance can't go negative.")
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="wallet.admin_adjust",
        target_type="user",
        target_id=str(target.id),
        metadata={"amount_minor": amount_minor, "reason": reason},
    )

    await message.answer(
        f"✅ Adjusted balance for {telegram_id_text} by {format_minor(amount_minor, currency)}.\n"
        f"New balance: {format_minor(txn.balance_after_minor, currency)}"
    )
