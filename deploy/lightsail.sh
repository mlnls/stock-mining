#!/bin/bash
# Deploy to an existing Lightsail (or any Ubuntu) host over SSH — no S3/SSM on the server.
# Secrets are read from SSM on THIS machine and piped to /etc/stockmining/env on the host.
#   HOST=54.116.117.190 KEY=~/Downloads/LightsailDefaultKey-ap-northeast-2.pem ./deploy/lightsail.sh
#   WEB_DOMAIN=stock.54-116-117-190.sslip.io ...  → also serve the dashboard through the host's Caddy
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${HOST:?HOST=ip}"; KEY="${KEY:?KEY=path/to/key.pem}"; USER_="${SSH_USER:-ubuntu}"
REGION="${AWS_REGION:-ap-northeast-2}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$USER_@$HOST")

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git archive --format=tar.gz -o "$TMP/sm.tgz" HEAD
scp -q -i "$KEY" "$TMP/sm.tgz" "$USER_@$HOST:/tmp/sm.tgz"
[ -f config.yaml ] && scp -q -i "$KEY" config.yaml "$USER_@$HOST:/tmp/sm-config.yaml"

# secrets: SSM (local) → stdin → host, never written to local disk
aws ssm get-parameters-by-path --region "$REGION" --path /stockmining/ --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text \
  | awk -F'\t' '{n=$1; sub(".*/","",n); print n "=" $2}' \
  | "${SSH[@]}" 'umask 077; cat > /tmp/sm-secrets'

"${SSH[@]}" "WEB_DOMAIN='${WEB_DOMAIN:-}'; "'set -e
  sudo rm -rf /tmp/sm && mkdir -p /tmp/sm && tar xzf /tmp/sm.tgz -C /tmp/sm
  sudo LOCAL=1 WEB_DOMAIN='"${WEB_DOMAIN:-}"' bash /tmp/sm/deploy/install.sh
  rm -f /tmp/sm.tgz /tmp/sm-secrets /tmp/sm-config.yaml'
