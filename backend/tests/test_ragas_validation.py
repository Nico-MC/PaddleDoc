from types import SimpleNamespace

import pytest

from app.services import ragas_validation
from app.services.ragas_validation import _classify_scores, _score_value


@pytest.mark.parametrize(('faithfulness', 'relevance', 'expected'), [
    (1.0, 1.0, 'accepted'),
    (0.85, 0.70, 'accepted'),
    (0.84, 0.70, 'review'),
    (0.80, 0.60, 'review'),
    (0.49, 1.0, 'rejected'),
    (1.0, 0.39, 'rejected'),
    (None, 1.0, 'review'),
    (1.0, None, 'review'),
])
def test_ragas_validation_classifies_scores(faithfulness, relevance, expected):
    assert _classify_scores(faithfulness, relevance) == expected


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -0.1, 1.1])
def test_ragas_validation_rejects_invalid_metric_values(value):
    with pytest.raises(ValueError):
        _score_value(SimpleNamespace(value=value))

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
    assert [event['arguments'] for event in events if event['type'] == 'validation_input'] == calls
    assert [event['score'] for event in events if event['type'] == 'validation_result'] == [1.0, 1.0]
    inputs = [event for event in events if event['type'] == 'validation_input']
    assert inputs[0]['metric'] == 'faithfulness'
    assert inputs[0]['arguments']['retrieved_contexts'] == ['Die Police gewährt eine Leistung.']
    assert inputs[1]['metric'] == 'answer_relevancy'
    assert 'retrieved_contexts' not in inputs[1]['arguments']
