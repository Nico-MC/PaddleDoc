"""Mail-ingestion API response schemas (docs/integrations/mail-ingestion.md).

Mirrors app/schemas/import_.py's structure (plain BaseModel per resource, one
section per endpoint group). Unlike ImportSourceResponse/ImportRunResponse,
none of these use `model_config = {'from_attributes': True}` +
`model_validate(row)`: every response here is hand-assembled in
app/api/mail_routes.py from a MailMessage row plus (for the detail endpoint)
its attachment Jobs' current status, since the JSON `parts` column needs
per-part enrichment the ORM row alone doesn't carry.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.models import JobStatus


# --- Parts --------------------------------------------------------------------

class MailPartResponse(BaseModel):
    """One `MailMessage.parts[]` manifest entry -- mirrors
    `app.services.mail_ingest.MailPart.to_dict()` plus the route-stamped
    `job_id` for `outcome == 'job'` entries."""

    index: int
    filename: str
    content_type: str
    size_bytes: int
    outcome: Literal['job', 'inline', 'skipped']
    job_id: str | None = None
    skip_reason: str | None = None


class MailPartDetail(MailPartResponse):
    # Populated only on GET /mail/messages/{id} (one join against the
    # attachment Jobs away from the stored manifest) -- list/ingest
    # responses leave these unset so a caller never mistakes a null
    # job_status there for "the job doesn't exist".
    job_status: JobStatus | None = None
    job_error_message: str | None = None


# --- Ingest (POST /mail/messages) ----------------------------------------------

class MailIngestResponse(BaseModel):
    id: str
    replayed: bool
    content_sha256: str
    rfc_message_id: str | None = None
    subject: str
    from_address: str
    recipients: dict = Field(default_factory=dict)
    sent_at: datetime | None = None
    source: str
    raw_size_bytes: int
    body_format: str | None = None
    has_body: bool
    parts: list[MailPartResponse] = Field(default_factory=list)
    created_at: datetime


# --- List / detail --------------------------------------------------------------

class MailMessageListItem(BaseModel):
    id: str
    content_sha256: str
    rfc_message_id: str | None = None
    subject: str
    from_address: str
    recipients: dict = Field(default_factory=dict)
    sent_at: datetime | None = None
    source: str
    raw_size_bytes: int
    body_format: str | None = None
    has_body: bool
    # Blob columns (raw_content) deferred at the query level; parts are
    # included as stored (not job-status-enriched -- see MailPartDetail) so
    # job states are one GET /mail/messages/{id} join away.
    parts: list[MailPartResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MailMessageListResponse(BaseModel):
    items: list[MailMessageListItem]
    total: int


class MailMessageDetailResponse(BaseModel):
    id: str
    content_sha256: str
    rfc_message_id: str | None = None
    subject: str
    from_address: str
    recipients: dict = Field(default_factory=dict)
    sent_at: datetime | None = None
    source: str
    raw_size_bytes: int
    body_format: str | None = None
    has_body: bool
    parts: list[MailPartDetail] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# --- Delete -----------------------------------------------------------------

class MailMessageDeleteResponse(BaseModel):
    id: str
    deleted_jobs: int = 0
