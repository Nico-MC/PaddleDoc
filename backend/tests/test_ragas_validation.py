import asyncio
from types import SimpleNamespace

import pytest

from app.services import ragas_validation
from app.services.ragas_validation import _classify_scores, _classify_validation, _score_value


def _mock_quality_metrics(monkeypatch, verdicts=None):
    from ragas import metrics

    class QualityMetric:
        def __init__(self, *, name, prompt, allowed_values):
            self.name = name
            self.prompt = prompt
            assert allowed_values == ['pass', 'review', 'fail']

        async def ascore(self, **arguments):
            assert arguments['question']
            assert arguments['answer']
            assert arguments['context']
            verdict = (verdicts or {}).get(self.name, 'pass')
            if isinstance(verdict, Exception):
                raise verdict
            return SimpleNamespace(value=verdict, reason='Konkreter Prüfgrund.')

    monkeypatch.setattr(metrics, 'DiscreteMetric', QualityMetric)


@pytest.mark.parametrize(('faithfulness', 'relevance', 'expected'), [
    (1.0, 1.0, 'accepted'),
    (0.85, 0.70, 'accepted'),
    (0.84, 0.70, 'review'),
    (0.80, 0.60, 'review'),
    (0.49, 1.0, 'rejected'),
    (1.0, 0.39, 'rejected'),
    (None, 1.0, 'review'),
    (1.0, None, 'review'),
    (0.4, None, 'rejected'),
])
def test_ragas_validation_classifies_scores(faithfulness, relevance, expected):
    assert _classify_scores(faithfulness, relevance) == expected


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -0.1, 1.1])
def test_ragas_validation_rejects_invalid_metric_values(value):
    with pytest.raises(ValueError):
        _score_value(SimpleNamespace(value=value))


def test_discrete_fail_overrides_good_numeric_scores():
    criteria = {name: 'pass' for name in ragas_validation.QUALITY_CRITERIA}
    criteria['informativeness'] = 'fail'
    assert _classify_validation(1.0, 0.94, criteria) == 'rejected'
    criteria['informativeness'] = 'review'
    assert _classify_validation(1.0, 0.94, criteria) == 'review'
    criteria['informativeness'] = 'pass'
    assert _classify_validation(1.0, 0.94, criteria) == 'accepted'
    assert _classify_validation(1.0, 0.94, {}) == 'review'


def test_installed_ragas_discrete_metric_returns_verdict_and_reason():
    from ragas.metrics import DiscreteMetric

    class FakeLLM:
        async def agenerate(self, prompt, *, response_model):
            assert 'Was steht in Tabelle 17?' in prompt
            assert 'Tabelle 17: Leistungen' in prompt
            return response_model(value='fail', reason='Keine konkrete fachliche Information.')

    metric = DiscreteMetric(
        name='informativeness', allowed_values=['pass', 'review', 'fail'],
        prompt=ragas_validation._quality_prompt('informativeness', ragas_validation.QUALITY_CRITERIA['informativeness']),
    )
    result = asyncio.run(metric.ascore(
        llm=FakeLLM(), question='Was steht in Tabelle 17?', answer='Tabelle 17.',
        context='Tabelle 17: Leistungen',
    ))
    assert result.value == 'fail'
    assert result.reason == 'Keine konkrete fachliche Information.'

def test_rejection_reason_names_failing_metrics_and_thresholds():
    assert ragas_validation._score_reason(0.11, 0.91, []) == (
        'Faithfulness 0.11 unter Ablehnungsgrenze 0.50'
    )
    assert ragas_validation._score_reason(0.9, 0.39, []) == (
        'Antwort-Relevanz 0.39 unter Ablehnungsgrenze 0.40'
    )


def test_failed_relevancy_keeps_question_and_records_message(monkeypatch):
    from ragas import llms
    from ragas.metrics import collections

    class FaithfulnessMetric:
        def __init__(self, *args, **kwargs):
            pass

        async def ascore(self, **arguments):
            return SimpleNamespace(value=1.0)

    class RelevancyMetric(FaithfulnessMetric):
        async def ascore(self, **arguments):
            raise ValueError('Nicht endlicher Relevanzwert')

    monkeypatch.setattr(llms, 'llm_factory', lambda *args, **kwargs: object())
    monkeypatch.setattr(collections, 'Faithfulness', FaithfulnessMetric)
    monkeypatch.setattr(collections, 'AnswerRelevancy', RelevancyMetric)
    _mock_quality_metrics(monkeypatch)
    monkeypatch.setattr(ragas_validation, '_local_embeddings', lambda: object())
    events = []
    rows, summary = ragas_validation.validate_ragas_dataset_rows(
        rows=[{'id': 'q007', 'question': 'Was gilt?', 'gold_answer': 'Eine Leistung.',
               'evidence_quote': 'Die Police gewährt eine Leistung.'}],
        api_base_url='http://localhost:12345/v1', api_key='test', model_name='fake-model',
        request_headers={}, audit_callback=events.append,
    )
    assert len(rows) == 1
    assert rows[0]['validation_status'] == 'review'
    assert rows[0]['validation_answer_relevancy'] is None
    assert rows[0]['validation_criteria']['informativeness'] == 'pass'
    assert rows[0]['validation_errors'] == ['answer_relevancy: ValueError: Nicht endlicher Relevanzwert']
    assert 'Nicht endlicher Relevanzwert' in summary['details'][0]['reason']
    failed = next(event for event in events if event['type'] == 'validation_result' and event['status'] == 'error')
    assert failed['error_message'] == 'Nicht endlicher Relevanzwert'


