import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services import gold_pipeline

MARKDOWN = (
    '# Leistungen\n\n'
    'Die Selbstbeteiligung beträgt 500 Euro pro Versicherungsjahr für Zahnersatz im Tarif Premium.\n\n'
    '# Kündigung\n\n'
    'Die Kündigung ist mit einer Frist von drei Monaten zum Ende des Versicherungsjahres möglich.\n'
)
QUOTES = {
    'Selbstbeteiligung': 'Die Selbstbeteiligung beträgt 500 Euro pro Versicherungsjahr für Zahnersatz im Tarif Premium.',
    'Kündigung': 'Die Kündigung ist mit einer Frist von drei Monaten zum Ende des Versicherungsjahres möglich.',
}
CONFIG = gold_pipeline.GoldConfig(
    api_base_url='https://api.openai.com', api_key='key',
    generator_model='gen-model', validator_model='val-model',
)
PASS = {
    'answerable_from_evidence': True, 'answer_complete_and_correct': True,
    'no_unsupported_information': True, 'evidence_sufficient': True, 'verdict': 'PASS', 'reason': 'ok',
}


def _job(question_count=2):
    return gold_pipeline.GoldJob(
        markdown=MARKDOWN, source_document='job-1.md', source_file='original.pdf',
        source_file_sha256='pdf-hash', question_count=question_count,
        question_style='user-paraphrases', focus='', sampling_seed=7,
    )


class _FakeOpenAI:
    """Simulates the Batch API; `handler(body)` returns the model's JSON answer."""

    def __init__(self, handler, pending_polls=0, batch_status='completed', error_message=None):
        self.handler = handler
        self.pending_polls = pending_polls
        self.batch_status = batch_status
        self.error_message = error_message
        self.submitted = []
        self.direct_calls = []
        self.polls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.files = SimpleNamespace(create=self._create_file, content=self._content)
        self.batches = SimpleNamespace(create=self._create_batch, retrieve=self._retrieve, cancel=lambda _id: None)
        self._inputs = {}

    def _create_file(self, file, purpose):
        assert purpose == 'batch'
        lines = [json.loads(line) for line in file[1].decode('utf-8').splitlines()]
        file_id = f'file-{len(self._inputs)}'
        self._inputs[file_id] = lines
        return SimpleNamespace(id=file_id)

    def _create_batch(self, input_file_id, endpoint, completion_window):
        assert endpoint == '/v1/chat/completions' and completion_window == '24h'
        self.submitted.append(self._inputs[input_file_id])
        return SimpleNamespace(id=f'batch-{len(self.submitted) - 1}')

    def _retrieve(self, batch_id):
        self.polls += 1
        if self.pending_polls > 0:
            self.pending_polls -= 1
            return SimpleNamespace(id=batch_id, status='in_progress', output_file_id=None, error_file_id=None)
        return SimpleNamespace(
            id=batch_id, status=self.batch_status, output_file_id=f'out-{batch_id}', error_file_id=None,
        )

    def _chat(self, timeout=None, **body):
        self.direct_calls.append(body)
        if self.error_message:
            raise RuntimeError(self.error_message)
        content = json.dumps(self.handler(body))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    def _content(self, file_id):
        lines = self.submitted[int(file_id.removeprefix('out-batch-'))]
        out = []
        for line in lines:
            if self.error_message:
                out.append(json.dumps({
                    'custom_id': line['custom_id'],
                    'response': {'status_code': 404, 'body': {'error': {'message': self.error_message}}},
                }))
                continue
            content = self.handler(line['body'])
            out.append(json.dumps({
                'custom_id': line['custom_id'],
                'response': {'status_code': 200, 'body': {'choices': [{'message': {'content': json.dumps(content)}}]}},
            }))
        return SimpleNamespace(text='\n'.join(out))


def _kind(body):
    system = body['messages'][0]['content']
    if system.startswith('Du erzeugst'):
        return 'generate'
    return 'escalate' if 'Zweitprüfung' in system else 'validate'


def _generated(body):
    user = body['messages'][1]['content']
    key = 'Selbstbeteiligung' if 'Selbstbeteiligung' in user else 'Kündigung'
    return {'questions': [{
        'question': f'Wie läuft das bei {key}?', 'gold_answer': 'Antwort', 'evidence_quote': QUOTES[key],
    }]}


