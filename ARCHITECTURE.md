# Premium Telegram Digital-Product Store Bot — Architecture

Stack: **Python 3.12+, aiogram 3.x, PostgreSQL, SQLAlchemy 2.x (async), Alembic, Redis, Pydantic Settings, APScheduler, httpx.**

> **Status:** Phases 0–8 are implemented (see §8). This document describes what is in the code
> today, not only the original plan. Where the built system diverged from the initial design, the
> divergence is written down rather than hidden — the two biggest ones are **crypto top-up
> replacing the manual proof flow** (§4, §9) and **an inline main menu alongside the reply panel**
> (§1).

---

## 1. Architecture Overview

### Framework shape

- **aiogram `Router` per domain**, all included into one root `Dispatcher` in `main.py`. No monolithic `bot.py`.
- **Middlewares** (outer → inner), registered on `dp.update.middleware`:
  1. `ErrorMiddleware` — catches everything, logs, sends a friendly message, never lets the bot crash.
  2. `ThrottlingMiddleware` — Redis token-bucket per `user_id`; silently drops/warns on flood.
  3. `DbSessionMiddleware` — opens an `AsyncSession` per update, injects it as a handler kwarg, commits/rolls back at the end.
  4. `UserMiddleware` — upserts the `User` row from `event.from_user`, loads locale, caches `chat_id`, injects `user` into handler data.
  5. `BanCheckMiddleware` — short-circuits banned users with a notice.

  Plus one **inner** middleware on the message observer only:
  `SupportExitNoticeMiddleware` — when a user with a live support ticket sends a message that was
  handled by some *other* handler (a menu button, a wizard step), it tells them once that the
  message did **not** reach support. Runs inner, so it can see which handler won.
- **Admin authorization is a `Filter`** (`IsAdmin`), applied per-handler/router — never inferred from a hidden button. Every admin router requires it explicitly; nothing admin-only is reachable by ID guess or replayed callback alone.
- **FSM** via aiogram's `FSMContext` backed by `RedisStorage` (survives restarts, supports multi-step admin wizards and ticket creation). States grouped in `states/` per domain (`ProductForm`, `CategoryForm`, `TicketForm`, `TopUpForm`, …).
- **Callback data**: aiogram's `CallbackData` factory (typed, `prefix:field1:field2` packed string, must stay ≤ 64 bytes — tighter than Discord's 100-char limit). Example: `class ProductCB(CallbackData, prefix="prod"): action: str; id: str`. Anything that doesn't fit (long filter lists, multi-field wizards) goes into an `InteractionState` row keyed by a short id, referenced from callback_data instead of embedded.
- **Callback data is a routing hint only — never authority.** Every mutating handler re-derives price, ownership, stock, and permission from the DB inside the handler, exactly as it would for a slash command.
- **Services layer** (`app/services/*.py`) holds business logic, is framework-agnostic (no aiogram imports), and is what gets unit-tested. **Repositories** (`app/database/repositories/*.py`) hold all SQLAlchemy queries — handlers never issue raw queries. **Keyboards** (`app/bot/keyboards/*.py`) are pure builder functions returning `InlineKeyboardMarkup`/`ReplyKeyboardMarkup`.
- **Money** stored as integer minor units (`price_minor: int`, `currency: str`). No floats, anywhere.

### Navigation model

Every screen is rendered by editing the existing bot message (`message.edit_text` / `callback.message.edit_text`) rather than sending new messages, except where Telegram forces a new message (after a `ReplyKeyboardMarkup` change, or after a user free-text reply). `[ 🔙 Back ]` and `[ 🏠 Home ]` are built by a shared `nav_row(back_target)` helper; back targets are encoded in the callback data (or `InteractionState` for deep stacks) so Back works even after a bot restart.

**One resolver for Back/Home.** `handlers/nav.py` owns a single `NavCB.filter()` handler and resolves
the generic targets (`home`, `categories`, `orders`, `profile`, `cat-<id>`). Domains that need their
own screen rebuilt register a narrow `NavCB.filter(F.target == "…")` handler in their own module
(`topup`, `gift`, `refer`, `support`, `warranty`, `claim_warranty`, `language`, `admin_panel`).

**Rule: a `back_target` must be a target some handler actually claims.** An unknown target falls
through to the nav catch-all and the user gets the alert *"This action is no longer available"* —
which reads like a dead button, because it is one. `home` also clears the FSM state, so Back out of
a half-finished form (top-up amount, gift code, ticket subject) can't leave the form swallowing the
user's next message; a domain Back that leaves a form (e.g. `topup`) clears state too.

