# Premium Telegram Digital-Product Store Bot — Architecture

Stack: **Python 3.12+, aiogram 3.x, PostgreSQL, SQLAlchemy 2.x (async), Alembic, Redis, Pydantic Settings.**

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
- **Admin authorization is a `Filter`** (`IsAdmin`), applied per-handler/router — never inferred from a hidden button. Every admin router requires it explicitly; nothing admin-only is reachable by ID guess or replayed callback alone.
- **FSM** via aiogram's `FSMContext` backed by `RedisStorage` (survives restarts, supports multi-step admin wizards and ticket creation). States grouped in `states/` per domain (`ProductForm`, `CategoryForm`, `TicketForm`, `TopUpForm`, …).
- **Callback data**: aiogram's `CallbackData` factory (typed, `prefix:field1:field2` packed string, must stay ≤ 64 bytes — tighter than Discord's 100-char limit). Example: `class ProductCB(CallbackData, prefix="prod"): action: str; id: str`. Anything that doesn't fit (long filter lists, multi-field wizards) goes into an `InteractionState` row keyed by a short id, referenced from callback_data instead of embedded.
- **Callback data is a routing hint only — never authority.** Every mutating handler re-derives price, ownership, stock, and permission from the DB inside the handler, exactly as it would for a slash command.
- **Services layer** (`app/services/*.py`) holds business logic, is framework-agnostic (no aiogram imports), and is what gets unit-tested. **Repositories** (`app/database/repositories/*.py`) hold all SQLAlchemy queries — handlers never issue raw queries. **Keyboards** (`app/bot/keyboards/*.py`) are pure builder functions returning `InlineKeyboardMarkup`/`ReplyKeyboardMarkup`.
- **Money** stored as integer minor units (`price_minor: int`, `currency: str`). No floats, anywhere.

### Navigation model

Every screen is rendered by editing the existing bot message (`message.edit_text` / `callback.message.edit_text`) rather than sending new messages, except where Telegram forces a new message (after a `ReplyKeyboardMarkup` change, or after a user free-text reply). `[ 🔙 Back ]` and `[ 🏠 Home ]` are built by a shared `nav_row(back_target)` helper; back targets are encoded in the callback data (or `InteractionState` for deep stacks) so Back works even after a bot restart.

### Persistent Reply Keyboard vs Inline Keyboards

- `ReplyKeyboardMarkup` = the persistent 4×2 main-menu grid (+ `🛡️ Admin Panel` row appended **only** when `IsAdmin` passes at render time — never a static keyboard baked in once).
- `InlineKeyboardMarkup` = everything contextual: category/product lists, pagination, confirmations, admin CRUD, settings.

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
- **`warranties`** — `id`, `order_item_id` (unique), `user_id`, `starts_at`, `expires_at`, `status`, `claim_notes`.

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

Indexes: every FK, plus `orders(user_id, placed_at)`, `wallet_transactions(wallet_id, created_at)`, `stock_items(product_id, status)`, `support_tickets(status, opened_at)`, `warranties(expires_at, status)`.

---

## 3. Folder Structure