def test_validation_audit_records_exact_metric_arguments(monkeypatch):
    from ragas import llms
    from ragas.metrics import collections

    calls = []

    class Metric:
        def __init__(self, *args, **kwargs):
            pass

        async def ascore(self, **arguments):
            calls.append(arguments)
            return SimpleNamespace(value=1.0)

    monkeypatch.setattr(llms, 'llm_factory', lambda *args, **kwargs: object())
    monkeypatch.setattr(collections, 'Faithfulness', Metric)
    monkeypatch.setattr(collections, 'AnswerRelevancy', Metric)
    _mock_quality_metrics(monkeypatch)
    monkeypatch.setattr(ragas_validation, '_local_embeddings', lambda: object())
    events = []
    rows, summary = ragas_validation.validate_ragas_dataset_rows(
        rows=[{'id': 'q001', 'question': 'Was gilt?', 'gold_answer': 'Eine Leistung.',
               'evidence_quote': 'Die Police gewährt eine Leistung.'}],
        api_base_url='http://localhost:12345/v1', api_key='test', model_name='fake-model',
        request_headers={}, audit_callback=events.append,
    )

    assert len(rows) == 1
    assert summary['accepted'] == 1
    assert summary['thresholds']['reject_faithfulness'] == 0.5
    assert summary['details'][0]['reason'] is None
    numeric_names = {'faithfulness', 'answer_relevancy'}
    assert [event['arguments'] for event in events if event['type'] == 'validation_input' and event['metric'] in numeric_names] == calls
    assert [event['score'] for event in events if event['type'] == 'validation_result' and event['metric'] in numeric_names] == [1.0, 1.0]
    inputs = [event for event in events if event['type'] == 'validation_input' and event['metric'] in numeric_names]
    assert inputs[0]['metric'] == 'faithfulness'
    assert inputs[0]['arguments']['retrieved_contexts'] == ['Die Police gewährt eine Leistung.']
    assert inputs[1]['metric'] == 'answer_relevancy'
    assert 'retrieved_contexts' not in inputs[1]['arguments']
    assert rows[0]['validation_criteria'] == {name: 'pass' for name in ragas_validation.QUALITY_CRITERIA}
    assert len([event for event in events if event['type'] == 'validation_input']) == 8


def test_informativeness_failure_rejects_candidate_with_reason(monkeypatch):
    from ragas import llms
    from ragas.metrics import collections

    class NumericMetric:
        def __init__(self, *args, **kwargs):
            pass

        async def ascore(self, **arguments):
            return SimpleNamespace(value=1.0)

    monkeypatch.setattr(llms, 'llm_factory', lambda *args, **kwargs: object())
    monkeypatch.setattr(collections, 'Faithfulness', NumericMetric)
    monkeypatch.setattr(collections, 'AnswerRelevancy', NumericMetric)
    monkeypatch.setattr(ragas_validation, '_local_embeddings', lambda: object())
    _mock_quality_metrics(monkeypatch, {'informativeness': 'fail'})
    rows, summary = ragas_validation.validate_ragas_dataset_rows(
        rows=[{'id': 'q003', 'question': 'Was steht in Tabelle 17?', 'gold_answer': 'Tabelle 17.',
               'evidence_quote': 'Tabelle 17: Leistungen'}],
        api_base_url='http://localhost:12345/v1', api_key='test', model_name='fake-model',
        request_headers={},
    )
    assert rows == []
    assert summary['rejected'] == 1
    detail = summary['details'][0]
    assert detail['decision'] == 'rejected'
    assert detail['criteria']['informativeness'] == 'fail'
    assert detail['issues'] == [{
        'metric': 'informativeness', 'status': 'fail', 'reason': 'Konkreter Prüfgrund.',
    }]
    assert 'informativeness: Konkreter Prüfgrund.' in detail['reason']


def test_discrete_metric_error_requires_human_review(monkeypatch):
    from ragas import llms
    from ragas.metrics import collections

    class NumericMetric:
        def __init__(self, *args, **kwargs):
            pass

        async def ascore(self, **arguments):
            return SimpleNamespace(value=1.0)

    monkeypatch.setattr(llms, 'llm_factory', lambda *args, **kwargs: object())
    monkeypatch.setattr(collections, 'Faithfulness', NumericMetric)
    monkeypatch.setattr(collections, 'AnswerRelevancy', NumericMetric)
    monkeypatch.setattr(ragas_validation, '_local_embeddings', lambda: object())
    _mock_quality_metrics(monkeypatch, {'answerability': RuntimeError('Modell nicht verfügbar')})
    rows, summary = ragas_validation.validate_ragas_dataset_rows(
        rows=[{'id': 'q002', 'question': 'Welche Leistung gilt?', 'gold_answer': 'Leistung A.',
               'evidence_quote': 'Leistung A gilt.'}],
        api_base_url='http://localhost:12345/v1', api_key='test', model_name='fake-model',
        request_headers={},
    )
    assert len(rows) == 1
    assert rows[0]['validation_status'] == 'review'
    assert rows[0]['validation_criteria']['answerability'] == 'review'
    assert rows[0]['review_decision'] == 'pending'
    assert summary['details'][0]['issues'][0]['metric'] == 'answerability'
    assert 'Modell nicht verfügbar' in rows[0]['validation_errors'][0]
