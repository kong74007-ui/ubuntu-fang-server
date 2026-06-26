#!/usr/bin/env bash
# 黄雀 AI 主站一键部署：rsync → 改属主 → 注入获客口令(从服务器 systemd env 读，不落 git)
# 用法：bash scripts/deploy_site.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HOME/.ssh/dapeng_server_ed25519"
HOST="dapeng-server"
WEBROOT="/var/www/huangquechuanmei"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $HOST"

echo "▸ 1/3 rsync site/ → $HOST:$WEBROOT"
rsync -az --delete \
  --exclude '_cloud_src/' --exclude '_logo_gen/' --exclude '_preview/' --exclude 'assets_raw/' --exclude '.DS_Store' \
  --rsync-path="sudo rsync" \
  -e "ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "$ROOT/site/" "$HOST:$WEBROOT/"

echo "▸ 2/3 改属主 www-data"
$SSH "sudo chown -R www-data:www-data $WEBROOT"

echo "▸ 3/3 注入获客口令(__LEADGEN_PW__ ← systemd env，口令不回显)"
$SSH '
PW=$(sudo grep -rhoE "LEADGEN_PASSWORD=[^ \"]+" /etc/systemd/system/*.service 2>/dev/null | head -1 | cut -d= -f2)
if [ -z "$PW" ]; then echo "  ⚠ 没读到 LEADGEN_PASSWORD，获客页口令未注入(获客功能会 403)"; exit 0; fi
sudo sed -i "s/__LEADGEN_PW__/$PW/" '"$WEBROOT"'/workbench/leads.html
if grep -q "__LEADGEN_PW__" '"$WEBROOT"'/workbench/leads.html; then echo "  ⚠ 注入失败(占位符仍在)"; else echo "  ✓ 口令已注入(长度 ${#PW})"; fi
'
echo "✅ 部署完成 → https://huangquechuanmei.com"
