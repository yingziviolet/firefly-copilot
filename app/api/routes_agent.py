import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.agent.runner import AgentError, run_agent
from app.config import get_settings
from app.db import get_session
from app.schemas.agent import AgentQuery, AgentResponse

router = APIRouter(prefix="/agent", tags=["agent"])
SessionDep = Annotated[Session, Depends(get_session)]


def require_agent_auth(
    supplied: Annotated[str | None, Header(alias="X-Console-Token")] = None,
) -> None:
    expected = get_settings().console_token
    if expected and (
        supplied is None
        or not secrets.compare_digest(supplied.encode(), expected.encode())
    ):
        raise HTTPException(status_code=401, detail="缺少或错误的访问令牌")


@router.post(
    "/query",
    response_model=AgentResponse,
    dependencies=[Depends(require_agent_auth)],
)
def agent_query(payload: AgentQuery, session: SessionDep) -> AgentResponse:
    try:
        return run_agent(payload.question, session)
    except AgentError as exc:
        raise HTTPException(status_code=503, detail="AI Agent 服务暂时不可用") from exc
