"""Confluence REST clients for the importer (Phase C).

Two implementations behind one protocol (`PageSource`): Confluence Cloud
speaks the v2 REST API (`/wiki/api/v2`, the v1 content/space endpoints are
formally deprecated there), Server/Data Center speaks v1 (`/rest/api`, the
only surface DC has). Which one a source gets is decided once, by
`detect_server_kind` during `POST /import/sources/{id}/test`, persisted on
the source (`server_kind` + `api_base_path`), and turned into a client via
`create_client`. Both request the `export_view` body format -- server-side
rendered HTML with macros already expanded -- which
app/services/confluence_markdown.py converts to markdown.

Every outbound request goes through app.services.safe_fetch.safe_fetch
(SSRF protection: private-IP block with the admin-managed
`allowed_private_hosts` exemption enforced per redirect hop, unconditional
cloud-metadata block, DNS pinning). Additionally, every URL taken from an
API response (pagination `_links.next`, attachment download links) is
joined against the *stored* base URL and rejected if the result points at a
different host -- a hostile/compromised Confluence must not be able to
bounce our Authorization header to a third party. URLs harvested from page
HTML are never fetched at all (that is the converter's problem, and it only
records them).

Credential handling: the auth header is built once and kept on the client;
error messages carry URLs/statuses but never headers, so the credential
cannot leak into `ImportRun.error_message` / `state.errors` / logs.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qs, quote, unquote, unquote_plus, urljoin, urlsplit

from app.models.models import ImportAuthType
from app.services.safe_fetch import SafeFetchError, safe_fetch

logger = logging.getLogger(__name__)

SERVER_KIND_CLOUD = 'cloud'
SERVER_KIND_DATACENTER = 'datacenter'
API_BASE_PATH_CLOUD = '/wiki/api/v2'
API_BASE_PATH_DATACENTER = '/rest/api'

_LIST_PAGE_LIMIT = 50
_DEFAULT_PORTS = {'http': 80, 'https': 443}

# Plain-language interpretation appended to a ConfluenceError's message for
# the status codes admins actually hit while diagnosing a broken import --
# the raw "HTTP 403" alone tells a user nothing actionable. 404 gets the most
# text: Data Center is well known to answer with 404 (not 403) when the
# account can see that a page id exists but lacks permission to read it, so
# a plain "page not found" reading is actively misleading there.
_STATUS_EXPLANATIONS: dict[int, str] = {
    401: 'Unauthorized -- the credential is missing, expired, or was rejected; re-check it on the source',
    403: 'Forbidden -- the account has no permission for this space/page (Data Center: often a per-page restriction)',
    404: (
        'Not Found -- the space/page does not exist, or the account cannot see it; on Server/Data Center a '
        'missing permission is frequently reported as 404 instead of 403'
    ),
    429: 'Too Many Requests -- Confluence is rate-limiting this account; the import will need to slow down or retry later',
}

# Cap on how much of a failed response's extracted JSON `message` field is
# ever logged -- generous enough for a full sentence, short enough that a
# pathological server can't flood the log table via worker_log_entries.
_BODY_MESSAGE_LOG_CHARS = 200


def _explain_status(status_code: int) -> str:
    explanation = _STATUS_EXPLANATIONS.get(status_code)
    return f' ({explanation})' if explanation else ''


def _space_not_found_message(space_key: str) -> str:
    # Both APIs answer a nonexistent key and a key the account cannot see
    # the same way (Cloud: empty results list; Server/DC: 404), so the two
    # causes are genuinely indistinguishable from here -- the message says
    # so explicitly instead of guessing. '~'-prefixed keys are Confluence's
    # personal-space convention and are worth calling out on their own: they
    # are frequently unresolvable simply because the importing account was
    # never granted access to that person's space.
    personal_hint = (
        " -- keys starting with '~' are personal spaces; confirm the account has been granted access to it"
        if space_key.startswith('~')
        else ''
    )
    return f'space {space_key!r} not found or not accessible to this account (check the key and its permissions){personal_hint}'


def _page_not_found_message(space_key: str, title: str) -> str:
    # Mirrors _space_not_found_message's reasoning: a nonexistent title and
    # one the account cannot see both come back as an empty result set on
    # both APIs, so this says so rather than guessing which it was.
    return (
        f'page {title!r} not found in space {space_key!r} or not accessible to this account '
        '(check the exact title and its permissions)'
    )


def _extract_json_error_message(body: bytes) -> str | None:
    """Best-effort pull of Confluence's own `message` field out of a failed
    response body, for the WARNING log only. JSON-only and capped -- an HTML
    error page (a misconfigured reverse proxy in front of Data Center is a
    common source of one) is never parsed as text and never logged verbatim,
    only silently ignored here."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    message = data.get('message')
    if not isinstance(message, str) or not message:
        return None
    message = message.strip()
    if not message:
        return None
    return message if len(message) <= _BODY_MESSAGE_LOG_CHARS else message[:_BODY_MESSAGE_LOG_CHARS] + '...'

