# Admin Panel Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the admin panel usable — fix the crash that makes stock impossible to add, give every screen a way out, replace typed input with buttons, drop the dead Payments screen, stop fabricating an "Uncategorized" folder, and add CSV bulk import plus per-product edit.

**Architecture:** Handler-layer work in `app/bot/handlers/admin/`, following aiogram 3 Router + FSM patterns already used by the broadcast wizard. One new service module (`product_import.py`) keeps CSV parsing separate from Telegram so it is testable without a bot. One migration makes `products.category_id` nullable.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy 2 (async), Alembic, pytest + pytest-asyncio.

## Global Constraints

- Button styles come only from `app/bot/keyboards/styles.py` — `PRIMARY` (blue), `SUCCESS` (green), `DANGER` (red), `NEUTRAL` (None). Always construct via `btn()`; it validates and rejects anything else.
- Leaving a screen is red: Back is `DANGER`, Home is `PRIMARY`. This is `nav_row`'s existing convention.
- Telegram `callback_data` is capped at **64 bytes**. Use short field codes, never full words.
- Admin screens hardcode locale `"en"` (existing convention, e.g. `payments.py:85`).
- Migration head is `0011`. The new migration is `0012`, `down_revision = "0011"`.
- CSV import: **no `stock` column**, by decision. Caps are 5000 rows and 1 MB.
- Tests: `asyncio_mode = "auto"`, `testpaths = ["tests"]`. Run with `python -m pytest`.
- Unit tests call render helpers directly with monkeypatched repos (see `tests/unit/test_admin_payments_screen.py`). Handler/FSM tests feed real `Update`s through the shared session `dispatcher` fixture (see `tests/unit/test_broadcast_back.py`).
- Integration tests use the `sqlite_sessionmaker` fixture — in-memory SQLite built from `Base.metadata.create_all`, not from Alembic.

## File Structure

| File | Responsibility |
|---|---|
| `app/services/catalog_service.py` | MODIFY — `create_product` accepts `category_id=None` |
| `app/bot/handlers/admin/products.py` | MODIFY — the bulk of the work: nav, buttons, stock step, import/export, search, edit |
| `app/bot/handlers/admin/categories.py` | MODIFY — nav row, skip buttons, description |
| `app/bot/handlers/admin/users.py` | MODIFY — nav row, description |
| `app/bot/handlers/admin/orders.py` | MODIFY — nav row, description |
| `app/bot/handlers/admin/panel.py` | MODIFY — remove Payments, dedupe panel text |
| `app/bot/states/product_form.py` | MODIFY — add `stock`, `search`, `edit_value` states |
| `app/services/product_import.py` | CREATE — CSV parse + upsert, no Telegram imports |
| `app/database/models/catalog.py` | MODIFY — `category_id` nullable |
| `app/database/repositories/product_repo.py` | MODIFY — count/search/uncategorized queries |
| `app/database/migrations/versions/0012_product_category_nullable.py` | CREATE |
| `app/bot/keyboards/products.py` | MODIFY — `product_detail` handles `category_id=None` |
| `app/bot/handlers/products/browse.py` | MODIFY — render loose products above the grid |

---

## Phase 1 — Fixes and UX

### Task 1: Fix the add_stock crash

`app/bot/handlers/admin/products.py:297` calls `add_stock(session, product_id, lines, user.telegram_id)` with four positional arguments. `catalog_service.add_stock` is defined `(session, *, product_id, plaintext_payloads, added_by_admin_id)` — everything after `session` is keyword-only. Every Add Stock attempt raises `TypeError`, so no product can be given stock and therefore nothing in the shop can sell. This is the highest-priority fix in the plan.

**Files:**
- Modify: `app/bot/handlers/admin/products.py:297`
- Test: `tests/unit/test_admin_add_stock.py`

**Interfaces:**
- Consumes: `catalog_service.add_stock(session, *, product_id, plaintext_payloads, added_by_admin_id) -> int`
- Produces: nothing new.

- [x] **Step 1: Write the failing test**

`tests/unit/test_admin_add_stock.py`:

```python
from __future__ import annotations

import inspect

from app.services.catalog_service import add_stock


def test_handler_calls_add_stock_with_a_valid_signature() -> None:
    """products.py called add_stock positionally against a keyword-only signature, so every
    "Add Stock" tap raised TypeError and no product could ever be stocked. Binding the call the
    handler makes proves the arguments actually fit."""
    sig = inspect.signature(add_stock)

    # The exact call the handler makes, minus the session.
    sig.bind(
        None,
        product_id=1,
        plaintext_payloads=["KEY-1"],
        added_by_admin_id=42,
    )


def test_add_stock_rejects_positional_arguments() -> None:
    """Locks in why the bug happened, so nobody "fixes" it by loosening the signature."""
    import pytest

    sig = inspect.signature(add_stock)
    with pytest.raises(TypeError):
        sig.bind(None, 1, ["KEY-1"], 42)
```

- [x] **Step 2: Run it — the signature tests pass, but prove the handler is broken**

Run: `python -m pytest tests/unit/test_admin_add_stock.py -v`
Expected: both PASS (they describe the contract, not the bug).

Now prove the call site is wrong:

Run: `python -c "import ast,inspect; from app.services.catalog_service import add_stock; src=open('app/bot/handlers/admin/products.py',encoding='utf-8').read(); print([n.lineno for n in ast.walk(ast.parse(src)) if isinstance(n,ast.Call) and getattr(n.func,'id','')=='add_stock' and len(n.args)>1])"`
Expected: `[297]` — a call with more than one positional argument.

- [x] **Step 3: Fix the call site**

In `app/bot/handlers/admin/products.py`, replace line 297:

```python
        count = await add_stock(session, product_id, lines, user.telegram_id)
```

with:

```python
        count = await add_stock(
            session,
            product_id=product_id,
            plaintext_payloads=lines,
            added_by_admin_id=user.telegram_id,
        )
```

- [x] **Step 4: Add the regression test that would have caught it**

Append to `tests/unit/test_admin_add_stock.py`:

```python
def test_no_add_stock_call_passes_extra_positional_args() -> None:
    """A static scan, because the real call sits behind an FSM state and a Telegram Document —
    expensive to reach in a unit test, and this is the failure that actually shipped."""
    import ast
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", "")) == "add_stock"
                and len(node.args) > 1
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        f"add_stock is keyword-only after `session`; positional call(s) at {offenders} "
        f"raise TypeError at runtime."
    )
```

- [x] **Step 5: Run and verify**

Run: `python -m pytest tests/unit/test_admin_add_stock.py -v`
Expected: 3 passed.

- [x] **Step 6: Commit**

```bash
git add tests/unit/test_admin_add_stock.py app/bot/handlers/admin/products.py
git commit -m "fix: add_stock called positionally against keyword-only signature

Every Add Stock attempt raised TypeError, so no product could be stocked
and nothing in the shop could sell."
```

---

### Task 2: Back button on every admin list screen

**Files:**
- Modify: `app/bot/handlers/admin/products.py:29-52` (`_list_keyboard`)
- Modify: `app/bot/handlers/admin/categories.py:23-38` (`_list_keyboard`)
- Modify: `app/bot/handlers/admin/users.py:39-52` (`_detail_keyboard`) and `:84-88` (search results)
- Modify: `app/bot/handlers/admin/orders.py` (list keyboard)
- Test: `tests/unit/test_admin_nav.py`

**Interfaces:**
- Consumes: `app.bot.keyboards.common.nav_row(locale, *, back_target, home) -> list[InlineKeyboardButton]`
- Produces: every admin list keyboard ends with a row containing `nav:admin_panel`.

- [x] **Step 1: Write the failing test**

`tests/unit/test_admin_nav.py`:

```python
from __future__ import annotations

import pytest

from app.utils.pagination import Page


def _targets(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_product_list_offers_a_way_back() -> None:
    """These four screens were dead ends: the only way out was retyping /admin."""
    from app.bot.handlers.admin.products import _list_keyboard

    markup = _list_keyboard([], Page(page=1, page_size=10, total_items=0))

    assert "nav:admin_panel" in _targets(markup)


def test_category_list_offers_a_way_back() -> None:
    from app.bot.handlers.admin.categories import _list_keyboard

    assert "nav:admin_panel" in _targets(_list_keyboard([]))


def test_back_button_is_red() -> None:
    """nav_row's convention: leaving a screen is always red, on every screen in the bot."""
    from app.bot.handlers.admin.categories import _list_keyboard

    back = [
        b
        for row in _list_keyboard([]).inline_keyboard
        for b in row
        if b.callback_data == "nav:admin_panel"
    ]
    assert back and back[0].style == "danger"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_nav.py -v`
Expected: FAIL — `assert 'nav:admin_panel' in [...]`.

- [x] **Step 3: Add the nav row to products**

In `app/bot/handlers/admin/products.py`, add the import:

```python
from app.bot.keyboards.common import nav_row
```

and replace the final two lines of `_list_keyboard`:

```python
    rows.append([btn("➕ Add Product", AdminProductCB(action="add").pack(), PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

with:

```python
    rows.append([btn("➕ Add Product", AdminProductCB(action="add").pack(), PRIMARY)])
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

- [x] **Step 4: Add the nav row to categories**

In `app/bot/handlers/admin/categories.py`, add `from app.bot.keyboards.common import nav_row`, then in `_list_keyboard` replace:

```python
    rows.append([btn("➕ Add Category", AdminCategoryCB(action="add").pack(), PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

with:

```python
    rows.append([btn("➕ Add Category", AdminCategoryCB(action="add").pack(), PRIMARY)])
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

- [x] **Step 5: Add the nav row to users**

In `app/bot/handlers/admin/users.py`, add `from app.bot.keyboards.common import nav_row`, then change `_detail_keyboard` to:

```python
def _detail_keyboard(target) -> InlineKeyboardMarkup:
    banned = target.status == UserStatus.BANNED
    toggle = ("✅ Unban", "unban") if banned else ("🚫 Ban", "ban")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(toggle[0], AdminUserCB(action=toggle[1], id=str(target.id)).pack(), PRIMARY)],
            nav_row("en", back_target="admin_panel", home=False),
        ]
    )
```

