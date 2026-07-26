#!/usr/bin/env bash
# firefly-copilot 一键部署脚本(Linux / macOS 服务器)
# 用法: ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ok()   { printf '\033[32m[ OK ] %s\033[0m\n' "$1"; }
info() { printf '[ .. ] %s\n' "$1"; }
fail() { printf '\033[31m[FAIL] %s\033[0m\n' "$1"; exit 1; }

# --- 1. Docker 可用性 ---
command -v docker >/dev/null 2>&1 || fail "未安装 Docker,请先安装: https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || fail "Docker 守护进程未运行(或当前用户无权限,试试加入 docker 组)"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 不可用,请升级 Docker"
ok "Docker 就绪"

# --- 2. .env ---
if [ ! -f .env ]; then
    cp .env.example .env
    ok "已从 .env.example 生成 .env(密钥稍后填,见最后的下一步提示)"
else
    info ".env 已存在,保持不动"
fi

# --- 3. .env.firefly(自动生成 APP_KEY)---
if [ ! -f .env.firefly ]; then
    key=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
    sed "s/^APP_KEY=.*/APP_KEY=${key}/" .env.firefly.example > .env.firefly
    ok "已生成 .env.firefly(含随机 APP_KEY)"
else
    info ".env.firefly 已存在,保持不动"
fi

# --- 4. 用 stdin tar 上下文构建应用镜像 ---
# 不用 docker compose build:buildkit 对非 ASCII 项目路径(如中文目录)会报
# "x-docker-expose-session-sharedkey" gRPC 错误;tar 流式上下文完全绕开路径。
info "构建应用镜像..."
tar -cf - --exclude=.venv --exclude=.git --exclude=.pytest_cache --exclude=.ruff_cache \
    pyproject.toml app alembic.ini alembic docker \
    | docker build -f docker/Dockerfile -t firefly-copilot-api -
docker tag firefly-copilot-api firefly-copilot-worker
docker tag firefly-copilot-api firefly-copilot-beat
ok "应用镜像构建完成(api/worker/beat 共用一个镜像)"

# --- 5. 启动全部服务(api 启动时自动跑数据库迁移)---
info "启动全部服务...(首次运行需下载 Firefly/Postgres/Redis 镜像)"
docker compose up -d
ok "全部服务已启动"
docker compose ps

cat <<'EOF'

=== 下一步(一次性配置)===
  1. 打开 http://<服务器IP>:8080 注册 Firefly III 账号
     Profile -> OAuth -> Personal Access Token -> 创建 PAT
  2. 企业微信告警:任意群 -> 群设置 -> 群机器人 -> 添加,复制 webhook 地址
  3. 编辑 .env 填入:
       FIREFLY_PAT、ANTHROPIC_API_KEY、WECOM_WEBHOOK_URL
       (公网部署再设 CONSOLE_TOKEN)
  4. 应用新配置:docker compose up -d --force-recreate api worker beat
  5. 自检:      docker compose run --rm api python -m app.doctor

Web 控制台:http://<服务器IP>:8000/review(快捷记账 / 上传 CSV / 复核)
日常使用:docker compose up -d(启动) / docker compose down(停止)
EOF
