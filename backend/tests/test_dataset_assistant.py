from types import SimpleNamespace

import pytest

from app.services import dataset_assistant


class _FakeCompletions:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output))],
        )


def _runner(outputs: list[str]):
    completions = _FakeCompletions(outputs)
    runner = SimpleNamespace(
        model_name='test-model',
        sampling_parameters=SimpleNamespace(seed=42),
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return runner, completions


def test_prepare_dataset_answer_uses_full_document_and_exact_source_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown = '# Leistungen\n\nDie Erstattung beträgt 42 Euro pro Jahr.'
    runner, completions = _runner([
        '''{
          "answerable": true,
          "gold_answer": "Es werden 42 Euro pro Jahr erstattet.",
          "evidence_passage_id": "p0001",
          "evidence_quote": "Die Erstattung beträgt 42 Euro  pro Jahr.",
          "review_note": "Bitte prüfen."
        }''',
    ])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)

    result = dataset_assistant.prepare_dataset_answer(
        markdown=markdown,
        question='Wie hoch ist die Erstattung?',
        api_base_url='https://example.test',
        api_key='secret',
    )

    assert result['answerable'] is True
    assert result['gold_answer'] == 'Es werden 42 Euro pro Jahr erstattet.'
    assert result['evidence_quote'] == 'Die Erstattung beträgt 42 Euro pro Jahr.'
    assert result['evidence_anchor'] == 'Leistungen'
    assert result['search_mode'] == 'full_document'
    assert len(completions.calls) == 1


def test_prepare_dataset_answer_searches_long_document_before_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown = '# Allgemein\n\nNicht relevant.\n\n# Erstattung\n\nVersichert sind 75 Prozent.'
    runner, completions = _runner([
        '{"matches":[{"passage_id":"p0002","relevance":3}]}',
        '''{
          "answerable": true,
          "gold_answer": "Versichert sind 75 Prozent.",
          "evidence_passage_id": "p0002",
          "evidence_quote": "Versichert sind 75 Prozent.",
          "review_note": ""
        }''',
    ])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    monkeypatch.setattr(dataset_assistant, '_DIRECT_DOCUMENT_MAX_CHARS', 10)

    result = dataset_assistant.prepare_dataset_answer(
        markdown=markdown,
        question='Wie viel Prozent sind versichert?',
        api_base_url='https://example.test',
        api_key='secret',
    )

    assert result['answerable'] is True
    assert result['evidence_anchor'] == 'Erstattung'
    assert result['search_mode'] == 'exhaustive_passage_search'
    assert len(completions.calls) == 2


def test_prepare_dataset_answer_does_not_invent_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = _runner([
        '''{
          "answerable": false,
          "gold_answer": "",
          "evidence_passage_id": "",
          "evidence_quote": "",
          "review_note": "Nicht im Dokument geregelt."
        }''',
    ])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)

    result = dataset_assistant.prepare_dataset_answer(
        markdown='# Inhalt\n\nNur vorhandener Text.',
        question='Was steht nicht darin?',
        api_base_url='https://example.test',
        api_key='secret',
    )

    assert result['answerable'] is False
    assert result['gold_answer'] == ''
    assert result['evidence_quote'] == ''
    assert result['review_note'] == 'Nicht im Dokument geregelt.'


def test_prepare_dataset_answer_requires_configured_ai() -> None:
    with pytest.raises(ValueError, match='OpenAI endpoint is not configured'):
        dataset_assistant.prepare_dataset_answer(
            markdown='Text',
            question='Frage?',
            api_base_url='',
            api_key='',
        )