and in `do_search`, change the multi-result keyboard:

```python
    rows = [
        [btn(f"@{u.username or u.telegram_id}", AdminUserCB(action="view", id=str(u.id)).pack(), PRIMARY)]
        for u in results
    ]
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    await message.answer(f"Found {len(results)} users:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
```

The search *prompt* at `prompt_search` sends no keyboard at all. Give it one:

```python
@router.callback_query(AdminMiscCB.filter(F.action == "users"))
async def prompt_search(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSearchForm.query)
    await query.message.edit_text(
        "👥 <b>USERS</b>\n\n"
        "Look up any account to inspect or moderate it.\n\n"
        "Send a <b>Telegram ID</b> (e.g. <code>123456789</code>) or a <b>@username</b>.\n"
        "You'll see their order count, wallet balance, and signup date, with a Ban/Unban control.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[nav_row("en", back_target="admin_panel", home=False)]
        ),
    )
    await query.answer()
```

- [x] **Step 6: Add the nav row to orders**

In `app/bot/handlers/admin/orders.py`, add `from app.bot.keyboards.common import nav_row` and append `nav_row("en", back_target="admin_panel", home=False)` as the final row of the **list** keyboard. The detail keyboard already has a Back to the list at line 44 — leave it.

- [x] **Step 7: Run tests**

Run: `python -m pytest tests/unit/test_admin_nav.py -v`
Expected: 3 passed.

Run: `python -m pytest -q`
Expected: no new failures.

- [x] **Step 8: Commit**

```bash
git add tests/unit/test_admin_nav.py app/bot/handlers/admin/
git commit -m "fix: admin list screens were dead ends with no Back button"
```

---

### Task 3: Back and Abort on every wizard step

**Files:**
- Modify: `app/bot/handlers/admin/products.py` (product wizard)
- Modify: `app/bot/handlers/admin/categories.py` (category wizard)
- Test: `tests/unit/test_admin_wizard_nav.py`

**Interfaces:**
- Produces: `products._step_keyboard(step: str, *, extra: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup` — every wizard screen's keyboard. Callback data `pback:<step>` and `pabort`.
- `_PRODUCT_STEPS: tuple[str, ...] = ("category", "name", "description", "price", "fulfillment_mode", "warranty_days", "delivery_info", "stock")` — the step order Back walks backwards through.

- [x] **Step 1: Write the failing test**

`tests/unit/test_admin_wizard_nav.py`:

```python
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.states.product_form import ProductForm
from app.core.config import get_settings

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]


def _context(dispatcher: Dispatcher) -> FSMContext:
    key = StorageKey(bot_id=BOT_ID, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    return FSMContext(storage=dispatcher.storage, key=key)


def _callback(data: str, bot) -> Update:
    tg = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    message = Message(
        message_id=10,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=tg,
        text="wizard",
    ).as_(bot)
    return Update(
        update_id=1,
        callback_query=CallbackQuery(id="1", from_user=tg, chat_instance="x", message=message, data=data),
    )


def test_every_wizard_step_keyboard_has_back_and_abort() -> None:
    from app.bot.handlers.admin.products import _PRODUCT_STEPS, _step_keyboard

    for step in _PRODUCT_STEPS:
        targets = [b.callback_data for row in _step_keyboard(step).inline_keyboard for b in row]
        assert f"pback:{step}" in targets, f"{step} has no Back"
        assert "pabort" in targets, f"{step} has no Abort"


@pytest.mark.asyncio
async def test_back_returns_to_previous_step_keeping_answers(dispatcher: Dispatcher) -> None:
    """Back must not throw away what was already typed — retyping the name because you wanted to
    change the price is the whole complaint."""
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.price)
    await ctx.update_data(category_id=1, name="Kiro Pro", description="desc")

    result = await dispatcher.feed_update(bot, _callback("pback:price", bot), session=None, user=None)

    assert result is not UNHANDLED, "no handler matched pback:price"
    assert await ctx.get_state() == ProductForm.description
    data = await ctx.get_data()
    assert data["name"] == "Kiro Pro", "Back must preserve earlier answers"


@pytest.mark.asyncio
async def test_back_on_the_first_step_aborts(dispatcher: Dispatcher) -> None:
    """There is no step before the first one, so Back exits rather than doing nothing."""
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.category)
    await ctx.update_data(category_id=1)

    result = await dispatcher.feed_update(bot, _callback("pback:category", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() is None, "first-step Back clears state like Abort"


@pytest.mark.asyncio
async def test_abort_clears_state(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.warranty_days)
    await ctx.update_data(name="Kiro Pro")

    result = await dispatcher.feed_update(bot, _callback("pabort", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() is None
    assert await ctx.get_data() == {}
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_wizard_nav.py -v`
Expected: FAIL — `ImportError: cannot import name '_step_keyboard'`.

- [x] **Step 3: Add the step keyboard and step order**

In `app/bot/handlers/admin/products.py`, after the imports:

```python
from aiogram.types import InlineKeyboardButton

from app.bot.keyboards.styles import DANGER

# The wizard's step order. Back walks one entry left; Abort leaves entirely. Keeping the order in
# one tuple means a new step cannot be added without also being reachable by Back.
_PRODUCT_STEPS: tuple[str, ...] = (
    "category",
    "name",
    "description",
    "price",
    "fulfillment_mode",
    "warranty_days",
    "delivery_info",
    "stock",
)

_STEP_STATES = {
    "category": ProductForm.category,
    "name": ProductForm.name,
    "description": ProductForm.description,
    "price": ProductForm.price,
    "fulfillment_mode": ProductForm.fulfillment_mode,
    "warranty_days": ProductForm.warranty_days,
    "delivery_info": ProductForm.delivery_info,
    "stock": ProductForm.stock,
}


def _step_keyboard(
    step: str, *, extra: list[list[InlineKeyboardButton]] | None = None
) -> InlineKeyboardMarkup:
    """Every wizard screen ends with [🔙 Back] [❌ Abort]. Red on both: they are the two ways to
    leave, and nav_row already established that leaving is red everywhere else in the bot."""
    rows = list(extra or [])
    rows.append(
        [
            btn("🔙 Back", f"pback:{step}", DANGER),
            btn("❌ Abort", "pabort", DANGER),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

- [x] **Step 4: Add the Back and Abort handlers**

Add to `app/bot/handlers/admin/products.py`:

```python
@router.callback_query(F.data == "pabort")
async def abort_wizard(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, markup = await _render_list(session, 1)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Cancelled.")


@router.callback_query(F.data.startswith("pback:"))
async def back_one_step(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Back reopens the previous step with the collected data untouched. On the first step there
    is nothing to go back to, so it behaves as Abort rather than silently doing nothing."""
    step = query.data.removeprefix("pback:")
    if step not in _PRODUCT_STEPS or _PRODUCT_STEPS.index(step) == 0:
        await abort_wizard(query, state, session)
        return

    previous = _PRODUCT_STEPS[_PRODUCT_STEPS.index(step) - 1]
    await _show_step(query.message, state, previous, session)
    await query.answer()
```

- [x] **Step 5: Add the single step renderer**

Every step is drawn by one function so Back and forward navigation cannot render different screens for the same step:

```python
async def _show_step(message: Message, state: FSMContext, step: str, session: AsyncSession) -> None:
    """One renderer per step, shared by forward progress and by Back."""
    await state.set_state(_STEP_STATES[step])

    if step == "category":
        categories = await CategoryRepo(session).list_active()
        rows = [[btn(f"{c.emoji or '📦'} {c.name}", f"pickcat:{c.id}", PRIMARY)] for c in categories]
        rows.append([btn("🚫 No category", "pickcat:none", PRIMARY)])
        await message.edit_text(
            "➕ <b>Add Product</b> — step 1 of 8\n\nChoose a category, or file it outside them:",
            reply_markup=_step_keyboard("category", extra=rows),
        )
        return

    if step == "name":
        await message.edit_text(
            "➕ <b>Add Product</b> — step 2 of 8\n\nSend the product name (1-128 characters):",
            reply_markup=_step_keyboard("name"),
        )
        return

    if step == "description":
        await message.edit_text(
            "➕ <b>Add Product</b> — step 3 of 8\n\n"
            "Send a description buyers will see on the product page, or skip it:",
            reply_markup=_step_keyboard(
                "description", extra=[[btn("⏭️ Skip", "pskip:description", PRIMARY)]]
            ),
        )
        return

    if step == "price":
        await message.edit_text(
            "➕ <b>Add Product</b> — step 4 of 8\n\n"
            "Send the price, e.g. <code>9.99</code>.\n"
            "USD is assumed unless you write the currency: <code>9.99 EUR</code>",
            reply_markup=_step_keyboard("price"),
        )
        return

    if step == "fulfillment_mode":
        await message.edit_text(
            "➕ <b>Add Product</b> — step 5 of 8\n\n"
            "How does this product get delivered?\n\n"
            "⚡ <b>Auto</b> — a stock item (licence key, account) is sent the moment payment clears.\n"
            "🙋 <b>Manual</b> — the order lands in your queue and you fulfil it yourself.",
            reply_markup=_step_keyboard(
                "fulfillment_mode",
                extra=[[btn("⚡ Auto", "pmode:auto", PRIMARY), btn("🙋 Manual", "pmode:manual", PRIMARY)]],
            ),
        )
        return

    if step == "warranty_days":
        await message.edit_text(
            "➕ <b>Add Product</b> — step 6 of 8\n\n"
            "How long can a buyer file a warranty claim after purchase?",
            reply_markup=_step_keyboard(
                "warranty_days",
                extra=[
                    [
                        btn("None", "pwar:0", PRIMARY),
                        btn("7d", "pwar:7", PRIMARY),
                        btn("30d", "pwar:30", PRIMARY),
                    ],
                    [
                        btn("90d", "pwar:90", PRIMARY),
                        btn("365d", "pwar:365", PRIMARY),
                        btn("✏️ Custom", "pwar:custom", PRIMARY),
                    ],
                ],
            ),
        )
        return

    if step == "delivery_info":
        await message.edit_text(
            "➕ <b>Add Product</b> — step 7 of 8\n\n"
            "Instructions shown to the buyer after purchase — where to redeem, how to log in. "
            "Skip if the stock item speaks for itself:",
            reply_markup=_step_keyboard(
                "delivery_info", extra=[[btn("⏭️ Skip", "pskip:delivery_info", PRIMARY)]]
            ),
        )
        return

    if step == "stock":
        await message.edit_text(
            "➕ <b>Add Product</b> — step 8 of 8\n\n"
            "Send the stock items — licence keys or account credentials, <b>one per line</b>. "
            "They are encrypted before they touch the database.\n\n"
            "Skip to create the product OUT OF STOCK and add them later.",
            reply_markup=_step_keyboard("stock", extra=[[btn("⏭️ Skip", "pskip:stock", PRIMARY)]]),
        )
        return
```

- [x] **Step 6: Point the existing entry point at the renderer**

Replace `start_add` in `app/bot/handlers/admin/products.py`:

```python
@router.callback_query(AdminProductCB.filter(F.action == "add"))
async def start_add(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await _show_step(query.message, state, "category", session)
    await query.answer()
```

- [x] **Step 7: Mirror Back/Abort in the category wizard**

Apply the same treatment in `app/bot/handlers/admin/categories.py` with `_CATEGORY_STEPS = ("name", "emoji", "description")`, callbacks `cback:<step>` and `cabort`, and a `_cat_step_keyboard` built the same way. Abort returns to the category list.

- [x] **Step 8: Run tests**

Run: `python -m pytest tests/unit/test_admin_wizard_nav.py -v`
Expected: 4 passed.

- [x] **Step 9: Commit**

```bash
git add tests/unit/test_admin_wizard_nav.py app/bot/handlers/admin/products.py app/bot/handlers/admin/categories.py
git commit -m "feat: Back and Abort on every admin wizard step"
```

---

### Task 4: Buttons instead of typed input

**Files:**
- Modify: `app/bot/handlers/admin/products.py`
- Modify: `app/bot/handlers/admin/categories.py`
- Test: `tests/unit/test_admin_wizard_buttons.py`

**Interfaces:**
- Consumes: `_show_step`, `_step_keyboard` from Task 3.
- Produces: callbacks `pmode:auto|manual`, `pwar:<int>|custom`, `pskip:description|delivery_info|stock`, `pickcat:<id>|none`.

- [x] **Step 1: Write the failing test**

`tests/unit/test_admin_wizard_buttons.py`:

```python
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.states.product_form import ProductForm
from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]


def _context(dispatcher: Dispatcher) -> FSMContext:
    return FSMContext(storage=dispatcher.storage, key=StorageKey(bot_id=BOT_ID, chat_id=ADMIN_ID, user_id=ADMIN_ID))


def _callback(data: str, bot) -> Update:
    tg = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    message = Message(
        message_id=10,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=tg,
        text="wizard",
    ).as_(bot)
    return Update(
        update_id=1,
        callback_query=CallbackQuery(id="1", from_user=tg, chat_instance="x", message=message, data=data),
    )


@pytest.mark.asyncio
async def test_auto_button_sets_the_same_data_typing_auto_would(dispatcher: Dispatcher) -> None:
    """Typing 'auto' was the complaint. The button must write identical FSM data, or the two paths
    silently produce different products."""
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.fulfillment_mode)

    result = await dispatcher.feed_update(bot, _callback("pmode:auto", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert (await ctx.get_data())["fulfillment_mode"] == FulfillmentMode.AUTO
    assert await ctx.get_state() == ProductForm.warranty_days


@pytest.mark.asyncio
async def test_warranty_preset_button_sets_days(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.warranty_days)

    result = await dispatcher.feed_update(bot, _callback("pwar:30", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert (await ctx.get_data())["warranty_days"] == 30


@pytest.mark.asyncio
async def test_custom_warranty_stays_on_the_step_for_typing(dispatcher: Dispatcher) -> None:
    """Custom must not invent a number — it waits for the admin to type one."""
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.warranty_days)

    result = await dispatcher.feed_update(bot, _callback("pwar:custom", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() == ProductForm.warranty_days
    assert "warranty_days" not in await ctx.get_data()


@pytest.mark.asyncio
async def test_typing_auto_still_works(dispatcher: Dispatcher) -> None:
    """The typed handlers stay as a fallback — an admin with the old habit is not punished."""
    from app.bot.handlers.admin import products

    assert any(
        getattr(h.callback, "__name__", "") == "set_fulfillment"
        for h in products.router.message.handlers
    ), "the typed auto/manual handler must remain registered"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_wizard_buttons.py -v`
Expected: FAIL — `pmode:auto` is UNHANDLED.

- [x] **Step 3: Implement the button handlers**

Add to `app/bot/handlers/admin/products.py`:

```python
@router.callback_query(F.data.startswith("pickcat:"), ProductForm.category)
async def pick_category(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """"none" stores a real NULL. The old branch here fabricated an "Uncategorized" Category row,
    which then showed up as a folder in the buyer-facing store."""
    raw = query.data.removeprefix("pickcat:")
    await state.update_data(category_id=None if raw == "none" else int(raw))
    await _show_step(query.message, state, "name", session)
    await query.answer()


@router.callback_query(F.data.startswith("pmode:"), ProductForm.fulfillment_mode)
async def pick_mode(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    mode = FulfillmentMode.AUTO if query.data.endswith("auto") else FulfillmentMode.MANUAL
    await state.update_data(fulfillment_mode=mode)
    await _show_step(query.message, state, "warranty_days", session)
    await query.answer()


@router.callback_query(F.data.startswith("pwar:"), ProductForm.warranty_days)
async def pick_warranty(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    raw = query.data.removeprefix("pwar:")
    if raw == "custom":
        await query.message.edit_text(
            "➕ <b>Add Product</b> — step 6 of 8\n\nSend the warranty length in whole days:",
            reply_markup=_step_keyboard("warranty_days"),
        )
        await query.answer()
        return
    await state.update_data(warranty_days=int(raw))
    await _show_step(query.message, state, "delivery_info", session)
    await query.answer()


@router.callback_query(F.data.startswith("pskip:"))
async def skip_step(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    field = query.data.removeprefix("pskip:")
    if field == "description":
        await state.update_data(description=None)
        await _show_step(query.message, state, "price", session)
    elif field == "delivery_info":
        await state.update_data(delivery_info=None)
        await _show_step(query.message, state, "stock", session)
    elif field == "stock":
        await _finish_product(query.message, state, session, stock_lines=[], admin_id=query.from_user.id)
    await query.answer()
```

- [x] **Step 4: Keep the typed handlers, and route them through `_show_step`**

Each existing `@router.message(ProductForm.X)` handler keeps its validation, but its final two lines change from `set_state` + `answer` to `await _show_step(message, state, "<next>", session)`. Add `session: AsyncSession` to any of these handlers that lacks it. Do not delete them — they are the fallback the fourth test asserts on.

- [x] **Step 5: Add Skip buttons to the category wizard**

In `app/bot/handlers/admin/categories.py`, the `emoji` and `description` steps get `[⏭️ Skip]` buttons with callbacks `cskip:emoji` and `cskip:description`, handled the same way. The typed `skip` handlers stay.

- [x] **Step 6: Run tests**

Run: `python -m pytest tests/unit/test_admin_wizard_buttons.py -v`
Expected: 4 passed.

- [x] **Step 7: Commit**

```bash
git add tests/unit/test_admin_wizard_buttons.py app/bot/handlers/admin/
git commit -m "feat: tap auto/manual, warranty presets and skip instead of typing them"
```

---

### Task 5: Real descriptions on admin sub-screens

**Files:**
- Modify: `app/bot/handlers/admin/products.py` (`_render_list`)
- Modify: `app/bot/handlers/admin/categories.py` (`list_categories`)
- Modify: `app/bot/handlers/admin/orders.py`, `logs.py`, `gifts.py` headers
- Test: `tests/unit/test_admin_screen_copy.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new.

- [x] **Step 1: Write the failing test**

`tests/unit/test_admin_screen_copy.py`:

```python
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_product_screen_explains_what_the_buttons_do(monkeypatch) -> None:
    """The whole body used to be "5 products total." — the panel above it documents every button,
    and the screens below it documented nothing."""
    import app.bot.handlers.admin.products as products

    async def _fake_counts(_session, **_kw):
        return 0

    monkeypatch.setattr(products, "_count_products", _fake_counts, raising=False)

    text, _ = await products._render_list(None, 1)

    assert "Add Product" in text
    assert "Import CSV" in text or "import" in text.lower()
    assert len(text) > 200, "a one-line screen is the defect being fixed"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_screen_copy.py -v`
Expected: FAIL — the text is short and mentions no buttons.

- [x] **Step 3: Write the copy**

In `app/bot/handlers/admin/products.py`, `_render_list` builds:

```python
    text = (
        "📦 <b>PRODUCT MANAGEMENT</b>\n\n"
        f"{total} products · {in_stock} in stock · {out_of_stock} out of stock\n\n"
        "Tap any product to see its stock, price and status, or to edit, disable or delete it.\n\n"
        "<b>Buttons:</b>\n"
        "➕ <b>Add Product</b> — walk through creating one product, stock included\n"
        "📥 <b>Import CSV</b> — upload a file to create or update products in bulk\n"
        "📤 <b>Export CSV</b> — download every product in the same format, edit it, re-upload\n"
        "🔍 <b>Search</b> — filter this list by name\n\n"
        "🟢 = active and visible to buyers · ⚫ = disabled and hidden"
    )
```

`in_stock` / `out_of_stock` come from the counts added in Task 12. Until that task lands, use `total` only and drop the stock line — do not leave a placeholder.

- [x] **Step 4: Write the same style of copy for categories, orders, logs and gifts**

Each screen states what it is for, what each button does, and what the status markers mean. Match the main panel's voice.

- [x] **Step 5: Run tests and commit**

Run: `python -m pytest tests/unit/test_admin_screen_copy.py -v`
Expected: PASS.

```bash
git add tests/unit/test_admin_screen_copy.py app/bot/handlers/admin/
git commit -m "feat: admin sub-screens explain what they do and what each button does"
```

---

### Task 6: Remove the Payments button and dedupe the panel text

**Files:**
- Modify: `app/bot/handlers/admin/panel.py:26-99`
- Test: `tests/unit/test_admin_panel_screen.py`

**Interfaces:**
- Produces: `panel._PANEL_TEXT: str` — the single copy of the panel body, used by both handlers.

- [x] **Step 1: Write the failing test**

`tests/unit/test_admin_panel_screen.py`:

```python
from __future__ import annotations

from app.bot.handlers.admin.panel import _PANEL_TEXT, _panel_keyboard


def test_panel_has_no_payments_button() -> None:
    """Payments opened the *manual* top-up queue. Top-ups are automatic crypto now, and the screen
    itself concedes an empty list is the healthy state."""
    targets = [b.callback_data for row in _panel_keyboard("en").inline_keyboard for b in row]

    assert not any(t.startswith("apay") for t in targets), "the dead Payments button is still here"


def test_panel_text_no_longer_advertises_payments() -> None:
    assert "Payments" not in _PANEL_TEXT


def test_panel_text_exists_exactly_once_in_source() -> None:
    """The body was pasted verbatim into both the command handler and the nav handler. Two copies
    drift; this keeps there being one."""
    import pathlib

    source = pathlib.Path(_panel_keyboard.__globals__["__file__"]).read_text(encoding="utf-8")

    assert source.count("Quick Commands") == 1, "the panel body must live in one constant"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_panel_screen.py -v`
Expected: FAIL — `ImportError: cannot import name '_PANEL_TEXT'`.

- [x] **Step 3: Extract the constant and drop Payments**

In `app/bot/handlers/admin/panel.py`, add above `_panel_keyboard`:

```python
# One copy, used by both the /admin command and the nav callback. It previously existed as two
# verbatim paste-ups that could drift apart on any edit.
_PANEL_TEXT = (
    "🛡️ <b>Admin Panel</b>\n\n"
    "<b>Quick Commands:</b>\n"
    "/adjust_balance — manually credit/debit a user's wallet\n"
    "/open_tickets — view the support queue and respond to tickets\n\n"
    "<b>Buttons:</b>\n"
    "📊 <b>Dashboard</b> — view analytics, sales stats, and user activity\n"
    "📦 <b>Products</b> — manage inventory and product listings\n"
    "📁 <b>Categories</b> — organize and edit product categories\n"
    "👥 <b>Users</b> — view and manage user accounts\n"
    "🛒 <b>Orders</b> — track and manage customer orders\n"
    "🎁 <b>Gift Codes</b> — create and manage gift code campaigns\n"
    "📢 <b>Broadcast</b> — send messages to all users\n"
    "⚙️ <b>Settings</b> — configure bot settings and preferences\n"
    "📝 <b>Logs</b> — view system logs and audit events"
)
```

Remove the `💰 Payments` button from `_panel_keyboard`, leaving Gift Codes alone on its row, and drop the now-unused `AdminPaymentCB` import. Both handlers become `await ... (_PANEL_TEXT, reply_markup=_panel_keyboard(user.locale))`.

Leave `app/bot/handlers/admin/payments.py` and its router registration in place — the approve/reject handlers stay reachable by callback if a manual provider ever returns. Only the panel button goes.

- [x] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_admin_panel_screen.py tests/unit/test_admin_payments_screen.py -v`
Expected: all pass — the payments screen tests still pass because the module is untouched.

- [x] **Step 5: Commit**

```bash
git add tests/unit/test_admin_panel_screen.py app/bot/handlers/admin/panel.py
git commit -m "feat: drop the dead Payments button, dedupe the panel body"
```

---

## Phase 2 — Structural

### Task 7: Make category optional

**Files:**
- Create: `app/database/migrations/versions/0012_product_category_nullable.py`
- Modify: `app/database/models/catalog.py:48`
- Modify: `app/services/catalog_service.py:65-93`
- Modify: `app/database/repositories/product_repo.py`
- Test: `tests/integration/test_uncategorized_products.py`

**Interfaces:**
- Produces:
  - `Product.category_id: Mapped[int | None]`
  - `create_product(..., category_id: int | None, ...) -> int`
  - `ProductRepo.list_uncategorized(*, offset: int = 0, limit: int = 50, active_only: bool = True) -> list[Product]`
  - `ProductRepo.count_uncategorized(*, active_only: bool = True) -> int`

- [x] **Step 1: Write the failing test**

`tests/integration/test_uncategorized_products.py`:

```python
from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product


async def test_a_product_can_exist_with_no_category(sqlite_sessionmaker) -> None:
    """Choosing "Uncategorized" used to manufacture a real Category row, which then appeared as a
    folder to buyers. A product with no category must simply have none."""
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session,
            category_id=None,
            name="Loose Product",
            description=None,
            price_minor=999,
            currency="USD",
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0,
            delivery_info=None,
            image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        assert product is not None
        assert product.category_id is None


async def test_uncategorized_products_are_listed_and_counted(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        for name in ("Loose A", "Loose B"):
            await create_product(
                session,
                category_id=None,
                name=name,
                description=None,
                price_minor=100,
                currency="USD",
                fulfillment_mode=FulfillmentMode.AUTO,
                warranty_days=0,
                delivery_info=None,
                image_file_id=None,
            )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        repo = ProductRepo(session)
        assert await repo.count_uncategorized(active_only=False) == 2
        names = {p.name for p in await repo.list_uncategorized(active_only=False)}
        assert names == {"Loose A", "Loose B"}


def test_migration_0012_chains_from_head() -> None:
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "app/database/migrations/versions/0012_product_category_nullable.py"
    )
    source = path.read_text(encoding="utf-8")

    assert 'revision = "0012"' in source
    assert 'down_revision = "0011"' in source
    assert "def upgrade()" in source and "def downgrade()" in source
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_uncategorized_products.py -v`
Expected: FAIL — `IntegrityError: NOT NULL constraint failed: products.category_id`.

- [x] **Step 3: Make the column nullable in the model**

In `app/database/models/catalog.py`, line 48:

```python
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), index=True, nullable=True
    )
```

- [x] **Step 4: Widen the service signature**

In `app/services/catalog_service.py`, change `create_product`'s parameter to `category_id: int | None`. The body already passes it straight through to `ProductRepo.create` — no other change needed.

- [x] **Step 5: Add the repo queries**

In `app/database/repositories/product_repo.py`:

```python
    async def list_uncategorized(
        self, *, offset: int = 0, limit: int = 50, active_only: bool = True
    ) -> list[Product]:
        """Products filed outside every category. They render above the folders in the store."""
        stmt = select(Product).where(Product.category_id.is_(None))
        if active_only:
            stmt = stmt.where(Product.is_active.is_(True))
        stmt = stmt.order_by(Product.name).offset(offset).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_uncategorized(self, *, active_only: bool = True) -> int:
        stmt = select(func.count()).select_from(Product).where(Product.category_id.is_(None))
        if active_only:
            stmt = stmt.where(Product.is_active.is_(True))
        return int((await self._session.execute(stmt)).scalar_one())
```

Add `from sqlalchemy import func, select` if not already imported, and check the session attribute name matches the file's existing convention.

- [x] **Step 6: Write the migration**

`app/database/migrations/versions/0012_product_category_nullable.py`:

```python
"""Let a product exist outside every category.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("category_id", existing_type=sa.BigInteger(), nullable=True)

    # The old admin flow fabricated a real "Uncategorized" Category the first time anyone picked
    # it, and it then showed up as a folder in the buyer-facing store. Empty its products out to
    # NULL and delete it. A no-op on any install where nobody ever picked that option.
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT id FROM categories WHERE slug = 'uncategorized'")
    ).fetchone()
    if row is not None:
        conn.execute(
            sa.text("UPDATE products SET category_id = NULL WHERE category_id = :cid"),
            {"cid": row[0]},
        )
        conn.execute(sa.text("DELETE FROM categories WHERE id = :cid"), {"cid": row[0]})


def downgrade() -> None:
    # Recreate the folder and refile everything loose into it, because the column cannot go back
    # to NOT NULL while any row holds a NULL.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO categories (name, slug, sort_order, is_active) "
            "VALUES ('Uncategorized', 'uncategorized', 9999, 1)"
        )
    )
    row = conn.execute(sa.text("SELECT id FROM categories WHERE slug = 'uncategorized'")).fetchone()
    conn.execute(
        sa.text("UPDATE products SET category_id = :cid WHERE category_id IS NULL"),
        {"cid": row[0]},
    )

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("category_id", existing_type=sa.BigInteger(), nullable=False)
```

- [x] **Step 7: Run tests**

Run: `python -m pytest tests/integration/test_uncategorized_products.py -v`
Expected: 3 passed.

Run: `python -m pytest -q`
Expected: no new failures.

- [x] **Step 8: Commit**

```bash
git add app/database/ app/services/catalog_service.py tests/integration/test_uncategorized_products.py
git commit -m "feat: products can exist outside every category

