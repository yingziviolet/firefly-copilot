# Firefly Copilot

基于 [Firefly III](https://github.com/firefly-iii/firefly-iii) 的智能记账增强服务。它不修改 Firefly III 源码,而是以独立服务的方式通过 REST API + Webhook 集成:多渠道账单(支付宝 / 微信 CSV)接入后,先走本地规则库、再走 LLM 自动分类;高置信度的交易直接写入 Firefly III,低置信度的进入人工复核队列,在内置 Web 控制台按钮裁决;用户的每次改正会自动回流成规则,让系统越用越准。全链路按 trace_id 落审计日志,并内置"重复扣费哨兵"定时巡检,告警推送企业微信。

## 技术栈

- Python 3.12+ / FastAPI / Pydantic v2
- Celery + Redis(异步任务、重试、定时巡检)
- PostgreSQL + SQLAlchemy 2.0 + Alembic
- Anthropic Messages API 协议的 LLM 分类(可通过 `ANTHROPIC_BASE_URL` 切换到自建网关)
- 内置 Web 复核控制台(零前端依赖)/ 企业微信群机器人告警(国内直连,免翻墙)
- structlog 结构化日志 / Docker Compose 部署

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

    subgraph Review["人工复核"]
        WEB[Web 控制台 /review<br/>批准 / 改分类 / 驳回]
    end

    NOTIFY[告警:企业微信群机器人]

    CSV --> UP --> Q
    WH --> FW --> Q
    Q --> ING
    ING -->|先查| RULES
    ING -->|未命中| LLM
    ING -->|置信度达标| FF
    ING -->|置信度不足| DB
    ING -->|待复核提醒| NOTIFY
    WEB -->|裁决| FIN --> FF
    WEB -->|改正回流| RULES
    SEN --> FF
    SEN -->|告警| NOTIFY
    Worker <--> DB
```

## 功能列表

- **多渠道账单接入**:支付宝 / 微信账单上传解析(CSV / XLSX 自动识别),逐笔异步入队处理
- **指纹去重**:同一笔交易(CSV 重复导入、webhook 重放、任务重试)只入库一次
- **两级自动分类**:本地规则库(商户 → 分类)命中即免 LLM;未命中走 LLM 分类,LLM 走 Anthropic Messages API 协议
- **置信度门控**:置信度达阈值(默认 0.9)直接写入 Firefly III;不达标进入人工复核队列
- **Web 复核控制台**:`/review` 一页完成快捷记账、CSV 上传、待复核裁决(批准 / 改分类 / 驳回),手机浏览器可用,`CONSOLE_TOKEN` 鉴权
- **人工复核闭环**:低置信度交易进入复核队列,Web 控制台按钮裁决,裁决后异步写入 Firefly III
- **规则自学习**:人工改正的分类自动回流规则库,同商户后续免 LLM
- **异常检测哨兵**:每日定时扫描近几天的支出,发现同商户同金额的疑似重复扣费即推送企业微信告警
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
2. 告警通道(企业微信,国内直连):任意群 → 群设置 → 群机器人 → 添加,复制 webhook 地址
3. 把 `FIREFLY_PAT`、`ANTHROPIC_API_KEY`、`WECOM_WEBHOOK_URL` 填进 `.env`(公网部署再设 `CONSOLE_TOKEN`)
4. 应用配置:`docker compose up -d --force-recreate api worker beat`
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
| `WECOM_WEBHOOK_URL` | 企业微信群机器人 webhook(告警通道) |
| `CONSOLE_TOKEN` | Web 控制台访问令牌,公网部署必设 |

#### 3. 数据库迁移

```bash
alembic upgrade head
```

迁移 URL 优先读环境变量 `DATABASE_URL`,未设置时回退到 `.env` 里的配置。Docker 方式下 api 容器启动时会自动执行,无需手动跑。

#### 4. 启动服务

Docker 方式(推荐):

```bash
docker compose up -d api worker beat
```

或本地逐个启动:

```bash
uvicorn app.main:app --reload --port 8000          # API
celery -A app.worker.celery_app worker -l INFO     # Worker(Windows 加 --pool=solo)
celery -A app.worker.celery_app beat -l INFO       # Beat(哨兵定时任务)
```

#### 5. 试一笔

```bash
curl -F "file=@alipay_record.csv" "http://localhost:8000/api/upload/csv?source=alipay"
```

返回 `202 {"trace_id": ..., "enqueued": n, "skipped": m}`,随后在 Firefly III 里看到自动分类入账的交易。更省事的方式是直接打开 **`http://localhost:8000/review`**——快捷记账(输入「早餐 15」)、上传 CSV、复核裁决都在这一页;低置信度交易会推提醒到企业微信。

## 日常使用

两个入口记住就够了:**记账/复核用 `:8000/review`,看报表用 `:8080`**。

| 场景 | 操作 |
| --- | --- |
| 随手记一笔 | 打开 `http://localhost:8000/review`,输入「早餐 15」「昨天 打车 23.5」提交,自动分类入账 |
| 批量导账单 | 支付宝/微信 App 导出账单(CSV 或新版 XLSX 都行),在 `/review` 页选择渠道上传;重复导入同一文件不会记重 |
| 处理待复核 | 低置信度交易出现在 `/review` 卡片列表(企业微信也会提醒),点 批准 / 改分类 / 驳回;**每改正一次,同商户下次自动分对** |
| 看账本报表 | `http://localhost:8080`(Firefly III 自带仪表盘、分类统计、预算管理) |
| 重复扣费提醒 | 无需操作,每天 09:00 哨兵自动扫描,命中即推企业微信告警 |
| 启动 / 停止 | `docker compose up -d` / `docker compose down`(数据保存在 Docker 卷里,停了不丢) |
| 排查问题 | `docker compose run --rm api python -m app.doctor` 逐项体检;`docker compose logs -f worker` 看处理日志 |

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
├── api/                # FastAPI 路由:healthz / CSV 上传 / Firefly webhook / Web 控制台
├── llm/                # LLM 客户端(Anthropic Messages API 协议)
├── models/             # SQLAlchemy 模型:rules / review_items / audit_logs / ingested_transactions
├── parsers/            # 解析器:支付宝 / 微信 CSV、快捷记账文本
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
docker-compose.yml      # Firefly III + PG x2 + Redis + api/worker/beat
```

排障自检:`python -m app.doctor`(容器内:`docker compose run --rm api python -m app.doctor`),逐项检查配置与依赖服务连通性;加 `--llm` 可做一次真实 LLM 分类实测。

## 常见问题

**构建报错 `x-docker-expose-session-sharedkey contains value with non-printable ASCII characters`**
项目路径含非 ASCII 字符(如中文目录)触发的 buildkit bug。setup 脚本已内置规避(tar 流式上下文构建);如果手动构建,请照抄脚本里的 `tar ... | docker build -` 写法,或把项目放到纯英文路径。

**装完 Docker Desktop 后反复弹「远程桌面 ActiveX 控件 rdclientax.dll」/「RemoteApp 无法连接」**
是 WSLg(WSL 的 Linux 图形组件,内部走远程桌面协议)在本机加载失败后无限重试,与本项目无关。跑容器用不到 WSLg,直接关掉:在 `%USERPROFILE%\.wslconfig` 写入

```ini
[wsl2]
guiApplications=false
```

然后 `wsl --shutdown` 并重启 Docker Desktop 即可。

**重启 WSL / Docker 后某个端口打不开(浏览器提示连接被重置)**
Docker Desktop 的端口转发在 WSL 重启后偶尔会失联,重启对应容器让它重新注册端口即可:

```bash
docker compose restart firefly   # 8080 打不开重启 firefly;8000 打不开重启 api
```

## Roadmap

### P1(已实现)

- [x] 支付宝 / 微信 CSV 接入与解析
- [x] 交易指纹去重(幂等入库)
- [x] 规则库 + LLM 两级分类,置信度门控
- [x] 人工复核闭环(Web 控制台裁决),改正自动回流规则库
- [x] 重复扣费哨兵(Celery beat 每日巡检 + 企业微信告警)
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
