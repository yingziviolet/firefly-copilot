# Firefly Copilot

基于 [Firefly III](https://github.com/firefly-iii/firefly-iii) 的智能记账增强服务:多渠道账单接入、LLM 自动分类与规则学习、异常检测哨兵、人工复核闭环。

不修改 Firefly III 源码,通过 REST API + Webhook 以独立服务方式集成。

## 技术栈

- Python 3.12 / FastAPI / Pydantic v2
- Celery + Redis(异步任务、重试与死信)
- PostgreSQL + SQLAlchemy 2.0
- Docker Compose 部署

## 状态

🚧 开发中(WIP)
