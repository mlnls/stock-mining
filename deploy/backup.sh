#!/bin/bash
# Consistent SQLite snapshots + principles (US and KR).
#   EC2: → S3 (bucket versioning keeps 30 days).  No bucket (Lightsail): → $DATA/backups, 14 daily copies.
set -euo pipefail
DATA=/var/lib/stockmining
SNAP=$(mktemp -d)
sqlite3 "$DATA/stockmining.db" ".backup '$SNAP/stockmining.db'"
[ -f "$DATA/stockmining_kr.db" ] && sqlite3 "$DATA/stockmining_kr.db" ".backup '$SNAP/stockmining_kr.db'"
if [ -f /etc/stockmining-bucket ]; then
  AWS=$(command -v aws || echo /snap/bin/aws)
  BUCKET=$(cat /etc/stockmining-bucket)
  $AWS s3 cp --only-show-errors "$SNAP/stockmining.db" "s3://$BUCKET/backups/stockmining.db"
  [ -f "$SNAP/stockmining_kr.db" ] && $AWS s3 cp --only-show-errors "$SNAP/stockmining_kr.db" "s3://$BUCKET/backups/stockmining_kr.db"
  for f in principles.md principles_kr.md; do
    if [ -f "$DATA/$f" ]; then $AWS s3 cp --only-show-errors "$DATA/$f" "s3://$BUCKET/backups/$f"; fi
  done
else
  B="$DATA/backups/$(date +%F)"; mkdir -p "$B"
  gzip -c "$SNAP/stockmining.db" > "$B/stockmining.db.gz"
  [ -f "$SNAP/stockmining_kr.db" ] && gzip -c "$SNAP/stockmining_kr.db" > "$B/stockmining_kr.db.gz"
  for f in principles.md principles_kr.md; do if [ -f "$DATA/$f" ]; then cp "$DATA/$f" "$B/"; fi; done
  ls -1d "$DATA"/backups/*/ | head -n -14 | xargs -r rm -rf
  chown -R stockmining:stockmining "$DATA/backups"
fi
rm -rf "$SNAP"
