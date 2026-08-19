"""OpenWebUI REST client for the push integration (upload a job's markdown
into an OpenWebUI knowledge collection).

Mirrors app/services/confluence.py's shape: a handful of module-level
functions raising a single `OpenWebUIError`, kept entirely FastAPI-free so
both app/api/openwebui_routes.py and app/workers/openwebui_tasks.py can call
it directly. Every outbound request goes through
app.services.safe_fetch.safe_fetch -- the same SSRF protection Confluence
gets (private-IP block with the admin-managed `allowed_private_hosts`
exemption, unconditional cloud-metadata block, DNS pinning, redirect
revalidation). Unlike Confluence, no response here ever hands back a URL to
follow (pagination is a `?page=` counter we build ourselves, not a
server-supplied `next` link), so there is no separate same-host check to
perform on a response value.

safe_fetch already accepts an arbitrary `method` and raw `body` bytes (see
its docstring), so POST/DELETE needed no changes there. What safe_fetch does
NOT know about is multipart/form-data -- there is no httpx/requests-style
encoder underneath it, so the file-upload body (`_multipart_body` below) is
built by hand: a single `file` part, boundary embedded in the returned
Content-Type header. (The reference implementation this API surface was
verified against uses a library that generates that header automatically and
warns against setting Content-Type yourself; safe_fetch has no such
auto-encoder, so here WE are that library and must set it ourselves, with
the exact boundary the body was built with.)

API surface (verified against a live OpenWebUI instance):
- Auth: 'Authorization: Bearer {api_key}', 'Accept: application/json'.
- GET  {base}/api/v1/auths                              -- connection test
- GET  {base}/api/v1/knowledge/?page={n}                 -- {items, total}, paginated
- POST {base}/api/v1/files/                              -- multipart, field 'file' -> {id}
- GET  {base}/api/v1/files/{file_id}/process/status      -- {status, error?}
- POST {base}/api/v1/knowledge/{knowledge_id}/file/add   -- {"file_id": ...}
- POST {base}/api/v1/knowledge/{knowledge_id}/file/remove -- {"file_id": ...} (best-effort at the call site)
- DELETE {base}/api/v1/files/{file_id}                   -- (best-effort at the call site)
"""

from __future__ import annotations

import json
import time
import uuid

from app.services.safe_fetch import SafeFetchError, safe_fetch

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_KNOWLEDGE_PAGE_SIZE_GUARD = 2000  # defensive iteration cap, see list_knowledge


class OpenWebUIError(Exception):
    """Raised for any OpenWebUI API failure: unreachable host, non-2xx
    response, malformed JSON, or an unexpected response shape. Wraps
    SafeFetchError for network/SSRF rejections. `status_code` carries the
    HTTP status when one was received (None for network-level failures)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _api_url(base_url: str, path: str) -> str:
    return base_url.rstrip('/') + path


def _auth_headers(api_key: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}


def _fetch(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    max_bytes: int,
    allowed_private_hosts: frozenset[str] | None,
):
    try:
        return safe_fetch(
            url,
            method=method,
            headers=headers,
            body=body,
            timeout=timeout,
            max_bytes=max_bytes,
            allowed_private_hosts=allowed_private_hosts,
        )
    except SafeFetchError as exc:
        raise OpenWebUIError(f'OpenWebUI request failed: {exc}') from exc


def _request_json(
    url: str,
    *,
    method: str,
    api_key: str,
    json_body: dict | None = None,
    timeout: float,
    max_bytes: int,
    allowed_private_hosts: frozenset[str] | None,
) -> dict:
    """GET/POST/DELETE against a JSON endpoint. Raises OpenWebUIError on any
    non-2xx status or a response that doesn't parse as a JSON object."""
    headers = _auth_headers(api_key)
    body: bytes | None = None
    if json_body is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(json_body).encode('utf-8')

    response = _fetch(
        url, method=method, headers=headers, body=body,
        timeout=timeout, max_bytes=max_bytes, allowed_private_hosts=allowed_private_hosts,
    )
    if not 200 <= response.status_code < 300:
        raise OpenWebUIError(
            f'OpenWebUI request to {method} {url!r} returned HTTP {response.status_code}',
            status_code=response.status_code,
        )
    if not response.body:
        return {}
    try:
        data = json.loads(response.body)
    except ValueError as exc:
        raise OpenWebUIError(f'OpenWebUI response from {method} {url!r} is not valid JSON') from exc
    if not isinstance(data, dict):
        raise OpenWebUIError(f'OpenWebUI response from {method} {url!r} has unexpected shape')
    return data


