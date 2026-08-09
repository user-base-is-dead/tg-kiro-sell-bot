You are an expert Telegram Bot developer and backend architect.

I want you to build a production-ready PREMIUM TELEGRAM DIGITAL STORE BOT.

The attached image is the visual reference for the bot's main menu. Use it as inspiration for the layout, navigation style, emoji usage, and premium shopping experience.

This is a TELEGRAM bot, NOT a Discord bot.

==================================================
1. TECH STACK
==================================================

Use:

- Python 3.12+
- aiogram 3.x
- PostgreSQL
- SQLAlchemy 2.x
- Alembic
- Redis if useful for FSM/state management and caching
- Pydantic settings
- Async architecture

Use clean modular architecture.

Never put everything into one bot.py file.

==================================================
2. MAIN MENU
==================================================

After the user sends:

/start

show a premium welcome message and the main menu.

The main menu should contain BOTH:

A) Telegram Reply Keyboard
B) Inline Buttons inside the bot message

The main menu should have:

🛍️ Products
📦 Orders
💬 Support
🌐 Language
🎁 Get Gift
🔗 Refer & Earn
🔧 Warranty
💳 Top Up

Example layout:

🛍️ Products     📦 Orders
💬 Support       🌐 Language
🎁 Get Gift      🔗 Refer & Earn
🔧 Warranty      💳 Top Up

Use Telegram's ReplyKeyboardMarkup for the persistent keyboard.

Also provide InlineKeyboardMarkup inside the relevant messages for navigation and actions.

==================================================
3. PREMIUM TELEGRAM UI
==================================================

The bot should look PREMIUM.

Use:

🔵 Blue-themed visual design
🟢 Green for available/in-stock products
🔴 Red for out-of-stock products
🟡 Yellow for warnings/low stock
💎 Premium emojis
✨ Decorative separators
🔥 Animated/custom Telegram emojis where supported

IMPORTANT:

Telegram does not allow arbitrary text colors inside normal bot *message text*.

Therefore DO NOT fake HTML color support in message bodies.

BUTTONS ARE THE EXCEPTION. Bot API 9.4 (Feb 2026) added a `style` field to
InlineKeyboardButton / KeyboardButton with three real filled backgrounds:
`primary` (blue), `success` (green), `danger` (red). Every keyboard in this bot
sets it through `app/bot/keyboards/styles.py` — never with colored-dot prefixes
standing in for a background. Requires aiogram >= 3.25 (this repo pins 3.30).
Clients older than 9.4 silently ignore `style` and render the default button, so
status rows keep their 🟢/⚫ text prefix as a fallback.

For message text, create a premium visual system using:

- Emojis
- Custom emoji entities where supported
- Unicode symbols
- Telegram Premium/custom emoji where available
- Inline buttons
- Rich formatting
- Bold/italic/code formatting
- Images
- Animated media where appropriate

The bot should feel colorful despite Telegram's text-color limitations.

==================================================
4. PRODUCT SYSTEM
==================================================

Create a complete product/catalog system.

Products belong to categories.

Each product must have:

- ID
- Name
- Description
- Category
- Price
- Currency
- Stock quantity
- Product image
- Product thumbnail
- Status
- Delivery information
- Warranty duration
- Created date
- Updated date

Product statuses:

🟢 IN STOCK
🟡 LOW STOCK
🔴 OUT OF STOCK
🔵 COMING SOON
⚫ DISABLED

Admin must be able to:

➕ Add product
✏️ Edit product
🗑️ Delete product
📦 Add stock
📉 Remove stock
💰 Change price
📁 Change category
🖼️ Change image
🔧 Change warranty
🔴 Disable product
🟢 Enable product

==================================================
5. CATEGORY SYSTEM
==================================================

Create complete category management.

Admin can:

➕ Add category
✏️ Edit category
🗑️ Delete category
📋 View categories
🔄 Enable/disable category
🎨 Set category emoji
🖼️ Set category image
↕️ Reorder categories if practical

Example:

🎮 Gaming
💻 Software
🎁 Gift Cards
🔐 Accounts
📦 Digital Products
⭐ Premium

==================================================
6. PRODUCTS USER FLOW
==================================================

When user presses:

🛍️ Products

show:

🛍️ STORE

Choose a category:

[ 🎮 Gaming ]
[ 💻 Software ]
[ 🎁 Gift Cards ]
[ 🔐 Accounts ]

