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
    # Delivery target for every job started in this batch (job.finished /
    # job.failed); None = no webhook. Validated against the caller's own
    # enabled connections once, up front, in start_collection_processing --
    # see app/api/routes.py's _validated_webhook_connection.
    webhook_connection_id: str | None = Field(default=None, min_length=1)


class CollectionStartResponse(BaseModel):
    collection_id: str
    started_jobs: int
    profile_id: str


class JobSaveRequest(BaseModel):
    markdown: str = Field(min_length=1)


class JobRestartRequest(BaseModel):
    profile_id: str | None = None


class JobSaveResponse(BaseModel):
    job_id: str
    version: int
    # Editor versions are now stored as job_markdown_versions rows rather
    # than on-disk '.v{n}.md' files (no shared volume between backend and
    # worker), so there is no longer a filesystem path to report here.
    path: str | None = None
    updated_at: datetime


class JobOwner(BaseModel):
    id: str
    username: str

    model_config = {'from_attributes': True}


class JobResponse(BaseModel):
    id: str
    original_filename: str
    status: JobStatus
    tags: list[str] = Field(default_factory=list)
    error_message: str | None = None
    processing_info: dict | None = None
    content_sha256: str | None = None
    document_version: int = 1
    previous_job_id: str | None = None
    benchmark_run_id: str | None = None
    created_at: datetime
    updated_at: datetime
    # None for legacy jobs (owner_id IS NULL) -- see Job.owner_id / _owner_visible.
    owner: JobOwner | None = None


class JobListResponse(BaseModel):
    items: list[JobResponse]


class JobSearchResponse(JobListResponse):
    total: int


class JobVersionEntry(BaseModel):
    job_id: str
    document_version: int
    content_sha256: str | None = None
    status: JobStatus
    created_at: datetime
    uploaded_by: str | None = None
    is_current: bool


class JobVersionsResponse(BaseModel):
    items: list[JobVersionEntry]


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
    # 'ocr' for the static presets, 'vl' for a dynamic 'vl:<connection_id>'
    # entry (one per enabled VlConnection) -- see
    # paddle_service.get_paddle_capabilities. Defaulted for forward
    # compatibility, though the service always sets it explicitly now.
    kind: str = 'ocr'
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


class EvaluationDatasetDetailResponse(BaseModel):
    path: str
    filename: str
    row_count: int
    source_documents: list[str] = Field(default_factory=list)
    size_bytes: int
    updated_at: datetime
    rows: list[dict[str, Any]] = Field(default_factory=list)


class EncourageIngestRequest(BaseModel):
    path: str = Field(min_length=1)
    query: str = Field(default='Worum geht es in diesem Dokument?', min_length=1)
    model_name: str | None = None
    run_generation: bool = False
    rag_method: str = 'Base'


class EncourageRetrieveRequest(BaseModel):
    pipeline_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    collection_name: str | None = None
    top_k: int | None = None


class EncourageGenerateRequest(BaseModel):
    pipeline_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    model_name: str | None = None


class EncourageGenerateResponse(BaseModel):
    pipeline_id: str
    query: str
    model_name: str
    answer: str
    raw_output: str = ''


class EncourageEvaluateRequest(BaseModel):
    pipeline_id: str = Field(min_length=1)
    dataset_path: str = Field(min_length=1)
    recall_k: int = 3
    evaluation_mode: Literal['standard', 'advanced'] = 'standard'
    model_name: str | None = None
    collection_name: str | None = None
    markdown_path: str | None = None
    top_k: int | None = None
    chunk_max_chars: int | None = None
    chunk_overlap_chars: int | None = None


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
    raw_output: str = ''


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
    mean_average_precision: float = 0.0
    ndcg: float = 0.0
    context_length: float = 0.0
    context_length_metric_source: str = 'encourage_context_length'
    recall_at_k: float
    hit_rate_at_k: float
    evaluation_mode: Literal['standard', 'advanced'] = 'standard'
    retrieval_metrics: dict[str, float] = Field(default_factory=dict)
    advanced_metrics: dict[str, float] = Field(default_factory=dict)
    advanced_status: str = 'disabled'
    warnings: list[str] = Field(default_factory=list)
    mlflow_experiment_id: str | None = None
    mlflow_run_id: str | None = None
    evaluation_summary: dict[str, Any] = Field(default_factory=dict)
    per_question_results: list[dict[str, Any]] = Field(default_factory=list)


class FolderActionRequest(BaseModel):
    folder: str = ''
    subfolder: str = ''


class FolderActionResponse(BaseModel):
    path: str
    deleted_jobs: int = 0


class PasswordVerificationRequest(BaseModel):
    password: str = Field(min_length=1)
