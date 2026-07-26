"""Firefly webhook 接收:验签 -> 落队列 -> 202。

POST /api/webhook/firefly
  读取原始 body,verify_signature(settings.firefly_webhook_secret, Signature 头, body)
  失败 -> 401;成功 -> handle_firefly_event.delay(json, trace_id) -> 202 {"trace_id"}
"""

import json

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.logger import get_logger, new_trace_id
from app.services.firefly_webhook import verify_signature
from app.worker.tasks_ingest import handle_firefly_event

logger = get_logger(__name__)

router = APIRouter(tags=["webhook"])


@router.post("/webhook/firefly", status_code=202)
async def firefly_webhook(request: Request) -> dict:
    # 必须用原始字节验签:任何反序列化/重编码都会破坏摘要
    raw_body = await request.body()
    signature = request.headers.get("Signature")
    settings = get_settings()
    if not verify_signature(settings.firefly_webhook_secret, signature, raw_body):
        raise HTTPException(status_code=401, detail="invalid signature")

    # body 非 JSON(或顶层不是对象)时降级为空 dict,保证任务参数类型稳定
    try:
        payload = json.loads(raw_body)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    trace_id = new_trace_id()
    handle_firefly_event.delay(payload, trace_id)
    logger.info("firefly_webhook_enqueued", trace_id=trace_id)
    return {"trace_id": trace_id}
