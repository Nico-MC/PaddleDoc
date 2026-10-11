import pytest

from app.services import ragas_dataset
from pydantic import ValidationError

from app.schemas.jobs import EvaluationDatasetGenerateRequest
from app.services.dataset_assistant import _Passage
from app.services.ragas_dataset import (
    _generation_candidates, _model_request_options, _query_mode_for_region, _section_context, _section_regions,
)


def test_ragas_sections_preserve_pages_but_remove_picture_text():
    markdown = (
        '<!-- page:1/2 -->\n'
        'Die Versicherung übernimmt belegbare Kosten für eine ärztliche Behandlung.\n\n'
        '<!-- Start of picture text -->\n'
        'Irreführende Bildaufschrift mit falschem Leistungsversprechen.\n'
        '<!-- End of picture text -->\n'
        '<!-- page:2/2 -->\n'
        'Die Wartezeit beträgt drei Monate ab Versicherungsbeginn.\n'
    )
    method, regions = _section_regions(markdown, 2, 17)
    assert method == 'pages'
    assert len(regions) == 2
    assert [region[0].page_number for region in regions] == [1, 2]
    assert all('Irreführende Bildaufschrift' not in passage.text for region in regions for passage in region)


def test_ragas_samples_ten_regions_across_ninety_pages():
    markdown = ''.join(
        f'<!-- page:{page}/90 -->\n'
        f'Seite {page}: Die Versicherung regelt auf dieser Seite eine eigenständige Leistung.\n\n'
        for page in range(1, 91)
    )
    method, regions = _section_regions(markdown, 10, 7)
    assert method == 'pages'
    assert len(regions) == 10
    first_pages = [min(passage.page_number for passage in region) for region in regions]
    assert first_pages[0] <= 9
    assert first_pages[-1] >= 82
    assert first_pages == sorted(first_pages)


@pytest.mark.parametrize(('query_mode', 'expected'), [
    ('facts', ['facts', 'facts']),
    ('themes', ['themes', 'themes']),
    ('mixed', ['facts', 'themes']),
])
def test_ragas_question_type_per_region(query_mode, expected):
    assert [_query_mode_for_region(query_mode, region) for region in (1, 2)] == expected


def test_short_snippets_do_not_exhaust_ragas_passage_tries():
    region = [
        _Passage(id=f'p{index:04d}', text='Kurz.' * 4, anchor='', page_number=90)
        for index in range(1, 4)
    ]
    region.append(_Passage(
        id='p0004', text='Diese Passage enthält eine ausreichend lange belegbare Aussage zur Leistung.',
        anchor='', page_number=90,
    ))
    candidates = _generation_candidates(region, passage_tries=3, context_chars=1400)
    assert [passage.id for passage, _ in candidates] == ['p0004']


def test_ragas_context_length_is_applied_without_rewriting_source():
    passage = _Passage(id='p0001', text='Wort ' * 500, anchor='', page_number=3)
    short = _section_context(passage, 800)
    long = _section_context(passage, 2400)
    assert len(short) <= 800
    assert len(long) <= 2400
    assert short in passage.text
    assert long in passage.text
    assert len(long) > len(short)


@pytest.mark.parametrize(('field', 'value'), [
    ('ragas_query_mode', 'multi-hop'),
    ('ragas_context_chars', 900),
    ('ragas_passage_tries', 0),
    ('ragas_passage_tries', 6),
])
def test_ragas_options_are_restricted_to_supported_values(field, value):
    with pytest.raises(ValidationError):
        EvaluationDatasetGenerateRequest(markdown_paths=['a.md'], **{field: value})


def test_ragas_model_parameters_are_provider_specific():
    assert _model_request_options('vllm/deepseek-ai/DeepSeek-V4.1-Flash') == {
        'extra_body': {'chat_template_kwargs': {'thinking': False}},
    }
    assert _model_request_options('vllm/Qwen/Qwen3.5-27B') == {
        'extra_body': {'chat_template_kwargs': {'enable_thinking': False}},
    }
    assert _model_request_options('vllm/google/gemma-4-26B-A4B-it') == {}

