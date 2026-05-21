#!/usr/bin/env bash
# HelixObs VM setup script.
# Run once after first SSH in, or re-run safely after a rebuild.
# Usage: bash setup-vm.sh [ANTHROPIC_API_KEY]
set -euo pipefail

HELIXOBS_DIR="${HELIXOBS_DIR:-/opt/helixobs}"
DATA_DEVICE="${DATA_DEVICE:-/dev/vdb}"
ANTHROPIC_API_KEY="${1:-${ANTHROPIC_API_KEY:-}}"

# ── Volume ────────────────────────────────────────────────────────────────────

if [ -b "$DATA_DEVICE" ] && ! blkid "$DATA_DEVICE" | grep -q ext4; then
    echo ">>> Formatting $DATA_DEVICE …"
    sudo mkfs.ext4 "$DATA_DEVICE"
fi

if ! mountpoint -q /data; then
    echo ">>> Mounting $DATA_DEVICE → /data"
    sudo mkdir -p /data
    sudo mount "$DATA_DEVICE" /data
fi

if ! grep -q "$DATA_DEVICE" /etc/fstab; then
    echo "$DATA_DEVICE /data ext4 defaults 0 2" | sudo tee -a /etc/fstab
fi

# ── Docker ────────────────────────────────────────────────────────────────────

if ! command -v docker &>/dev/null; then
    echo ">>> Installing Docker …"
    sudo mkdir -p /data/docker /etc/docker
    echo '{"data-root": "/data/docker"}' | sudo tee /etc/docker/daemon.json
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker ubuntu
    echo ">>> Docker installed. You may need to log out and back in for group to take effect."
else
    echo ">>> Docker already installed ($(docker --version))"
fi

# ── Git ───────────────────────────────────────────────────────────────────────

if ! command -v git &>/dev/null; then
    sudo apt-get install -y git
fi

# ── Repos ─────────────────────────────────────────────────────────────────────

REPOS=(deploy herald ui sherlock mock-telescope client-python)

sudo mkdir -p "$HELIXOBS_DIR"
sudo chown ubuntu:ubuntu "$HELIXOBS_DIR"

for repo in "${REPOS[@]}"; do
    if [ -d "$HELIXOBS_DIR/$repo" ]; then
        echo ">>> Pulling $repo …"
        git -C "$HELIXOBS_DIR/$repo" pull --ff-only
    else
        echo ">>> Cloning $repo …"
        git clone "https://github.com/HelixObs/$repo" "$HELIXOBS_DIR/$repo"
    fi
done

# ── Environment ───────────────────────────────────────────────────────────────

ENV_FILE="$HELIXOBS_DIR/deploy/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
GITHUB_TOKEN=
# Public URL of this host (used by UI and Sherlock for Grafana links)
GRAFANA_URL=http://206.12.91.148:3001
HERALD_URL=http://206.12.91.148:8080
SHERLOCK_URL=http://206.12.91.148:8082
EOF
    echo ">>> Created $ENV_FILE — fill in ANTHROPIC_API_KEY if not already set"
else
    echo ">>> $ENV_FILE already exists — skipping"
fi

echo ""
echo "════════════════════════════════════════"
echo " Setup complete."
echo " 1. Edit $ENV_FILE and set ANTHROPIC_API_KEY"
echo " 2. Log out and back in (docker group)"
echo " 3. cd $HELIXOBS_DIR/deploy && docker compose --env-file .env up -d --build"
echo "════════════════════════════════════════"