### Button colors (Bot API 9.4)

`keyboards/styles.py` is the only place that touches the `style` field. It exposes `PRIMARY` (blue),
`SUCCESS` (green), `DANGER` (red) and `NEUTRAL` (`None`, default background), plus `btn()` / `url_btn()`
builders that reject any other value before Telegram can. Convention used across screens:

- **PRIMARY** — navigation and neutral primary actions (menu entries, pagination, admin panel).
- **SUCCESS** — money-in and confirm actions (Enter Custom Amount, Check Payment Status, Buy/Confirm).
- **DANGER** — Back / cancel / reject / delete.
- **NEUTRAL** — inert filler (page indicators) and long scrollable lists, where solid color reads as noise.

Every inline button should go through `btn()`. A raw `InlineKeyboardButton(...)` silently ships an
unstyled button — that's how styling drifts back out of a screen.

### Persistent Reply Keyboard vs Inline Keyboards

- **Inline main menu** (`main_inline_keyboard`) is the primary menu: a 2×4 grid attached to the
  welcome message (Products/Orders, Profile/Top Up, Gift/Refer, Warranty/Support) + an
  `🛡️ Admin Panel` row appended **only** when `IsAdmin` passes at render time.
- `ReplyKeyboardMarkup` (`main_reply_keyboard`) is the persistent bottom panel mirroring the same
  entries in the same order, with a leading `🚀 Start` row — the panel is the only surface visible
  from *inside* another screen, so it carries the way back to the top. Its buttons send their
  localized label as plain text, matched back to an i18n key by the `MenuButton` filter — so it must
  be re-sent whenever the locale changes, or the labels stop matching.
- `InlineKeyboardMarkup` = everything else contextual: category/product lists, pagination,
  confirmations, admin CRUD, settings.

---

## 2. Database Schema (SQLAlchemy models, PostgreSQL)

All tables: `id` (BigInteger PK or UUID — using UUID for public-ish entities like orders/tickets, BigInteger elsewhere), `created_at`, `updated_at` timestamps unless noted. Money = `Numeric` avoided; **`Integer` minor units** used throughout.

### Identity & money

- **`users`** — `id`, `telegram_id` (unique, indexed), `username`, `first_name`, `last_name`, `locale`, `status` (`ACTIVE`/`BANNED`), `chat_id` (cached, = telegram_id for private chats but kept explicit), `referred_by_id` → users.id, `referral_code` (unique), `first_seen_at`, `last_seen_at`, `notes`.
- **`admins`** — `id`, `telegram_id` (unique), `role` (`OWNER`/`ADMIN`/`SUPPORT`), `granted_by_id`. Seeded from `ADMIN_IDS` env at boot (idempotent upsert); env IDs are always treated as `OWNER`-equivalent even if the table is empty/corrupted — lockout-proof floor.
- **`wallets`** — `id`, `user_id` (unique, 1:1), `balance_minor` (Integer), `currency`, `version` (Integer, optimistic lock).
- **`wallet_transactions`** — `id`, `wallet_id`, `type` (`TOPUP`/`PURCHASE`/`REFUND`/`GIFT`/`REFERRAL`/`ADMIN_ADJUST`), `amount_minor` (signed Integer), `balance_after_minor`, `status`, `ref_type`/`ref_id` (polymorphic), `idempotency_key` (unique), `created_at`. **Append-only.**

### Catalog

- **`categories`** — `id`, `name`, `slug` (unique), `description`, `emoji`, `image_file_id` (Telegram `file_id`, not a URL — reuses uploaded file), `sort_order`, `is_active`.
- **`products`** — `id`, `category_id`, `name`, `slug` (unique), `description`, `price_minor`, `currency`, `status` (`IN_STOCK`/`LOW_STOCK`/`OUT_OF_STOCK`/`COMING_SOON`/`DISABLED` — derived from stock count except the two manual overrides), `fulfillment_mode` (`AUTO`/`MANUAL`), `low_stock_threshold`, `image_file_id`, `thumbnail_file_id`, `delivery_info`, `warranty_days`, `notes`, `max_per_user`, `is_active`, `sort_order`.
- **`stock_items`** — `id`, `product_id`, `payload` (encrypted at rest, Fernet/AES-GCM via `ENCRYPTION_KEY`), `status` (`AVAILABLE`/`RESERVED`/`DELIVERED`/`VOID`), `order_item_id?`, `batch_id`, `added_by_admin_id`. Index `(product_id, status)` — the hot claim path.