def _run(monkeypatch, client, job=None):
    monkeypatch.setattr(gold_pipeline, '_client', lambda config: client)
    job = job or _job()
    state = gold_pipeline.new_state(job)
    return gold_pipeline.advance(state, job, CONFIG), job


def test_pipeline_validates_and_saves_gold_rows(monkeypatch):
    def handler(body):
        return _generated(body) if _kind(body) == 'generate' else PASS

    client = _FakeOpenAI(handler)
    state, job = _run(monkeypatch, client)

    assert state['stage'] == 'done'
    assert [{request['body']['model'] for request in batch} for batch in client.submitted] == [
        {'gen-model'}, {'val-model'},
    ]
    assert all(request['body']['reasoning_effort'] == 'medium' for batch in client.submitted for request in batch)
    rows = gold_pipeline.final_rows(state, job, CONFIG)
    assert [row['id'] for row in rows] == ['q001', 'q002']
    for row in rows:
        assert MARKDOWN[row['evidence_start']:row['evidence_end']] == row['evidence_quote']
        assert row['validation_status'] == 'PASS'
        assert row['validation_stage'] == 'validator'
        assert row['generator_model'] == 'gen-model'
        assert row['validator_model'] == 'val-model'
        assert row['prompt_version'] == gold_pipeline.PROMPT_VERSION
        assert row['review_status'] == 'gold'
        assert row['source_file'] == 'original.pdf'
        assert row['evidence_passage_id'].startswith('p')


def test_validator_only_sees_question_answer_and_evidence(monkeypatch):
    client = _FakeOpenAI(lambda body: _generated(body) if _kind(body) == 'generate' else PASS)
    _run(monkeypatch, client)
    prompt = client.submitted[1][0]['body']['messages'][1]['content']
    assert 'evidence_quote:' in prompt and 'Frage:' in prompt and 'Gold-Antwort:' in prompt
    assert '# Leistungen' not in prompt


def test_fail_is_escalated_to_generator_and_passes(monkeypatch):
    def handler(body):
        kind = _kind(body)
        if kind == 'generate':
            return _generated(body)
        return PASS if kind == 'escalate' else {**PASS, 'verdict': 'UNCERTAIN'}

    client = _FakeOpenAI(handler)
    state, job = _run(monkeypatch, client)

    assert {request['body']['model'] for request in client.submitted[2]} == {'gen-model'}
    rows = gold_pipeline.final_rows(state, job, CONFIG)
    assert {row['validation_stage'] for row in rows} == {'escalation'}
    assert {row['validated_by'] for row in rows} == {'gen-model'}


def test_contradictory_pass_is_escalated_and_failed_escalation_is_rejected(monkeypatch):
    def handler(body):
        kind = _kind(body)
        if kind == 'generate':
            return _generated(body)
        return {**PASS, 'evidence_sufficient': False}

    client = _FakeOpenAI(handler)
    state, job = _run(monkeypatch, client)

    assert len(client.submitted) == 3
    assert state['counters']['rejected'] == 2
    with pytest.raises(RuntimeError, match='validation'):
        gold_pipeline.final_rows(state, job, CONFIG)


def test_unverifiable_and_header_evidence_is_dropped_before_validation(monkeypatch):
    def handler(body):
        return {'questions': [
            {'question': 'Was ist SFCR?', 'gold_answer': 'HMK', 'evidence_quote': 'SFCR - HMK'},
            {'question': 'Erfunden?', 'gold_answer': 'Ja', 'evidence_quote': 'Dieser Satz steht nirgends im Dokument.'},
            {'question': 'Heading?', 'gold_answer': 'Ja', 'evidence_quote': '# Leistungen'},
        ]}

    client = _FakeOpenAI(handler)
    state, job = _run(monkeypatch, client)

    # No candidate survives, so no validation batch is ever submitted.
    assert all(_kind(request['body']) == 'generate' for batch in client.submitted for request in batch)
    assert state['accepted'] == []
    with pytest.raises(RuntimeError):
        gold_pipeline.final_rows(state, job, CONFIG)


