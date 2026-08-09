# Admin Panel Overhaul — Design

**Date:** 2026-08-10
**Status:** Approved, pending implementation plan

## Problem

The admin panel works but is hostile to use. Nine concrete defects, all confirmed in code:

1. **Dead-end screens.** Products list, Categories list, Users, and Orders list have no Back
   button. The only escape is retyping `/admin` or pressing the reply panel. This violates the
   rule the codebase already states in `app/bot/keyboards/common.py:26`: *"Every menu destination
   gets one, so no screen — including the text-only ones and the ones waiting on a form reply — is
   a dead end."*
2. **Typing where buttons belong.** The product wizard asks the admin to *type* `auto` or `manual`
   (`products.py:219`) and to type a warranty number (`products.py:230`). Both are closed sets.
   `skip` is also typed rather than tapped.
3. **Bare sub-screen text.** The main panel has rich per-button descriptions, but every screen
   below it is a stub — the whole body of the product list is
   `"📦 PRODUCT MANAGEMENT\n\n5 products total."`
4. **A Payments button for a dead feature.** It opens the *manual* top-up review queue. Top-ups are
   automatic (USDT on BNB Chain, 30s checker job). The screen's own copy concedes that "an empty
   list is the normal, healthy state."
5. **Fabricated "Uncategorized" folder.** `Product.category_id` is `NOT NULL`
   (`app/database/models/catalog.py:48`), so choosing "Uncategorized" makes
   `products.py:158-164` create a **real** `Category` row. It then appears as a folder to buyers.
6. **Products are born dead.** The wizard never asks for stock, so every new product reads OUT OF
   STOCK until the admin makes a second trip to Manage Stock.
7. **No bulk anything.** Adding 1000 products means running the wizard 1000 times.
8. **The list does not scale.** `_render_list` (`products.py:78-80`) loads *every* product row into
   Python just to count them, then slices 10. There is no search.
9. **No edit.** The product detail screen offers Add Stock / Toggle / Delete / Back. A price cannot
   be changed without deleting and recreating the product. `AdminProductCB` has documented `"edit"`
   as a valid action since it was written (`callbacks.py:40`); it was never implemented.

## Decisions taken

| Question | Decision |
|---|---|
| Bulk product management | CSV file upload with upsert, plus CSV export |
| Credentials/stock in CSV | **No.** Stock stays manual, entered per product |
| Payments button | Remove |
| Uncategorized products | `category_id` becomes nullable; loose products render above the folders |
| Stock during creation | Yes — ask at the end of the wizard, skippable |

The credentials decision is deliberate. Pipe-separated secrets inside CSV quoting is precisely
where a quoting mistake silently ships the wrong key to a paying buyer. The cost is one trip per
product to paste keys; the Add Stock flow already accepts multi-line paste, so it is one paste per
product regardless of key count.

## Non-goals

- Editing categories (only products get an edit flow; category edit stays out of scope).
- Any change to the buyer checkout, payment, or warranty flows.
- Removing the manual top-up approve/reject handlers — only the panel button goes. The handlers
  stay reachable by callback in case a manual provider is ever put back in front of users.

---

## Phase 1 — UX fixes

No schema changes. Independently shippable.

### 1.1 Back and Abort everywhere

Append `nav_row(locale, back_target="admin_panel", home=False)` to the keyboards of:

- `admin/products.py::_list_keyboard`
- `admin/categories.py::_list_keyboard`
- `admin/users.py` — search prompt, search results, and user detail
- `admin/orders.py` — list keyboard

`admin_panel` is already a live nav target, handled at `admin/panel.py:77`.

Every wizard step gains an inline `[🔙 Back] [❌ Abort]` row:

- **Back** returns to the previous step with prior answers intact (FSM data is not cleared).
- **Abort** clears state and returns to the screen the wizard was launched from.
- **Back on the first step** behaves as Abort — there is no earlier step to return to, so it
  clears state and exits to the list rather than doing nothing.

This mirrors the broadcast wizard's existing treatment (`admin/broadcast.py:74-86`), so both
wizards behave identically. Back is `DANGER` (red), matching `nav_row`'s convention that leaving a
screen is red.

Affected wizards: `ProductForm`, `CategoryForm`, `UserSearchForm`, `StockUploadForm`.

### 1.2 Buttons instead of typed input