### Commerce

- **`orders`** — `id` (UUID), `order_number` (unique, human-readable `ORD-8F3K2Q`), `user_id`, `status` (`PENDING`/`PROCESSING`/`COMPLETED`/`CANCELLED`/`FAILED`), `subtotal_minor`, `discount_minor`, `total_minor`, `currency`, `payment_txn_id`, `idempotency_key` (unique), `placed_at`, `completed_at`, `cancelled_at`, `failure_reason`.
- **`order_items`** — `id`, `order_id`, `product_id`, snapshot fields (`product_name`, `unit_price_minor`, `qty`, `warranty_days`) so later product edits never rewrite history.
- **`deliveries`** — `id`, `order_item_id`, `mode`, `payload?`, `delivered_at`, `delivered_by_admin_id?`, `delivery_message_id` (Telegram message id, for reference/audit).
- **`order_holds`** — `id`, `product_id`, `user_id`, `order_id?`, `held_at`, `expires_at` (default `held_at + 5 min`). Index `(product_id, expires_at)`. A checkout screen reserves the product for a 5-minute payment window so two users can't stare at the same last unit; the hold is advisory (the transactional stock claim in §7.3 is still what makes overselling impossible).
- **`warranties`** — `id`, `order_item_id` (unique), `user_id`, `starts_at`, `expires_at`, `status`, `claim_notes`, `claim_started_at?`, `claim_ticket_id?` → support_tickets.id. The last two are set when a user files a claim: `claim_started_at` starts the 24h staff-response clock, `claim_ticket_id` links the claim to the support ticket it is discussed in.

### Crypto payments

- **`crypto_payments`** — `id`, `user_id`, `product_amount_minor`, `expected_amount` (string, the **total including service fee** — what the user must actually send), `actual_amount?`, `fee_amount?`, `currency` (`USDT`), `status` (`PENDING`/`CONFIRMED`/`MISMATCH`/`EXPIRED`), `description` (e.g. `topup:25.0`), `tx_hash?` (indexed), `wallet_transaction_id?`, `created_at`, `confirmed_at?`. There is **no `expires_at` column** — the payment window is derived as `created_at + PAYMENT_TIMEOUT_MINUTES` everywhere it's needed.

### Growth

- **`gift_codes`** — `id`, `code` (unique, hashed + last-4 shown), `value_minor`, `currency`, `max_uses`, `used_count`, `expires_at`, `status`, `created_by_admin_id`, `per_user_limit`.
- **`gift_redemptions`** — `id`, `gift_code_id`, `user_id`, `wallet_transaction_id`, `redeemed_at`; unique `(gift_code_id, user_id)`.
- **`referrals`** — `id`, `referrer_id`, `referee_id` (unique), `qualified_at?`, `reward_minor`, `reward_txn_id?`. Qualification rule (first completed order) lives in `bot_settings`, not code. Referral link = `t.me/<bot_username>?start=ref_<referral_code>`.

### Support & ops

- **`support_tickets`** — `id`, `ticket_number` (unique), `user_id`, `category`, `subject`, `status` (`OPEN`/`PENDING`/`RESOLVED`/`CLOSED`), `priority`, `topic_id` (Telegram **forum topic** id inside `SUPPORT_GROUP_ID` — the Telegram analog of "private channel per ticket"), `assigned_staff_id?`, `opened_at`, `closed_at`, `close_reason`.
- **`ticket_messages`** — `id`, `ticket_id`, `author_type` (`USER`/`STAFF`/`SYSTEM`), `author_telegram_id`, `content`, `attachment_file_ids` (Array), `relayed_message_id`.
- **`broadcasts`** — `id`, `created_by_admin_id`, `title`, `body`, `image_file_id?`, `buttons_json?`, `audience_filter_json`, `status`, `total_targets`, `sent_count`, `failed_count`, `started_at`, `finished_at`.
- **`broadcast_deliveries`** — `id`, `broadcast_id`, `user_id`, `status` (`PENDING`/`SENT`/`FAILED`/`BLOCKED`), `error?`. Unique `(broadcast_id, user_id)` — worker is safely resumable after a crash/restart.
- **`bot_settings`** — `key` (unique), `value_json`, `updated_by_admin_id`, `updated_at`. In-memory cache with invalidation on write.
- **`audit_logs`** — `id`, `actor_telegram_id`, `actor_role`, `action`, `target_type`, `target_id`, `metadata_json`, `context`, `created_at`. Indexed on `(actor_telegram_id, created_at)` and `(action, created_at)`.
- **`interaction_states`** — `id` (short random token), `user_id`, `payload_json`, `expires_at` — overflow storage for callback_data that would exceed 64 bytes, and for wizard step state that needs to survive across FSM resets.

