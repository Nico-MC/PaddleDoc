from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.models import JobStatus


class UploadResponse(BaseModel):
    job_id: str
    status: JobStatus


class CollectionCreateRequest(BaseModel):
    email: str = ''
    department: str = ''
    folder: str = ''
    subfolder: str = ''
    password: str = ''


class CollectionResponse(BaseModel):
    collection_id: str
    email: str
    department: str
    folder: str = ''
    subfolder: str = ''
    job_ids: list[str] = Field(default_factory=list)


class CollectionStartRequest(BaseModel):
    profile_id: str = Field(min_length=1)


class CollectionStartResponse(BaseModel):
    collection_id: str
    started_jobs: int
    profile_id: str


class JobSaveRequest(BaseModel):
    markdown: str = Field(min_length=1)


class JobSaveResponse(BaseModel):
    job_id: str
    version: int
    path: str
    updated_at: datetime


class JobResponse(BaseModel):
    id: str
    original_filename: str
    status: JobStatus
    tags: list[str] = Field(default_factory=list)
    error_message: str | None = None
    processing_info: dict | None = None
    created_at: datetime
    updated_at: datetime


class JobListResponse(BaseModel):
    items: list[JobResponse]


class JobSearchResponse(JobListResponse):
    total: int


class DashboardStatsResponse(BaseModel):
    processed_documents: int
    processed_pages: int
    errors: int
    database_size_bytes: int | None = None


class HealthResponse(BaseModel):
    status: str


class RuntimeCapabilityInfo(BaseModel):
    torch_available: bool
    cuda_available: bool
    selected_device: Literal['gpu', 'cuda', 'cpu']
    platform: str
    no_cuda_reason: str | None = None


class ContainerState(BaseModel):
    name: str
    state: Literal['running', 'stopped', 'degraded', 'unknown']
    detail: str | None = None


class PaddleStatusResponse(BaseModel):
    status: Literal['running', 'failed', 'stopped']
    detail: str | None = None
    runtime: RuntimeCapabilityInfo | None = None
    pending_jobs: int = 0
    running_jobs: int = 0
    queue_total: int = 0
    running_workers: int = 0
    worker_nodes: list[str] = Field(default_factory=list)
    containers: list[ContainerState] = Field(default_factory=list)


class PaddleSettingsResponse(BaseModel):
    default_profile: str
    timeout_seconds: int


class PaddleSettingsUpdate(BaseModel):
    default_profile: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class PaddleOption(BaseModel):
    value: str
    label: str
    description: str
    text_detection_model_name: str | None = None
    text_recognition_model_name: str | None = None


class PaddleCapabilitiesResponse(BaseModel):
    profiles: list[PaddleOption]


class MarkdownFileEntry(BaseModel):
    path: str
    filename: str
    folder: str
    size_bytes: int
    updated_at: datetime


class MarkdownBrowserResponse(BaseModel):
    items: list[MarkdownFileEntry]


class EvaluationDatasetEntry(BaseModel):
    path: str
    filename: str
    row_count: int
    source_documents: list[str] = Field(default_factory=list)
    size_bytes: int
    updated_at: datetime


class EvaluationDatasetBrowserResponse(BaseModel):
    items: list[EvaluationDatasetEntry]


class EncourageIngestRequest(BaseModel):
    path: str = Field(min_length=1)
    query: str = Field(default='Worum geht es in diesem Dokument?', min_length=1)
    model_name: str | None = None
    run_generation: bool = True


class EncourageRetrieveRequest(BaseModel):
    pipeline_id: str = Field(min_length=1)
    query: str = Field(min_length=1)


class EncourageEvaluateRequest(BaseModel):
    pipeline_id: str = Field(min_length=1)
    dataset_path: str = Field(min_length=1)
    recall_k: int = 3


class EncourageDocumentResponse(BaseModel):
    id: str
    content: str
    score: float
    distance: float | None = None
    meta_data: dict[str, Any] = Field(default_factory=dict)


class EncouragePipelineResponse(BaseModel):
    pipeline_id: str
    collection_name: str
    document_count: int
    top_k: int
    rag_method: str
    ready: bool


class EncourageDebugPayloadResponse(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    collection: dict[str, Any] = Field(default_factory=dict)
    document_dump: dict[str, Any] = Field(default_factory=dict)


class EncourageRagRunResponse(BaseModel):
    query: str
    model_name: str
    answer: str


class EncourageIngestResponse(BaseModel):
    path: str
    filename: str
    source_markdown: dict[str, Any]
    document: EncourageDocumentResponse
    pipeline: EncouragePipelineResponse
    debug: EncourageDebugPayloadResponse
    rag_run: EncourageRagRunResponse | None = None


class EncourageRetrieveResponse(BaseModel):
    pipeline_id: str
    collection_name: str
    query: str
    top_k: int
    results: list[EncourageDocumentResponse] = Field(default_factory=list)


class EncourageEvaluationResponse(BaseModel):
    pipeline_id: str
    collection_name: str
    markdown_path: str
    dataset_path: str
    dataset_filename: str
    question_count: int
    evaluated_question_count: int
    top_k: int
    recall_k: int
    mrr: float
    recall_at_k: float
    hit_rate_at_k: float
    mlflow_run_id: str | None = None


class FolderActionRequest(BaseModel):
    folder: str = ''
    subfolder: str = ''


class FolderActionResponse(BaseModel):
    path: str
    deleted_jobs: int = 0


class PasswordVerificationRequest(BaseModel):
    password: str = Field(min_length=1)
