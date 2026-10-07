from types import SimpleNamespace
import hashlib
import json

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


def test_generate_dataset_rows_rejects_invented_evidence(monkeypatch):
    runner, completions = _runner([
        '{"question":"Wie viel?","gold_answer":"42 Euro.","evidence_quote":"42 Euro pro Jahr."}',
        '{"question":"Welche Frist?","gold_answer":"30 Tage.","evidence_quote":"Erfundener Beleg."}',
        '{}',
    ])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    markdown = '# Leistung\n\n42 Euro pro Jahr.\n\n# Frist\n\n30 Tage.'
    rows = dataset_assistant.generate_dataset_rows(
        markdown=markdown,
        source_document='job.md', source_file='original.pdf',
        source_file_sha256='pdf-sha256',
        api_base_url='http://ollama:11434/v1', api_key='ollama',
        model_name='qwen2.5:14b', question_count=2,
    )
    assert len(rows) == 1
    assert rows[0]['evidence_quote'] == '42 Euro pro Jahr.'
    assert rows[0]['review_status'] == 'synthetic'
    assert rows[0]['source_file'] == 'original.pdf'
    assert rows[0]['source_file_sha256'] == 'pdf-sha256'
    assert rows[0]['source_markdown_sha256'] == hashlib.sha256(markdown.encode('utf-8')).hexdigest()
    assert len(completions.calls) == 3


def test_generate_dataset_rows_requires_valid_evidence(monkeypatch):
    runner, _ = _runner(['{}'])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    with pytest.raises(RuntimeError, match='No questions'):
        dataset_assistant.generate_dataset_rows(
            markdown='Eine Passage.', source_document='job.md', source_file='original.pdf',
            api_base_url='http://ollama:11434/v1', api_key='ollama', model_name='test',
        )


def test_generate_ten_questions_from_one_passage(monkeypatch):
    facts = [f'Leistung {index} betraegt {index * 10} Euro.' for index in range(1, 11)]
    questions = [
        {'question': f'Wie hoch ist Leistung {index}?', 'gold_answer': f'{index * 10} Euro.', 'evidence_quote': fact}
        for index, fact in enumerate(facts, start=1)
    ]
    runner, completions = _runner([
        json.dumps({'questions': questions[:5]}),
        json.dumps({'questions': questions[5:]}),
    ])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    rows = dataset_assistant.generate_dataset_rows(
        markdown=' '.join(facts), source_document='job.md', source_file='original.pdf',
        api_base_url='http://ollama:11434/v1', api_key='ollama', model_name='test', question_count=10,
    )
    assert len(rows) == 10
    assert len(completions.calls) == 2
    assert all(row['evidence_quote'] in ' '.join(facts) for row in rows)


def test_generate_dataset_stops_when_model_repeats_questions(monkeypatch):
    response = json.dumps({'questions': [
        {'question': 'Wie hoch ist die Leistung?', 'gold_answer': '42 Euro.', 'evidence_quote': '42 Euro.'},
    ]})
    runner, completions = _runner([response, response])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    rows = dataset_assistant.generate_dataset_rows(
        markdown='42 Euro.', source_document='job.md', source_file='original.pdf',
        api_base_url='http://ollama:11434/v1', api_key='ollama', model_name='test', question_count=10,
    )
    assert len(rows) == 1
    assert len(completions.calls) == 2
    assert 'Wie hoch ist die Leistung?' in completions.calls[1]['messages'][1]['content']


def test_generate_dataset_does_not_exceed_requested_count(monkeypatch):
    runner, completions = _runner([json.dumps({'questions': [
        {'question': f'Frage {index}?', 'gold_answer': '42 Euro.', 'evidence_quote': '42 Euro.'}
        for index in range(5)
    ]})])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    rows = dataset_assistant.generate_dataset_rows(
        markdown='42 Euro.', source_document='job.md', source_file='original.pdf',
        api_base_url='http://ollama:11434/v1', api_key='ollama', model_name='test', question_count=2,
    )
    assert len(rows) == 2
    assert len(completions.calls) == 1


