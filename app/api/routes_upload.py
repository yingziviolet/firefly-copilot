"""CSV 上传接口:解析 -> 逐笔入队,同步只做轻量解析,不做业务。

POST /api/upload/csv  (multipart)
  参数:source=alipay|wechat, file=csv
  流程:按 source 选解析器 -> 每笔 ingest_transaction.delay(txn.dump_for_queue(), trace_id)
  响应 202:{"trace_id", "enqueued": n, "skipped": m}
  解析整体失败(ParseError)-> 400;source 非法 -> 422
"""

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, UploadFile

from app.logger import get_logger, new_trace_id
from app.parsers.alipay import parse_alipay_csv
from app.parsers.base import ParseError, ParseResult
from app.parsers.wechat import parse_wechat_csv
from app.worker.tasks_ingest import ingest_transaction

logger = get_logger(__name__)

# source -> 解析器;新增渠道在此注册
_PARSERS: dict[str, Callable[[bytes], ParseResult]] = {
    "alipay": parse_alipay_csv,
    "wechat": parse_wechat_csv,
}

router = APIRouter(tags=["upload"])


def enqueue_csv(source: str, raw: bytes) -> tuple[str, int, int]:
    """解析 CSV 并逐笔入队,JSON 端点与控制台表单共用。

    返回 (trace_id, enqueued, skipped);source 非法抛 ValueError,解析失败抛 ParseError。
    """
    parser = _PARSERS.get(source)
    if parser is None:
        raise ValueError(f"unsupported source: {source}")

    txns, skipped = parser(raw)

    # 整批共用一个 trace_id,便于按批次全链路追踪
    trace_id = new_trace_id()
    for txn in txns:
        ingest_transaction.delay(txn.dump_for_queue(), trace_id)

    logger.info(
        "csv_upload_enqueued",
        source=source,
        trace_id=trace_id,
        enqueued=len(txns),
        skipped=skipped,
    )
    return trace_id, len(txns), skipped


@router.post("/upload/csv", status_code=202)
async def upload_csv(source: str, file: UploadFile) -> dict:
    raw = await file.read()
    try:
        trace_id, enqueued, skipped = enqueue_csv(source, raw)
    except ParseError as exc:  # 注意:ParseError 是 ValueError 子类,必须先捕获
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"trace_id": trace_id, "enqueued": enqueued, "skipped": skipped}