def _escape_header_value(value: str) -> str:
    # Minimal Content-Disposition escaping (RFC 6266-ish) for the hand-built
    # multipart header below. Control characters (<0x20, 0x7f) and anything
    # outside printable ASCII are stripped first -- same printable-ASCII-only
    # filter as routes._content_disposition's fallback filename -- since
    # `filename` here can be attacker-influenced (job.original_filename, e.g.
    # sourced from a mail attachment; see app/workers/openwebui_tasks.py) and
    # an unescaped CR/LF would inject arbitrary header lines into this
    # hand-built outbound request. The two characters that would still break
    # the quoted-string are then backslash-escaped.
    filtered = ''.join(ch for ch in value if 32 <= ord(ch) < 127)
    return filtered.replace('\\', '\\\\').replace('"', '\\"')


def _multipart_body(field_name: str, filename: str, content: bytes, content_type: str) -> tuple[bytes, str]:
    """Hand-built single-file multipart/form-data body (see module
    docstring for why: safe_fetch has no multipart encoder). Returns
    (body, boundary) -- the caller sets the Content-Type header with it."""
    boundary = uuid.uuid4().hex
    # _escape_header_value can strip a filename down to nothing (e.g. one
    # made entirely of control characters) -- fall back to a stand-in name
    # rather than sending an empty filename="" to OpenWebUI.
    safe_filename = _escape_header_value(filename) or 'document.md'
    preamble = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="{_escape_header_value(field_name)}"; '
        f'filename="{safe_filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'
    ).encode('utf-8')
    epilogue = f'\r\n--{boundary}--\r\n'.encode('utf-8')
    return preamble + content + epilogue, boundary


def test_connection(
    base_url: str,
    api_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    allowed_private_hosts: frozenset[str] | None = None,
) -> None:
    """GET /api/v1/auths. Raises OpenWebUIError (never returns a value) on
    anything other than a 200 -- the caller decides how to render that."""
    url = _api_url(base_url, '/api/v1/auths')
    response = _fetch(
        url, method='GET', headers=_auth_headers(api_key), body=None,
        timeout=timeout, max_bytes=max_bytes, allowed_private_hosts=allowed_private_hosts,
    )
    if response.status_code in (401, 403):
        raise OpenWebUIError(
            f'authentication failed (HTTP {response.status_code}) -- check the API key', status_code=response.status_code
        )
    if response.status_code != 200:
        raise OpenWebUIError(
            f'connection test returned HTTP {response.status_code}', status_code=response.status_code
        )


def list_knowledge(
    base_url: str,
    api_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    allowed_private_hosts: frozenset[str] | None = None,
) -> list[dict]:
    """GET /api/v1/knowledge/?page={n}, looping until the accumulated item
    count reaches the server-reported `total` (per the verified contract).
    Bounded by _KNOWLEDGE_PAGE_SIZE_GUARD pages regardless -- a server that
    never reports enough items must not loop us forever."""
    items: list[dict] = []
    page = 1
    total: int | None = None
    while page <= _KNOWLEDGE_PAGE_SIZE_GUARD:
        url = _api_url(base_url, f'/api/v1/knowledge/?page={page}')
        data = _request_json(
            url, method='GET', api_key=api_key, timeout=timeout, max_bytes=max_bytes,
            allowed_private_hosts=allowed_private_hosts,
        )
        raw_items = data.get('items')
        if not isinstance(raw_items, list):
            raise OpenWebUIError(f'OpenWebUI knowledge response has unexpected shape (page {page})')
        raw_total = data.get('total')
        total = int(raw_total) if isinstance(raw_total, (int, float)) else total
        if not raw_items:
            break
        for entry in raw_items:
            if not isinstance(entry, dict) or not entry.get('id'):
                continue
            items.append({
                'id': str(entry['id']),
                'name': str(entry.get('name') or ''),
                'description': entry.get('description') if isinstance(entry.get('description'), str) else None,
            })
        if total is not None and len(items) >= total:
            break
        page += 1
    return items


