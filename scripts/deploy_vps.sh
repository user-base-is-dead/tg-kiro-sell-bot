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
    echo "⚠️  .env file missing! Creating from .env.example..."
    cp .env.example .env
    echo "❗ Please edit .env with your real BOT_TOKEN, ADMIN_IDS, and credentials before running again."
    exit 1
fi

# 4. Build and start containers with Docker Compose
echo "🐳 Building and starting services (PostgreSQL, Redis, Bot)..."
DOCKER_CMD="docker"
if ! docker info &> /dev/null; then
    DOCKER_CMD="sudo docker"
fi

$DOCKER_CMD compose down || true
$DOCKER_CMD compose up --build -d

echo "✅ Bot successfully deployed and running in background!"
echo "📋 To check logs, run: $DOCKER_CMD compose logs -f bot"