When category is selected:

show products using inline buttons.

Example:

🛍️ GAMING PRODUCTS

🟢 Product A — $10
🟢 Product B — $15
🔴 Product C — $20

Use pagination:

[ ◀️ ] [ 1/5 ] [ ▶️ ]

Every product should open a detailed product page.

==================================================
7. PRODUCT DETAILS
==================================================

Example:

━━━━━━━━━━━━━━━━━━
🛍️ PRODUCT NAME
━━━━━━━━━━━━━━━━━━

Premium digital product.

💰 Price: $9.99
📦 Stock: 24
🛡️ Warranty: 7 Days

🟢 IN STOCK

[ 🛒 Buy Now ]
[ 🔙 Back ]

For out of stock:

🔴 OUT OF STOCK

[ 🔙 Back ]

For low stock:

🟡 LOW STOCK
Only 3 remaining!

==================================================
8. ORDER SYSTEM
==================================================

Create a complete order system.

When user clicks:

🛒 Buy Now

Flow:

1. Show product details.
2. Ask for confirmation.
3. Check stock.
4. Check user's balance/payment.
5. Create order.
6. Generate unique order ID.
7. Deduct stock.
8. Process delivery.
9. Save transaction.
10. Show order confirmation.

Order statuses:

🟡 Pending
🔵 Processing
🟢 Completed
🔴 Cancelled
⚠️ Failed

Users can view:

📦 My Orders

with pagination.

==================================================
9. USER SYSTEM
==================================================

Every Telegram user should be stored in the database.

Store:

- Telegram user ID
- Username
- First name
- Last name
- Language
- Registration date
- Last activity
- Balance
- Referral code
- Referral count
- Order count
- Total spending
- Account status

==================================================
10. USER PROFILE
==================================================

Create:

👤 My Profile

Example:

━━━━━━━━━━━━━━━━━━
👤 MY PROFILE
━━━━━━━━━━━━━━━━━━

Username: @username
ID: 123456789

📦 Orders: 12
💰 Total Spent: $120
💳 Balance: $20
🔗 Referrals: 5

[ 📦 My Orders ]
[ 💳 Balance ]
[ 🔗 Referrals ]

==================================================
11. ADMIN PANEL
==================================================

VERY IMPORTANT:

The Admin Panel must ONLY be visible and accessible to authorized Telegram admin IDs.

Normal users MUST NOT see:

🛡️ Admin Panel
📊 Dashboard
👥 Users
📦 Product Management
📁 Category Management
💰 Payments
📢 Broadcast
⚙️ Settings

Use ADMIN_IDS from environment variables.

Example:

ADMIN_IDS=123456789,987654321

Every admin callback/action must independently verify admin authorization.

Do NOT rely only on hiding the button.

==================================================
12. ADMIN PANEL MENU
==================================================

Admin panel:

🛡️ ADMIN PANEL

[ 📊 Dashboard ]

[ 📦 Products ]
[ 📁 Categories ]

[ 👥 Users ]
[ 🛒 Orders ]

[ 💰 Payments ]
[ 🎁 Gift Codes ]

[ 🔗 Referrals ]
[ 💬 Support ]

[ 📢 Broadcast ]
[ ⚙️ Settings ]

[ 📝 Logs ]

==================================================
13. ADMIN DASHBOARD
==================================================

Show:

📊 DASHBOARD

👥 Total Users
📦 Total Products
📁 Categories
🛒 Total Orders
⏳ Pending Orders
✅ Completed Orders
❌ Cancelled Orders
💰 Total Revenue
🟢 Products In Stock
🔴 Products Out of Stock

Add useful statistics such as:

- Today's orders
- Today's revenue
- New users today
- Weekly revenue
- Monthly revenue

==================================================
14. ADMIN → PRODUCTS
==================================================

Create:

📦 PRODUCT MANAGEMENT

[ ➕ Add Product ]
[ 📋 All Products ]
[ ✏️ Edit Product ]
[ 🗑️ Delete Product ]
[ 📦 Manage Stock ]
[ 🔍 Search Product ]

When adding a product, use Telegram FSM + inline steps or Telegram ReplyKeyboard + FSM.

Ask:

1. Product name
2. Description
3. Category
4. Price
5. Currency
6. Stock
7. Product image
8. Warranty duration
9. Delivery information
10. Confirmation

