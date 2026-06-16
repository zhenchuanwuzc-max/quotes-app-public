#!/bin/bash
# quotes-app uninstall.sh — 卸载 launchd cron
# 注意：不删数据库 ~/quotes/quotes.db 和备份目录，要删手动 rm
set -e
LABEL="com.ocean.quotes-app"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm "$PLIST_DST"
    echo "✅ 已卸载 $LABEL"
else
    echo "⚠️  $PLIST_DST 不存在，可能已卸载"
fi

echo ""
echo "ℹ️  数据未删（手动 rm 才会删）："
echo "    rm ~/quotes/quotes.db                                              # 主库"
echo "    rm -rf \"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_backups\"  # 备份"
