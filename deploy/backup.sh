#!/bin/bash
# Consistent SQLite snapshot + principles.md → S3 (bucket versioning keeps 30 days of history).
set -euo pipefail
AWS=$(command -v aws || echo /snap/bin/aws)
BUCKET=$(cat /etc/stockmining-bucket)
DATA=/var/lib/stockmining
SNAP=$(mktemp -d)
sqlite3 "$DATA/stockmining.db" ".backup '$SNAP/stockmining.db'"
$AWS s3 cp --only-show-errors "$SNAP/stockmining.db" "s3://$BUCKET/backups/stockmining.db"
[ -f "$DATA/principles.md" ] && $AWS s3 cp --only-show-errors "$DATA/principles.md" "s3://$BUCKET/backups/principles.md"
rm -rf "$SNAP"