Indexes: every FK, plus `orders(user_id, placed_at)`, `wallet_transactions(wallet_id, created_at)`, `stock_items(product_id, status)`, `support_tickets(status, opened_at)`, `warranties(expires_at, status)`, `order_holds(product_id, expires_at)`, `crypto_payments(tx_hash)`.

### Migrations

`0001_initial_schema` is a baseline generated with `Base.metadata.create_all` (no live Postgres was
available while scaffolding), so it covers **every model that existed or was added before the first
real deploy** — that's why `0002`, `0004` and `0005` are intentionally empty no-op revisions: the
tables they name were already in the baseline. From `0006` onward, revisions are ordinary
`op.add_column`/`op.create_table` migrations. `0003` does not exist.

---

## 3. Folder Structure

Actual tree (kept in sync with the repo — names below are the real module names, not the planned ones):

```
app/
  bot/
    handlers/
      user/             start, help, profile, language, menu_placeholders
      products/         browse (categories → paginated products → detail)
      orders/           checkout, history
      admin/
        panel.py, dashboard.py, products.py, categories.py, users.py, orders.py,
        payments.py, gifts.py, support.py, broadcast.py, settings.py, logs.py,
        balance_adjust.py, warranty_claims.py,
        guard.py        # deny+audit router, registered AFTER every admin router
      support/          create.py, my_tickets.py, relay.py (group↔user; relay is the catch-all)
      payments/         topup.py (entry screen), topup_crypto.py (USDT flow), crypto_webhook.py
      referrals/        screen.py — refer & earn
      gifts/            redeem.py — redeem gift code
      warranty/         screen.py (list), claim.py (file a claim → support ticket)
      nav.py            single Back/Home resolver (see §1 Navigation model)
    keyboards/
      main_menu.py      inline main menu + persistent reply panel (+ conditional admin row)
      products.py, orders.py, common.py (nav_row, confirm_row, back_keyboard, with_nav)
      styles.py         PRIMARY/SUCCESS/DANGER/NEUTRAL + btn()/url_btn() — the only `style` users
    middlewares/
      error.py, throttling.py, db_session.py, user.py, ban_check.py, support_exit_notice.py
    filters/
      is_admin.py (IsAdmin + is_admin_user), menu_button.py (reply-label → i18n key)
    states/
      product_form.py, category_form.py, ticket_form.py, topup_form.py, broadcast_form.py,
      gift_form.py, order_fulfill_form.py, settings_form.py, user_search_form.py
    callbacks.py        all CallbackData factory classes, one place, collision-checked
    commands.py         the /command list used by set_bot_commands.py
  database/
    session.py          async engine + sessionmaker + session_scope
    base.py             declarative base + BigIntPKMixin/TimestampMixin/new_uuid
    models/             admin, audit, broadcast, catalog, crypto, gift, interaction_state,
                        order (Order/OrderItem/OrderHold/Delivery/Warranty), referral,
                        settings, support, user, wallet
    repositories/       one per aggregate — all queries live here
    migrations/         Alembic (0001 baseline; 0002/0004/0005 no-ops; 0006 warranty claim fields)
  services/
    catalog_service.py, order_service.py, order_hold_service.py, wallet_service.py,
    referral_service.py, gift_service.py, support_service.py, broadcast_service.py,
    settings_service.py, stats_service.py,
    payments/
      provider.py       PaymentProvider ABC
      registry.py       provider lookup (currently only "manual")
      manual.py         manual proof provider (kept for the admin approval queue)
      crypto.py         CryptoPaymentProcessor — webhook signature check + credit
      blockchain_monitor.py  BSCscan polling for USDT (BEP-20) transfers
  core/
    config.py           Pydantic Settings, fails fast on missing env
    logging.py          std logging, secret redaction
    security.py         encryption (Fernet), idempotency key helpers
    redis.py            Redis client + RedisStorage factory
  locales/
    en.json             (English only today — the i18n layer supports more, no second file yet)
    i18n.py             t(key, locale, **vars), missing-key logging, supported_locales()
  utils/
    pagination.py, money.py, errors.py (UserError), status_emoji.py
  jobs/
    scheduler.py        APScheduler wiring
    warranty_expiry.py, ticket_archival.py, crypto_payment_checker.py,
    broadcast_worker.py, warranty_auto_reject.py
  main.py               bootstrap: config → engine → bot → dispatcher → scheduler → polling
scripts/
  seed.py, set_bot_commands.py, gen_encryption_key.py, get_chat_id.py
tests/
  unit/  (services, admin-guard source scan)
  integration/ (repositories, stock-claim race test, gift limits)
deploy/ , Dockerfile , docker-compose.yml
.env.example , requirements.txt , pyproject.toml , alembic.ini , README.md
```

