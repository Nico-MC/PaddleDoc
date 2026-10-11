from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from openai import OpenAI

from app.services.encourage_bridge import _normalize_openai_base_url
from app.services.openai_provider import openai_provider_name


HCDS_DEFAULT_BASE_URL = 'https://llm.api.hcds.uni-hamburg.de/v1'
HCDS_DEFAULT_MODEL = 'vllm/deepseek-ai/DeepSeek-V4.1-Flash'
HCDS_AUTH_HEADER = 'x-bf-vk'


def normalize_hcds_base_url(base_url: str) -> str:
    cleaned = base_url.strip().rstrip('/')
    if not cleaned:
        cleaned = HCDS_DEFAULT_BASE_URL
    if cleaned.endswith('/v1'):
        return cleaned
    return f'{cleaned}/v1'


@dataclass(frozen=True)
class DatasetLLMEndpoint:
    provider: str
    base_url: str
    display_name: str
    api_key: str
    model_name: str
    request_headers: dict[str, str]


class HCDSClient:
    """OpenAI-compatible HCDS client using the required virtual-key header."""

    def __init__(
        self,
        virtual_key: str,
        *,
        base_url: str = HCDS_DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        key = virtual_key.strip()
        if not key:
            raise ValueError('HCDS_API_BEARER_TOKEN is not configured.')
        self.base_url = normalize_hcds_base_url(base_url)
        self.virtual_key = key
        self.client = OpenAI(
            base_url=self.base_url,
            # The SDK requires api_key even though HCDS authenticates via x-bf-vk.
            api_key=key,
            default_headers={HCDS_AUTH_HEADER: key},
            timeout=timeout,
            max_retries=max_retries,
        )

    @property
    def request_headers(self) -> dict[str, str]:
        return {HCDS_AUTH_HEADER: self.virtual_key}

    def list_models(self) -> list[str]:
        models = self.client.models.list()
        return sorted({
            model.id for model in models.data
            if isinstance(model.id, str) and model.id.strip()
        })

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> 'HCDSClient':
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def resolve_dataset_llm_providers(settings: Any) -> list[DatasetLLMEndpoint]:
    """Return every configured provider available to standard dataset generation."""

    providers: list[DatasetLLMEndpoint] = []
    generic_url = str(getattr(settings, 'dataset_llm_api_base_url', '')).strip()
    generic_key = str(getattr(settings, 'dataset_llm_api_key', '')).strip()
    if generic_url and generic_key:
        hostname = urlsplit(generic_url).hostname or generic_url
        providers.append(DatasetLLMEndpoint(
            provider='configured',
            base_url=generic_url,
            display_name=f'LLM-Server – {hostname}',
            api_key=generic_key,
            model_name=str(getattr(settings, 'dataset_llm_model', '')).strip(),
            request_headers={},
        ))

    hcds_key = str(getattr(settings, 'hcds_api_bearer_token', '')).strip()
    if hcds_key:
        hcds_base_url = normalize_hcds_base_url(str(getattr(settings, 'hcds_api_base_url', '')))
        hostname = urlsplit(hcds_base_url).hostname or hcds_base_url
        providers.append(DatasetLLMEndpoint(
            provider='hcds',
            base_url=hcds_base_url,
            display_name=f'HCDS – {hostname}',
            api_key=hcds_key,
            model_name=(str(getattr(settings, 'hcds_model', '')).strip() or HCDS_DEFAULT_MODEL),
            request_headers={HCDS_AUTH_HEADER: hcds_key},
        ))

    openai_url = str(getattr(settings, 'openai_api_base_url', '')).strip()
    openai_key = str(getattr(settings, 'openai_api_bearer_token', '')).strip()
    if openai_url and openai_key:
        openai_base_url = _normalize_openai_base_url(openai_url)
        providers.append(DatasetLLMEndpoint(
            provider='openai',
            base_url=openai_base_url,
            display_name=openai_provider_name(openai_base_url),
            api_key=openai_key,
            model_name=str(getattr(settings, 'openai_model', 'gpt-6.1-sol')).strip(),
            request_headers={},
        ))
    return providers


def resolve_dataset_llm(settings: Any, provider: str | None = None) -> DatasetLLMEndpoint | None:
    providers = resolve_dataset_llm_providers(settings)
    if provider is not None:
        return next((endpoint for endpoint in providers if endpoint.provider == provider), None)
    # Preserve the previous default: explicit generic config, then HCDS, then OpenAI.
    return providers[0] if providers else None
