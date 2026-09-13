#!/usr/bin/env bash
set -euo pipefail

# trade_master 部署脚本：scp 直传（绕开 GitHub，国内服务器 git 不稳）+ docker compose 重建
# 用法：bash deploy/deploy.sh

# 生产机地址**不写死在这个文件里**（本仓库是公开仓库）。三种给法，优先级从高到低：
#   1) 环境变量   DEPLOY_HOST=1.2.3.4 bash deploy/deploy.sh
#   2) 本机 .env 里的一行 DEPLOY_HOST=1.2.3.4（.env 不入库）
#   3) 都没有 → 直接报错退出，不猜一个默认值
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [ -z "${DEPLOY_HOST:-}" ] && [ -f "$ENV_FILE" ]; then
  # 只抠这一行，不 source 整个文件——.env 里的值不一定都是合法的 shell 赋值
  DEPLOY_HOST="$(grep -E '^[[:space:]]*(export[[:space:]]+)?DEPLOY_HOST=' "$ENV_FILE" | tail -1 \
    | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" -e 's/[[:space:]]*#.*$//')"
fi
DEPLOY_HOST="${DEPLOY_HOST:?请设置 DEPLOY_HOST（生产机地址）——写在 .env 里或用环境变量传入}"
IP="$DEPLOY_HOST"

PEM="${PEM:-$HOME/Downloads/xiongdaxian.pem}"   # SSH 私钥不入库（.gitignore 已挡 *.pem）
REMOTE="${REMOTE:-/root/trade-master}"

# 1. 建远端目录（首次）
ssh -i "$PEM" root@$IP "mkdir -p $REMOTE/data $REMOTE/deploy"

# 2. 传代码（注意：不覆盖服务器上的 .env 和 data/）
scp -i "$PEM" -r app engine app_frontend Dockerfile docker-compose.yml requirements.txt .dockerignore root@$IP:$REMOTE/
scp -i "$PEM" deploy/backup.sh root@$IP:$REMOTE/deploy/

# 3. 若远端还没有 .env，从模板生成（否则跳过，保留已有生产配置）
if ! ssh -i "$PEM" root@$IP "test -f $REMOTE/.env"; then
  echo "⚠️  远端 $REMOTE/.env 不存在，请先手动放置生产 .env（参考 deploy/.env.production.example）再执行部署"
  exit 1
fi

# 4. 重建并启动
ssh -i "$PEM" root@$IP "cd $REMOTE && docker compose up -d --build"

# 5. 健康检查
echo "--- health ---"
ssh -i "$PEM" root@$IP "curl -s http://127.0.0.1:8010/health"
echo ""
echo "--- 容器状态 ---"
ssh -i "$PEM" root@$IP "docker ps --filter name=trade-master"