def test_generation_enforces_json_mode_and_retries_non_json_response(monkeypatch):
    runner, completions = _runner([
        'Hier ist meine Antwort ohne JSON.',
        '{"questions":[{"question":"Wie hoch?","gold_answer":"42 Euro.","evidence_quote":"42 Euro."}]}',
    ])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    rows = dataset_assistant.generate_dataset_rows(
        markdown='42 Euro.', source_document='job.md', source_file='original.pdf',
        api_base_url='http://ollama:11434/v1', api_key='ollama', model_name='test', question_count=1,
    )
    assert len(rows) == 1
    assert len(completions.calls) == 2
    assert all(call['response_format'] == {'type': 'json_object'} for call in completions.calls)


def test_generation_json_retries_are_bounded(monkeypatch):
    runner, completions = _runner(['kein JSON', 'noch kein JSON', '{"questions":'])
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: runner)
    with pytest.raises(RuntimeError, match='after 3 attempt'):
        dataset_assistant.generate_dataset_rows(
            markdown='42 Euro.', source_document='job.md', source_file='original.pdf',
            api_base_url='http://ollama:11434/v1', api_key='ollama', model_name='test', question_count=1,
        )
    assert len(completions.calls) == 3


def test_json_retry_increases_token_limit_on_truncation():
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{}'),
            finish_reason='length' if len(calls) == 1 else 'stop',
        )])
    runner = SimpleNamespace(
        sampling_parameters=SimpleNamespace(seed=42),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    result = dataset_assistant._completion_json(
        runner, model_name='test', system_prompt='Return JSON.', user_prompt='Test.',
        max_tokens=900, json_mode=True,
    )
    assert result == {}
    assert [call['max_tokens'] for call in calls] == [900, 1800]


def test_passage_page_markers_and_offsets():
    markdown = '---\npage_count: 70\n---\n\n## Page 1\n\nErster Beleg.\n\n<!-- page:70/70 -->\nLetzter Beleg.'
    passages = dataset_assistant._markdown_passages(markdown)
    assert [passage.page_number for passage in passages] == [1, 70]
    assert [passage.text for passage in passages] == ['Erster Beleg.', 'Letzter Beleg.']
    assert all(markdown[passage.start:passage.start + len(passage.text)] == passage.text for passage in passages)


def test_sampling_covers_seventy_pages_reproducibly():
    markdown = '\n\n'.join(f'## Page {page}\n\nLeistung auf Seite {page} ist versichert.' for page in range(1, 71))
    passages = dataset_assistant._markdown_passages(markdown)
    mode, regions = dataset_assistant._sampling_regions(passages, 10, 42)
    assert mode == 'pages'
    assert len(regions) == 10
    assert all(len(region) == 7 for region in regions)
    assert [region[0].id for region in regions] == [region[0].id for region in dataset_assistant._sampling_regions(passages, 10, 42)[1]]
    assert [region[0].id for region in regions] != [region[0].id for region in dataset_assistant._sampling_regions(passages, 10, 43)[1]]


def test_sampling_without_markers_uses_text_positions():
    passages = dataset_assistant._markdown_passages('# Inhalt\n\nErster Inhalt.\n\nLetzter Inhalt.')
    mode, regions = dataset_assistant._sampling_regions(passages, 2, 42)
    assert mode == 'text_position'
    assert all(passage.page_number is None for passage in passages)
    assert len(regions) == 2


def test_sampling_excludes_contents_and_empty_material():
    passages = dataset_assistant._markdown_passages('# Inhaltsverzeichnis\n\nKapitel .... 7\n\n# Leistungen\n\n42 Euro.\n\n---')
    _, regions = dataset_assistant._sampling_regions(passages, 10, 42)
    assert [passage.text for region in regions for passage in region] == ['42 Euro.']


def test_generation_distributes_ten_questions_across_seventy_pages(monkeypatch):
    markdown = '\n\n'.join(f'## Page {page}\n\nLeistung auf Seite {page} ist versichert.' for page in range(1, 71))
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: SimpleNamespace())
    calls = []
    def complete(runner, **kwargs):
        passage = kwargs['user_prompt'].split('Passage:\n')[1]
        calls.append(passage)
        return {'questions': [{'question': f'Was sagt {passage}?', 'gold_answer': passage, 'evidence_quote': passage}]}
    monkeypatch.setattr(dataset_assistant, '_completion_json', complete)
    rows = dataset_assistant.generate_dataset_rows(
        markdown=markdown, source_document='job.md', source_file='original.pdf',
        api_base_url='local', api_key='local', model_name='test', question_count=10, sampling_seed=42,
    )
    assert len(rows) == len(calls) == 10
    assert {row['sampling_region'] for row in rows} == set(range(1, 11))
    assert all((row['source_page'] - 1) // 7 + 1 == row['sampling_region'] for row in rows)
    assert all(markdown[row['evidence_start']:row['evidence_end']] == row['evidence_quote'] for row in rows)
    assert all(row['sampling_seed'] == 42 for row in rows)


def test_generation_reports_passage_sampling_progress(monkeypatch):
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: SimpleNamespace())
    monkeypatch.setattr(dataset_assistant, '_completion_json', lambda runner, **kwargs: {
        'questions': [{
            'question': 'Welche Leistung wird genannt?',
            'gold_answer': 'Die Leistung ist versichert.',
            'evidence_quote': 'Die Leistung ist versichert.',
        }],
    })
    progress = []
    rows = dataset_assistant.generate_dataset_rows(
        markdown='## Page 1\n\nDie Leistung ist versichert.',
        source_document='job.md', source_file='original.pdf',
        api_base_url='local', api_key='local', model_name='test', question_count=1,
        sampling_seed=42, progress_callback=progress.append,
    )

    assert len(rows) == 1
    assert progress[-1]['region'] == 1
    assert progress[-1]['region_count'] == 1
    assert progress[-1]['passages_checked'] == 1
    assert progress[-1]['questions_generated'] == 1