Drops the fabricated Uncategorized folder that leaked into the buyer store."
```

---

### Task 8: Show loose products above the category folders

**Files:**
- Modify: `app/bot/handlers/products/browse.py:25-29` (`render_categories`)
- Modify: `app/bot/keyboards/products.py:27-43` (`category_grid`), `:79-83` (`product_detail`)
- Test: `tests/unit/test_store_loose_products.py`

**Interfaces:**
- Consumes: `ProductRepo.list_uncategorized`, `ProductRepo.count_uncategorized` from Task 7.
- Produces: `category_grid(categories, locale, *, loose: list[Product] | None = None)`.

- [x] **Step 1: Write the failing test**

`tests/unit/test_store_loose_products.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from app.bot.keyboards.products import category_grid, product_detail


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_loose_products_render_above_the_folders() -> None:
    """Asked for explicitly: a product with no category belongs outside the folders, not inside a
    fake one."""
    loose = [SimpleNamespace(id=1, name="Kiro Pro", price_minor=999, currency="USD")]
    categories = [SimpleNamespace(id=5, name="Software", emoji="💾")]

    labels = _labels(category_grid(categories, "en", loose=loose))

    assert labels.index("📦 Kiro Pro — $9.99") < labels.index("💾 Software")


def test_no_divider_when_there_are_no_loose_products() -> None:
    labels = _labels(category_grid([SimpleNamespace(id=5, name="Software", emoji="💾")], "en", loose=[]))

    assert not any("─" in label for label in labels)


