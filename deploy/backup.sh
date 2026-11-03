#!/bin/bash
# Consistent SQLite snapshot + principles.md.
#   EC2: → S3 (bucket versioning keeps 30 days).  No bucket (Lightsail): → $DATA/backups, 14 daily copies.
set -euo pipefail
DATA=/var/lib/stockmining
SNAP=$(mktemp -d)
sqlite3 "$DATA/stockmining.db" ".backup '$SNAP/stockmining.db'"
if [ -f /etc/stockmining-bucket ]; then
  AWS=$(command -v aws || echo /snap/bin/aws)
  BUCKET=$(cat /etc/stockmining-bucket)
  $AWS s3 cp --only-show-errors "$SNAP/stockmining.db" "s3://$BUCKET/backups/stockmining.db"
  [ -f "$DATA/principles.md" ] && $AWS s3 cp --only-show-errors "$DATA/principles.md" "s3://$BUCKET/backups/principles.md"
else
  B="$DATA/backups/$(date +%F)"; mkdir -p "$B"
  gzip -c "$SNAP/stockmining.db" > "$B/stockmining.db.gz"
  [ -f "$DATA/principles.md" ] && cp "$DATA/principles.md" "$B/"
  ls -1d "$DATA"/backups/*/ | head -n -14 | xargs -r rm -rf
  chown -R stockmining:stockmining "$DATA/backups"
fi
rm -rf "$SNAP"