# Cloud pretty links (/spaces/KEY/pages/123/Title) and DC viewpage.action
# (?pageId=123). Title-only /display/KEY/Title links and viewpage.action
# ?spaceKey=&title= links carry no numeric id -- see _extract_display_parts,
# which the API layer uses to resolve those via resolve_page_by_title.
_PAGE_ID_PATH_RE = re.compile(r'/pages/(\d+)(?:[/?#]|$)')
_PAGE_ID_QUERY_RE = re.compile(r'[?&]pageId=(\d+)(?:[&#]|$)')

# /display/SPACEKEY/Page+Title (SPACEKEY may contain '~' for personal
# spaces, e.g. /display/~jdoe/My+Notes). The title segment is percent/plus
# decoded by _extract_display_parts, matching how Confluence itself encodes
# titles into these links.
_DISPLAY_PATH_RE = re.compile(r'/display/([^/?#]+)/([^/?#]+)')


class ConfluenceError(Exception):
    """Raised for any Confluence API failure: unreachable host, non-2xx
    response, malformed JSON, unexpected response shape, or a response URL
    pointing off the source's host. Wraps SafeFetchError for network/SSRF
    rejections. `status_code` carries the HTTP status when one was
    received (None for network-level failures)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    version: int
    html: str  # export_view body (server-rendered, macros expanded)
    url: str  # human-facing page URL on the source


@dataclass(frozen=True)
class PageContext:
    """Where a page sits in its space: the space key (None when the
    response carries no space) and the titles of its ancestors, ordered
    from the space root down to the page's direct parent (the page itself
    is never included)."""

    space_key: str | None
    ancestor_titles: list[str]


@dataclass
class AttachmentMeta:
    id: str
    filename: str
    # Remote-claimed media type -- informational only; callers must classify
    # content independently (extension + magic bytes) before storing.
    media_type: str
    size_bytes: int | None
    # Absolute download URL, joined against the stored base URL and
    # host-checked. This is the ONLY URL attachment bytes may come from.
    download_url: str
    page_id: str
    _fetch: Callable[[], bytes] = field(repr=False, compare=False)

    def fetch_bytes(self) -> bytes:
        """Download the attachment through safe_fetch (auth header attached,
        capped at the client's max_attachment_bytes)."""
        return self._fetch()


class PageSource(Protocol):
    """The seam import_tasks.py crawls through -- it never imports a
    concrete client. A future generic-website importer implements this same
    protocol."""

    def fetch_page(self, page_id: str) -> Page: ...

    def fetch_context(self, page_id: str) -> PageContext: ...

    def iter_children(self, page_id: str) -> Iterator[str]: ...

    def iter_attachments(self, page_id: str) -> Iterator[AttachmentMeta]: ...

    def resolve_space_root(self, space_key: str) -> str: ...

    def resolve_page_by_title(self, space_key: str, title: str) -> str: ...


def extract_page_id(value: str) -> str | None:
    """Extract a Confluence page id from a bare numeric id, a Cloud pretty
    URL (`/pages/{id}/...`), or a DC viewpage URL (`?pageId={id}`). Returns
    None when no id is recognizable (e.g. title-only /display links or
    viewpage.action?spaceKey=&title= links) -- callers fall back to
    `_extract_display_parts` + `resolve_page_by_title` for those."""
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return value
    match = _PAGE_ID_QUERY_RE.search(value)
    if match:
        return match.group(1)
    match = _PAGE_ID_PATH_RE.search(value)
    if match:
        return match.group(1)
    return None


def _extract_display_parts(value: str) -> tuple[str, str] | None:
    """Extract (space_key, title) from a title-only Confluence link that
    carries no numeric page id: `/display/SPACEKEY/Page+Title` (Cloud and
    Server/DC both serve this legacy path) or the classic Server/DC
    `viewpage.action?spaceKey=...&title=...` form. The title is fully
    URL-decoded ('+' and %XX both become their plain characters, matching
    how Confluence itself builds these links from a page title). Returns
    None when neither form is recognizable; the caller resolves the id via
    `PageSource.resolve_page_by_title`."""
    value = value.strip()
    if not value:
        return None
    query = urlsplit(value).query
    if query:
        params = parse_qs(query)
        space_keys = params.get('spaceKey')
        titles = params.get('title')
        if space_keys and titles and space_keys[0] and titles[0]:
            return space_keys[0], titles[0]
    match = _DISPLAY_PATH_RE.search(value)
    if match:
        space_key = unquote(match.group(1))
        title = unquote_plus(match.group(2))
        if space_key and title:
            return space_key, title
    return None


def build_auth_header(auth_type: ImportAuthType | str, auth_username: str, credential: str) -> dict[str, str]:
    """Authorization header for a source. cloud_basic = Basic
    base64(email:api_token); pat_bearer = Bearer <PAT> (Server/DC >= 7.9)."""
    try:
        kind = ImportAuthType(auth_type)
    except ValueError:
        raise ConfluenceError(f'unknown auth type {auth_type!r}') from None
    if not credential:
        raise ConfluenceError('source has no credential')
    if kind is ImportAuthType.CLOUD_BASIC:
        if not auth_username:
            raise ConfluenceError('cloud_basic auth requires an email/username')
        token = base64.b64encode(f'{auth_username}:{credential}'.encode()).decode('ascii')
        return {'Authorization': f'Basic {token}'}
    return {'Authorization': f'Bearer {credential}'}


def _wiki_base(base_url: str) -> str:
    # Cloud URLs live under /wiki; tolerate users saving the base with the
    # /wiki suffix already present.
    base_url = base_url.rstrip('/')
    if base_url.endswith('/wiki'):
        return base_url
    return base_url + '/wiki'


def _host_key(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url)
    hostname = (parts.hostname or '').lower()
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme.lower())
    return hostname, port


