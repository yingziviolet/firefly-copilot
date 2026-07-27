<h1 align="center">Firefly Copilot</h1>

<p align="center">
  <strong>面向后端 + AI Agent 开发的财务自动化项目：FastAPI 编排多步调查，LLM 动态调用只读工具，Firefly III 提供真实账本能力。</strong>
</p>

<p align="center">
  <a href="https://github.com/yingziviolet/firefly-copilot/actions/workflows/ci.yml">
    <img alt="CI" src="https://github.com/yingziviolet/firefly-copilot/actions/workflows/ci.yml/badge.svg">
  </a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white">
  <img alt="Docker Compose" src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white">
  <img alt="Self-hosted" src="https://img.shields.io/badge/deploy-self--hosted-16a34a">
</p>

<p align="center">
  <img src="docs/assets/review-console.png" width="760" alt="Firefly Copilot 记账复核台">
</p>

Firefly Copilot 是运行在 [Firefly III](https://github.com/firefly-iii/firefly-iii) 旁边的后端 + AI Agent 增强服务。项目重点不是套一层聊天页面，而是实现一套可运行、可约束、可审计的 Agent 工作流：LLM 根据问题和观察结果规划下一步，FastAPI 与 Pydantic 负责参数校验和工具路由，Firefly API 提供账本数据，SQLAlchemy 持久化完整调查轨迹。

除多步财务调查 Agent 外，项目还覆盖账单导入、规则优先的 AI 分类、置信度门控、人工复核、自然语言查账、异常检测和每周简报。它不修改也不 fork Firefly III 源码，可以通过 Docker Compose 独立部署。

> **当前状态:** P1/P2 与多步财务调查 Agent 已完成，Docker Compose 可复现部署，项目包含 220+ 项自动测试。

## 在 Firefly III 之上增加了什么

| 环节 | Firefly III / 原有基础 | Firefly Copilot 新增 |
| --- | --- | --- |
| 账单接入 | 提供账本与交易 API | 支付宝、微信 CSV/XLSX 解析，渠道自动识别 |
| 重复控制 | 保存最终交易 | 指纹去重，重复上传和任务重试不重复入账 |
| 自动分类 | 提供分类与交易字段 | 本地商户规则优先，未命中再调用 LLM |
| 风险控制 | 人工管理交易 | 置信度门控：高置信度自动入账，低置信度进入复核队列 |
| 人工纠错 | 可手动修改交易 | Web 复核台批准、改分类、驳回；纠错自动沉淀为商户规则 |
| 主动提醒 | 用户主动打开报表 | 企业微信待复核提醒与每周财务简报 |
| 财务洞察 | 图表、预算和报表 | 订阅识别、涨价检测、疑似重复扣费 |
| 查账交互 | 页面筛选与报表 | DeepSeek 理解自然语言，本地生成受限参数并查询 Firefly |
| 多步调查 | 单次筛选或报表 | LLM 按观察结果动态选择四个只读工具，最多三步后形成结论 |
| 本地运行 | Docker 自托管 | Windows 控制面板、一键启动、自检与健康检查 |

这不是聊天机器人直接访问数据库：自然语言查账时，LLM 只负责把问题转换成日期、收支方向、分类、商户和统计方式；程序完成校验后调用 Firefly API，金额计算留在本地，模型不能生成或执行 SQL。

## 技术栈

- Python 3.12+ / FastAPI / Pydantic v2
- Celery + Redis(异步任务、重试、定时巡检)
- PostgreSQL + SQLAlchemy 2.0 + Alembic
- Anthropic Messages API 协议的 LLM 分类与意图解析(`.env.example` 默认 DeepSeek V4 Flash,可切换兼容网关)
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
        AQ[/POST /api/agent/query/]
    end

    Q[(Redis 队列)]

    subgraph Worker["Celery worker / beat"]
        ING[ingest_transaction<br/>指纹去重 → 分类 → 置信度门控]
        FIN[finalize_review]
        EVT[handle_firefly_event<br/>异步审计]
        SEN[周报:收支 / 订阅 / 重复扣费<br/>每周一 09:00]
    end

    RULES[(规则库)]
    LLM[LLM 分类器]
    QUERY[自然语言意图<br/>参数归一化与校验]
    AGENT[财务调查 Agent<br/>决策 → 工具 → observation<br/>最多 3 次]
    DB[(PostgreSQL<br/>rules / review_items /<br/>audit_logs / ingested_transactions)]
    FF[Firefly III API]

    subgraph Review["人工复核"]
        WEB[Web 控制台 /review<br/>记账 / 查账 / 复核]
    end

    NOTIFY[告警:企业微信群机器人]

    CSV --> UP --> Q
    WH --> FW --> EVT --> DB
    Q --> ING
    ING -->|先查| RULES
    ING -->|未命中| LLM
    ING -->|置信度达标| FF
    ING -->|置信度不足| DB
    ING -->|待复核提醒| NOTIFY
    WEB -->|裁决| FIN --> FF
    WEB -->|改正回流| RULES
    WEB -->|快捷记账 / 上传| Q
    WEB -->|自然语言查账| QUERY --> FF
    AQ --> AGENT
    AGENT -->|4 个只读工具| FF
    AGENT -->|逐步审计| DB
    SEN --> FF
    SEN -->|告警| NOTIFY
    Worker <--> DB
```

## 多步财务调查 Agent

`POST /api/agent/query` 提供只读、多步财务调查。它不是把用户问题一次性交给模型，而是运行一个受控循环：

1. LLM 根据问题和已有 `observation` 选择下一项工具及结构化参数；
2. Pydantic 校验参数，后端调用 Firefly API，并用 `Decimal` 完成金额计算；
3. 工具结果作为新 `observation` 回给 LLM，由模型决定继续调查还是结束；
4. 最多允许三次工具尝试，到达上限后强制基于现有证据总结。

模型只能动态选择以下四个只读工具：

- `summarize_spending`：按分类或商户汇总支出；
- `search_transactions`：按日期、分类和商户查询有限条交易；
- `detect_subscriptions`：识别持续订阅和涨价；
- `find_duplicate_charges`：识别相同商户、相同金额的疑似重复扣费。

每次开始、工具调用、成功或失败、最终结束都会按同一个 `trace_id` 写审计日志。模型没有 SQL、写账或修改分类权限，错误工具名和无效参数也只会变成可纠正的 observation。

```bash
curl -X POST http://localhost:8000/api/agent/query \
  -H "Content-Type: application/json" \
  -H "X-Console-Token: $CONSOLE_TOKEN" \
  -d '{"question":"调查这个月支出增加的原因，并检查是否有重复扣费"}'
```

### 可视化演示

Windows 用户双击 `启动记账系统.cmd`，点击“启动并打开财务 Agent”即可进入
`http://localhost:8000/agent`。页面支持直接提问，并展示最终回答、工具调用步骤、
状态和 `trace_id`。

该页面只是演示层；Agent 决策、工具执行、金额计算、参数校验和审计仍全部位于
Python 后端。

### FastAPI、Agent、LangChain 和 RAG 的关系

| 技术 | 在本项目里的职责 | 是否使用 |
| --- | --- | --- |
| FastAPI | 暴露 HTTP 接口、校验请求、鉴权、注入数据库会话和映射错误 | 使用 |
| LLM | 理解调查目标，根据 observation 决定下一步并生成最终结论 | 使用 |
| Agent | 编排“决策 → 工具 → observation → 再决策”，限制步数并审计 | 使用，自研小型 typed loop |
| LangChain | 可提供 Agent/RAG 的通用组件，但不是 Agent 的必要条件 | 未使用；当前循环很小，直接 SDK 更清楚 |
| RAG | 从向量库检索非结构化知识，再作为上下文交给模型 | 未使用；账目是结构化实时数据，应调用受限工具而不是向量检索 |

面试时可以这样概括：**这是一个 FastAPI 承载的、基于 Anthropic structured output 的只读财务 Agent。LLM 负责规划和工具选择，Pydantic 与工具白名单负责边界，确定性 Python 代码负责查询和金额计算，SQLAlchemy 审计每一步；最多三次工具调用防止失控。** 与原来的“单次自然语言转查询参数”相比，关键升级是模型能根据上一步真实结果动态决定下一步。

## 功能列表

- **多渠道账单接入**:支付宝 / 微信账单上传解析(CSV / XLSX 自动识别),逐笔异步入队处理
- **指纹去重**:同一笔交易(CSV 重复导入、webhook 重放、任务重试)只入库一次
- **两级自动分类**:本地规则库(商户 → 分类)命中即免 LLM;未命中走 LLM 分类,LLM 走 Anthropic Messages API 协议
- **置信度门控**:置信度达阈值(默认 0.9)直接写入 Firefly III;不达标进入人工复核队列
- **Web 复核控制台**:`/review` 一页完成快捷记账、自然语言查账、CSV 上传、待复核裁决(批准 / 改分类 / 驳回),手机浏览器可用,`CONSOLE_TOKEN` 鉴权
- **人工复核闭环**:低置信度交易进入复核队列,Web 控制台按钮裁决,裁决后异步写入 Firefly III
- **规则自学习**:人工改正的分类自动回流规则库,同商户后续免 LLM
- **每周财务简报**:每周一 09:00(`Asia/Shanghai`)汇总上周收支、支出分类、订阅涨价与疑似重复扣费,整轮只推送一条企业微信消息
- **订阅管家**:同商户最近 3 笔扣费间隔均为 25–35 天且最近一次不超过 40 天时识别为持续订阅,并比较最近两期金额
- **自然语言查账**:支持「上月餐饮花了多少」「今年打车多少笔」等聚合问题;LLM 只生成受限查询参数,不生成 SQL
- **多步财务调查 Agent**:四个只读工具按 observation 动态编排,最多三步,结构化决策、参数校验、强制收敛
- **全链路审计**:一笔账从接入到入库的每一步都按 trace_id 落审计日志
- **Firefly Webhook 接收**:HMAC 验签后异步记录审计事件,为后续事件联动保留入口

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

DeepSeek 配置示例:

```env
ANTHROPIC_API_KEY=sk-你的密钥
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
LLM_MODEL=deepseek-v4-flash
```

日常使用:`docker compose up -d` 启动全部,`docker compose down` 停止。

### Windows 一键启动

已经完成 `.env` 配置后，双击根目录的 `启动记账系统.cmd`，点击“启动并打开复核台”即可。控制面板还可打开 Firefly III、立即补发周报、查看容器状态和停止服务。

> 每周一 09:00 的自动周报依赖本机和 Docker Desktop 保持运行；错过后可点击“立即补发本周周报”。

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
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `LLM_MODEL` | LLM 凭据;DeepSeek 使用 `https://api.deepseek.com/anthropic` 和 `deepseek-v4-flash`,Anthropic 官方则将 base URL 留空 |
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
# 将路径替换为你自己的支付宝账单文件
curl -F "file=@path/to/alipay_record.csv" "http://localhost:8000/api/upload/csv?source=alipay"
```

返回 `202 {"trace_id": ..., "enqueued": n, "skipped": m}`,随后在 Firefly III 里看到自动分类入账的交易。更省事的方式是直接打开 **`http://localhost:8000/review`**——快捷记账(输入「早餐 15」)、自然语言查账、上传 CSV、复核裁决都在这一页;低置信度交易会推提醒到企业微信。

## 日常使用

三个入口：**Agent 调查用 `:8000/agent`，记账/复核用 `:8000/review`，看报表用
`:8080`**。

| 场景 | 操作 |
| --- | --- |
| 多步财务调查 | 双击启动器并打开财务 Agent，输入问题后查看回答和右侧工具步骤 |
| 随手记一笔 | 打开 `http://localhost:8000/review`,输入「早餐 15」「昨天 打车 23.5」提交,自动分类入账 |
| 自然语言查账 | 在 `/review` 输入「六月在美团花了多少」「这三个月以来交通花了多少钱」「今年打车多少笔」;支持最长 366 天、收入/支出、分类/商户与合计/笔数 |
| 批量导账单 | 支付宝/微信 App 导出账单(CSV 或新版 XLSX 都行),在 `/review` 页选择渠道上传;重复导入同一文件不会记重 |
| 处理待复核 | 低置信度交易出现在 `/review` 卡片列表(企业微信也会提醒),点 批准 / 改分类 / 驳回;**每改正一次,同商户下次自动分对** |
| 看账本报表 | `http://localhost:8080`(Firefly III 自带仪表盘、分类统计、预算管理) |
| 每周财务简报 | 无需操作,每周一 09:00 推送上一完整自然周;收支、订阅和重复扣费合并为一条消息 |
| 启动 / 停止 | `docker compose up -d` / `docker compose down`(数据保存在 Docker 卷里,停了不丢) |
| 排查问题 | `docker compose run --rm api python -m app.doctor` 逐项体检;`docker compose logs -f worker` 看处理日志 |

查账结果会先显示程序理解出的日期、方向、分类或商户和统计方式，再显示 Firefly III 的实际查询结果。结果为 `0.00 CNY` 或 `0 笔` 表示条件已识别、但该范围内没有匹配账目。

更新代码后执行 `docker compose up -d --build --force-recreate api worker beat`。想立即试看周报而不等到周一:

```bash
docker compose exec worker celery -A app.worker.celery_app call app.worker.tasks_sentinel.send_weekly_digest
```

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
├── agent/              # 多步调查循环与四个只读财务工具
├── api/                # FastAPI 路由:healthz / Agent / CSV 上传 / webhook / Web 控制台
├── llm/                # LLM 客户端:分类、意图解析、Agent 结构化决策
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

## 当前边界

- 当前是单用户、自托管项目，不包含 SaaS 多租户与公网托管
- 已内置支付宝、微信账单解析；其他渠道需要新增解析器
- 普通自然语言查账只支持受限聚合；Agent 工具查询最长 366 天且最多返回 20 条交易
- Agent 最多调用三次只读工具，不包含 RAG、长期记忆、multi-agent，也不允许模型生成 SQL
- 每周定时任务依赖运行本项目的电脑或服务器保持在线
- 项目不会读取邮箱或信用卡账户，也不提供投资建议

## Roadmap

### P1(已实现)

- [x] 支付宝 / 微信 CSV 接入与解析
- [x] 交易指纹去重(幂等入库)
- [x] 规则库 + LLM 两级分类,置信度门控
- [x] 人工复核闭环(Web 控制台裁决),改正自动回流规则库
- [x] 重复扣费哨兵
- [x] 全链路 trace_id 审计日志
- [x] Firefly webhook 验签接收(P1 仅落审计)
- [x] Alembic 迁移、Docker Compose 部署、GitHub Actions CI

### P2(已实现)

- [x] 每周财务简报单次推送
- [x] 订阅周期识别与涨价检测
- [x] 受限自然语言查账

### Agent(已实现)

- [x] 四个只读财务调查工具
- [x] 最多三步的结构化决策与 observation 循环
- [x] FastAPI 接口、令牌鉴权和逐步 trace_id 审计

### 后续

- [ ] 本地设置与状态页(Key 只显示配置状态,不回显完整密钥)
- [ ] 周报趋势图与任务运行状态
