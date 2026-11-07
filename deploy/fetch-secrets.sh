#!/bin/bash
# Pull /stockmining/* SecureString parameters from SSM into /etc/stockmining/env (KEY=value).
set -euo pipefail
AWS=$(command -v aws || echo /snap/bin/aws)
REGION=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60" \
  | xargs -I{} curl -s -H "X-aws-ec2-metadata-token: {}" http://169.254.169.254/latest/meta-data/placement/region)
TMP=$(mktemp)
$AWS ssm get-parameters-by-path --region "$REGION" --path /stockmining/ --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text | while IFS=$'\t' read -r name value; do
  echo "${name##*/}=${value}"
done > "$TMP"
{
  echo "STOCKMINING_HOME=/opt/stockmining"
  echo "STOCKMINING_CONFIG=/etc/stockmining/config.yaml"
  cat "$TMP"
} > /etc/stockmining/env
rm -f "$TMP"
chmod 0640 /etc/stockmining/env
chgrp stockmining /etc/stockmining/env 2>/dev/null || true
