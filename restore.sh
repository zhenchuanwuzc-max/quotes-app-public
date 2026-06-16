#!/bin/bash
# quotes-app restore.sh — 从 iCloud _backups/ 一键回滚 SQLite
# 用法：
#   bash restore.sh latest           恢复最新一份备份
#   bash restore.sh 20260601-103000  恢复指定时间戳的备份
#   bash restore.sh list             列所有备份
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$SCRIPT_DIR/_backups"
DB_FILE="$HOME/quotes/quotes.db"

if [ ! -d "$BACKUP_DIR" ]; then
    echo "❌ 备份目录不存在：$BACKUP_DIR"
    exit 1
fi

TARGET="${1:-list}"

if [ "$TARGET" = "list" ] || [ -z "$TARGET" ]; then
    echo "可用备份（最新在最上）："
    ls -lht "$BACKUP_DIR"/quotes-*.db 2>/dev/null | head -20 | awk '{print "  " $NF, "(" $5 ", " $6, $7, $8 ")"}'
    echo ""
    echo "用法：bash restore.sh <时间戳 | latest>"
    exit 0
fi

if [ "$TARGET" = "latest" ]; then
    SRC=$(ls -t "$BACKUP_DIR"/quotes-*.db 2>/dev/null | head -1)
    if [ -z "$SRC" ]; then
        echo "❌ 备份目录里没有 quotes-*.db"
        exit 1
    fi
else
    SRC="$BACKUP_DIR/quotes-$TARGET.db"
    if [ ! -f "$SRC" ]; then
        echo "❌ 备份不存在：$SRC"
        echo "   列所有备份：bash restore.sh list"
        exit 1
    fi
fi

echo "📦 准备恢复"
echo "    源：$SRC"
echo "    目标：$DB_FILE"

if [ -f "$DB_FILE" ]; then
    # 恢复前先把当前库备份一份（防误操作）
    SAFETY_BAK="$BACKUP_DIR/quotes-before-restore-$(date +%Y%m%d-%H%M%S).db"
    cp "$DB_FILE" "$SAFETY_BAK"
    echo "    当前库保险备份：$SAFETY_BAK"
fi

read -p "确认恢复？[y/N] " ans
if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
    echo "已取消"
    exit 0
fi

mkdir -p "$(dirname "$DB_FILE")"
cp "$SRC" "$DB_FILE"
echo "✅ 已恢复，记得重启 server（如果在跑）："
echo "    launchctl unload ~/Library/LaunchAgents/com.ocean.quotes-app.plist"
echo "    launchctl load ~/Library/LaunchAgents/com.ocean.quotes-app.plist"