def test_generation_tries_fresh_passage_after_empty_result(monkeypatch):
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: SimpleNamespace())
    calls = []
    def complete(runner, **kwargs):
        passage = kwargs['user_prompt'].split('Passage:\n')[1]
        calls.append(passage)
        return {} if len(calls) == 1 else {'questions': [{'question': 'Welche Leistung?', 'gold_answer': passage, 'evidence_quote': passage}]}
    monkeypatch.setattr(dataset_assistant, '_completion_json', complete)
    rows = dataset_assistant.generate_dataset_rows(
        markdown='Erste Leistung.\n\nAndere Leistung.', source_document='job.md', source_file='original.pdf',
        api_base_url='local', api_key='local', model_name='test', question_count=1, sampling_seed=42,
    )
    assert len(rows) == 1
    assert len(calls) == 2 and calls[0] != calls[1]
    assert 'source_page' not in rows[0]


def test_generation_retries_unproductive_region_before_backfill(monkeypatch):
    passages = [f'Abschnitt {index} enthält eine eigene belegbare Leistung.' for index in range(1, 7)]
    monkeypatch.setattr(dataset_assistant, 'create_llm_runner', lambda **_: SimpleNamespace())
    calls = []

    def complete(runner, **kwargs):
        passage = kwargs['user_prompt'].split('Passage:\n')[1]
        calls.append(passage)
        if len(calls) < 5:
            return {'questions': []}
        return {'questions': [{
            'question': 'Welche Leistung wird genannt?',
            'gold_answer': passage,
            'evidence_quote': passage,
        }]}

    monkeypatch.setattr(dataset_assistant, '_completion_json', complete)
    rows = dataset_assistant.generate_dataset_rows(
        markdown='\n\n'.join(passages), source_document='job.md', source_file='original.pdf',
        api_base_url='local', api_key='local', model_name='test', question_count=1, sampling_seed=42,
    )
    assert len(rows) == 1
    assert len(calls) == 5
    assert len(set(calls)) == 5
