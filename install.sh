#!/bin/bash
# 一键安装：venv + systemd（需 root 执行 systemctl 部分）
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

[ -f config.json ] || cp config.example.json config.json
python3 -m venv .venv 2>/dev/null || true
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
chmod 600 config.json

mkdir -p logs
sed "s|__APP_DIR__|$APP_DIR|g" loomy2api.service > .loomy2api.service.generated

echo "✅ 依赖就绪。启动方式二选一："
echo "   A) 前台调试：  ./.venv/bin/python app.py"
echo "   B) systemd：   sudo install -m 644 .loomy2api.service.generated /etc/systemd/system/loomy2api.service"
echo "                  sudo systemctl daemon-reload && sudo systemctl enable --now loomy2api"
echo
echo "首次启动会自动生成面板密码，见 logs/service.log："
echo "   grep 面板密码 logs/service.log"