Differences from the original plan worth knowing: there is no `is_owner.py` filter (role lives on the
`admins` row), no separate `referrals` admin screen, no `utils/ids.py` (number generators live with
the models/services that use them), and `product_service.py`/`user_service.py`/`warranty_service.py`/
`audit_service.py` were never needed as separate modules — that logic sits in `catalog_service.py`,
the repositories, and the handlers' service calls.

`handlers/` never contains business logic; `services/` never imports `aiogram`. Each admin CRUD flow follows the same shape: list screen → detail screen → FSM wizard for create/edit → confirm → service call → audit log.

---

## 4. User Flow

```
/start
 └─ upsert user, parse ?start=ref_<code> deep link → pending referral
 └─ send premium welcome (photo/caption) + ReplyKeyboardMarkup main menu

Main menu — inline (attached to the welcome message) and mirrored by the persistent bottom panel:
 🛍️ Products   📦 Orders
 👤 Profile    💳 Top Up
 🎁 Free Gift  🔗 Refer & Earn
 🔧 Warranty   💬 Support
 [ 🛡️ Admin Panel ]  ← only when IsAdmin passes at render time
 (the bottom panel additionally carries a leading 🚀 Start row; 🌐 Language is reachable
  via /language — English is the only locale shipped so far)

🛍️ Products
 → categories (inline grid, paginated if >8)
   → category selected → product list (🟢/🟡/🔴 status prefix, price), paginated [◀️][n/N][▶️]
     → product selected → product detail card (desc, price, stock badge, warranty)
       → 🛒 Buy Now → confirm → stock check → balance check → debit wallet → claim stock
         → AUTO: deliver payload inline in chat (and store delivery_message_id)
         → MANUAL: order goes to admin fulfillment queue, user sees "processing"
         → order confirmation screen with order number
       → 🔙 Back → 🏠 Home

📦 Orders → paginated order history → order detail (items, status, delivery/warranty links)

💬 Support → 🎫 Create Ticket (category → free-text issue → confirm) → topic created in staff group
          → 📋 My Tickets → ticket detail (message thread mirrors relay) → reply / close

🌐 Language → 🇬🇧 English / 🇮🇳 Hindi → persists to users.locale, re-renders current screen

🎁 Get Gift → enter code (FSM) → validate (exists, not expired, uses left, per-user limit) → credit wallet

🔗 Refer & Earn → shows referral link + stats (total/qualified/reward earned) → share button

🔧 Warranty → list of warranties (status emoji, product, start/expiry, time remaining), paginated
            → 🛡️ Claim Warranty → opens a support ticket, sets claim_started_at (24h staff clock)
            → empty state explains that warranties are created automatically on purchase

💳 Top Up → ✏️ Enter Custom Amount (FSM, $1.00–$10,000.00)
          → payment screen: BNB Smart Chain address, USDT (BEP-20), amount + $0.20 service fee,
            total to send, 15-minute window
          → ✓ Check Payment Status (on-demand) and a 30s background checker (see §9)
          → on match: wallet credited, WalletTransaction row, user notified

👤 Profile → username, id, orders count, total spent, balance, referrals → shortcuts to each section
```

## 5. Admin Flow