class _ConfluenceClientBase:
    # Overridden by each concrete subclass; only used for diagnostics (log
    # lines), never for branching request logic.
    _server_kind_label = 'unknown'

    def __init__(
        self,
        base_url: str,
        *,
        auth_type: ImportAuthType | str,
        auth_username: str = '',
        credential: str,
        allowed_private_hosts: frozenset[str] | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = 5 * 1024 * 1024,
        max_attachment_bytes: int = 20 * 1024 * 1024,
        max_children_per_page: int = 1000,
        max_attachments_per_page: int = 50,
    ) -> None:
        self._base_url = base_url.rstrip('/')
        self._auth_headers = build_auth_header(auth_type, auth_username, credential)
        self._allowed_private_hosts = allowed_private_hosts
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._max_attachment_bytes = max_attachment_bytes
        self._max_children_per_page = max_children_per_page
        self._max_attachments_per_page = max_attachments_per_page
        self._host = _host_key(self._base_url)

    def _check_same_host(self, url: str) -> str:
        # Every response-supplied URL (next links, download links) must stay
        # on the source's host: following it carries our Authorization
        # header, and a hostile server must not redirect that elsewhere.
        if _host_key(url) != self._host:
            raise ConfluenceError(f'Confluence response URL {urlsplit(url).netloc!r} points off the source host')
        return url

    def _absolutize(self, link: str, context_base: str) -> str:
        if link.startswith(('http://', 'https://')):
            return self._check_same_host(link)
        if link.startswith('/'):
            return context_base.rstrip('/') + link
        return context_base.rstrip('/') + '/' + link

    def _fetch(self, url: str, headers: dict[str, str], max_bytes: int):
        try:
            return safe_fetch(
                url,
                headers=headers,
                timeout=self._timeout,
                max_bytes=max_bytes,
                allowed_private_hosts=self._allowed_private_hosts,
            )
        except SafeFetchError as exc:
            raise ConfluenceError(f'Confluence request failed: {exc}') from exc

    def _http_error(self, url: str, response, *, context: str) -> ConfluenceError:
        """Build the ConfluenceError for a non-200 response, logging a
        WARNING alongside it. The log line carries the path (never the query
        string -- credentials never live there for this API, but tokens
        occasionally do turn up as query params on other APIs, and this
        keeps the discipline uniform), status, server kind, and Confluence's
        own `message` field when the body is a JSON error object. The
        exception message carries only the status and a fixed plain-language
        explanation -- never the response body -- so it stays safe to surface
        in ImportRun.error_message / state.errors (spec: never leak
        credentials/tokens into the log or the error text)."""
        path = urlsplit(url).path
        body_message = _extract_json_error_message(response.body)
        logger.warning(
            'Confluence %s request failed: GET %s -> HTTP %s [%s]%s',
            context,
            path,
            response.status_code,
            self._server_kind_label,
            f' -- Confluence said: {body_message!r}' if body_message else '',
        )
        return ConfluenceError(
            f'Confluence API request to {path!r} returned HTTP {response.status_code}{_explain_status(response.status_code)}',
            status_code=response.status_code,
        )

    def _get_json(self, url: str) -> dict:
        response = self._fetch(url, {'Accept': 'application/json', **self._auth_headers}, self._max_response_bytes)
        if response.status_code != 200:
            raise self._http_error(url, response, context='API')
        logger.debug('Confluence API request: GET %s -> HTTP 200 [%s]', urlsplit(url).path, self._server_kind_label)
        try:
            data = json.loads(response.body)
        except ValueError as exc:
            raise ConfluenceError(f'Confluence API response from {urlsplit(url).path!r} is not valid JSON') from exc
        if not isinstance(data, dict):
            raise ConfluenceError(f'Confluence API response from {urlsplit(url).path!r} has unexpected shape')
        return data

    def _get_bytes(self, url: str) -> bytes:
        self._check_same_host(url)
        response = self._fetch(url, dict(self._auth_headers), self._max_attachment_bytes)
        if response.status_code != 200:
            raise self._http_error(url, response, context='attachment download')
        logger.debug(
            'Confluence attachment download: GET %s -> HTTP 200 [%s]', urlsplit(url).path, self._server_kind_label
        )
        return response.body

    def _iter_results(self, first_url: str, next_url_fn) -> Iterator[dict]:
        """Yield result items across pagination, defensively bounded: a
        repeated/looping next URL or an empty results page stops iteration
        (a hostile server must not be able to loop us forever)."""
        url: str | None = first_url
        seen_urls: set[str] = set()
        while url and url not in seen_urls:
            seen_urls.add(url)
            data = self._get_json(url)
            results = data.get('results')
            if not isinstance(results, list) or not results:
                return
            for item in results:
                if isinstance(item, dict):
                    yield item
            url = next_url_fn(url, data, len(results))

    def _attachment_meta(self, item: dict, page_id: str, download_context_base: str) -> AttachmentMeta | None:
        attachment_id = str(item.get('id') or '')
        link = self._attachment_download_link(item)
        if not attachment_id or not link:
            return None
        download_url = self._absolutize(link, download_context_base)
        size_raw = self._attachment_size(item)
        size_bytes = int(size_raw) if isinstance(size_raw, (int, float)) else None
        return AttachmentMeta(
            id=attachment_id,
            filename=str(item.get('title') or f'attachment-{attachment_id}'),
            media_type=self._attachment_media_type(item),
            size_bytes=size_bytes,
            download_url=download_url,
            page_id=page_id,
            _fetch=lambda url=download_url: self._get_bytes(url),
        )

    def _attachment_download_link(self, item: dict) -> str:
        raise NotImplementedError

    def _attachment_media_type(self, item: dict) -> str:
        raise NotImplementedError

    def _attachment_size(self, item: dict):
        raise NotImplementedError

    def _parse_context(self, data: dict) -> PageContext:
        # `ancestors` comes back root-first (Confluence's own order), so no
        # reordering is needed -- the direct parent is simply the last one.
        space = data.get('space')
        space_key = space.get('key') if isinstance(space, dict) else None
        ancestor_titles: list[str] = []
        ancestors = data.get('ancestors')
        if isinstance(ancestors, list):
            for ancestor in ancestors:
                title = ancestor.get('title') if isinstance(ancestor, dict) else None
                if isinstance(title, str):
                    ancestor_titles.append(title)
        return PageContext(
            space_key=str(space_key) if space_key else None,
            ancestor_titles=ancestor_titles,
        )