def test_product_detail_back_target_survives_a_null_category() -> None:
    """nav.py does int(target.removeprefix("cat-")), so a None category id would produce
    "cat-None" and raise ValueError the moment a buyer pressed Back."""
    product = SimpleNamespace(id=1, name="Kiro Pro", price_minor=999, currency="USD", category_id=None)
    view = SimpleNamespace(display_status=SimpleNamespace(value="IN_STOCK"), available_stock=3, product=product)

    targets = [
        b.callback_data
        for row in product_detail(product, view, "en", None).inline_keyboard
        for b in row
        if b.callback_data
    ]

    assert "nav:cat-None" not in targets
    assert "nav:categories" in targets
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_store_loose_products.py -v`
Expected: FAIL — `category_grid() got an unexpected keyword argument 'loose'`.

- [x] **Step 3: Extend `category_grid`**

In `app/bot/keyboards/products.py`:

```python
def category_grid(categories: list[Category], locale: str, *, loose: list[Product] | None = None):
    rows: list[list[InlineKeyboardButton]] = []

    # Products filed outside every category sit above the folders, one per row so they read as
    # items rather than as more folders.
    for product in loose or []:
        rows.append(
            [
                btn(
                    f"📦 {product.name} — {format_minor(product.price_minor, product.currency)}",
                    ProductCB(action="view", id=str(product.id)).pack(),
                    PRIMARY,
                )
            ]
        )
    if loose:
        rows.append([btn("─────────────", "noop", NEUTRAL)])

    row: list[InlineKeyboardButton] = []
    for i, cat in enumerate(categories):
        label = f"{cat.emoji or '📦'} {cat.name}"
        style = SUCCESS if i % 2 == 0 else PRIMARY
        row.append(btn(label, CategoryCB(action="open", id=str(cat.id)).pack(), style))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    return with_nav(rows, locale, back_target="home", home=False)
```

Import `NEUTRAL` and `format_minor` if not already present.

- [x] **Step 4: Fix the null back-target**

In the same file:

```python
def product_detail(product: Product, view: ProductView, locale: str, category_id: int | None):
    rows: list[list[InlineKeyboardButton]] = []
    if view.display_status.value in ("IN_STOCK", "LOW_STOCK"):
        rows.append([btn("🛒 Buy Now", ProductCB(action="buy", id=str(product.id)).pack(), SUCCESS)])
    # nav.py parses "cat-<int>"; a product with no category has no folder to return to, so Back
    # goes to the store root instead of building an unparseable "cat-None".
    target = f"cat-{category_id}" if category_id is not None else "categories"
    return with_nav(rows, locale, back_target=target)
```

- [x] **Step 5: Feed loose products into the store screen**

In `app/bot/handlers/products/browse.py`:

```python
async def render_categories(session: AsyncSession, locale: str) -> tuple[str, object]:
    categories = await CategoryRepo(session).list_active()
    loose = await ProductRepo(session).list_uncategorized()

    if not categories and not loose:
        return (
            "🛍️ <b>STORE</b>\n\n💳 Premium products available now! Pay with crypto (💎 USDT/BNB) "
            "and get instant delivery. Browse categories or visit /products to see all available items.",
            category_grid([], locale),
        )
    return "🛍️ <b>STORE</b>\n\nChoose a category:", category_grid(categories, locale, loose=loose)
```

- [x] **Step 6: Run tests**

Run: `python -m pytest tests/unit/test_store_loose_products.py -v`
Expected: 3 passed.

- [x] **Step 7: Commit**

```bash
git add tests/unit/test_store_loose_products.py app/bot/keyboards/products.py app/bot/handlers/products/browse.py
git commit -m "feat: store lists category-less products above the folders"
```

---

### Task 9: Ask for stock inside the creation wizard

**Files:**
- Modify: `app/bot/states/product_form.py` — add `stock`
- Modify: `app/bot/handlers/admin/products.py` — `_finish_product`
- Test: `tests/integration/test_product_wizard_stock.py`

**Interfaces:**
- Consumes: `_show_step` (Task 3), `add_stock` (Task 1), `create_product` (Task 7).
- Produces: `_finish_product(message, state, session, *, stock_lines: list[str], admin_id: int) -> None`.

- [x] **Step 1: Add the state**

In `app/bot/states/product_form.py`, add `stock = State()` to `ProductForm` between `delivery_info` and `confirm`.

- [x] **Step 2: Write the failing test**

`tests/integration/test_product_wizard_stock.py`:

```python
from __future__ import annotations

from app.database.models.catalog import FulfillmentMode, ProductStatus
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import add_stock, compute_display_status, create_product


async def test_a_product_created_with_stock_is_immediately_sellable(sqlite_sessionmaker) -> None:
    """Products used to be born OUT OF STOCK and needed a second trip to Manage Stock — which was
    itself crashing. Creating with stock has to land IN_STOCK in one pass."""
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session,
            category_id=None,
            name="Kiro Pro",
            description=None,
            price_minor=999,
            currency="USD",
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0,
            delivery_info=None,
            image_file_id=None,
        )
        count = await add_stock(
            session,
            product_id=product_id,
            plaintext_payloads=["KEY-1", "KEY-2", "KEY-3"],
            added_by_admin_id=1,
        )
        await session.commit()

    assert count == 3

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)
        assert view.available_stock == 3
        assert view.display_status is not ProductStatus.OUT_OF_STOCK


async def test_skipping_stock_leaves_the_product_out_of_stock(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session,
            category_id=None,
            name="Empty Product",
            description=None,
            price_minor=999,
            currency="USD",
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0,
            delivery_info=None,
            image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)
        assert view.available_stock == 0
```

- [x] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_product_wizard_stock.py -v`
Expected: FAIL if Task 1's fix is not in — this is the same crash. With Task 1 done, it passes and documents the contract `_finish_product` must honour.

- [x] **Step 4: Implement `_finish_product`**

Replace the existing `set_delivery_info` terminal logic in `app/bot/handlers/admin/products.py`:

```python
async def _finish_product(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    stock_lines: list[str],
    admin_id: int,
) -> None:
    """The single exit from the wizard, whether stock was supplied, skipped, or never asked for."""
    data = await state.get_data()

    product_id = await create_product(
        session,
        category_id=data.get("category_id"),
        name=data["name"],
        description=data.get("description"),
        price_minor=data["price_minor"],
        currency=data["currency"],
        fulfillment_mode=data["fulfillment_mode"],
        warranty_days=data["warranty_days"],
        delivery_info=data.get("delivery_info"),
        image_file_id=None,
    )
    await session.flush()

    added = 0
    if stock_lines:
        added = await add_stock(
            session,
            product_id=product_id,
            plaintext_payloads=stock_lines,
            added_by_admin_id=admin_id,
        )
    await session.flush()
    await state.clear()

    if added:
        tail = f"✅ <b>LIVE</b> with {added} stock item{'s' if added != 1 else ''}."
    elif data["fulfillment_mode"] is FulfillmentMode.MANUAL:
        tail = "✅ <b>LIVE</b> — you fulfil each order by hand, so it needs no stock."
    else:
        tail = "⚠️ Shows <b>OUT OF STOCK</b> until you add stock from its product page."

    text, markup = await _render_list(session, 1)
    await message.edit_text(
        f"✅ Product <b>{data['name']}</b> created (id {product_id}).\n{tail}\n\n{text}",
        reply_markup=markup,
    )
```

- [x] **Step 5: Wire delivery_info into the stock step, skipping it for MANUAL**

The `delivery_info` handler (typed and skipped alike) ends with:

```python
    data = await state.get_data()
    if data["fulfillment_mode"] is FulfillmentMode.MANUAL:
        # A manual product has no stock pool — asking for keys would be a step with no answer.
        await _finish_product(message, state, session, stock_lines=[], admin_id=admin_id)
        return
    await _show_step(message, state, "stock", session)
```

Add the message handler for typed stock:

```python
@router.message(ProductForm.stock)
async def receive_wizard_stock(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if not lines:
        await message.answer("Send at least one stock item, one per line — or press Skip.")
        return
    await _finish_product(
        message, state, session, stock_lines=lines, admin_id=user.telegram_id
    )
```

Note `_finish_product` calls `edit_text`; when reached from a typed message use `message.answer` instead. Branch on whether the trigger was a callback or a message rather than assuming.

- [x] **Step 6: Run tests and commit**

Run: `python -m pytest tests/integration/test_product_wizard_stock.py -v`
Expected: 2 passed.

```bash
git add app/bot/states/product_form.py app/bot/handlers/admin/products.py tests/integration/test_product_wizard_stock.py
git commit -m "feat: product wizard asks for stock so products are born sellable"
```

---

### Task 10: CSV parser service

**Files:**
- Create: `app/services/product_import.py`
- Test: `tests/unit/test_product_import.py`

