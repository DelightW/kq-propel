from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter

from app.agent import run_agent_turn

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str
    message: str


@router.post("")
def chat(req: ChatRequest):
    return run_agent_turn(req.session_id, req.message)
