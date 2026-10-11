from types import SimpleNamespace

from app.services import openai_provider


def test_openai_provider_name_uses_endpoint_hostname() -> None:
    assert openai_provider.openai_provider_name('https://api.openai.com/v1') == 'OpenAI – api.openai.com'
    assert openai_provider.openai_provider_name('https://llm.example/v1') == 'OpenAI-kompatibel – llm.example'


def test_dataset_model_filters_non_chat_models() -> None:
    assert openai_provider._dataset_model('gpt-5.1') is True
    assert openai_provider._dataset_model('o3-mini') is True
    assert openai_provider._dataset_model('gpt-image-1') is False
    assert openai_provider._dataset_model('gpt-4o-realtime-preview') is False
    assert openai_provider._dataset_model('text-embedding-3-large') is False


def test_openai_provider_lists_filtered_models(monkeypatch) -> None:
    created = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.models = SimpleNamespace(list=lambda: SimpleNamespace(data=[
                SimpleNamespace(id='gpt-5.1'),
                SimpleNamespace(id='gpt-image-1'),
                SimpleNamespace(id='o3-mini'),
            ]))

        def close(self) -> None:
            created['closed'] = True

    monkeypatch.setattr(openai_provider, 'OpenAI', FakeOpenAI)
    with openai_provider.OpenAIProviderClient(
        ' sk-test ', base_url='https://api.openai.com', timeout=12.0, max_retries=0,
    ) as client:
        assert client.list_models() == ['gpt-5.1', 'o3-mini']

    assert created['base_url'] == 'https://api.openai.com/v1'
    assert created['api_key'] == 'sk-test'
    assert created['timeout'] == 12.0
    assert created['max_retries'] == 0
    assert created['closed'] is True