| Step | Current | Replacement |
|---|---|---|
| `ProductForm.fulfillment_mode` | type `auto` / `manual` | `[⚡ Auto] [🙋 Manual]` |
| `ProductForm.warranty_days` | type an integer | `[None] [7d] [30d] [90d] [365d] [✏️ Custom]` |
| `ProductForm.description` | type `skip` | `[⏭️ Skip]` |
| `ProductForm.delivery_info` | type `skip` | `[⏭️ Skip]` |
| `CategoryForm.emoji` | type `skip` | `[⏭️ Skip]` |
| `CategoryForm.description` | type `skip` | `[⏭️ Skip]` |

`✏️ Custom` on warranty falls through to the existing typed-integer handler, so arbitrary values
remain possible.

Name and price stay typed — they are free text and no button can express them. The typed handlers
for every converted step are **kept** as a fallback, so an admin who types `auto` out of habit is
not punished.

### 1.3 Real descriptions on sub-screens

Each admin sub-screen gets a header written in the same voice as the main panel: what the screen
is for, and one line per button explaining what it does. Applies to Products, Categories, Users,
Orders, Gift Codes, and Logs.

### 1.4 Remove Payments

- Drop the `💰 Payments` button from `admin/panel.py::_panel_keyboard`.
- Drop its line from the panel description.
- The panel description currently exists as two verbatim copies (`panel.py:58-72` and `:82-96`),
  one for the command handler and one for the nav handler. Extract to a single module constant so
  the two cannot drift. This is a targeted fix in code being edited anyway, not speculative
  refactoring.

---

## Phase 2 — Structural

### 2.1 Category becomes optional

**Migration `0012_product_category_nullable.py`:**

1. Drop `NOT NULL` from `products.category_id`.
2. Data step: find the `Category` with slug `uncategorized`; set its products' `category_id` to
   `NULL`; delete the row. No-op when the row was never created.
3. Downgrade recreates the category, refiles null products into it, and restores `NOT NULL`.

**Model:** `Product.category_id: Mapped[int | None]`.

**Service:** `create_product` accepts `category_id=None` and no longer manufactures a category.

**Admin picker:** `🚫 No category` replaces `📁 Uncategorized` and stores `None`. The
category-creating branch at `products.py:156-164` is deleted outright.

**Store rendering** (`handlers/products/browse.py::render_categories`): fetch active products with
`category_id IS NULL` and render them as rows directly above the category grid, separated by a
divider:

```
🛍️ STORE

  📦 Kiro Pro — $9.99        ← loose products
  📦 Kiro Lite — $4.99
  ─────────────
  📁 Software                ← real categories
  📁 Gaming
```

**Repo:** add `ProductRepo.list_uncategorized(offset, limit, *, active_only=True)` and
`count_uncategorized()`, mirroring the existing `list_by_category` / `count_by_category` pair.

**Back-target edge case:** `render_product_detail` passes `product.category_id` into
`product_detail(...)` as the back target (`browse.py:92`). When it is `None`, the back button must
target the store root instead of a category id.

### 2.2 Stock inside the creation wizard

New state `ProductForm.stock`, entered after `delivery_info`.

- Shown **only** for `FulfillmentMode.AUTO`. MANUAL products have no stock pool and skip straight
  to creation.
