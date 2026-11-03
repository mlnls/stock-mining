#!/bin/bash
# Runs ON the server as root, from the extracted release directory.
#   EC2:       BUCKET=<bucket> bash install.sh      (via SSM send-command from deploy.sh)
#   Lightsail: LOCAL=1 bash install.sh              (via SSH from lightsail.sh; no S3/SSM)
set -euo pipefail

LOCAL="${LOCAL:-}"
[ -n "$LOCAL" ] || BUCKET="${BUCKET:-$(cat /etc/stockmining-bucket)}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/stockmining
DATA=/var/lib/stockmining
ETC=/etc/stockmining
AWS=$(command -v aws || echo /snap/bin/aws)

if [ -n "$LOCAL" ]; then  # what EC2 user-data does
  command -v sqlite3 >/dev/null && dpkg -s python3-venv >/dev/null 2>&1 \
    || { apt-get update -q && apt-get install -y -q python3-venv python3-pip sqlite3 curl; }
  id stockmining >/dev/null 2>&1 \
    || useradd --system --home /var/lib/stockmining --create-home --shell /usr/sbin/nologin stockmining
fi
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
if [ -n "$LOCAL" ] && [ -f /tmp/sm-config.yaml ]; then
  cp /tmp/sm-config.yaml "$ETC/config.yaml"; echo "config uploaded"
elif [ -z "$LOCAL" ] && $AWS s3 cp "s3://$BUCKET/config/config.yaml" "$ETC/config.yaml" 2>/dev/null; then
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
if [ -n "$LOCAL" ]; then
  if [ -s /tmp/sm-secrets ]; then
    { echo "STOCKMINING_HOME=/opt/stockmining"; echo "STOCKMINING_CONFIG=/etc/stockmining/config.yaml"
      cat /tmp/sm-secrets; } > "$ETC/env"
  fi
else
  install -m 0755 "$APP/deploy/fetch-secrets.sh" /usr/local/bin/stockmining-fetch-secrets
  /usr/local/bin/stockmining-fetch-secrets
fi

# 4) restore DB + principles from the latest backup if this is a fresh disk
if [ -z "$LOCAL" ] && [ ! -f "$DATA/stockmining.db" ]; then
  $AWS s3 cp "s3://$BUCKET/backups/stockmining.db" "$DATA/stockmining.db" 2>/dev/null && echo "DB restored" || true
  $AWS s3 cp "s3://$BUCKET/backups/principles.md" "$DATA/principles.md" 2>/dev/null || true
fi
chown -R stockmining:stockmining "$DATA"
chmod 0640 "$ETC"/*; chgrp stockmining "$ETC"/*

# 5) systemd units
install -m 0755 "$APP/deploy/backup.sh" /usr/local/bin/stockmining-backup
cp "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
if [ -n "$LOCAL" ]; then  # shared host: cap jobs so the other services keep running
  for u in stockmining@ stockmining-bot stockmining-web; do
    mkdir -p "/etc/systemd/system/$u.service.d"
    cp "$APP/deploy/systemd/lightsail/limits.conf" "/etc/systemd/system/$u.service.d/limits.conf"
  done
fi
systemctl daemon-reload
systemctl enable --now stockmining-morning.timer stockmining-evening.timer stockmining-weekly.timer
systemctl enable stockmining-bot.service
systemctl restart stockmining-bot.service

# 5b) web dashboard behind the host's existing Caddy (docker) — only when WEB_DOMAIN is given
if [ -n "${WEB_DOMAIN:-}" ]; then
  CADDY="${CADDY_CONTAINER:-more-mlnls-caddy-1}"
  CADDYFILE="${CADDYFILE:-/srv/more-mlnls/deploy/Caddyfile}"
  NET=$(docker inspect "$CADDY" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' | awk '{print $1}')
  GW=$(docker network inspect "$NET" -f '{{(index .IPAM.Config 0).Gateway}}')
  touch "$ETC/web.env"
  grep -q '^WEB_PASSWORD=' "$ETC/web.env" || echo "WEB_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')" >> "$ETC/web.env"
  sed -i '/^WEB_HOST=/d;/^WEB_PORT=/d' "$ETC/web.env"
  printf 'WEB_HOST=%s\nWEB_PORT=8787\n' "$GW" >> "$ETC/web.env"
  chmod 0640 "$ETC/web.env"; chgrp stockmining "$ETC/web.env"
  systemctl enable stockmining-web.service
  systemctl restart stockmining-web.service
  # replace our block between markers; write in place so the container's single-file bind mount sees it
  cp "$CADDYFILE" "$CADDYFILE.bak.stockmining"
  NEW=$(awk '/^# >>> stockmining/{skip=1} !skip{print} /^# <<< stockmining/{skip=0}' "$CADDYFILE")
  printf '%s\n# >>> stockmining (stock-mining/deploy/install.sh 에서 관리)\n%s {\n\tencode zstd gzip\n\theader {\n\t\tX-Robots-Tag "noindex, nofollow"\n\t\t-Server\n\t}\n\treverse_proxy %s:8787\n}\n# <<< stockmining\n' \
    "$NEW" "$WEB_DOMAIN" "$GW" > "$CADDYFILE"
  if docker exec "$CADDY" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
    echo "dashboard: https://$WEB_DOMAIN"
  else
    echo "WARN: Caddyfile invalid — restoring backup"; cat "$CADDYFILE.bak.stockmining" > "$CADDYFILE"
  fi
fi

# 6) first install: initial backfill + baseline backtests (≈5–10 min) in the background
if [ ! -s "$DATA/stockmining.db" ] || ! sqlite3 "$DATA/stockmining.db" "select 1 from prices limit 1" | grep -q 1; then
  systemctl start --no-block stockmining@init.service
  echo "initial backfill started: journalctl -u stockmining@init -f"
else
  # pick up new/changed built-in strategies
  systemctl start --no-block stockmining@backtest.service
fi
echo "install done"
