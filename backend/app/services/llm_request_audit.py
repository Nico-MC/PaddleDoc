"""Record the JSON bodies sent to and received from an OpenAI-compatible LLM API.

Only request/response bodies and status are recorded. In particular, authorization
headers, bearer tokens and endpoint URLs must never enter the dataset audit.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


def audited_llm_http_client(
    audit_callback: Callable[[dict[str, Any]], None] | None,
    scope: Callable[[], Mapping[str, Any]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Capture each actual transport attempt, including SDK retries."""

    limits = httpx.Limits(max_keepalive_connections=0)
    if audit_callback is None:
        return httpx.AsyncClient(limits=limits, transport=transport)

    def emit(event: dict[str, Any]) -> None:
        try:
            audit_callback(event)
        except Exception:
            logger.exception('Could not record LLM request audit event')

    async def on_request(request: httpx.Request) -> None:
        request_id = uuid4().hex
        request.extensions['dataset_audit_id'] = request_id
        request.extensions['dataset_audit_started'] = time.monotonic()
        body = await request.aread()
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {'non_json_body': True, 'byte_count': len(body)}
        emit({
            'type': 'llm_http_request', 'request_id': request_id,
            'at': datetime.now(timezone.utc).isoformat(),
            'scope': dict(scope()), 'body': payload,
        })

    async def on_response(response: httpx.Response) -> None:
        request = response.request
        request_id = request.extensions.get('dataset_audit_id')
        if not request_id:
            return
        body = await response.aread()
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {'non_json_body': True, 'byte_count': len(body)}
        started = request.extensions.get('dataset_audit_started')
        emit({
            'type': 'llm_http_response', 'request_id': request_id,
            'at': datetime.now(timezone.utc).isoformat(),
            'status_code': response.status_code,
            'duration_ms': round((time.monotonic() - started) * 1000) if isinstance(started, float) else None,
            'body': payload,
        })

    return httpx.AsyncClient(limits=limits, transport=transport, event_hooks={'request': [on_request], 'response': [on_response]})
