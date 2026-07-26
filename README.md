# Firefly Copilot

基于 [Firefly III](https://github.com/firefly-iii/firefly-iii) 的智能记账增强服务。它不修改 Firefly III 源码,而是以独立服务的方式通过 REST API + Webhook 集成:多渠道账单(支付宝 / 微信 CSV)接入后,先走本地规则库、再走 LLM 自动分类;高置信度的交易直接写入 Firefly III,低置信度的进入人工复核队列,由 Telegram 机器人按钮裁决;用户的每次改正会自动回流成规则,让系统越用越准。全链路按 trace_id 落审计日志,并内置"重复扣费哨兵"定时巡检。

## 技术栈

- Python 3.12+ / FastAPI / Pydantic v2
- Celery + Redis(异步任务、重试、定时巡检)
- PostgreSQL + SQLAlchemy 2.0 + Alembic
- Anthropic Messages API 协议的 LLM 分类(可通过 `ANTHROPIC_BASE_URL` 切换到自建网关)
- aiogram Telegram Bot / structlog 结构化日志 / Docker Compose 部署

## 架构

```mermaid
flowchart LR
    subgraph 接入层
        CSV[CSV 上传<br/>支付宝 / 微信]
        WH[Firefly Webhook]
    end

    subgraph API["FastAPI (api)"]
        UP[/POST /api/upload/csv/]
        FW[/POST /api/webhook/firefly/]
    end

    Q[(Redis 队列)]

    subgraph Worker["Celery worker / beat"]
        ING[ingest_transaction<br/>指纹去重 → 分类 → 置信度门控]
        FIN[finalize_review]
        SEN[哨兵:重复扣费扫描<br/>每日 09:00]
    end

    RULES[(规则库)]
    LLM[LLM 分类器]
    DB[(PostgreSQL<br/>rules / review_items /<br/>audit_logs / ingested_transactions)]
    FF[Firefly III API]

    subgraph Bot["Telegram Bot (bot)"]
        TG[复核按钮:通过 / 改分类 / 拒绝]
    end

    CSV --> UP --> Q
    WH --> FW --> Q
    Q --> ING
    ING -->|先查| RULES
    ING -->|未命中| LLM
    ING -->|置信度达标| FF
    ING -->|置信度不足| DB
    ING --> TG
    TG -->|裁决| FIN --> FF
    TG -->|改正回流| RULES
    SEN --> FF
    SEN -->|告警| TG
    Worker <--> DB
```

## 功能列表

- **多渠道账单接入**:支付宝 / 微信账单 CSV 上传解析,逐笔异步入队处理
- **指纹去重**:同一笔交易(CSV 重复导入、webhook 重放、任务重试)只入库一次
- **两级自动分类**:本地规则库(商户 → 分类)命中即免 LLM;未命中走 LLM 分类,LLM 走 Anthropic Messages API 协议
- **置信度门控**:置信度达阈值(默认 0.9)直接写入 Firefly III;不达标进入人工复核队列
- **Telegram 人工复核闭环**:待复核交易推送到 Telegram,按钮裁决(通过 / 改分类 / 拒绝),裁决后异步写入 Firefly III
- **规则自学习**:人工改正的分类自动回流规则库,同商户后续免 LLM
- **异常检测哨兵**:每日定时扫描近几天的支出,发现同商户同金额的疑似重复扣费即 Telegram 告警
- **全链路审计**:一笔账从接入到入库的每一步都按 trace_id 落审计日志
- **Firefly Webhook 接收**:HMAC 验签后落队列处理

## 快速开始

### 一键启动(推荐)

唯一前置条件:装好 [Docker Desktop](https://www.docker.com/products/docker-desktop/)(Windows / macOS)或 [Docker Engine](https://docs.docker.com/engine/install/)(Linux 服务器)。然后:

```powershell
# Windows(PowerShell)
.\scripts\setup.ps1
```

```bash
# Linux / macOS
./scripts/setup.sh
```

脚本自动完成:生成 `.env` 和 `.env.firefly`(含随机 APP_KEY)→ 构建镜像 → 按依赖顺序拉起全部服务(api 容器启动时自动执行数据库迁移)。

之后只剩一次性的账号配置(密钥只能人来创建,脚本最后也会打印这份清单):

1. 打开 `http://localhost:8080` 注册 Firefly III 账号;Profile → OAuth → 创建 Personal Access Token
2. Telegram:找 @BotFather `/newbot` 拿 bot token;找 @userinfobot 拿自己的数字 user id
3. 把 `FIREFLY_PAT`、`ANTHROPIC_API_KEY`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_ALLOWED_USER_IDS`、`TELEGRAM_ALERT_CHAT_ID` 填进 `.env`
4. 应用配置:`docker compose up -d --force-recreate api worker beat bot`
5. 自检:`docker compose run --rm api python -m app.doctor`——逐项告诉你哪里没配好、怎么配

日常使用:`docker compose up -d` 启动全部,`docker compose down` 停止。

### 手动分步(可选,想了解每一步时看这里)

#### 1. 起基础设施(Firefly III + PostgreSQL + Redis)

```bash
# 复制 .env.firefly.example 为 .env.firefly 并生成 APP_KEY(32 位随机串)
docker compose up -d firefly firefly-db agent-db redis
```

启动后访问 `http://localhost:8080` 完成 Firefly III 初始化,创建 Personal Access Token(PAT)。

#### 2. 配置 .env

```bash
cp .env.example .env
```

关键变量(完整列表见 `app/config.py`):

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | 本服务自己的库,默认 `postgresql+psycopg://copilot:copilot@localhost:5433/copilot` |
| `REDIS_URL` | Celery broker/backend,默认 `redis://localhost:6379/0` |
| `FIREFLY_BASE_URL` / `FIREFLY_PAT` | Firefly III 地址与 Personal Access Token |
| `FIREFLY_WEBHOOK_SECRET` | Firefly webhook 验签密钥 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | LLM 凭据;`ANTHROPIC_BASE_URL` 留空走官方,填自建网关地址即可切换 |
| `CONFIDENCE_THRESHOLD` | 自动入账的置信度阈值,默认 `0.9` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_IDS` / `TELEGRAM_ALERT_CHAT_ID` | Telegram 机器人配置 |

#### 3. 数据库迁移

```bash
alembic upgrade head
```

迁移 URL 优先读环境变量 `DATABASE_URL`,未设置时回退到 `.env` 里的配置。Docker 方式下 api 容器启动时会自动执行,无需手动跑。

#### 4. 启动服务

Docker 方式(推荐):

```bash
docker compose up -d api worker beat bot
```

或本地逐个启动:

```bash
uvicorn app.main:app --reload --port 8000          # API
celery -A app.worker.celery_app worker -l INFO     # Worker(Windows 加 --pool=solo)
celery -A app.worker.celery_app beat -l INFO       # Beat(哨兵定时任务)
python -m app.bot.runner                           # Telegram Bot(长轮询)
```

#### 5. 试一笔

```bash
curl -F "file=@alipay_record.csv" "http://localhost:8000/api/upload/csv?source=alipay"
```

返回 `202 {"trace_id": ..., "enqueued": n, "skipped": m}`,随后在 Firefly III 里看到自动分类入账的交易,低置信度的会出现在 Telegram 复核消息里。

## 本地开发

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

pytest -q          # 测试(内存 SQLite + Celery eager + respx mock,不访问真实外部服务)
ruff check .       # Lint
```

注意:

- Windows 下 Celery 的默认进程池不可用,worker 需加 `--pool=solo`
- 测试不需要 Docker,也不需要任何真实凭据,`tests/conftest.py` 已注入假环境变量
- CI(GitHub Actions)在每次 push / PR 时执行 `ruff check .` 与 `pytest -q`

## 目录结构

```
app/
├── api/                # FastAPI 路由:healthz / CSV 上传 / Firefly webhook
├── bot/                # Telegram 机器人:复核按钮裁决(aiogram 长轮询)
├── llm/                # LLM 客户端(Anthropic Messages API 协议)
├── models/             # SQLAlchemy 模型:rules / review_items / audit_logs / ingested_transactions
├── parsers/            # 账单解析器:支付宝 / 微信 CSV
├── schemas/            # Pydantic 模型:标准交易、分类结果
├── services/           # 领域服务:指纹、去重、规则、分类、复核、Firefly 客户端、通知
├── worker/             # Celery:入库管道任务、哨兵定时任务
├── config.py           # pydantic-settings 配置
├── db.py               # engine / session 工厂
├── logger.py           # structlog 配置
└── main.py             # FastAPI 应用工厂
alembic/                # 数据库迁移
docker/                 # Dockerfile
scripts/                # 一键部署脚本(setup.ps1 / setup.sh)
tests/                  # pytest(内存 SQLite + Celery eager + respx)
docker-compose.yml      # Firefly III + PG x2 + Redis + api/worker/beat/bot
```

排障自检:`python -m app.doctor`(容器内:`docker compose run --rm api python -m app.doctor`),逐项检查配置与依赖服务连通性;加 `--llm` 可做一次真实 LLM 分类实测。

## Roadmap

### P1(已实现)

- [x] 支付宝 / 微信 CSV 接入与解析
- [x] 交易指纹去重(幂等入库)
- [x] 规则库 + LLM 两级分类,置信度门控
- [x] Telegram 人工复核闭环,改正自动回流规则库
- [x] 重复扣费哨兵(Celery beat 每日巡检 + Telegram 告警)
- [x] 全链路 trace_id 审计日志
- [x] Firefly webhook 验签接收(P1 仅落审计)
- [x] Alembic 迁移、Docker Compose 部署、GitHub Actions CI

### P2(计划)

- [ ] 邮件账单 / 银行流水等更多接入渠道
- [ ] 更多哨兵规则:预算超支预警、订阅涨价检测、大额异常支出
- [ ] Firefly webhook 事件深度处理(双向同步、账单变更联动)
- [ ] 周报 / 月报自动生成与推送
- [ ] 规则库管理界面(查看 / 编辑 / 禁用规则)
- [ ] 多用户与权限隔离
