"""app/services/openwebui.py, the OpenWebUI REST client: the connection
test, knowledge-collection pagination (terminates on total reached AND on
an empty page), the hand-built multipart upload body (no httpx/requests
encoder underneath safe_fetch -- see the module docstring), and the
processing-status poll loop (completed/failed/timeout). Same
mock-safe_fetch-directly pattern as test_confluence_client.py; no real
network involved.
"""

import json
from unittest.mock import patch

import pytest

from app.services.openwebui import OpenWebUIError, _escape_header_value, list_knowledge, upload_file, wait_for_processing
from app.services.openwebui import test_connection as owui_test_connection
from app.services.safe_fetch import SafeFetchError, SafeFetchResponse


def _response(status_code: int, body: dict | bytes) -> SafeFetchResponse:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
    return SafeFetchResponse(status_code=status_code, headers={}, body=raw, final_url='https://owui.example.com/x')


def test_connection_test_ok_and_auth_error():
    with patch('app.services.openwebui.safe_fetch', return_value=_response(200, {})) as mock_fetch:
        owui_test_connection('https://owui.example.com', 'sk-1')
    assert mock_fetch.call_args.kwargs['method'] == 'GET'
    assert mock_fetch.call_args[0][0] == 'https://owui.example.com/api/v1/auths'
    assert mock_fetch.call_args.kwargs['headers']['Authorization'] == 'Bearer sk-1'

    with patch('app.services.openwebui.safe_fetch', return_value=_response(401, {})):
        with pytest.raises(OpenWebUIError, match='authentication failed'):
            owui_test_connection('https://owui.example.com', 'bad-key')

    with patch('app.services.openwebui.safe_fetch', side_effect=SafeFetchError('dns fail')):
        with pytest.raises(OpenWebUIError, match='OpenWebUI request failed'):
            owui_test_connection('https://owui.example.com', 'sk-1')


def test_list_knowledge_paginates_until_total_reached():
    pages = [
        _response(200, {'items': [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B', 'description': 'd'}], 'total': 3}),
        _response(200, {'items': [{'id': 'c', 'name': 'C'}], 'total': 3}),
    ]
    with patch('app.services.openwebui.safe_fetch', side_effect=pages) as mock_fetch:
        items = list_knowledge('https://owui.example.com', 'sk-1')
    assert [item['id'] for item in items] == ['a', 'b', 'c']
    assert items[1]['description'] == 'd'
    assert items[0]['description'] is None
    assert mock_fetch.call_count == 2
    assert mock_fetch.call_args_list[0][0][0].endswith('/api/v1/knowledge/?page=1')
    assert mock_fetch.call_args_list[1][0][0].endswith('/api/v1/knowledge/?page=2')


def test_list_knowledge_stops_on_empty_page_even_if_total_not_reached():
    pages = [_response(200, {'items': [], 'total': 50})]
    with patch('app.services.openwebui.safe_fetch', side_effect=pages):
        items = list_knowledge('https://owui.example.com', 'sk-1')
    assert items == []


def test_upload_file_builds_multipart_and_returns_id():
    captured = {}

    def fake_fetch(url, *, method, headers, body, timeout, max_bytes, allowed_private_hosts):
        captured['url'] = url
        captured['method'] = method
        captured['headers'] = headers
        captured['body'] = body
        return _response(200, {'id': 'file-abc'})

    with patch('app.services.openwebui.safe_fetch', side_effect=fake_fetch):
        file_id = upload_file('https://owui.example.com', 'sk-1', 'report.md', b'# hello\nworld')

    assert file_id == 'file-abc'
    assert captured['url'] == 'https://owui.example.com/api/v1/files/'
    assert captured['method'] == 'POST'
    content_type = captured['headers']['Content-Type']
    assert content_type.startswith('multipart/form-data; boundary=')
    boundary = content_type.split('boundary=', 1)[1]

    body = captured['body']
    assert body.startswith(f'--{boundary}\r\n'.encode())
    assert body.endswith(f'--{boundary}--\r\n'.encode())
    assert b'Content-Disposition: form-data; name="file"; filename="report.md"' in body
    assert b'Content-Type: text/markdown;charset=utf-8' in body
    assert b'# hello\nworld' in body
    # Exactly one embedded copy of the payload -- preamble/epilogue framing
    # must not duplicate or mangle the content bytes.
    assert body.count(b'# hello\nworld') == 1


def test_escape_header_value_strips_control_chars_and_injection_attempts():
    assert _escape_header_value('report.md') == 'report.md'
    # CR/LF would otherwise inject an arbitrary extra header line into the
    # hand-built multipart preamble (original_filename can be attacker-
    # influenced, e.g. via a mail attachment -- see
    # app/workers/openwebui_tasks.py).
    injected = _escape_header_value('evil\r\nX-Injected: true\r\n.md')
    assert '\r' not in injected
    assert '\n' not in injected
    # Other control characters (tab, NUL) and DEL are stripped too, not just
    # CR/LF.
    assert _escape_header_value('a\tb\x00c\x7fd') == 'abcd'
    # Quote/backslash still escaped for whatever survives the filter.
    assert _escape_header_value('a"b\\c') == 'a\\"b\\\\c'


def test_upload_file_strips_crlf_from_a_malicious_filename():
    captured = {}

    def fake_fetch(url, *, method, headers, body, timeout, max_bytes, allowed_private_hosts):
        captured['body'] = body
        return _response(200, {'id': 'file-abc'})

    malicious_filename = 'report\r\nX-Injected: evil\r\n.md'
    with patch('app.services.openwebui.safe_fetch', side_effect=fake_fetch):
        upload_file('https://owui.example.com', 'sk-1', malicious_filename, b'content')

    body = captured['body']
    assert b'\r\nX-Injected' not in body
    assert b'filename="reportX-Injected: evil.md"' in body


def test_upload_file_falls_back_to_document_md_for_an_all_control_char_filename():
    captured = {}

    def fake_fetch(url, *, method, headers, body, timeout, max_bytes, allowed_private_hosts):
        captured['body'] = body
        return _response(200, {'id': 'file-abc'})

    with patch('app.services.openwebui.safe_fetch', side_effect=fake_fetch):
        upload_file('https://owui.example.com', 'sk-1', '\r\n\x00', b'content')

    assert b'filename="document.md"' in captured['body']


def test_wait_for_processing_completed_failed_and_timeout():
    with patch('app.services.openwebui.safe_fetch', return_value=_response(200, {'status': 'completed'})):
        wait_for_processing('https://owui.example.com', 'sk-1', 'f1', timeout_seconds=5, poll_interval_seconds=0.01)

    with patch('app.services.openwebui.safe_fetch', return_value=_response(200, {'status': 'failed', 'error': 'bad pdf'})):
        with pytest.raises(OpenWebUIError, match='bad pdf'):
            wait_for_processing('https://owui.example.com', 'sk-1', 'f1', timeout_seconds=5, poll_interval_seconds=0.01)

    with patch('app.services.openwebui.safe_fetch', return_value=_response(200, {'status': 'pending'})):
        with pytest.raises(OpenWebUIError, match='did not complete'):
            wait_for_processing('https://owui.example.com', 'sk-1', 'f1', timeout_seconds=0.05, poll_interval_seconds=0.02)
