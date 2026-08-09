from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminOrderCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.bot.states.order_fulfill_form import OrderFulfillForm
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.user_repo import UserRepo
from app.services import order_service
from app.utils.errors import UserError
from app.utils.money import format_minor

router = Router(name="admin.orders")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _list_keyboard(orders: list) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"🔵 {o.order_number} — {format_minor(o.total_minor, o.currency)}",
                AdminOrderCB(action="view", id=o.id).pack(),
                PRIMARY,
            )
        ]
        for o in orders
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[btn("— none —", "noop", NEUTRAL)]])


def _detail_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Fulfill", AdminOrderCB(action="fulfill", id=order_id).pack(), PRIMARY)],
            [btn("🔴 Cancel & Refund", AdminOrderCB(action="cancel", id=order_id).pack(), PRIMARY)],
            [btn("🔙 Back", AdminOrderCB(action="list").pack(), PRIMARY)],
        ]
    )


@router.callback_query(AdminOrderCB.filter(F.action == "list"))
@router.message(Command("pending_orders"))
async def list_pending(event, session: AsyncSession) -> None:
    orders = await OrderRepo(session).list_pending_manual()
    text = f"🛒 <b>PENDING FULFILLMENT</b>\n\n{len(orders)} order(s) awaiting manual delivery."
    markup = _list_keyboard(orders)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


@router.callback_query(AdminOrderCB.filter(F.action == "view"))
async def view_order(query: CallbackQuery, callback_data: AdminOrderCB, session: AsyncSession) -> None:
    order = await OrderRepo(session).get_by_id(callback_data.id)
    if order is None:
        await query.answer("Order not found.", show_alert=True)
        return
    lines = [f"🛒 <b>{order.order_number}</b>", f"Status: {order.status.value}", ""]
    for item in order.items:
        lines.append(f"• {item.product_name} x{item.qty} — {format_minor(item.unit_price_minor, order.currency)}")
    await query.message.edit_text("\n".join(lines), reply_markup=_detail_keyboard(order.id))
    await query.answer()


@router.callback_query(AdminOrderCB.filter(F.action == "cancel"))
async def cancel_order(query: CallbackQuery, callback_data: AdminOrderCB, session: AsyncSession, user) -> None:
    try:
        await order_service.cancel_order(session, order_id=callback_data.id, reason="Cancelled by admin")
    except UserError:
        await query.answer("Can't cancel this order.", show_alert=True)
        return
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id, action="order.cancel", target_type="order", target_id=callback_data.id
    )
    await query.answer("Order cancelled and refunded.")
    orders = await OrderRepo(session).list_pending_manual()
    await query.message.edit_text(
        f"🛒 <b>PENDING FULFILLMENT</b>\n\n{len(orders)} order(s) awaiting manual delivery.",
        reply_markup=_list_keyboard(orders),
    )


@router.callback_query(AdminOrderCB.filter(F.action == "fulfill"))
async def start_fulfill(query: CallbackQuery, callback_data: AdminOrderCB, state: FSMContext) -> None:
    await state.set_state(OrderFulfillForm.payload)
    await state.update_data(order_id=callback_data.id)
    await query.message.edit_text("✅ Send the delivery content for this order (or /cancel):")
    await query.answer()


@router.message(Command("cancel"), OrderFulfillForm.payload)
async def cancel_fulfill(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(OrderFulfillForm.payload)
async def receive_fulfill_payload(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    try:
        order = await order_service.fulfill_manual_order(
            session,
            order_id=data["order_id"],
            delivery_payload=message.text or "",
            admin_telegram_id=user.telegram_id,
        )
    except UserError:
        await message.answer("This order can no longer be fulfilled (already completed/cancelled).")
        await state.clear()
        return

    await state.clear()
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id, action="order.fulfill", target_type="order", target_id=order.id
    )
    await message.answer(f"✅ Order {order.order_number} marked delivered.")

    buyer = await UserRepo(session).get_by_id(order.user_id)
    if buyer and buyer.chat_id:
        try:
            await message.bot.send_message(
                buyer.chat_id,
                f"📬 <b>Your order {order.order_number} has been delivered:</b>\n\n<code>{message.text}</code>",
            )
        except Exception:  # noqa: BLE001 — best-effort notify; buyer may have blocked the bot
            await message.answer("⚠️ Couldn't DM the buyer (they may have blocked the bot).")