def test_ragas_reaches_long_passage_after_three_short_ones(monkeypatch):
    from ragas import llms, testset
    from ragas.testset.synthesizers.single_hop import specific
    from ragas.testset.transforms.extractors import llm_based

    short = [
        _Passage(id=f'p{index:04d}', text='Kurz.' * 4, anchor='', page_number=90)
        for index in range(1, 4)
    ]
    long_text = 'Diese belegbare Passage beschreibt eine konkrete Leistung über mehr als vierzig Zeichen.'
    passages = short + [_Passage(id='p0004', text=long_text, anchor='Leistungen', page_number=90)]
    monkeypatch.setattr(ragas_dataset, '_markdown_passages', lambda markdown: passages)
    monkeypatch.setattr(ragas_dataset, 'filter_generation_passages', lambda source, **kwargs: (source, []))
    monkeypatch.setattr(ragas_dataset, '_sampling_regions', lambda source, count, seed: ('pages', [source]))
    monkeypatch.setattr(ragas_dataset, '_local_embeddings', lambda: object())
    monkeypatch.setattr(llms, 'llm_factory', lambda *args, **kwargs: object())
    monkeypatch.setattr(llm_based, 'NERExtractor', lambda **kwargs: object())
    monkeypatch.setattr(llm_based, 'KeyphrasesExtractor', lambda **kwargs: object())
    monkeypatch.setattr(specific, 'SingleHopSpecificQuerySynthesizer', lambda **kwargs: object())

    class FakeClient:
        def __init__(self, **kwargs):
            self.http_client = kwargs['http_client']

        async def close(self):
            await self.http_client.aclose()

    seen_contexts = []

    class FakeGenerator:
        def __init__(self, **kwargs):
            pass

        def generate_with_chunks(self, *, chunks, **kwargs):
            context = chunks[0].page_content
            seen_contexts.append(context)

            class FakeSet:
                def to_list(self):
                    return [{
                        'reference_contexts': [context],
                        'user_input': 'Welche Leistung wird beschrieben?',
                        'reference': 'Eine konkrete Leistung.',
                    }]

            return FakeSet()

    monkeypatch.setattr(ragas_dataset, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(testset, 'TestsetGenerator', FakeGenerator)
    events = []
    rows = ragas_dataset.generate_ragas_dataset_rows(
        markdown=(
            '<!-- page:14/90 -->\nBelegbarer Inhalt.\n'
            '<!-- Start of picture text -->Irreführender Grafiktext<!-- End of picture text -->'
        ), source_document='quelle.md', source_file='quelle.pdf',
        source_file_sha256=None, api_base_url='http://localhost:12345/v1', api_key='test',
        model_name='fake-model', question_count=1, question_style='user-paraphrases',
        focus='', sampling_seed=17, request_headers={}, passage_tries=3,
        audit_callback=events.append,
    )

    assert len(rows) == 1
    prepared = next(event for event in events if event['type'] == 'prepared_document')
    assert '<!-- page:14/90 -->' in prepared['text']
    assert 'Irreführender Grafiktext' not in prepared['text']
    assert prepared['picture_text_blocks_removed'] == 1
    assert seen_contexts == [long_text]
    assert [event['passage_id'] for event in events if event['type'] == 'generation_input'] == ['p0004']
    region_result = next(event for event in events if event['type'] == 'region_result')
    assert region_result['attempts'] == 1
    assert region_result['generated'] == 1
    assert region_result['short_passages_in_region'] == 3
    generation_result = next(event for event in events if event['type'] == 'generation_result')
    assert generation_result['raw_samples'][0]['user_input'] == 'Welche Leistung wird beschrieben?'
    assert generation_result['sample_decisions'] == [
        {'sample_index': 0, 'outcome': 'accepted', 'row_id': 'q001'},
    ]
