"""环境自检:python -m app.doctor

用法:
  容器内(推荐):docker compose run --rm api python -m app.doctor
  本地 venv:     python -m app.doctor
  附加 --llm 会真实调用一次 LLM 分类(消耗少量 token)。

逐项检查配置完整性与依赖服务连通性,输出 [ OK ]/[FAIL]/[SKIP],
存在 FAIL 时退出码为 1。
"""

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from app.config import get_settings

CheckResult = tuple[str, str]  # (status: ok/fail/skip, detail)


def _check_config_required() -> list[tuple[str, CheckResult]]:
    s = get_settings()
    items = [
        (
            "配置 FIREFLY_PAT",
            s.firefly_pat,
            "Firefly 后台 Profile -> OAuth -> 创建 PAT 后填入 .env",
        ),
        ("配置 ANTHROPIC_API_KEY", s.anthropic_api_key, "LLM 分类需要;填入 .env"),
        (
            "配置 WECOM_WEBHOOK_URL",
            s.wecom_webhook_url,
            "企业微信任意群 -> 群机器人 -> 添加,复制 webhook 地址填入 .env(告警通道)",
        ),
    ]
    results = []
    for name, value, hint in items:
        if value:
            results.append((name, ("ok", "已填写")))
        else:
            results.append((name, ("fail", f"未填写 —— {hint}")))
    if get_settings().firefly_webhook_secret:
        results.append(("配置 FIREFLY_WEBHOOK_SECRET", ("ok", "已填写")))
    else:
        results.append(
            (
                "配置 FIREFLY_WEBHOOK_SECRET",
                ("skip", "未填写(webhook 功能可选,创建 webhook 后再填)"),
            )
        )
    return results


def _check_database() -> CheckResult:
    from sqlalchemy import inspect

    from app.db import get_engine

    try:
        engine = get_engine()
        with engine.connect():
            pass
        existing = set(inspect(engine).get_table_names())
    except Exception as exc:
        return "fail", f"连接失败:{exc} —— 确认 agent-db 已启动、DATABASE_URL 正确"
    expected = {"rules", "review_items", "audit_logs", "ingested_transactions"}
    missing = expected - existing
    if missing:
        return "fail", (
            f"缺表 {sorted(missing)} —— 执行 alembic upgrade head"
            "(docker 的 api 容器启动时会自动执行)"
        )
    return "ok", "连接正常,表结构齐全"


def _check_redis() -> CheckResult:
    import redis as redis_lib

    try:
        client = redis_lib.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=3, socket_timeout=3
        )
        client.ping()
    except Exception as exc:
        return "fail", f"连接失败:{exc} —— 确认 redis 已启动、REDIS_URL 正确"
    return "ok", "PING 正常"


def _check_firefly_reachable() -> CheckResult:
    from app.services.firefly_client import get_firefly_client

    if get_firefly_client().ping():
        return "ok", "API 可达"
    return "fail", f"{get_settings().firefly_base_url} 不可达 —— 确认 firefly 容器已启动"


def _check_firefly_pat() -> CheckResult:
    from app.services.firefly_client import FireflyError, get_firefly_client

    if not get_settings().firefly_pat:
        return "skip", "PAT 未配置,跳过"
    try:
        categories = get_firefly_client().list_categories()
    except FireflyError as exc:
        return "fail", f"PAT 校验失败:{exc}"
    return "ok", f"PAT 有效(Firefly 现有 {len(categories)} 个分类)"


def _check_wecom() -> CheckResult:
    """企微 webhook 连通性:发送一条自检测试消息(仅在已配置时)。"""
    from app.services.notifier import send_wecom_message

    if not get_settings().wecom_webhook_url:
        return "skip", "webhook 未配置,跳过"
    if send_wecom_message("firefly-copilot 自检测试消息 ✅"):
        return "ok", "测试消息已发送,去群里确认"
    return "fail", "发送失败 —— 检查 webhook 地址是否完整正确"


def _check_llm_live() -> CheckResult:
    from app.llm.client import LLMError, get_llm_client
    from app.schemas.classify import DEFAULT_CATEGORIES
    from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource

    txn = CanonicalTransaction(
        source=TxnSource.MANUAL,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime.now(UTC),
        amount=Decimal("15.00"),
        counterparty="肯德基",
        description="doctor 自检样例",
    )
    try:
        result = get_llm_client().classify_transaction(txn, DEFAULT_CATEGORIES)
    except LLMError as exc:
        return "fail", f"调用失败:{exc}"
    return "ok", f"实测分类「肯德基 15 元」->「{result.category}」(置信度 {result.confidence:.2f})"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    run_llm = "--llm" in argv

    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("数据库(agent 自有库)", _check_database),
        ("Redis(任务队列)", _check_redis),
        ("Firefly III 连通性", _check_firefly_reachable),
        ("Firefly PAT 有效性", _check_firefly_pat),
        ("企业微信告警通道", _check_wecom),
    ]

    results: list[tuple[str, CheckResult]] = list(_check_config_required())
    for name, fn in checks:
        try:
            results.append((name, fn()))
        except Exception as exc:  # 防御:单项异常不阻断其余检查
            results.append((name, ("fail", f"检查过程异常:{exc}")))

    if run_llm:
        try:
            results.append(("LLM 实测分类", _check_llm_live()))
        except Exception as exc:
            results.append(("LLM 实测分类", ("fail", f"检查过程异常:{exc}")))
    else:
        results.append(("LLM 实测分类", ("skip", "默认跳过,加 --llm 参数做一次真实调用")))

    label = {"ok": "[ OK ]", "fail": "[FAIL]", "skip": "[SKIP]"}
    failed = 0
    for name, (status, detail) in results:
        if status == "fail":
            failed += 1
        print(f"{label[status]} {name}:{detail}")

    print()
    if failed:
        print(f"共 {failed} 项未通过。按上面的提示逐项处理后重跑本命令。")
        return 1
    print("全部通过,系统就绪 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