**Interfaces:**
- Produces:
  ```python
  MAX_ROWS = 5000
  MAX_BYTES = 1_000_000
  REQUIRED_COLUMNS = frozenset({"name", "price"})

  @dataclass(frozen=True)
  class ImportRow:
      line: int
      product_id: int | None
      name: str
      category: str | None      # None means "no category"
      price_minor: int
      currency: str
      mode: FulfillmentMode
      warranty_days: int
      description: str | None
      delivery_info: str | None
      is_active: bool

  @dataclass(frozen=True)
  class ParseResult:
      rows: list[ImportRow]
      errors: list[str]         # "Line 12: price "abc" is not a number"

  def parse_csv(text: str, *, default_currency: str = "USD") -> ParseResult
  ```
  `parse_csv` raises `ValueError` for a malformed header or a file over the caps — those abort the whole import rather than becoming row errors.

- [x] **Step 1: Write the failing test**

`tests/unit/test_product_import.py`:

```python
from __future__ import annotations

import pytest

from app.database.models.catalog import FulfillmentMode
from app.services.product_import import MAX_ROWS, parse_csv

HEADER = "id,name,category,price,currency,mode,warranty,description,delivery_info,active"


def test_minimal_row_uses_defaults() -> None:
    result = parse_csv(f"{HEADER}\n,Kiro Pro,,9.99,,,,,,")

    assert result.errors == []
    row = result.rows[0]
    assert row.name == "Kiro Pro"
    assert row.price_minor == 999
    assert row.currency == "USD"
    assert row.mode is FulfillmentMode.AUTO
    assert row.warranty_days == 0
    assert row.is_active is True
    assert row.category is None, "a blank category means no category, not a folder named ''"


def test_full_row_is_parsed() -> None:
    result = parse_csv(f"{HEADER}\n14,Kiro Lite,Software,4.99,EUR,manual,30,Cheap tier,Log in,no")

    row = result.rows[0]
    assert (row.product_id, row.category, row.currency) == (14, "Software", "EUR")
    assert row.mode is FulfillmentMode.MANUAL
    assert row.warranty_days == 30
    assert row.is_active is False


def test_bad_price_is_a_row_error_not_a_crash() -> None:
    """One bad row must not cost the admin the other 999."""
    result = parse_csv(f"{HEADER}\n,Good,,1.00,,,,,,\n,Bad,,abc,,,,,,")

    assert len(result.rows) == 1
    assert result.rows[0].name == "Good"
    assert len(result.errors) == 1
    assert "Line 3" in result.errors[0]
    assert "abc" in result.errors[0]


def test_missing_name_is_a_row_error() -> None:
    result = parse_csv(f"{HEADER}\n,,,1.00,,,,,,")

    assert result.rows == []
    assert "name" in result.errors[0].lower()


def test_unknown_mode_is_rejected_not_coerced() -> None:
    """Silently defaulting a typo'd mode to auto would ship instant delivery on a product the
    admin meant to fulfil by hand."""
    result = parse_csv(f"{HEADER}\n,Kiro,,1.00,,atuo,,,,")

    assert result.rows == []
    assert "atuo" in result.errors[0]


def test_missing_required_column_aborts_the_whole_file() -> None:
    with pytest.raises(ValueError, match="price"):
        parse_csv("id,name,category\n,Kiro,Software")


def test_row_cap_is_enforced() -> None:
    body = "\n".join(f",Product {i},,1.00,,,,,," for i in range(MAX_ROWS + 1))
    with pytest.raises(ValueError, match="rows"):
        parse_csv(f"{HEADER}\n{body}")


def test_excel_bom_and_semicolons_are_tolerated() -> None:
    """Excel writes a BOM, and a European locale writes semicolons. Both are what an admin
    actually uploads."""
    text = "﻿" + HEADER.replace(",", ";") + "\n;Kiro Pro;;9.99;;;;;;"

    result = parse_csv(text)

    assert result.errors == []
    assert result.rows[0].name == "Kiro Pro"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_product_import.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.product_import'`.

- [x] **Step 3: Implement the parser**

`app/services/product_import.py`:

```python
"""CSV product import. Deliberately free of aiogram imports so the parsing rules can be tested
without standing up a bot — the handler in admin/products.py only downloads the file and reports."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from app.database.models.catalog import FulfillmentMode
from app.utils.money import parse_to_minor

MAX_ROWS = 5000
MAX_BYTES = 1_000_000
REQUIRED_COLUMNS = frozenset({"name", "price"})

_TRUE = {"yes", "true", "1", "y"}
_FALSE = {"no", "false", "0", "n"}


@dataclass(frozen=True)
class ImportRow:
    line: int
    product_id: int | None
    name: str
    category: str | None
    price_minor: int
    currency: str
    mode: FulfillmentMode
    warranty_days: int
    description: str | None
    delivery_info: str | None
    is_active: bool


@dataclass(frozen=True)
class ParseResult:
    rows: list[ImportRow]
    errors: list[str]


def _blank_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def parse_csv(text: str, *, default_currency: str = "USD") -> ParseResult:
    """Row errors are collected; header and size problems raise, because those mean the file is
    not what the admin thinks it is and applying half of it would be worse than refusing."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"File is larger than {MAX_BYTES // 1000} KB.")

    text = text.lstrip("﻿")  # Excel writes a BOM.

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS - header
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}.")

    rows: list[ImportRow] = []
    errors: list[str] = []

    for offset, raw in enumerate(reader):
        line = offset + 2  # header is line 1
        if offset >= MAX_ROWS:
            raise ValueError(f"File has more than {MAX_ROWS} rows.")

        cell = {(k or "").strip().lower(): (v or "") for k, v in raw.items()}

        name = cell.get("name", "").strip()
        if not name:
            errors.append(f"Line {line}: name is required.")
            continue
        if len(name) > 128:
            errors.append(f"Line {line}: name is longer than 128 characters.")
            continue

        raw_id = cell.get("id", "").strip()
        product_id: int | None = None
        if raw_id:
            if not raw_id.isdigit():
                errors.append(f'Line {line}: id "{raw_id}" is not a number.')
                continue
            product_id = int(raw_id)

        raw_price = cell.get("price", "").strip()
        try:
            price_minor = parse_to_minor(raw_price)
        except ValueError:
            errors.append(f'Line {line}: price "{raw_price}" is not a number.')
            continue
        if price_minor <= 0:
            errors.append(f'Line {line}: price "{raw_price}" must be greater than zero.')
            continue

        raw_mode = cell.get("mode", "").strip().lower() or "auto"
        if raw_mode not in ("auto", "manual"):
            errors.append(f'Line {line}: mode "{raw_mode}" must be auto or manual.')
            continue

        raw_warranty = cell.get("warranty", "").strip() or "0"
        if not raw_warranty.isdigit():
            errors.append(f'Line {line}: warranty "{raw_warranty}" must be a whole number of days.')
            continue

        raw_active = cell.get("active", "").strip().lower() or "yes"
        if raw_active not in _TRUE | _FALSE:
            errors.append(f'Line {line}: active "{raw_active}" must be yes or no.')
            continue

        rows.append(
            ImportRow(
                line=line,
                product_id=product_id,
                name=name,
                category=_blank_to_none(cell.get("category")),
                price_minor=price_minor,
                currency=(cell.get("currency", "").strip().upper() or default_currency)[:8],
                mode=FulfillmentMode.AUTO if raw_mode == "auto" else FulfillmentMode.MANUAL,
                warranty_days=int(raw_warranty),
                description=_blank_to_none(cell.get("description")),
                delivery_info=_blank_to_none(cell.get("delivery_info")),
                is_active=raw_active in _TRUE,
            )
        )

    return ParseResult(rows=rows, errors=errors)
```

- [x] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_product_import.py -v`
Expected: 8 passed.

- [x] **Step 5: Commit**

```bash
git add app/services/product_import.py tests/unit/test_product_import.py
git commit -m "feat: CSV product import parser"
```

---

### Task 11: Apply the import, and export

**Files:**
- Modify: `app/services/product_import.py` — add `apply_rows`
- Modify: `app/bot/handlers/admin/products.py` — import/export handlers
- Modify: `app/bot/states/product_form.py` — add `ProductImportForm`
- Test: `tests/integration/test_product_import_apply.py`

**Interfaces:**
- Consumes: `ParseResult`, `ImportRow` from Task 10.
- Produces:
  ```python
  @dataclass(frozen=True)
  class ApplyReport:
      created: int
      updated: int
      errors: list[str]
      categories_created: list[str]

  async def apply_rows(session, rows: list[ImportRow]) -> ApplyReport
  def to_csv(products: list[Product], category_names: dict[int, str]) -> str
  ```

- [x] **Step 1: Write the failing test**

`tests/integration/test_product_import_apply.py`:

```python
from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product
from app.services.product_import import apply_rows, parse_csv

HEADER = "id,name,category,price,currency,mode,warranty,description,delivery_info,active"


