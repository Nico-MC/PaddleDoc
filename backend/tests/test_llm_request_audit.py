import asyncio
import json

import httpx

from app.services.llm_request_audit import audited_llm_http_client


def test_records_exact_json_request_and_response_without_credentials():
    events = []
    seen = []
    scope = {'phase': 'generation', 'passage_id': 'p0007'}

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'model': 'test-model', 'choices': [{'message': {'content': 'Frage?'}}], 'usage': {'total_tokens': 12}})

    async def run():
        async with audited_llm_http_client(events.append, lambda: scope, httpx.MockTransport(respond)) as client:
            response = await client.post('https://example.invalid/v1/chat/completions?secret=hidden',
                headers={'Authorization': 'Bearer secret-token'},
                json={'model': 'test-model', 'messages': [{'role': 'user', 'content': 'Belegbarer Text'}]})
            assert response.json()['usage']['total_tokens'] == 12

    asyncio.run(run())
    assert len(events) == 2
    request_event, response_event = events
    assert request_event['body'] == seen[0]
    assert request_event['scope'] == scope
    assert response_event['request_id'] == request_event['request_id']
    assert response_event['status_code'] == 200
    assert response_event['body']['usage']['total_tokens'] == 12
    serialized = json.dumps(events)
    assert 'secret-token' not in serialized
    assert 'secret=hidden' not in serialized


def test_response_error_is_recorded_without_changing_response():
    events = []

    async def run():
        async with audited_llm_http_client(events.append, lambda: {'phase': 'validation'},
                httpx.MockTransport(lambda request: httpx.Response(429, json={'error': {'message': 'rate limited'}}))) as client:
            response = await client.post('https://example.invalid/v1/chat/completions', json={'model': 'test-model'})
            assert response.status_code == 429
            assert response.json()['error']['message'] == 'rate limited'

    asyncio.run(run())
    assert [event['type'] for event in events] == ['llm_http_request', 'llm_http_response']
    assert events[1]['status_code'] == 429
