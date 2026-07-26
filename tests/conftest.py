"""测试基座:内存 SQLite + Celery eager + 假外部凭据。

所有环境变量必须在导入 app.* 之前设置(settings/engine 都是 lru_cache)。
"""

import os

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["FIREFLY_BASE_URL"] = "http://firefly.test"
os.environ["FIREFLY_PAT"] = "test-pat"
os.environ["FIREFLY_WEBHOOK_SECRET"] = "test-secret"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test-token"
os.environ["TELEGRAM_ALERT_CHAT_ID"] = "10001"
os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "10001"

import pytest  # noqa: E402

from app.db import get_engine, get_session, get_sessionmaker  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture()
def db_session():
    engine = get_engine()
    Base.metadata.create_all(engine)
    session = get_sessionmaker()()
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def celery_eager():
    from app.worker.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield celery_app
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