```
/admin  (IsAdmin filter; unauthorized users get a generic "unknown command", no existence hint)
 └─ 🛡️ ADMIN PANEL
    [ 📊 Dashboard ]
    [ 📦 Products ] [ 📁 Categories ]
    [ 👥 Users ]    [ 🛒 Orders ]
    [ 💰 Payments ] [ 🎁 Gift Codes ]
    [ 🔗 Referrals ][ 💬 Support ]
    [ 📢 Broadcast ][ ⚙️ Settings ]
    [ 📝 Logs ]

📊 Dashboard → totals (users/products/categories/orders by status/revenue) + today/week/month deltas

📦 Products → ➕ Add (FSM wizard: name→desc→category→price→currency→stock→image→warranty→delivery info→confirm)
            → 📋 All (paginated, search) → ✏️ Edit (same wizard, prefilled) → 🗑️ Delete (confirm)
            → 📦 Manage Stock (add/remove stock items, bulk paste or file upload)
            → 🔴/🟢 Enable/Disable

📁 Categories → ➕ Add (name→emoji→description→image→confirm) → 📋 List → ✏️ Edit → 🗑️ Delete → reorder

👥 Users → search (by id/username) → detail (profile, orders, spending, balance) → 🚫 Ban / ✅ Unban
         → 💳 adjust balance (ADMIN_ADJUST wallet txn, reason required, audit-logged)

🛒 Orders → filter by status → detail → for MANUAL orders: fulfill (enter/select delivery payload) → deliver
          → cancel/refund (reverses wallet debit, restores stock if not yet delivered)

💰 Payments → pending top-up proofs queue → approve (credits wallet) / reject (with reason)
            (manual provider only — crypto top-ups credit themselves, no admin step)

🛡️ Warranty claims → handled by command inside the claim's support topic:
            /done <warranty_id> approves, /reject <warranty_id> rejects. Both are on a router with
            a blanket IsAdmin() filter, and both are listed in the guard's _ADMIN_COMMANDS so a
            non-admin gets "not authorized" instead of silence.

🎁 Gift Codes → ➕ Create (value, currency, expiry, max uses, per-user limit) → 📋 List → usage stats → disable

🔗 Referrals → configure reward amount, qualification rule → view leaderboard/stats

💬 Support → open tickets queue → assign → the relay itself happens via forum-topic messages,
            not a separate admin screen — admin just replies inside the ticket's topic

📢 Broadcast → compose (title, body, image, buttons, audience filter) → preview → confirm → send
             → resumable worker (rate-limited) → live progress (sent/failed/blocked) → final report

⚙️ Settings → edit bot_settings (currency, referral reward, qualification rule, low-stock threshold, etc.)

📝 Logs → filterable audit_log viewer (by actor, action, date range)
```

Every admin callback re-checks `IsAdmin` independently (filter runs per-handler, not just at router mount), exactly as required — a leaked/replayed admin callback_data from a non-admin user hits the filter and gets a generic rejection, logged to `audit_logs` as a denied attempt.

### The admin guard router (`handlers/admin/guard.py`)

Registered **after every admin router and before nav/catch-all**, so a real admin's update is
already handled and never reaches it. What is left is exactly the interesting case: a non-admin
invoking an admin command, or replaying/forging admin `callback_data`. Without it aiogram simply
finds no matching handler and the bot goes silent — which reads like a bug and tells a prober
nothing. It answers "not authorized" and writes an `audit_logs` row (`security.unauthorized_admin`).

It guards three surfaces, each with a list that must be kept current:

- `_ADMIN_COMMANDS` — admin-only commands (`/admin`, `/dashboard`, `/pending_orders`, `/open_tickets`,
  `/close`, `/adjust_balance`, `/broadcast_status`, `/done`, `/reject`). `/cancel` is deliberately
  absent: it is a shared FSM-exit command that non-admin flows use too.
- `_ADMIN_CB_PREFIXES` — the callback prefixes owned by admin routers, plus `_ADMIN_NAV_TARGETS`
  for admin-only nav targets.
- `_ADMIN_STATE_GROUPS` — admin FSM wizards, for the demotion case: an admin who starts a wizard
  and loses admin mid-flow would otherwise have their next plain-text step fall through to the
  support relay catch-all and be sent to staff.

`tests/unit/test_admin_guard.py` source-scans the admin handlers and fails the build if a new admin
command or `Admin*CB` factory isn't in these lists — so this stays honest without anyone remembering.

---

## 6. Callback Data & Interaction Architecture

- Typed `CallbackData` factories per domain, e.g.:
  - `CategoryCB(prefix="cat")`: `action: str` (`open`), `id: str`
  - `ProductCB(prefix="prod")`: `action: str` (`view`/`buy`/`page`), `id: str`, `page: int`
  - `AdminProductCB(prefix="aprod")`: `action: str`, `id: str`
