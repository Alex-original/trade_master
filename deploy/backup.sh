#!/usr/bin/env bash
set -euo pipefail

# trade_master PostgreSQL 每日备份
# cron 示例：0 3 * * * /root/trade-master/deploy/backup.sh >> /root/trade-master/backups/backup.log 2>&1

BACKUP_DIR="/root/trade-master/backups"
mkdir -p "$BACKUP_DIR"

STAMP=$(date +%Y%m%d_%H%M%S)
OUT="$BACKUP_DIR/trade_master_$STAMP.sql"

docker exec trade-master-db pg_dump -U trade_master trade_master > "$OUT"

# 只保留最近 7 天
find "$BACKUP_DIR" -name 'trade_master_*.sql' -mtime +7 -delete

echo "backup -> $OUT ($(du -h "$OUT" | cut -f1))"