def test_advance_returns_while_batch_is_running_and_resumes(monkeypatch):
    client = _FakeOpenAI(lambda body: _generated(body) if _kind(body) == 'generate' else PASS, pending_polls=1)
    monkeypatch.setattr(gold_pipeline, '_client', lambda config: client)
    job = _job()
    state = gold_pipeline.advance(gold_pipeline.new_state(job), job, CONFIG)

    assert state['stage'] == 'generate' and state['batch_id'] == 'batch-0'
    state = json.loads(json.dumps(state))
    state = gold_pipeline.advance(state, job, CONFIG)
    assert state['stage'] == 'done'
    assert len(client.submitted) == 2


DIRECT = replace(CONFIG, mode='direct')


def test_direct_mode_runs_all_stages_without_batches_and_reports_progress(monkeypatch):
    client = _FakeOpenAI(lambda body: _generated(body) if _kind(body) == 'generate' else PASS)
    monkeypatch.setattr(gold_pipeline, '_client', lambda config: client)
    job = _job()
    steps = []
    state = gold_pipeline.advance(
        gold_pipeline.new_state(job), job, DIRECT,
        on_step=lambda current: steps.append(gold_pipeline.progress(current, job)),
    )

    assert state['stage'] == 'done' and client.submitted == []
    assert {call['model'] for call in client.direct_calls} == {'gen-model', 'val-model'}
    assert any(step['stage_total'] == 2 and step['stage_done'] == 2 for step in steps)
    assert {step['mode'] for step in steps} == {'direct'}
    assert [row['validation_status'] for row in gold_pipeline.final_rows(state, job, DIRECT)] == ['PASS', 'PASS']


def test_direct_mode_failure_restores_state_and_reports_error(monkeypatch):
    client = _FakeOpenAI(lambda body: {}, error_message='model gen-model not found')
    monkeypatch.setattr(gold_pipeline, '_client', lambda config: client)
    job = _job()
    state = gold_pipeline.new_state(job)

    with pytest.raises(RuntimeError, match='not found'):
        gold_pipeline.advance(state, job, DIRECT)
    assert state['stage'] == 'generate' and state['used_passages'] == [] and 'stage_progress' not in state


def test_failed_batch_submission_does_not_consume_passages(monkeypatch):
    client = _FakeOpenAI(lambda body: {})

    def failing_upload(file, purpose):
        raise ConnectionError('network down')

    client.files.create = failing_upload
    monkeypatch.setattr(gold_pipeline, '_client', lambda config: client)
    job = _job()
    state = gold_pipeline.new_state(job)

    with pytest.raises(ConnectionError):
        gold_pipeline.advance(state, job, CONFIG)
    assert state['used_passages'] == [] and state['plan'] == {} and state['batch_id'] is None


def test_failed_batch_reports_openai_error_details():
    batch = SimpleNamespace(
        id='batch-1', status='failed', output_file_id=None, error_file_id=None,
        errors=SimpleNamespace(data=[SimpleNamespace(code='token_limit_exceeded', message='Enqueued token limit reached')]),
    )
    with pytest.raises(RuntimeError, match='token_limit_exceeded: Enqueued token limit reached'):
        gold_pipeline._collect(None, batch)


def test_failed_batch_requests_surface_the_openai_error(monkeypatch):
    client = _FakeOpenAI(lambda body: {}, error_message='The model `gen-model` does not exist')
    with pytest.raises(RuntimeError, match='does not exist'):
        _run(monkeypatch, client)


def test_markdown_change_during_run_is_rejected(monkeypatch):
    monkeypatch.setattr(gold_pipeline, '_client', lambda config: _FakeOpenAI(lambda body: {}))
    state = gold_pipeline.new_state(_job())
    changed = gold_pipeline.GoldJob(**{**_job().__dict__, 'markdown': MARKDOWN + '\nNeu.'})
    with pytest.raises(RuntimeError, match='changed'):
        gold_pipeline.advance(state, changed, CONFIG)


def test_substantive_evidence_rejects_headers_and_toc():
    assert not gold_pipeline._substantive_evidence('SFCR - HMK')
    assert not gold_pipeline._substantive_evidence('## Überschrift mit vielen Wörtern in einer einzigen Zeile hier')
    assert not gold_pipeline._substantive_evidence('Kapitel mit einem sehr langen Titel für das Verzeichnis ...... 12')
    assert gold_pipeline._substantive_evidence(QUOTES['Kündigung'])
