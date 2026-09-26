#!/usr/bin/env bash
set -e

echo "=== 🚀 TG Store Bot VPS Deployment Script ==="

# 1. Update and install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "📦 Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
fi

# 2. Ensure docker service is running
sudo systemctl enable --now docker

# 3. Check for .env file
if [ ! -f .env ]; then
    echo "⚠️  .env file missing! Create it next to docker-compose.yml before running again."
    echo "❗ Required: BOT_TOKEN, DATABASE_URL, REDIS_URL, ENCRYPTION_KEY, WALLET_ADDRESS."
    echo "   Usually also: ADMIN_IDS, SUPPORT_GROUP_ID, ORDERS_GROUP_ID (see app/core/config.py)."
    exit 1
fi

# 4. Build and start containers with Docker Compose
echo "🐳 Building and starting services (PostgreSQL, Redis, Bot)..."
docker compose down || true
docker compose up --build -d

echo "✅ Bot successfully deployed and running in background!"
echo "📋 To check logs, run: docker compose logs -f bot"