class ConfluenceV2Client(_ConfluenceClientBase):
    """Confluence Cloud, v2 REST (`/wiki/api/v2`), cursor pagination via
    `_links.next`, body format export_view."""

    _server_kind_label = SERVER_KIND_CLOUD

    def __init__(self, base_url: str, **kwargs) -> None:
        super().__init__(base_url, **kwargs)
        self._wiki = _wiki_base(self._base_url)
        self._api = self._wiki + '/api/v2'

    def _next_by_cursor(self, current_url: str, data: dict, _result_count: int) -> str | None:
        links = data.get('_links')
        next_link = links.get('next') if isinstance(links, dict) else None
        if not next_link:
            return None
        return self._check_same_host(urljoin(current_url, next_link))

    def fetch_page(self, page_id: str) -> Page:
        data = self._get_json(f'{self._api}/pages/{quote(str(page_id), safe="")}?body-format=export_view')
        body = data.get('body')
        export_view = body.get('export_view') if isinstance(body, dict) else None
        html = export_view.get('value') if isinstance(export_view, dict) else None
        if not isinstance(html, str):
            raise ConfluenceError(f'page {page_id}: response has no export_view body')
        version = data.get('version')
        version_number = version.get('number') if isinstance(version, dict) else None
        links = data.get('_links')
        webui = links.get('webui') if isinstance(links, dict) else None
        url = self._absolutize(webui, self._wiki) if webui else f'{self._api}/pages/{page_id}'
        return Page(
            id=str(data.get('id') or page_id),
            title=str(data.get('title') or ''),
            version=int(version_number) if isinstance(version_number, int) else 0,
            html=html,
            url=url,
        )

    def fetch_context(self, page_id: str) -> PageContext:
        data = self._get_json(f'{self._api}/pages/{quote(str(page_id), safe="")}?expand=ancestors,space')
        return self._parse_context(data)

    def iter_children(self, page_id: str) -> Iterator[str]:
        first = f'{self._api}/pages/{quote(str(page_id), safe="")}/children?limit={_LIST_PAGE_LIMIT}'
        yielded = 0
        for item in self._iter_results(first, self._next_by_cursor):
            if item.get('type', 'page') != 'page':
                continue
            child_id = str(item.get('id') or '')
            if not child_id:
                continue
            yield child_id
            yielded += 1
            if yielded >= self._max_children_per_page:
                return

    def iter_attachments(self, page_id: str) -> Iterator[AttachmentMeta]:
        first = f'{self._api}/pages/{quote(str(page_id), safe="")}/attachments?limit={_LIST_PAGE_LIMIT}'
        yielded = 0
        for item in self._iter_results(first, self._next_by_cursor):
            meta = self._attachment_meta(item, page_id, self._wiki)
            if meta is None:
                continue
            yield meta
            yielded += 1
            if yielded >= self._max_attachments_per_page:
                return

    def resolve_space_root(self, space_key: str) -> str:
        data = self._get_json(f'{self._api}/spaces?keys={quote(space_key, safe="")}&limit=1')
        results = data.get('results')
        if not isinstance(results, list) or not results:
            raise ConfluenceError(_space_not_found_message(space_key))
        homepage_id = results[0].get('homepageId') if isinstance(results[0], dict) else None
        if not homepage_id:
            raise ConfluenceError(f'space {space_key!r} has no homepage')
        return str(homepage_id)

    def resolve_page_by_title(self, space_key: str, title: str) -> str:
        # v2 has no spaceKey+title lookup on /pages -- it needs the space's
        # numeric id first, same two-step resolution resolve_space_root uses.
        space_data = self._get_json(f'{self._api}/spaces?keys={quote(space_key, safe="")}&limit=1')
        space_results = space_data.get('results')
        if not isinstance(space_results, list) or not space_results:
            raise ConfluenceError(_space_not_found_message(space_key))
        space_id = space_results[0].get('id') if isinstance(space_results[0], dict) else None
        if not space_id:
            raise ConfluenceError(f'space {space_key!r} has no id in the response')
        page_data = self._get_json(
            f'{self._api}/pages?space-id={quote(str(space_id), safe="")}&title={quote(title, safe="")}&limit=1'
        )
        page_results = page_data.get('results')
        if not isinstance(page_results, list) or not page_results:
            raise ConfluenceError(_page_not_found_message(space_key, title))
        page_id = page_results[0].get('id') if isinstance(page_results[0], dict) else None
        if not page_id:
            raise ConfluenceError(f'page {title!r} in space {space_key!r} has no id in the response')
        return str(page_id)

    def _attachment_download_link(self, item: dict) -> str:
        link = item.get('downloadLink')
        if not link:
            links = item.get('_links')
            link = links.get('download') if isinstance(links, dict) else None
        return str(link or '')

    def _attachment_media_type(self, item: dict) -> str:
        return str(item.get('mediaType') or '')

    def _attachment_size(self, item: dict):
        return item.get('fileSize')


