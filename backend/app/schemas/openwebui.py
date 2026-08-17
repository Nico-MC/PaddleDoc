from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# --- Connections ----------------------------------------------------------

class OpenWebUIConnectionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=1024)
    # Write-only: no response schema in this module has an api_key field.
    api_key: str = Field(min_length=1, max_length=4096)


class OpenWebUIConnectionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=1024)
    # Write-only update: omitted or empty keeps the stored key.
    api_key: str | None = Field(default=None, max_length=4096)


class OpenWebUIConnectionResponse(BaseModel):
    id: str
    name: str
    base_url: str
    # Never the key itself -- just whether one is on file.
    has_api_key: bool = True
    created_at: datetime
    updated_at: datetime

    model_config = {'from_attributes': True}


class OpenWebUIConnectionListResponse(BaseModel):
    items: list[OpenWebUIConnectionResponse]


class OpenWebUIConnectionTestResponse(BaseModel):
    ok: bool
    detail: str | None = None


class OpenWebUIKnowledgeItem(BaseModel):
    id: str
    name: str
    description: str | None = None


class OpenWebUIKnowledgeListResponse(BaseModel):
    items: list[OpenWebUIKnowledgeItem]


# --- Pushes -----------------------------------------------------------------

class OpenWebUIPushCreateRequest(BaseModel):
    connection_id: str = Field(min_length=1)
    knowledge_id: str = Field(min_length=1, max_length=255)
    knowledge_name: str = Field(min_length=1, max_length=255)
    job_ids: list[str] = Field(min_length=1, max_length=200)


class OpenWebUIPushResponse(BaseModel):
    id: str
    job_id: str
    connection_id: str | None = None
    connection_name: str
    knowledge_id: str
    knowledge_name: str
    status: Literal['pending', 'running', 'finished', 'failed']
    error_message: str | None = None
    openwebui_file_id: str | None = None
    # sha256(pushed markdown) != sha256(the job's CURRENT markdown), computed
    # at read time -- see app/api/openwebui_routes.py. Always False for a
    # push that never finished (nothing was pushed to compare against).
    content_stale: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {'from_attributes': True}


class OpenWebUIPushListResponse(BaseModel):
    items: list[OpenWebUIPushResponse]