Allow admin to cancel the operation at any step.

==================================================
15. ADMIN → CATEGORIES
==================================================

Create:

📁 CATEGORY MANAGEMENT

[ ➕ Add Category ]
[ 📋 List Categories ]
[ ✏️ Edit Category ]
[ 🗑️ Delete Category ]

Category creation should ask:

- Name
- Emoji
- Description
- Image
- Status

==================================================
16. ADMIN → USERS
==================================================

Create:

👥 USERS

Show:

Total users
Active users
New users

Admin can:

🔍 Search user
👤 View user
📦 View user's orders
💰 View spending
💳 View balance
🚫 Ban user
✅ Unban user

Use pagination.

==================================================
17. GIFT CODE SYSTEM
==================================================

Create:

🎁 Get Gift

Users can redeem gift codes.

Admin can:

➕ Create gift code
🗑️ Delete gift code
🔴 Disable gift code
📊 View gift code usage

Gift code fields:

- Code
- Value
- Currency
- Expiry
- Usage limit
- Per-user limit
- Status

==================================================
18. REFERRAL SYSTEM
==================================================

Create:

🔗 Refer & Earn

Every user receives a unique referral link.

Track:

- Total referrals
- Successful referrals
- Referral rewards

Admin can configure:

- Reward amount
- Minimum requirements
- Enable/disable referral system

==================================================
19. WARRANTY SYSTEM
==================================================

Create:

🔧 Warranty

Users can see warranty information for purchased products.

Show:

Order ID
Product
Purchase date
Warranty duration
Expiry date
Status

Example:

🛡️ WARRANTY

Order: #ORD-92831
Product: Premium Product

📅 Purchased: 08 Aug 2026
⏳ Warranty: 30 Days

🟢 WARRANTY ACTIVE

==================================================
20. SUPPORT SYSTEM
==================================================

Create a support system.

User:

💬 Support

[ 🎫 Create Ticket ]
[ 📋 My Tickets ]

Ticket creation:

1. Select category
2. Enter issue
3. Create ticket
4. Notify admins/support staff

Admin can:

- View ticket
- Reply
- Close ticket
- Reopen ticket
- Assign ticket

==================================================
21. LANGUAGE SYSTEM
==================================================

Create multilingual architecture.

Initially support:

🇬🇧 English
🇮🇳 Hindi

But make it easy to add more languages later.

NEVER hard-code every message directly inside handlers.

Use:

locales/
  en.json
  hi.json

or an equivalent localization architecture.

==================================================
22. TOP UP / WALLET
==================================================

Create:

💳 Top Up

Users can:

- View balance
- Top up
- View transaction history

Make payment architecture modular.

Do not hard-code a payment provider.

Create a PaymentService abstraction so providers can be added later.

==================================================
23. REPLY KEYBOARD
==================================================

The persistent Telegram keyboard should remain available.

Example:

🛍️ Products     📦 Orders
💬 Support       🌐 Language
🎁 Get Gift      🔗 Refer & Earn
🔧 Warranty      💳 Top Up

Admin users should additionally receive:

🛡️ Admin Panel

Normal users should NEVER receive the Admin Panel button.

==================================================
24. INLINE KEYBOARDS
==================================================

Use inline keyboards extensively for:

- Product selection
- Categories
- Product details
- Buying
- Confirmation
- Pagination
- Admin management
- User management
- Orders
- Settings
- Back navigation

Every inline callback must be validated.

==================================================
25. ANIMATED / PREMIUM EMOJIS
==================================================

The bot should support Telegram Premium/custom emojis where possible.

Create a configurable emoji system.

Example configuration:

PRODUCTS_EMOJI=
ORDERS_EMOJI=
SUPPORT_EMOJI=
SUCCESS_EMOJI=
ERROR_EMOJI=
STOCK_EMOJI=
ADMIN_EMOJI=

If animated/custom emoji IDs are unavailable, gracefully fall back to normal Unicode emojis.

Do not make the bot dependent on unavailable custom emojis.

==================================================
26. DATABASE
==================================================

Use PostgreSQL + SQLAlchemy.

Tables/models:

users
admins
categories
products
orders
order_items
transactions
wallets
gift_codes
gift_redemptions
referrals
warranties
support_tickets
bot_settings
audit_logs

Use proper:

- Foreign keys
- Indexes
- Constraints
- Transactions
- Relationships

Prevent stock race conditions when multiple users purchase simultaneously.

==================================================
27. PROJECT STRUCTURE
==================================================

Use something like:

app/
    bot/
        handlers/
            user/
            admin/
            products/
            orders/
            support/
            payments/
            referrals/
            gifts/

        keyboards/
            user.py
            admin.py
            products.py
            orders.py

        middlewares/

        states/

        filters/

    database/
        models/
        repositories/
        migrations/

    services/
        product_service.py
        order_service.py
        user_service.py
        payment_service.py
        referral_service.py
        gift_service.py
        warranty_service.py

    core/
        config.py
        logging.py
        security.py

    locales/
        en.json
        hi.json

    utils/

    main.py

tests/

.env.example
requirements.txt
README.md

Keep business logic OUT of Telegram handlers whenever possible.

==================================================
28. SECURITY
==================================================

Implement:

- Admin authorization
- Input validation
- Callback authorization
- Rate limiting
- Flood protection
- Database transaction safety
- Secure environment variables
- Audit logs
- Error handling
- Duplicate purchase protection
- Stock locking/race-condition protection

NEVER trust callback data from users.

Every admin callback must verify:

Telegram user ID ∈ ADMIN_IDS

==================================================
29. ERROR HANDLING
==================================================

The bot must not crash because of:

- Invalid input
- Expired callback
- Database error
- Telegram API error
- Missing product
- Out-of-stock product
- Unauthorized access

Users should receive friendly messages.

Technical errors should be logged.

==================================================
30. BOT COMMANDS
==================================================

User commands:

/start
/products
/orders
/profile
/support
/language
/gift
/refer
/warranty
/topup
/help

Admin:

/admin

Do not expose admin commands to unauthorized users.

==================================================
31. NAVIGATION
==================================================

Every page should have:

[ 🔙 Back ]

and where appropriate:

[ 🏠 Home ]

Avoid sending unnecessary new messages.

Prefer editing existing Telegram messages using callback queries when possible.

==================================================
32. CONFIGURATION
==================================================

Use .env:

BOT_TOKEN=
DATABASE_URL=
REDIS_URL=
ADMIN_IDS=
SUPPORT_GROUP_ID=
DEFAULT_CURRENCY=

Never hard-code secrets.

==================================================
33. DEPLOYMENT
==================================================

The bot must be production-ready for a Linux VPS.

Provide:

- Installation instructions
- Python virtual environment
- Dependency installation
- PostgreSQL setup
- Migration commands
- Environment setup
- Bot startup
- systemd configuration
- Docker configuration if useful
- Logging
- Backup recommendations

==================================================
34. DEVELOPMENT PROCESS
==================================================

DO NOT immediately dump thousands of lines of code.

First provide:

1. Complete architecture
2. Database schema
3. Folder structure
4. User flow
5. Admin flow
6. Callback/interaction architecture
7. Security architecture
8. Implementation phases

Then implement phase by phase.

Every phase must produce working code.

After each phase explain:

- Files created
- Files changed
- What works
- How to run
- How to test
- Next phase

==================================================
FINAL TARGET
==================================================

Build a premium Telegram digital-product store bot with:

✅ Premium Telegram UI
✅ Reply Keyboard
✅ Inline Keyboards
✅ Products
✅ Categories
✅ Product management
✅ Category management
✅ Stock management
✅ Orders
✅ Users
✅ Admin Dashboard
✅ Admin-only panel
✅ Wallet/Top-up architecture
✅ Gift codes
✅ Referral system
✅ Warranty system
✅ Support tickets
✅ Multilingual system
✅ Broadcast
✅ Statistics
✅ Audit logs
✅ PostgreSQL
✅ SQLAlchemy
✅ Secure admin authorization
✅ Production-ready architecture
✅ Linux VPS deployment

Use the attached screenshot as the visual inspiration for the main menu.

IMPORTANT:
This is specifically a TELEGRAM BOT using aiogram 3.x. Do NOT use discord.py, discord.js, Discord interactions, or any Discord-specific architecture.

START NOW BY PROVIDING ONLY:

1. Architecture
2. Database schema
3. Folder structure
4. Complete user flow
5. Complete admin flow
6. Implementation phases

DO NOT WRITE THE FULL IMPLEMENTATION YET.