class ConfluenceV1Client(_ConfluenceClientBase):
    """Confluence Server/Data Center, v1 REST (`/rest/api`), start/limit
    pagination, body via expand=body.export_view."""

    _server_kind_label = SERVER_KIND_DATACENTER

    def __init__(self, base_url: str, **kwargs) -> None:
        super().__init__(base_url, **kwargs)
        self._api = self._base_url + '/rest/api'

    def _next_by_start(self, current_url: str, _data: dict, result_count: int) -> str | None:
        # A short page (fewer than `limit` results) is the last one.
        if result_count < _LIST_PAGE_LIMIT:
            return None
        match = re.search(r'([?&])start=(\d+)', current_url)
        if match is None:
            return None
        next_start = int(match.group(2)) + _LIST_PAGE_LIMIT
        return current_url[: match.start()] + f'{match.group(1)}start={next_start}' + current_url[match.end():]

    def fetch_page(self, page_id: str) -> Page:
        data = self._get_json(
            f'{self._api}/content/{quote(str(page_id), safe="")}?expand=body.export_view,version,space'
        )
        body = data.get('body')
        export_view = body.get('export_view') if isinstance(body, dict) else None
        html = export_view.get('value') if isinstance(export_view, dict) else None
        if not isinstance(html, str):
            raise ConfluenceError(f'page {page_id}: response has no export_view body')
        version = data.get('version')
        version_number = version.get('number') if isinstance(version, dict) else None
        links = data.get('_links')
        webui = links.get('webui') if isinstance(links, dict) else None
        url = self._absolutize(webui, self._base_url) if webui else f'{self._api}/content/{page_id}'
        return Page(
            id=str(data.get('id') or page_id),
            title=str(data.get('title') or ''),
            version=int(version_number) if isinstance(version_number, int) else 0,
            html=html,
            url=url,
        )

    def fetch_context(self, page_id: str) -> PageContext:
        data = self._get_json(f'{self._api}/content/{quote(str(page_id), safe="")}?expand=ancestors,space')
        return self._parse_context(data)

    def iter_children(self, page_id: str) -> Iterator[str]:
        first = f'{self._api}/content/{quote(str(page_id), safe="")}/child/page?start=0&limit={_LIST_PAGE_LIMIT}'
        yielded = 0
        for item in self._iter_results(first, self._next_by_start):
            child_id = str(item.get('id') or '')
            if not child_id:
                continue
            yield child_id
            yielded += 1
            if yielded >= self._max_children_per_page:
                return

    def iter_attachments(self, page_id: str) -> Iterator[AttachmentMeta]:
        first = (
            f'{self._api}/content/{quote(str(page_id), safe="")}/child/attachment'
            f'?start=0&limit={_LIST_PAGE_LIMIT}&expand=metadata'
        )
        yielded = 0
        for item in self._iter_results(first, self._next_by_start):
            meta = self._attachment_meta(item, page_id, self._base_url)
            if meta is None:
                continue
            yield meta
            yielded += 1
            if yielded >= self._max_attachments_per_page:
                return

    def resolve_space_root(self, space_key: str) -> str:
        try:
            data = self._get_json(f'{self._api}/space/{quote(space_key, safe="")}?expand=homepage')
        except ConfluenceError as exc:
            if exc.status_code == 404:
                # Data Center is well known to answer 404 for a space the
                # account merely lacks permission on -- see
                # _space_not_found_message.
                raise ConfluenceError(_space_not_found_message(space_key), status_code=404) from exc
            raise
        homepage = data.get('homepage')
        homepage_id = homepage.get('id') if isinstance(homepage, dict) else None
        if not homepage_id:
            raise ConfluenceError(f'space {space_key!r} has no homepage')
        return str(homepage_id)

    def resolve_page_by_title(self, space_key: str, title: str) -> str:
        data = self._get_json(
            f'{self._api}/content?spaceKey={quote(space_key, safe="")}&title={quote(title, safe="")}&limit=1'
        )
        results = data.get('results')
        if not isinstance(results, list) or not results:
            raise ConfluenceError(_page_not_found_message(space_key, title))
        page_id = results[0].get('id') if isinstance(results[0], dict) else None
        if not page_id:
            raise ConfluenceError(f'page {title!r} in space {space_key!r} has no id in the response')
        return str(page_id)

    def _attachment_download_link(self, item: dict) -> str:
        links = item.get('_links')
        link = links.get('download') if isinstance(links, dict) else None
        return str(link or '')

    def _attachment_media_type(self, item: dict) -> str:
        metadata = item.get('metadata')
        media_type = metadata.get('mediaType') if isinstance(metadata, dict) else None
        if not media_type:
            extensions = item.get('extensions')
            media_type = extensions.get('mediaType') if isinstance(extensions, dict) else None
        return str(media_type or '')

    def _attachment_size(self, item: dict):
        extensions = item.get('extensions')
        return extensions.get('fileSize') if isinstance(extensions, dict) else None


