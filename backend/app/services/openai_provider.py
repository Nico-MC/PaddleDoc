from __future__ import annotations

from urllib.parse import urlsplit

from openai import OpenAI

from app.services.encourage_bridge import _normalize_openai_base_url


_OPENAI_TEXT_MODEL_PREFIXES = ('gpt-', 'chatgpt-', 'o1', 'o3', 'o4', 'ft:gpt-')
_OPENAI_NON_TEXT_MARKERS = (
    'audio', 'codex', 'deep-research', 'image', 'instruct', 'live', 'realtime',
    'search', 'transcribe', 'tts',
)


def openai_provider_name(base_url: str) -> str:
    hostname = urlsplit(_normalize_openai_base_url(base_url)).hostname or base_url
    prefix = 'OpenAI' if hostname == 'api.openai.com' else 'OpenAI-kompatibel'
    return f'{prefix} – {hostname}'


def _dataset_model(model_id: str) -> bool:
    """Keep models likely to support text generation for dataset creation."""

    lowered = model_id.lower()
    return (
        model_id.startswith(_OPENAI_TEXT_MODEL_PREFIXES)
        and not any(marker in lowered for marker in _OPENAI_NON_TEXT_MARKERS)
    )


class OpenAIProviderClient:
    """OpenAI provider client with dynamic model discovery."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise ValueError('OPENAI_API_BEARER_TOKEN is not configured.')
        self.base_url = _normalize_openai_base_url(base_url)
        self.api_key = key
        self.display_name = openai_provider_name(self.base_url)
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    def list_models(self) -> list[str]:
        models = self.client.models.list()
        return sorted({
            model.id for model in models.data
            if isinstance(model.id, str) and _dataset_model(model.id)
        })

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> 'OpenAIProviderClient':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
