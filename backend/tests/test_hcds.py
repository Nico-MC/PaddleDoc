from types import SimpleNamespace

import pytest

from app.services import hcds


def test_normalize_hcds_base_url() -> None:
    assert hcds.normalize_hcds_base_url('https://llm.example/') == 'https://llm.example/v1'
    assert hcds.normalize_hcds_base_url('https://llm.example/v1') == 'https://llm.example/v1'


def test_hcds_client_configures_virtual_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(hcds, 'OpenAI', FakeOpenAI)
    client = hcds.HCDSClient(' university-key ', base_url='https://llm.example')

    assert client.base_url == 'https://llm.example/v1'
    assert captured['api_key'] == 'university-key'
    assert captured['default_headers'] == {'x-bf-vk': 'university-key'}


def test_hcds_client_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match='HCDS_API_BEARER_TOKEN'):
        hcds.HCDSClient('  ')


def test_resolve_dataset_llm_uses_hcds_fallback() -> None:
    endpoint = hcds.resolve_dataset_llm(SimpleNamespace(
        dataset_llm_api_base_url='', dataset_llm_api_key='', dataset_llm_model='local',
        hcds_api_base_url='https://llm.example', hcds_api_bearer_token='vk-test',
        hcds_model='test/model',
    ))

    assert endpoint is not None
    assert endpoint.provider == 'hcds'
    assert endpoint.display_name == 'HCDS – llm.example'
    assert endpoint.base_url == 'https://llm.example/v1'
    assert endpoint.request_headers == {'x-bf-vk': 'vk-test'}
    assert endpoint.model_name == 'test/model'


def test_resolve_dataset_llm_keeps_explicit_provider_precedence() -> None:
    endpoint = hcds.resolve_dataset_llm(SimpleNamespace(
        dataset_llm_api_base_url='http://ollama:11434/v1', dataset_llm_api_key='ollama',
        dataset_llm_model='qwen', hcds_api_bearer_token='vk-test',
    ))

    assert endpoint is not None
    assert endpoint.provider == 'configured'
    assert endpoint.display_name == 'LLM-Server – ollama'
    assert endpoint.request_headers == {}