- Packed string must stay ≤ 64 bytes; anything larger (e.g. a filter set, a multi-field draft) is written to `interaction_states` and referenced as `st:<token>`.
- FSM (`RedisStorage`) holds the *current step* of a wizard; `interaction_states` holds *cross-step payload* that must survive when the FSM state itself gets reset (e.g., "which product am I editing" surviving a cancel-and-resume).
- All navigation edits the existing message; only genuinely new content (a fresh reply-keyboard change, a user's free-text answer) sends a new message.
- Errors (expired callback, stale message, deleted product mid-flow) are caught in `ErrorMiddleware`, logged, and answered with a friendly "this session expired, use /start" — never a raw Telegram API exception surfaced to the user.

---

## 7. Security Architecture

1. **Admin authorization on every admin handler**, via `IsAdmin` filter reading `admins` table + `ADMIN_IDS` env (env ids always pass, even if the table is empty — lockout-proof). Never inferred from keyboard visibility.
2. **Ownership checks** — order/ticket/warranty detail handlers verify `record.user_id == user.id` before rendering, regardless of what id is in the callback data.
3. **Stock race safety** — claim runs inside one DB transaction:
   `SELECT ... FROM stock_items WHERE product_id=:pid AND status='AVAILABLE' LIMIT :qty FOR UPDATE SKIP LOCKED` (SQLAlchemy `.with_for_update(skip_locked=True)`) → mark `RESERVED` → debit wallet → mark `DELIVERED`. Any failure rolls back the whole transaction. Overselling is structurally impossible.
4. **Idempotency** — `orders.idempotency_key` unique, derived from `(user_id, product_id, 30s window)`; a double-tapped "Buy Now" returns the existing order instead of creating a second one. Backed by a short Redis lock during the click itself to avoid two concurrent handlers racing before the DB constraint even applies.
5. **Input validation** — every FSM free-text/step input validated with Pydantic models before touching the DB; prices parsed as minor-unit integers, never floats.
6. **Rate limiting / flood protection** — Redis token bucket per user in `ThrottlingMiddleware`; separate slower throttle for outbound broadcast sends (Telegram's ~30 msg/sec global cap, ~1/sec/user).
7. **Secrets** — `core/config.py` (Pydantic Settings) fails fast on missing `BOT_TOKEN`/`DATABASE_URL`; logging redacts token/DB URL; stock `payload` encrypted at rest (`ENCRYPTION_KEY`, Fernet/AES-256-GCM) so a DB dump doesn't leak sellable goods.
8. **Audit logging** — every admin mutation writes an `audit_logs` row in the same transaction as the mutation.
9. **Ban enforcement** — `BanCheckMiddleware` short-circuits `BANNED` users before any handler runs.
10. **Error taxonomy** — `UserError` (expected, e.g. "out of stock") → friendly localized message. Unexpected exceptions → generic message to user, full trace to logs (and optionally a Telegram log channel/group).

### `.env` / secrets vs git — answering your earlier concern directly

- `.env` (real `BOT_TOKEN`, `DATABASE_URL`, `ENCRYPTION_KEY`) is **git-ignored** from the start (`.gitignore` includes `.env`, `.env.*`, excluding `.env.example`). Only `.env.example` (placeholder values) is committed.
- **The database itself is never touched by git.** Only Alembic *migration files* (schema changes) are version-controlled. On the server, `git pull` updates code + migration files only; your actual data (rows) lives in Postgres on that server and is untouched by the pull. You then run `alembic upgrade head` to apply any new migrations — existing data is preserved, not overwritten.
- Each environment (your local machine, the server) keeps its own `.env` with its own `DATABASE_URL`, so local dev data and production data are always separate databases by construction.

---

## 8. Implementation Phases

| # | Phase | Delivers |
|---|-------|----------|
| 0 | **Scaffold** | `pyproject.toml`/`requirements.txt`, folder skeleton, `core/config.py`, `core/logging.py`, Alembic init, async DB engine, `.env.example`, `.gitignore`, bot boots and replies to `/start` with a plain message. |
| 1 | **Core framework** | All middlewares, `IsAdmin`/`IsOwner` filters, FSM+Redis storage wiring, `callbacks.py` factories, i18n (en+hi), keyboard builder kit (`nav_row`, `confirm_row`, main menu reply keyboard incl. conditional admin row), error handling, `/start` full main menu + `/help`, `set_bot_commands.py`. |
| 2 | **Catalog** | Category & Product models + repos + Alembic migration, admin CRUD via FSM wizards, stock upload (bulk paste + file), derived status, user browse: categories → paginated products → detail screen with Buy/Back. |
| 3 | **Orders & fulfillment** | Checkout confirm → transactional stock claim → wallet debit → order + delivery. AUTO delivers payload inline; MANUAL queues to admin fulfillment. Order history/detail, admin order management, cancel/refund. Warranty rows auto-created. |
| 4 | **Wallet & payments** | Wallet, append-only transactions, Top Up flow + balance UI, `PaymentProvider` ABC, `manual` provider (proof submission → admin approval queue → credit), admin manual credit/debit with audit trail. **Superseded for users by the crypto flow — see §9.** |
| 5 | **Gifts & referrals** | Gift code creation/redemption/usage log. Referral codes + `?start=ref_<code>` deep link, qualification on first completed order, configurable reward, stats screen. |
| 6 | **Support** | Ticket creation wizard, forum-topic-per-ticket in `SUPPORT_GROUP_ID`, bidirectional relay (user DM ↔ topic messages), status/priority/assignment, close/reopen, "My Tickets". |
| 7 | **Admin panel** | Dashboard with live stats, Users (search/detail/ban/history/balance adjust), Settings editor, Logs viewer, Broadcast composer + resumable rate-limited worker with honest delivery stats. |
| 8 | **Hardening & deploy** | APScheduler jobs (warranty expiry, ticket archival, broadcast worker), seed script, unit tests (stock-claim concurrency, wallet math, gift limits), systemd unit + optional Docker, deploy docs, graceful shutdown. |

Each phase ends with a short report: files touched, what works, how to run, how to test, what's next — same discipline as before, just Python-flavored (`pytest`, `alembic upgrade head`, `python -m app.main`).

All eight phases are implemented. Phase 9 (below) was added after the fact and is not in the
original plan.

---

## 9. Crypto Top-Up (added after Phase 8)

The user-facing top-up flow is **USDT (BEP-20) on BNB Smart Chain, monitored directly on-chain** —
no payment processor, no admin approval step. The manual proof provider still exists (`services/
payments/manual.py` + the admin Payments queue) but nothing in the user menu routes to it anymore.

**Money path**

1. User picks an amount (`$1.00`–`$10,000.00`). A `crypto_payments` row is created with
   `expected_amount = amount + SERVICE_FEE` ($0.20) — the total the user must actually send.
2. The screen shows `WALLET_ADDRESS`, the token/network, the fee breakdown, and a 15-minute window
   (`PAYMENT_TIMEOUT_MINUTES`, derived from `created_at` — there is no stored expiry).
3. `jobs/crypto_payment_checker.py` runs **every 30 s** (APScheduler): it pulls recent BEP-20
   transfers to the wallet from the **BSCscan** API and matches them against `PENDING` rows.
4. `services/payments/blockchain_monitor.py` owns the matching: USDT contract address, and a
   `MATCH_TOLERANCE` of 0.004 so sub-cent float noise doesn't reject a correct payment.
5. On a match: status → `CONFIRMED`, `tx_hash` stored, wallet credited via `wallet_service` (append-only
   `wallet_transactions` row, idempotency key), user notified. A wrong amount lands in `MISMATCH`
   (user is told to contact support); an untouched window lapses to `EXPIRED`.

`handlers/payments/crypto_webhook.py` + `services/payments/crypto.py` hold an HMAC-verified webhook
path (`CRYPTO_WEBHOOK_SECRET`) as an alternative to polling.

**Amount matching is the security boundary here.** The bot credits on an on-chain transfer that
matches an expected amount — so amounts must stay unique enough per open window, and `tx_hash` is
indexed and checked so one transfer can never be credited twice.

**Config** (`core/config.py`, all fail-fast except where noted): `WALLET_ADDRESS`, `BSCSCAN_API_KEY`,
`CRYPTO_WEBHOOK_SECRET` (defaults to `dev_secret` — must be set in production).

### Scheduled jobs (`jobs/scheduler.py`)

| Job | Interval | What |
|---|---|---|
| `warranty_expiry` | 1 h | Flips `ACTIVE` warranties past `expires_at` to `EXPIRED`. |
| `ticket_archival` | 1 h | Closes/archives stale support tickets. |
| `crypto_payments` | 30 s | Blockchain check + auto-credit (above). |

`jobs/broadcast_worker.py` is driven by the broadcast flow (and resumed at boot by
`resume_interrupted_broadcasts`), not by the scheduler. **`jobs/warranty_auto_reject.py` is written
but not registered** — the 24h auto-reject of unanswered warranty claims does not currently run.