- Accepts multi-line input, one payload per line — the same format `StockUploadForm` already uses.
- `[⏭️ Skip]` creates the product with no stock (today's behaviour).
- Product and stock are created in one transaction. On success the confirmation reports the real
  state: `✅ Kiro Pro created with 3 stock items — LIVE`, or the existing OUT OF STOCK warning when
  skipped.

### 2.3 CSV import and export

New module `app/services/product_import.py`. Parsing is separated from the Telegram handler so it
is unit-testable without a bot.

**Columns** — `name` and `price` required, everything else optional:

```
id,name,category,price,currency,mode,warranty,description,delivery_info,active
,Kiro Pro,Software,9.99,USD,auto,30,Premium tier,Login at example.com,yes
14,Kiro Lite,Software,4.99,USD,auto,0,,,yes
```

There is no `stock` column, by decision.

**Value formats:** `mode` accepts `auto` / `manual`, case-insensitive. `active` accepts
`yes/no`, `true/false`, `1/0`, case-insensitive; blank defaults to active. `warranty` is a
non-negative integer, blank defaults to `0`. `currency` blank defaults to the configured
`default_currency`. Any other value in these columns is a row error, never a silent coercion.

**Upsert matching, in order:**

1. `id` present and non-empty → update that product. If no such product exists, the row is an
   error (it is not silently created — a stale id usually means a bad export).
2. Otherwise match on `name`, case-insensitive. Exactly one match → update.
3. No match → create.
4. Ambiguous (two existing products share the name) → row error.

**Category column:** matched by name, case-insensitive. Auto-created when missing, and each
auto-creation is called out in the report. Blank means `NULL` — no category, consistent with 2.1.

**Error handling:** row-level. Valid rows are applied and committed; invalid rows are collected
and reported with their line numbers. A malformed *header* aborts the whole import, since that
means the file is not what the admin thinks it is.

**Report:**

```
✅ Import complete
   847 created · 153 updated · 3 errors
   2 categories auto-created: Gaming, Utilities

❌ Line 12: price "abc" is not a number
❌ Line 88: id 9001 not found
❌ Line 204: name "Kiro Pro" matches 2 existing products
```

**Limits:** 5000 rows and 1 MB, rejected before parsing. UTF-8 with BOM tolerated (Excel writes
one). Both `,` and `;` delimiters accepted via `csv.Sniffer`.

**Handler:** `📥 Import CSV` on the product list opens an FSM state awaiting a Telegram document.
`📤 Export CSV` writes every product in exactly the import schema, so the round trip
export → edit in Excel → re-upload is the intended bulk-edit path.

### 2.4 Product list at scale

- Replace the load-everything count in `_render_list` with a `SELECT count(*)`, and fetch only the
  current page via `LIMIT`/`OFFSET`.
- Add `🔍 Search` → FSM state → case-insensitive `name LIKE` filter. The search term is carried in
  FSM data so pagination works within a filtered result set.
- Show `page/total` and the active filter in the header.

### 2.5 Per-product edit

`✏️ Edit` on the product detail screen opens a field picker: name, price, description, warranty,
fulfillment mode, category, delivery info. Choosing a field enters a single-field FSM state,
reusing the Phase 1 button widgets where the field is a closed set (mode, warranty, category).
Saving returns to the product detail screen.

---

## Testing

Existing suite convention (`tests/unit/test_broadcast_back.py`, `test_nav_targets.py`) is
keyboard-shape and handler assertions against a fake bot — the new tests follow it.

**Unit**

- Every admin list keyboard's last row is a nav row targeting `admin_panel`.
- Each wizard step's keyboard carries Back and Abort; Back preserves FSM data, Abort clears it.
- Auto/Manual and warranty-preset buttons write the same FSM data the typed handlers do.
- `_panel_keyboard` contains no `AdminPaymentCB`.
- CSV parser: header validation, required-field validation, each of the four match cases, blank
  category → `None`, row-level error collection, row and byte limits, BOM and `;` delimiter.
- Export output re-imports as a clean no-op (round-trip property).

**Integration**

- Migration 0012 up: an existing `uncategorized` category's products end at `category_id IS NULL`
  and the row is gone. Down: restored.
- `render_categories` lists loose products above the grid, and omits the divider when there are
  none.
- Product detail for a null-category product has a back button targeting the store root.
- AUTO wizard including stock yields a product that is immediately IN STOCK; MANUAL skips the
  stock step; Skip yields OUT OF STOCK.
- Import of a file mixing creates, updates, and errors commits the valid rows only.

## Risks

- **2.1 is buyer-visible.** Products currently sitting in a real "Uncategorized" category move out
  of it. On live data buyers will see the store layout change. The migration is reversible.
- **Callback data size.** `AdminProductCB` carries `action`, `id`, and `page`; the edit flow adds a
  field selector. Telegram's callback_data limit is 64 bytes — field names must be short codes
  (`nm`, `pr`, `ds`) rather than full words.
- **Import memory.** 5000 rows is held in memory during parsing. Acceptable at that cap; the byte
  limit is the real guard.

## Implementation order

Phase 1 first and shippable on its own — it fixes the daily friction (dead ends, typing) without
touching the schema. Phase 2 follows, with 2.1 (the migration) landing before 2.3, since the CSV
importer's blank-category behaviour depends on the nullable column.
