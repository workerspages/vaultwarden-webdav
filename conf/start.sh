#!/bin/bash
# 启动入口脚本

echo "=========================================="
echo "  Vaultwarden Backup Admin Starting..."
echo "=========================================="

# 检查是否配置了 DDNSTO Token
if [ -n "$DDNSTO_TOKEN" ] && [ -f /usr/local/bin/ddnsto ]; then
    echo "[DDNSTO] Token detected, enabling DDNSTO service..."
    # 导出 TOKEN 环境变量供 ddnsto 使用
    export TOKEN="$DDNSTO_TOKEN"
else
    echo "[DDNSTO] No token configured or binary missing, DDNSTO disabled."
fi

# 启动 supervisord
exec /usr/bin/supervisord -c /etc/supervisord.conf