def upload_file(
    base_url: str,
    api_key: str,
    filename: str,
    content: bytes,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    allowed_private_hosts: frozenset[str] | None = None,
) -> str:
    """POST /api/v1/files/ as multipart/form-data (field 'file'), content
    type text/markdown. Returns the new file's id."""
    body, boundary = _multipart_body('file', filename, content, 'text/markdown;charset=utf-8')
    headers = _auth_headers(api_key)
    headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'

    url = _api_url(base_url, '/api/v1/files/')
    response = _fetch(
        url, method='POST', headers=headers, body=body,
        timeout=timeout, max_bytes=max_bytes, allowed_private_hosts=allowed_private_hosts,
    )
    if not 200 <= response.status_code < 300:
        raise OpenWebUIError(f'file upload returned HTTP {response.status_code}', status_code=response.status_code)
    try:
        data = json.loads(response.body)
    except ValueError as exc:
        raise OpenWebUIError('file upload response is not valid JSON') from exc
    file_id = data.get('id') if isinstance(data, dict) else None
    if not isinstance(file_id, str) or not file_id:
        raise OpenWebUIError('file upload response has no file id')
    return file_id


def wait_for_processing(
    base_url: str,
    api_key: str,
    file_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    request_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    allowed_private_hosts: frozenset[str] | None = None,
) -> None:
    """Poll GET /api/v1/files/{file_id}/process/status every
    poll_interval_seconds until status is 'completed' (returns) or 'failed'
    (raises with the server's error text). Raises OpenWebUIError if
    timeout_seconds elapses first."""
    url = _api_url(base_url, f'/api/v1/files/{file_id}/process/status')
    deadline = time.monotonic() + timeout_seconds
    while True:
        data = _request_json(
            url, method='GET', api_key=api_key, timeout=request_timeout, max_bytes=max_bytes,
            allowed_private_hosts=allowed_private_hosts,
        )
        status_value = data.get('status')
        if status_value == 'completed':
            return
        if status_value == 'failed':
            error_text = data.get('error')
            raise OpenWebUIError(f'file processing failed: {error_text}' if error_text else 'file processing failed')
        if time.monotonic() >= deadline:
            raise OpenWebUIError(f'file processing did not complete within {timeout_seconds:.0f}s')
        time.sleep(poll_interval_seconds)


def add_to_knowledge(
    base_url: str,
    api_key: str,
    knowledge_id: str,
    file_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    allowed_private_hosts: frozenset[str] | None = None,
) -> None:
    """POST /api/v1/knowledge/{knowledge_id}/file/add. Call only after
    wait_for_processing has confirmed 'completed' for file_id."""
    url = _api_url(base_url, f'/api/v1/knowledge/{knowledge_id}/file/add')
    _request_json(
        url, method='POST', api_key=api_key, json_body={'file_id': file_id},
        timeout=timeout, max_bytes=max_bytes, allowed_private_hosts=allowed_private_hosts,
    )


def remove_from_knowledge(
    base_url: str,
    api_key: str,
    knowledge_id: str,
    file_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    allowed_private_hosts: frozenset[str] | None = None,
) -> None:
    """POST /api/v1/knowledge/{knowledge_id}/file/remove. Raises
    OpenWebUIError like every other call here -- best-effort tolerance
    (catch, log, move on) is the CALLER's job (see
    app/workers/openwebui_tasks.py's replace step), not this function's."""
    url = _api_url(base_url, f'/api/v1/knowledge/{knowledge_id}/file/remove')
    _request_json(
        url, method='POST', api_key=api_key, json_body={'file_id': file_id},
        timeout=timeout, max_bytes=max_bytes, allowed_private_hosts=allowed_private_hosts,
    )


def delete_file(
    base_url: str,
    api_key: str,
    file_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    allowed_private_hosts: frozenset[str] | None = None,
) -> None:
    """DELETE /api/v1/files/{file_id}. Same best-effort-is-the-caller's-job
    contract as remove_from_knowledge."""
    url = _api_url(base_url, f'/api/v1/files/{file_id}')
    response = _fetch(
        url, method='DELETE', headers=_auth_headers(api_key), body=None,
        timeout=timeout, max_bytes=max_bytes, allowed_private_hosts=allowed_private_hosts,
    )
    if not 200 <= response.status_code < 300:
        raise OpenWebUIError(f'file delete returned HTTP {response.status_code}', status_code=response.status_code)