def create_client(
    *,
    base_url: str,
    server_kind: str,
    auth_type: ImportAuthType | str,
    auth_username: str = '',
    credential: str,
    allowed_private_hosts: frozenset[str] | None = None,
    timeout: float = 30.0,
    max_response_bytes: int = 5 * 1024 * 1024,
    max_attachment_bytes: int = 20 * 1024 * 1024,
    max_children_per_page: int = 1000,
    max_attachments_per_page: int = 50,
) -> PageSource:
    """Build the client matching the source's persisted `server_kind`
    ('cloud' -> v2, 'datacenter' -> v1). An empty/unknown kind means the
    source was never successfully tested -- the caller gets a clean error
    instead of a guessed endpoint shape."""
    if server_kind == SERVER_KIND_CLOUD:
        client_cls = ConfluenceV2Client
    elif server_kind == SERVER_KIND_DATACENTER:
        client_cls = ConfluenceV1Client
    else:
        raise ConfluenceError(
            f'source has no resolved server kind ({server_kind!r}) -- run a successful connection test first'
        )
    return client_cls(
        base_url,
        auth_type=auth_type,
        auth_username=auth_username,
        credential=credential,
        allowed_private_hosts=allowed_private_hosts,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        max_attachment_bytes=max_attachment_bytes,
        max_children_per_page=max_children_per_page,
        max_attachments_per_page=max_attachments_per_page,
    )