```
app/
  bot/
    handlers/
      user/            start, menu, profile, language
      products/         category list, product list+pagination, product detail, buy flow
      orders/            order history, order detail
      admin/
        products.py, categories.py, users.py, orders.py,
        payments.py, gifts.py, referrals.py, support.py,
        broadcast.py, settings.py, logs.py, dashboard.py
      support/          create ticket, my tickets, relay (group→user, user→group)
      payments/         top-up flow (manual provider)
      referrals/        refer & earn screen
      gifts/            redeem gift code
      warranty/         warranty list/detail
    keyboards/
      main_menu.py       reply keyboard (+ conditional admin row)
      products.py, orders.py, admin.py, common.py (nav_row, confirm_row)
    middlewares/
      error.py, throttling.py, db_session.py, user.py, ban_check.py
    filters/
      is_admin.py, is_owner.py
    states/
      product_form.py, category_form.py, ticket_form.py, topup_form.py, broadcast_form.py
    callbacks.py         all CallbackData factory classes, one place, collision-checked
  database/
    session.py            async engine + sessionmaker
    base.py                declarative base
    models/                one file per aggregate (matches schema above)
    repositories/           one per aggregate — all queries live here
    migrations/             Alembic
  services/
    product_service.py, order_service.py, user_service.py,
    payment_service.py (PaymentProvider ABC + registry),
    referral_service.py, gift_service.py, warranty_service.py,
    support_service.py, broadcast_service.py, audit_service.py, stats_service.py
  core/
    config.py             Pydantic Settings, fails fast on missing env
    logging.py             structlog/std logging, secret redaction
    security.py            encryption (Fernet/AES-GCM), idempotency key helpers
    redis.py                Redis client + RedisStorage factory
  locales/
    en.json, hi.json
    i18n.py                 t(key, locale, **vars), missing-key logging
  utils/
    pagination.py, money.py, ids.py (order/ticket number generators)
  jobs/
    warranty_expiry.py, ticket_archival.py, broadcast_worker.py   # APScheduler
  main.py                   bootstrap: config → engine → bot → dispatcher → polling/webhook
scripts/
  seed.py                    seed categories/products/admins for dev
  set_bot_commands.py         registers /start /products ... via setMyCommands
tests/
  unit/  (services)
  integration/ (repositories, stock-claim race test)
.env.example
requirements.txt / pyproject.toml
alembic.ini
README.md
```

`handlers/` never contains business logic; `services/` never imports `aiogram`. Each admin CRUD flow follows the same shape: list screen → detail screen → FSM wizard for create/edit → confirm → service call → audit log.

---

## 4. User Flow

```
/start
 └─ upsert user, parse ?start=ref_<code> deep link → pending referral
 └─ send premium welcome (photo/caption) + ReplyKeyboardMarkup main menu

Main menu (reply keyboard, mirrors reference image):
 🛍️ Products   📦 Orders
 💬 Support    🌐 Language
 🎁 Get Gift   🔗 Refer & Earn
 🔧 Warranty   💳 Top Up
 (👤 Profile reachable via /profile and a button on most screens)

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

🔧 Warranty → list of orders with active/expired warranty → detail (dates, status, claim button)

💳 Top Up → enter amount → PaymentProvider.render_instructions() (manual: proof upload) →
           admin approves → wallet credited, WalletTransaction row, user notified

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
| 4 | **Wallet & payments** | Wallet, append-only transactions, Top Up flow + balance UI, `PaymentProvider` ABC, working `manual` provider (proof submission → admin approval queue → credit), admin manual credit/debit with audit trail. |
| 5 | **Gifts & referrals** | Gift code creation/redemption/usage log. Referral codes + `?start=ref_<code>` deep link, qualification on first completed order, configurable reward, stats screen. |
| 6 | **Support** | Ticket creation wizard, forum-topic-per-ticket in `SUPPORT_GROUP_ID`, bidirectional relay (user DM ↔ topic messages), status/priority/assignment, close/reopen, "My Tickets". |
| 7 | **Admin panel** | Dashboard with live stats, Users (search/detail/ban/history/balance adjust), Settings editor, Logs viewer, Broadcast composer + resumable rate-limited worker with honest delivery stats. |
| 8 | **Hardening & deploy** | APScheduler jobs (warranty expiry, ticket archival, broadcast worker), seed script, unit tests (stock-claim concurrency, wallet math, gift limits), systemd unit + optional Docker, deploy docs, graceful shutdown. |

Each phase ends with a short report: files touched, what works, how to run, how to test, what's next — same discipline as before, just Python-flavored (`pytest`, `alembic upgrade head`, `python -m app.main`).

---

**Not started yet** — per your instructions, this is architecture/schema/flow only. Say the word and I'll begin Phase 0.