async def test_new_names_are_created(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n,Kiro Pro,,9.99,,,,,,").rows)
        await session.commit()

    assert (report.created, report.updated) == (1, 0)


async def test_an_existing_name_is_updated_not_duplicated(sqlite_sessionmaker) -> None:
    """Upsert is the whole point — re-uploading an edited export must not double the catalogue."""
    async with sqlite_sessionmaker() as session:
        await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n,kiro pro,,19.99,,,,,,").rows)
        await session.commit()

    assert (report.created, report.updated) == (0, 1)

    async with sqlite_sessionmaker() as session:
        products = await ProductRepo(session).list_uncategorized(active_only=False)
        assert len(products) == 1
        assert products[0].price_minor == 1999, "match is case-insensitive on name"


async def test_a_stale_id_is_an_error_not_a_silent_create(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n9001,Ghost,,1.00,,,,,,").rows)
        await session.commit()

    assert report.created == 0
    assert any("9001" in e for e in report.errors)


async def test_a_missing_category_is_created_and_reported(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n,Kiro Pro,Gaming,9.99,,,,,,").rows)
        await session.commit()

    assert report.categories_created == ["Gaming"]


async def test_export_round_trips_as_a_no_op(sqlite_sessionmaker) -> None:
    """Export → edit in Excel → re-upload is the documented bulk-edit path, so an unedited export
    must update everything and create nothing."""
    from app.services.product_import import to_csv

    async with sqlite_sessionmaker() as session:
        await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        products = await ProductRepo(session).list_uncategorized(active_only=False)
        text = to_csv(products, {})

    async with sqlite_sessionmaker() as session:
        parsed = parse_csv(text)
        assert parsed.errors == []
        report = await apply_rows(session, parsed.rows)
        await session.commit()

    assert (report.created, report.updated) == (0, 1)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_product_import_apply.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_rows'`.

- [x] **Step 3: Implement `apply_rows` and `to_csv`**

Append to `app/services/product_import.py`:

```python
@dataclass(frozen=True)
class ApplyReport:
    created: int
    updated: int
    errors: list[str]
    categories_created: list[str]


async def apply_rows(session, rows: list[ImportRow]) -> ApplyReport:
    """Match order: explicit id, then unique name, else create. A stale id is an error rather than
    a create — it almost always means a hand-edited or outdated export, and quietly creating a
    duplicate is harder to notice than a reported line."""
    from sqlalchemy import func, select

    from app.database.models.catalog import Category, Product
    from app.services.catalog_service import create_category, create_product

    created = updated = 0
    errors: list[str] = []
    categories_created: list[str] = []
    category_ids: dict[str, int] = {}

    async def _category_id(name: str | None) -> int | None:
        if name is None:
            return None
        key = name.lower()
        if key in category_ids:
            return category_ids[key]
        found = (
            await session.execute(select(Category).where(func.lower(Category.name) == key))
        ).scalars().first()
        if found is None:
            new_id = await create_category(
                session, name=name, emoji=None, description=None, image_file_id=None
            )
            await session.flush()
            categories_created.append(name)
            category_ids[key] = new_id
            return new_id
        category_ids[key] = found.id
        return found.id

    for row in rows:
        target: Product | None = None

        if row.product_id is not None:
            target = await session.get(Product, row.product_id)
            if target is None:
                errors.append(f"Line {row.line}: id {row.product_id} not found.")
                continue
        else:
            matches = (
                await session.execute(
                    select(Product).where(func.lower(Product.name) == row.name.lower())
                )
            ).scalars().all()
            if len(matches) > 1:
                errors.append(
                    f'Line {row.line}: name "{row.name}" matches {len(matches)} existing products.'
                )
                continue
            target = matches[0] if matches else None

        category_id = await _category_id(row.category)

        if target is None:
            await create_product(
                session,
                category_id=category_id,
                name=row.name,
                description=row.description,
                price_minor=row.price_minor,
                currency=row.currency,
                fulfillment_mode=row.mode,
                warranty_days=row.warranty_days,
                delivery_info=row.delivery_info,
                image_file_id=None,
            )
            created += 1
        else:
            target.name = row.name
            target.category_id = category_id
            target.description = row.description
            target.price_minor = row.price_minor
            target.currency = row.currency
            target.fulfillment_mode = row.mode
            target.warranty_days = row.warranty_days
            target.delivery_info = row.delivery_info
            target.is_active = row.is_active
            updated += 1

        await session.flush()

    return ApplyReport(
        created=created, updated=updated, errors=errors, categories_created=categories_created
    )


_EXPORT_COLUMNS = (
    "id", "name", "category", "price", "currency",
    "mode", "warranty", "description", "delivery_info", "active",
)


def to_csv(products, category_names: dict[int, str]) -> str:
    """Emits exactly the import schema, so export → edit → re-upload is a clean round trip."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_EXPORT_COLUMNS)
    for p in products:
        writer.writerow(
            [
                p.id,
                p.name,
                category_names.get(p.category_id, "") if p.category_id else "",
                f"{p.price_minor / 100:.2f}",
                p.currency,
                p.fulfillment_mode.value.lower(),
                p.warranty_days,
                p.description or "",
                p.delivery_info or "",
                "yes" if p.is_active else "no",
            ]
        )
    return buffer.getvalue()
```

- [x] **Step 4: Add the FSM state**

In `app/bot/states/product_form.py`:

```python
class ProductImportForm(StatesGroup):
    document = State()
```

- [x] **Step 5: Add the Telegram handlers**

In `app/bot/handlers/admin/products.py`:

```python
@router.callback_query(AdminProductCB.filter(F.action == "import"))
async def start_import(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProductImportForm.document)
    await query.message.edit_text(
        "📥 <b>Import products from CSV</b>\n\n"
        "Upload a <code>.csv</code> file with this header:\n\n"
        "<code>id,name,category,price,currency,mode,warranty,description,delivery_info,active</code>\n\n"
        "Only <b>name</b> and <b>price</b> are required. A blank <b>category</b> files the product "
        "outside every folder; a category that does not exist yet is created.\n\n"
        "Rows with an <b>id</b> update that product. Rows without one update a product of the same "
        "name, or create it.\n\n"
        "Stock is not imported — add licence keys per product afterwards.\n\n"
        f"Limits: {MAX_ROWS} rows, {MAX_BYTES // 1000} KB.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("📤 Export current products", AdminProductCB(action="export").pack(), PRIMARY)],
                nav_row("en", back_target="admin_panel", home=False),
            ]
        ),
    )
    await query.answer()


@router.message(ProductImportForm.document, F.document)
async def receive_import(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.document.file_size and message.document.file_size > MAX_BYTES:
        await message.answer(f"❌ File is larger than {MAX_BYTES // 1000} KB.")
        return

    buffer = await message.bot.download(message.document)
    try:
        text = buffer.read().decode("utf-8")
    except UnicodeDecodeError:
        await message.answer("❌ The file is not UTF-8 text. Re-save it as CSV UTF-8.")
        return

    try:
        parsed = parse_csv(text, default_currency=get_settings().default_currency)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    report = await apply_rows(session, parsed.rows)
    await state.clear()

    lines = [
        "✅ <b>Import complete</b>",
        f"   {report.created} created · {report.updated} updated · "
        f"{len(parsed.errors) + len(report.errors)} errors",
    ]
    if report.categories_created:
        lines.append(f"   Categories created: {', '.join(report.categories_created)}")

    problems = (parsed.errors + report.errors)[:20]
    if problems:
        lines.append("")
        lines.extend(f"❌ {p}" for p in problems)
    total_problems = len(parsed.errors) + len(report.errors)
    if total_problems > 20:
        lines.append(f"…and {total_problems - 20} more.")

    await message.answer("\n".join(lines))


@router.callback_query(AdminProductCB.filter(F.action == "export"))
async def export_products(query: CallbackQuery, session: AsyncSession) -> None:
    from aiogram.types import BufferedInputFile
    from sqlalchemy import select

    from app.database.models.catalog import Category, Product

    products = list((await session.execute(select(Product).order_by(Product.id))).scalars().all())
    categories = list((await session.execute(select(Category))).scalars().all())
    text = to_csv(products, {c.id: c.name for c in categories})

    await query.message.answer_document(
        BufferedInputFile(text.encode("utf-8"), filename="products.csv"),
        caption=f"📤 {len(products)} products. Edit and re-upload via Import CSV.",
    )
    await query.answer()
```

Add `📥 Import CSV` and `📤 Export CSV` buttons to `_list_keyboard`, and the required imports (`ProductImportForm`, `parse_csv`, `apply_rows`, `to_csv`, `MAX_ROWS`, `MAX_BYTES`, `get_settings`).

- [x] **Step 6: Run tests and commit**

Run: `python -m pytest tests/integration/test_product_import_apply.py tests/unit/test_product_import.py -v`
Expected: 13 passed.

```bash
git add app/services/product_import.py app/bot/handlers/admin/products.py app/bot/states/product_form.py tests/integration/test_product_import_apply.py
git commit -m "feat: bulk product import and export via CSV"
```

---

### Task 12: Make the product list scale

**Files:**
- Modify: `app/bot/handlers/admin/products.py:73-83` (`_render_list`)
- Modify: `app/database/repositories/product_repo.py`
- Modify: `app/bot/states/product_form.py` — add `ProductSearchForm`
- Test: `tests/integration/test_product_list_scale.py`

**Interfaces:**
- Produces:
  - `ProductRepo.count_all(*, name_like: str | None = None) -> int`
  - `ProductRepo.list_page(*, offset: int, limit: int, name_like: str | None = None) -> list[Product]`

- [x] **Step 1: Write the failing test**

`tests/integration/test_product_list_scale.py`:

```python
from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product


async def _seed(sessionmaker, names: list[str]) -> None:
    async with sessionmaker() as session:
        for name in names:
            await create_product(
                session, category_id=None, name=name, description=None, price_minor=100,
                currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
                delivery_info=None, image_file_id=None,
            )
        await session.commit()


async def test_count_does_not_load_every_row(sqlite_sessionmaker) -> None:
    """_render_list used to SELECT every product into Python just to len() it, then slice ten."""
    await _seed(sqlite_sessionmaker, [f"Product {i}" for i in range(25)])

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).count_all() == 25


async def test_page_returns_only_its_slice(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, [f"Product {i:02d}" for i in range(25)])

    async with sqlite_sessionmaker() as session:
        page = await ProductRepo(session).list_page(offset=10, limit=10)
        assert len(page) == 10


async def test_search_filters_by_name(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, ["Kiro Pro", "Kiro Lite", "Other Thing"])

    async with sqlite_sessionmaker() as session:
        repo = ProductRepo(session)
        assert await repo.count_all(name_like="kiro") == 2
        names = {p.name for p in await repo.list_page(offset=0, limit=10, name_like="kiro")}
        assert names == {"Kiro Pro", "Kiro Lite"}
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_product_list_scale.py -v`
Expected: FAIL — `AttributeError: 'ProductRepo' object has no attribute 'count_all'`.

- [x] **Step 3: Add the repo queries**

```python
    async def count_all(self, *, name_like: str | None = None) -> int:
        stmt = select(func.count()).select_from(Product)
        if name_like:
            stmt = stmt.where(Product.name.ilike(f"%{name_like}%"))
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_page(
        self, *, offset: int, limit: int, name_like: str | None = None
    ) -> list[Product]:
        stmt = select(Product)
        if name_like:
            stmt = stmt.where(Product.name.ilike(f"%{name_like}%"))
        stmt = stmt.order_by(Product.id).offset(offset).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())
```

- [x] **Step 4: Rewrite `_render_list`**

```python
async def _render_list(
    session: AsyncSession, page_num: int, *, name_like: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    repo = ProductRepo(session)
    total = await repo.count_all(name_like=name_like)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    products = await repo.list_page(offset=page.offset, limit=PAGE_SIZE, name_like=name_like)

    filter_line = f"\n🔍 Filtered by “{name_like}”\n" if name_like else "\n"
    text = (
        "📦 <b>PRODUCT MANAGEMENT</b>\n\n"
        f"{total} products total.{filter_line}\n"
        "Tap any product to see its stock, price and status, or to edit, disable or delete it.\n\n"
        "<b>Buttons:</b>\n"
        "➕ <b>Add Product</b> — walk through creating one product, stock included\n"
        "📥 <b>Import CSV</b> — upload a file to create or update products in bulk\n"
        "📤 <b>Export CSV</b> — download every product in the same format, edit it, re-upload\n"
        "🔍 <b>Search</b> — filter this list by name\n\n"
        "🟢 = active and visible to buyers · ⚫ = disabled and hidden"
    )
    return text, _list_keyboard(products, page, name_like=name_like)
```

`_list_keyboard` takes `name_like` and threads it into the pagination callbacks so paging inside a filtered set keeps the filter. Because `AdminProductCB` cannot carry a free-text term within 64 bytes, the term lives in FSM data and the callback carries only the page number; `list_products` reads it back from state.

- [x] **Step 5: Add the search handler**

```python
class ProductSearchForm(StatesGroup):
    term = State()
```

```python
@router.callback_query(AdminProductCB.filter(F.action == "search"))
async def start_search(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProductSearchForm.term)
    await query.message.edit_text(
        "🔍 <b>Search products</b>\n\nSend part of a product name. Send <code>*</code> to clear "
        "the filter and see everything again.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[nav_row("en", back_target="admin_panel", home=False)]
        ),
    )
    await query.answer()


@router.message(ProductSearchForm.term)
async def apply_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    term = (message.text or "").strip()
    term = None if term == "*" else term
    await state.clear()
    await state.update_data(product_filter=term)
    text, markup = await _render_list(session, 1, name_like=term)
    await message.answer(text, reply_markup=markup)
```

- [x] **Step 6: Run tests and commit**

Run: `python -m pytest tests/integration/test_product_list_scale.py -v`
Expected: 3 passed.

```bash
git add app/database/repositories/product_repo.py app/bot/handlers/admin/products.py app/bot/states/product_form.py tests/integration/test_product_list_scale.py
git commit -m "feat: product list counts in SQL and can be searched by name"
```

---

### Task 13: Per-product edit

**Files:**
- Modify: `app/bot/handlers/admin/products.py` — `_detail_keyboard`, edit flow
- Modify: `app/bot/states/product_form.py` — add `ProductEditForm`
- Test: `tests/integration/test_product_edit.py`

**Interfaces:**
- Consumes: `AdminProductCB(action="edit", id=...)`, already documented at `callbacks.py:40`.
- Produces: callback `pedit:<field>:<product_id>` where field is one of `nm`, `pr`, `ds`, `wr`, `md`, `ct`, `dv` — short codes because `callback_data` is capped at 64 bytes.

- [x] **Step 1: Write the failing test**

`tests/integration/test_product_edit.py`:

```python
from __future__ import annotations

from app.bot.handlers.admin.products import _EDIT_FIELDS, _detail_keyboard
from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product


def test_detail_screen_offers_edit() -> None:
    """The detail screen had Add Stock / Toggle / Delete / Back and no Edit at all — a price could
    not be changed without deleting and recreating the product."""
    from types import SimpleNamespace

    product = SimpleNamespace(id=7, is_active=True)
    targets = [b.callback_data for row in _detail_keyboard(product).inline_keyboard for b in row]

    assert any(t.startswith("aprod:edit") for t in targets)


def test_every_edit_callback_fits_telegram_limit() -> None:
    """callback_data is capped at 64 bytes; a product id is unbounded in principle."""
    for code in _EDIT_FIELDS:
        data = f"pedit:{code}:{9_999_999_999}"
        assert len(data.encode("utf-8")) <= 64, f"{code} callback is too long"


async def test_editing_price_persists(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        product.price_minor = 1999
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await ProductRepo(session).get_by_id(product_id)).price_minor == 1999
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_product_edit.py -v`
Expected: FAIL — `ImportError: cannot import name '_EDIT_FIELDS'`.

- [x] **Step 3: Implement the edit flow**

In `app/bot/handlers/admin/products.py`:

```python
# Short codes, not field names: callback_data is capped at 64 bytes and has to carry the id too.
_EDIT_FIELDS: dict[str, str] = {
    "nm": "Name",
    "pr": "Price",
    "ds": "Description",
    "wr": "Warranty",
    "md": "Fulfillment mode",
    "ct": "Category",
    "dv": "Delivery info",
}
```

Add to `_detail_keyboard`, above Add Stock:

```python
            [btn("✏️ Edit", AdminProductCB(action="edit", id=str(product.id)).pack(), PRIMARY)],
```

```python
@router.callback_query(AdminProductCB.filter(F.action == "edit"))
async def choose_edit_field(query: CallbackQuery, callback_data: AdminProductCB) -> None:
    rows = [
        [btn(f"✏️ {label}", f"pedit:{code}:{callback_data.id}", PRIMARY)]
        for code, label in _EDIT_FIELDS.items()
    ]
    rows.append(
        [btn("🔙 Back", AdminProductCB(action="view", id=callback_data.id).pack(), DANGER)]
    )
    await query.message.edit_text(
        "✏️ <b>Edit product</b>\n\nWhich field?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await query.answer()
```

The per-field prompt reuses the wizard widgets: `md` shows the Auto/Manual pair, `wr` the warranty presets, `ct` the category picker including `🚫 No category`; `nm`, `pr`, `ds`, `dv` set `ProductEditForm.value` and wait for a typed reply. Saving writes the one field, flushes, and re-renders the detail screen via `view_product`.

Add to `app/bot/states/product_form.py`:

```python
class ProductEditForm(StatesGroup):
    value = State()
```

- [x] **Step 4: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add app/bot/handlers/admin/products.py app/bot/states/product_form.py tests/integration/test_product_edit.py
git commit -m "feat: edit any product field without deleting and recreating it"
```

---

## Verification

- [x] `.venv/Scripts/python.exe -m pytest -q` — 212 passed, 1 skipped (baseline was 135 passed,
      1 skipped). Note the venv python: `aiogram` is not installed in the system interpreter, so a
      bare `python -m pytest` fails at conftest import.
- [x] Migration 0012 up → down → up against a scratch SQLite database, including the data step:
      the fabricated `uncategorized` category is deleted and its products freed to NULL on the way
      up, recreated and refiled on the way down, with real categories untouched in both directions.

      **Chain caveat found during verification:** `alembic upgrade head` from an *empty* database
      is broken in this repo, independently of 0012. `0001_initial_schema.py` builds every table
      from `Base.metadata.create_all` — i.e. from the *current* models — so later migrations then
      try to add columns 0001 already created (`0007` fails with "duplicate column name:
      description" on `gift_codes`). 0012 was therefore verified against a hand-built pre-0012
      schema with the version stamped at 0011, which exercises exactly the code in 0012. Replaying
      the whole chain needs 0001 rewritten as explicit `op.create_table` calls; that is a separate
      piece of work.
- [ ] Manual smoke in Telegram — **left for the user**: create a product through the wizard using
      only buttons, with stock, and confirm it shows IN STOCK; press Back at every step; export
      CSV, edit a price, re-upload, confirm "1 updated"; confirm no "Uncategorized" folder appears
      in the store.

      The first three are covered automatically by `tests/integration/test_product_wizard_e2e.py`
      and `test_admin_csv_handlers.py`, which drive real `Update`s through the real dispatcher
      against a real database — but nothing substitutes for seeing it render on a client.

## Added beyond the plan

- `tests/integration/test_admin_no_dead_buttons.py` — feeds every `callback_data` the panel,
  product list, product detail, edit picker and both wizards emit through the dispatcher and
  asserts it routes. This found the Gift Codes list had no Back button either.
- `tests/integration/test_product_wizard_e2e.py` — drives the whole wizard button-by-button
  against a real database, which is what actually proves the reported complaint is fixed. The
  plan's wizard tests all stub the database out.
- `tests/integration/test_admin_csv_handlers.py` — the plan tested the CSV *service*; this covers
  the Telegram handler path, including export → edit → re-upload updating in place.
- Three FSM state groups (`ProductSearchForm`, `ProductImportForm`, `ProductEditForm`) added to
  `guard.py`'s `_ADMIN_STATE_GROUPS`. The plan did not mention this and an existing test caught it:
  unguarded, a demoted admin's next typed message would fall through to the support relay and be
  delivered to staff.
- `tests/unit/test_admin_guard.py::test_every_admin_fsm_state_group_is_guarded` reparsed with `ast`
  instead of a regex — a parenthesized multi-line import made it capture `"("` as a state group
  name, so it had stopped guarding anything.

## Spec Coverage

| Spec section | Task |
|---|---|
| 1.1 Back and Abort | 2, 3 |
| 1.2 Buttons instead of typing | 4 |
| 1.3 Real descriptions | 5 |
| 1.4 Remove Payments | 6 |
| 2.1 Category optional | 7 |
| 2.1 Store rendering | 8 |
| 2.2 Stock in wizard | 9 |
| 2.3 CSV import/export | 10, 11 |
| 2.4 List at scale | 12 |
| 2.5 Per-product edit | 13 |
| *(not in spec — found during planning)* | 1 — `add_stock` crash |