def _probe(url: str, headers: dict[str, str], *, timeout: float, max_bytes: int,
           allowed_private_hosts: frozenset[str] | None) -> tuple[int, bool]:
    """One detection probe: (status, looks_like_confluence). A 200 only
    counts when the body parses as a JSON object with a `results` list --
    an arbitrary web server answering 200 with HTML must not be detected
    as Confluence."""
    response = safe_fetch(
        url,
        headers={'Accept': 'application/json', **headers},
        timeout=timeout,
        max_bytes=max_bytes,
        allowed_private_hosts=allowed_private_hosts,
    )
    if response.status_code != 200:
        return response.status_code, False
    try:
        data = json.loads(response.body)
    except ValueError:
        return response.status_code, False
    return response.status_code, isinstance(data, dict) and isinstance(data.get('results'), list)


def detect_server_kind(
    base_url: str,
    *,
    auth_type: ImportAuthType | str,
    auth_username: str = '',
    credential: str,
    allowed_private_hosts: frozenset[str] | None = None,
    timeout: float = 30.0,
    max_bytes: int = 5 * 1024 * 1024,
) -> tuple[str, str]:
    """Detection used by POST /import/sources/{id}/test: probe the Cloud v2
    spaces endpoint, then the Server/DC v1 space endpoint. Returns
    (server_kind, api_base_path) on success; raises ConfluenceError with a
    user-presentable message (never containing the credential) otherwise."""
    base_url = base_url.rstrip('/')
    headers = build_auth_header(auth_type, auth_username, credential)

    try:
        v2_status, v2_ok = _probe(
            f'{_wiki_base(base_url)}/api/v2/spaces?limit=1',
            headers,
            timeout=timeout,
            max_bytes=max_bytes,
            allowed_private_hosts=allowed_private_hosts,
        )
    except SafeFetchError as exc:
        raise ConfluenceError(f'could not reach {base_url}: {exc}') from exc
    if v2_ok:
        return SERVER_KIND_CLOUD, API_BASE_PATH_CLOUD

    try:
        v1_status, v1_ok = _probe(
            f'{base_url}/rest/api/space?limit=1',
            headers,
            timeout=timeout,
            max_bytes=max_bytes,
            allowed_private_hosts=allowed_private_hosts,
        )
    except SafeFetchError as exc:
        raise ConfluenceError(f'could not reach {base_url}: {exc}') from exc
    if v1_ok:
        return SERVER_KIND_DATACENTER, API_BASE_PATH_DATACENTER

    auth_status = next((status for status in (v2_status, v1_status) if status in (401, 403)), None)
    if auth_status is not None:
        raise ConfluenceError(
            f'authentication failed (HTTP {auth_status}) -- check the credential and auth type',
            status_code=auth_status,
        )
    raise ConfluenceError(
        f'not recognized as a Confluence server (Cloud v2 probe: HTTP {v2_status}, '
        f'Server/DC v1 probe: HTTP {v1_status})'
    )
