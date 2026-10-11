from app.services.dataset_assistant import _markdown_passages
from app.services.generation_source_filter import (
    SectionClassification,
    excluded_passage_ids,
    filter_generation_passages,
    section_inventory,
)


def _sample_passages():
    markdown = (
        '<!-- page:1/5 -->\n\nSFCR 2025. Solvabilitäts- und Finanzbericht.\n\n'
        '<!-- page:2/5 -->\n\n## **Inhaltsverzeichnis**\n\nKapitel A .... 4\n\n'
        '<!-- page:3/5 -->\n\n## **Tabellen- und Abbildungsverzeichnis**\n\nTabelle 17: Solvabilitätsübersicht .... 45\n\n'
        '<!-- page:4/5 -->\n\n## **Geschäftsentwicklung**\n\n'
        'Tabelle 17 zeigt die tatsächliche Solvabilitätsübersicht mit Eigenmitteln und SCR.\n\n'
        '<!-- page:5/5 -->\n\n## **Glossar**\n\nSCR bedeutet Solvenzkapitalanforderung.\n'
    )
    return _markdown_passages(markdown)


def test_filter_removes_navigation_but_keeps_real_table_content():
    passages = _sample_passages()
    kept, excluded = filter_generation_passages(passages)
    assert [passage.page_number for passage in kept] == [1, 4]
    assert any('Tabelle 17 zeigt' in passage.text for passage in kept)
    assert {section.page for section in excluded} == {2, 3, 5}
    assert len(passages) == 5  # The original passage list is not mutated.


def test_classifier_receives_only_section_inventory_and_can_exclude_cover():
    class FakeLLM:
        def __init__(self):
            self.prompts = []

        def generate(self, prompt, response_model):
            self.prompts.append(prompt)
            assert response_model is SectionClassification
            return SectionClassification(sections=[{'id': 's001', 'kind': 'cover'}])

    llm = FakeLLM()
    audit_events = []
    kept, excluded = filter_generation_passages(_sample_passages(), llm=llm, audit_callback=audit_events.append)
    assert [passage.page_number for passage in kept] == [4]
    assert {section.page for section in excluded} == {1, 2, 3, 5}
    assert len(llm.prompts) == 1
    assert audit_events[0] == {'type': 'classification_input', 'prompt': llm.prompts[0]}
    assert audit_events[1]['type'] == 'classification_result'
    assert audit_events[1]['decisions']['sections'][0] == {'id': 's001', 'kind': 'cover'}
    assert audit_events[2]['type'] == 'section_inventory'
    assert audit_events[2]['sections'][0]['excluded'] is True
    assert 'SFCR 2025' in llm.prompts[0]
    assert 'Tabelle 17 zeigt die tatsächliche' in llm.prompts[0]


def test_late_content_is_not_removed_by_untrusted_model_decision():
    class FakeLLM:
        def generate(self, prompt, response_model):
            return SectionClassification(sections=[{'id': 's001', 'kind': 'glossary'}])

    passages = _markdown_passages('<!-- page:40/90 -->\n\n## Fachkapitel\n\nEine belegbare Regelung.')
    kept, excluded = filter_generation_passages(passages, llm=FakeLLM())
    assert kept == passages
    assert excluded == []


def test_toc_is_only_a_hint_and_does_not_remove_substantive_children():
    sections = section_inventory(_sample_passages(), [[1, 'Deckblatt', 1], [1, 'Hauptteil', 4]])
    assert sections[0].toc_heading == 'Deckblatt'
    assert sections[3].toc_heading == 'Hauptteil'
    assert sections[3].passage_ids[0] not in excluded_passage_ids(sections)
