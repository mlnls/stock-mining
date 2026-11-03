#!/bin/bash
# Runs ON the EC2 instance (as root, via SSM send-command from deploy.sh).
# Usage: BUCKET=<bucket> bash install.sh   (run from the extracted release directory)
set -euo pipefail

BUCKET="${BUCKET:-$(cat /etc/stockmining-bucket)}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/stockmining
DATA=/var/lib/stockmining
ETC=/etc/stockmining
AWS=$(command -v aws || echo /snap/bin/aws)

mkdir -p "$APP" "$DATA" "$ETC"

# 1) code
rsync -a --delete --exclude .venv --exclude data "$SRC"/ "$APP"/ 2>/dev/null || {
  rm -rf "$APP.new" && cp -r "$SRC" "$APP.new" && rm -rf "$APP" && mv "$APP.new" "$APP"; }
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q "$APP"

# 1b) Claude Code (analysis on your Claude subscription via CLAUDE_CODE_OAUTH_TOKEN; skipped if it fails)
chown stockmining:stockmining "$DATA"
if [ ! -x "$DATA/.local/bin/claude" ]; then
  sudo -u stockmining -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash' \
    || echo "WARN: Claude Code install failed — Claude analysis will be skipped"
fi

# 2) config: user's config.yaml from S3 if uploaded, else the example; data paths forced to /var/lib
if $AWS s3 cp "s3://$BUCKET/config/config.yaml" "$ETC/config.yaml" 2>/dev/null; then
  echo "config from S3"
elif [ ! -f "$ETC/config.yaml" ]; then
  cp "$APP/config.example.yaml" "$ETC/config.yaml"
fi
"$APP/.venv/bin/python" - "$ETC/config.yaml" <<'PY'
import sys, yaml
p = sys.argv[1]
c = yaml.safe_load(open(p, encoding="utf-8")) or {}
c["db_path"] = "/var/lib/stockmining/stockmining.db"
c["principles_path"] = "/var/lib/stockmining/principles.md"
yaml.safe_dump(c, open(p, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
PY

# 3) secrets from SSM Parameter Store (/stockmining/ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
install -m 0755 "$APP/deploy/fetch-secrets.sh" /usr/local/bin/stockmining-fetch-secrets
/usr/local/bin/stockmining-fetch-secrets

# 4) restore DB + principles from the latest backup if this is a fresh disk
if [ ! -f "$DATA/stockmining.db" ]; then
  $AWS s3 cp "s3://$BUCKET/backups/stockmining.db" "$DATA/stockmining.db" 2>/dev/null && echo "DB restored" || true
  $AWS s3 cp "s3://$BUCKET/backups/principles.md" "$DATA/principles.md" 2>/dev/null || true
fi
chown -R stockmining:stockmining "$DATA"
chmod 0640 "$ETC"/*; chgrp stockmining "$ETC"/*

# 5) systemd units
install -m 0755 "$APP/deploy/backup.sh" /usr/local/bin/stockmining-backup
cp "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stockmining-morning.timer stockmining-evening.timer stockmining-weekly.timer
systemctl enable stockmining-bot.service
systemctl restart stockmining-bot.service

# 6) first install: initial backfill + baseline backtests (≈5–10 min) in the background
if [ ! -s "$DATA/stockmining.db" ] || ! sqlite3 "$DATA/stockmining.db" "select 1 from prices limit 1" | grep -q 1; then
  systemctl start --no-block stockmining@init.service
  echo "initial backfill started: journalctl -u stockmining@init -f"
else
  # pick up new/changed built-in strategies
  systemctl start --no-block stockmining@backtest.service
fi
echo "install done"
