"""Mail ingestion: pure MIME parsing/rendering (docs/integrations/mail-ingestion.md).

Everything in this module is a pure function of its arguments -- no DB session,
no ORM import, no Celery, no `datetime.now()` / `uuid4()` calls of its own
(`ingested_at` and `content_sha256` are passed in by the caller, which is the
route handler that already computed the streaming sha256 per the design doc's
step 1). That keeps this module trivially unit-testable with hand-built
`email.message.EmailMessage` fixtures and reusable from both the ingest
endpoint (build the manifest once) and the part-content endpoint (re-run the
walk against stored `raw_content` bytes to extract one part on demand -- "no
double storage").

Two things this module deliberately does NOT do, both owned by the API layer:
  - decide `outcome: 'job'` means "create a Job row" -- this module only
    decides whether a part *qualifies* (extension/MIME/size); persistence and
    `job_id` stamping happen in app/api/mail_routes.py.
  - raise `HTTPException` -- `MailParseError` is a plain exception so this
    module has zero FastAPI/Starlette dependency; the route converts it to a
    422.

## Why not `iter_attachments()`

`Message.iter_attachments()` (stdlib) only inspects the immediate children of
the part it's called on. Handed a `multipart/signed` (S/MIME) message, it
yields the inner `multipart/mixed` container as a single opaque, unclassified
"attachment" -- a real PDF nested inside is silently lost, and inline
`Content-ID` images surface or vanish depending on exact nesting depth. So
`_walk` below implements its own deterministic depth-first traversal instead,
recursing into *every* multipart container uniformly (including
`multipart/signed`'s two children: the protected content and the detached
signature -- the signature leaf simply ends up `skipped/unsupported_type`,
same as any other unrecognized extension).

## Body selection vs. part manifest

`Message.get_body(preferencelist=('html', 'plain'))` (stdlib) is reused
as-is for body selection -- it already implements the correct depth-first
"best alternative" search the design doc calls for. `_walk` is a *second*,
independent traversal that additionally classifies every other leaf into
`inline` / `job` / `skipped`, using `is` identity against the `get_body()`
result to know which single leaf to leave out of the manifest. One
extra rule beyond identity: `_walk` also drops the *other*, un-chosen
`text/plain`/`text/html` siblings of a `multipart/alternative` group that
contains the chosen body -- they are alternate renderings of the exact same
content (e.g. the plain-text mirror of an HTML mail), not separate
attachments, and listing them as "skipped: unsupported_type" on literally
every HTML email would be manifest noise the design's response example never
shows. A `multipart/related` sibling (e.g. an inline image next to the HTML
part `get_body` picked) is *not* covered by that rule and is always
classified individually -- it is a distinct resource, not a duplicate
rendering.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup

from app.core.config import settings
from app.services.confluence_markdown import html_to_markdown, render_frontmatter, sanitize_filename
from app.services.storage import ALLOWED_EXTENSIONS, ALLOWED_MIME_TYPES, _EXTENSION_TO_MIME_TYPES, _GENERIC_MIME_TYPES

# Every reason `_validate_attachment` / `_walk` can hand back in a `skipped`
# part -- kept as a single reference set so downstream consumers (routes,
# frontend status labels) don't have to grep this file for the string
# literals.
SKIP_REASONS = frozenset({'unsupported_type', 'too_large', 'nested_message', 'unsupported_container'})

Outcome = Literal['job', 'inline', 'skipped']


class MailParseError(ValueError):
    """Raw bytes could not be parsed as an RFC-822 message.

    `email.parser.BytesParser(policy=policy.default)` is extremely lenient
    (it records defects rather than raising for most malformed input), so in
    practice this fires only for degenerate input (empty body, or something
    the parser itself chokes on) -- but the route layer must never let a
    parser exception become an unhandled 500, so every failure path is
    funneled through this one exception type with the exact detail string
    the design doc specifies for the 422 response.
    """


# --- public result shapes ------------------------------------------------------

@dataclass(frozen=True)
class Envelope:
    subject: str
    from_address: str
    to: list[str]
    cc: list[str]
    sent_at: datetime | None
    rfc_message_id: str | None


@dataclass(frozen=True)
class MailBody:
    format: str | None  # 'text/plain' | 'text/html' | None (no body candidate found)
    markdown: str | None  # frontmatter + rendered body, or None alongside format=None


@dataclass(frozen=True)
class MailPart:
    """One manifest entry -- mirrors `MailMessage.parts[]` (models.py) minus
    `job_id`, which only the route layer (after creating the Job row) can
    fill in."""

    index: int
    filename: str
    content_type: str
    size_bytes: int
    outcome: Outcome
    skip_reason: str | None = None

    def to_dict(self) -> dict:
        data: dict = {
            'index': self.index,
            'filename': self.filename,
            'content_type': self.content_type,
            'size_bytes': self.size_bytes,
            'outcome': self.outcome,
        }
        if self.skip_reason is not None:
            data['skip_reason'] = self.skip_reason
        return data


@dataclass(frozen=True)
class ParsedMail:
    envelope: Envelope
    body: MailBody
    parts: list[MailPart]


@dataclass(frozen=True)
class ExtractedPart:
    """Result of re-walking `raw_content` for one part index -- the
    part-content endpoint's payload, before any HTTP-specific headers are
    attached."""

    filename: str
    content_type: str
    content: bytes


# --- envelope / hashing ---------------------------------------------------------

def compute_content_sha256(raw: bytes) -> str:
    """sha256 hex over the raw bytes -- the dedup key (see design doc). A
    convenience whole-buffer helper for callers that already have the full
    body in memory (the multipart/form-data ingest path); the streaming
    request.stream() path computes this incrementally instead and passes the
    result into `parse_mail_message` directly."""
    return hashlib.sha256(raw).hexdigest()


def _parse_bytes(raw: bytes) -> EmailMessage:
    if not raw:
        raise MailParseError('Unable to parse message')
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:  # pragma: no cover - BytesParser rarely raises; defensive only
        raise MailParseError('Unable to parse message') from exc
    return msg


def _decoded_header(msg: EmailMessage, name: str) -> str:
    value = msg.get(name)
    return str(value).strip() if value is not None else ''


def _addr_list(msg: EmailMessage, name: str) -> list[str]:
    header = msg.get(name)
    if header is None:
        return []
    try:
        addresses = header.addresses
    except AttributeError:
        # policy.default should always give address headers a structured
        # `.addresses`; this is a defensive fallback for a malformed/oddly
        # folded header the policy still handed back as a plain string.
        addresses = None
    if addresses is not None:
        return [addr.addr_spec for addr in addresses if addr.addr_spec]
    return [addr for _name, addr in getaddresses([str(header)]) if addr]


def _from_address(msg: EmailMessage) -> str:
    header = msg.get('From')
    if header is None:
        return ''
    addresses = getattr(header, 'addresses', None)
    if addresses:
        return addresses[0].addr_spec
    return str(header).strip()


def _sent_at(msg: EmailMessage) -> datetime | None:
    header = msg.get('Date')
    if header is None:
        return None
    try:
        return header.datetime
    except (AttributeError, ValueError, TypeError):
        # A present-but-unparseable Date header -- `sent_at` stays NULL
        # rather than failing the whole ingest over a cosmetic field.
        return None


def _rfc_message_id(msg: EmailMessage) -> str | None:
    header = msg.get('Message-ID')
    if header is None:
        return None
    text = str(header).strip()
    return text or None


def _extract_envelope(msg: EmailMessage) -> Envelope:
    return Envelope(
        subject=_decoded_header(msg, 'Subject'),
        from_address=_from_address(msg),
        to=_addr_list(msg, 'To'),
        cc=_addr_list(msg, 'Cc'),
        sent_at=_sent_at(msg),
        rfc_message_id=_rfc_message_id(msg),
    )


# --- MIME-tree walk ---------------------------------------------------------

def _clean_content_id(value: str | None) -> str | None:
    """`Content-ID` headers are conventionally `<foo@bar>`; `cid:` URIs in
    HTML reference the same id *without* the angle brackets. Normalize both
    sides to the bracket-free form so lookups match."""
    if not value:
        return None
    cleaned = value.strip().strip('<>').strip()
    return cleaned or None


@dataclass
class _Leaf:
    index: int
    filename: str
    content_type: str
    content: bytes
    outcome: Outcome
    skip_reason: str | None
    content_id: str | None


def _leaf_filename(part: EmailMessage, index: int, content_type: str) -> str:
    raw_name = part.get_filename()
    if raw_name:
        return sanitize_filename(raw_name)
    ext = mimetypes.guess_extension(content_type) or '.bin'
    return f'part-{index}{ext}'


def _leaf_payload(part: EmailMessage) -> bytes:
    if part.get_content_type() == 'message/rfc822':
        # payload is `[<the embedded EmailMessage>]` once parsed from real
        # bytes (never a raw blob) -- re-serialize the sub-message to get
        # "this part's original bytes" for size/download purposes.
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            return payload[0].as_bytes()
        return b''
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b''


def _is_supported_attachment(filename: str, content_type: str) -> bool:
    """Non-raising mirror of `storage._validate_mime`'s tolerance rules,
    reusing its exact ALLOWED_EXTENSIONS / _EXTENSION_TO_MIME_TYPES /
    _GENERIC_MIME_TYPES tables. Deliberately simpler than the upload-side
    check: a MIME part's declared Content-Type is a far more reliable signal
    than a browser-supplied UploadFile.content_type, so the
    guess-from-filename fallback branch storage.py needs for browser
    uploads is not reproduced here. Never raises -- one unsupported part
    must not fail the whole ingest (a mail with one PDF and one .zip still
    ingests the PDF)."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return False
    content_type = (content_type or '').lower()
    if content_type in ALLOWED_MIME_TYPES or content_type in _GENERIC_MIME_TYPES:
        return True
    return content_type in _EXTENSION_TO_MIME_TYPES.get(suffix, set())


def _validate_attachment(filename: str, content_type: str, size_bytes: int) -> tuple[Outcome, str | None]:
    if not _is_supported_attachment(filename, content_type):
        return 'skipped', 'unsupported_type'
    if size_bytes > settings.max_upload_bytes:
        return 'skipped', 'too_large'
    return 'job', None


def _classify_leaf(part: EmailMessage, index: int) -> _Leaf:
    content_type = part.get_content_type()
    payload = _leaf_payload(part)
    filename = _leaf_filename(part, index, content_type)
    content_id = _clean_content_id(part.get('Content-ID'))
    disposition = part.get_content_disposition()

    if disposition == 'inline' and content_id and not settings.ocr_inline_images:
        # `ocr_inline_images` (default False) is the opt-out valve: with the
        # default, inline Content-ID parts (signature images, logos) are
        # classified 'inline' and never become a Job, so worker time isn't
        # burnt OCR-ing them. Flip the setting on to route them through the
        # same validate/Job path as ordinary attachments instead.
        outcome: Outcome = 'inline'
        skip_reason = None
    else:
        outcome, skip_reason = _validate_attachment(filename, content_type, len(payload))

    return _Leaf(
        index=index,
        filename=filename,
        content_type=content_type,
        content=payload,
        outcome=outcome,
        skip_reason=skip_reason,
        content_id=content_id,
    )


def _container_leaf(part: EmailMessage, index: int, *, skip_reason: str) -> _Leaf:
    """Build a manifest entry for a node we deliberately do not recurse
    into further: `message/rfc822` (v1 does not flatten forwarded mail) or a
    multipart container whose payload isn't the list of sub-parts we expect
    (malformed/empty -- 'unclassifiable container', never silently
    dropped)."""
    content_type = part.get_content_type()
    return _Leaf(
        index=index,
        filename=_leaf_filename(part, index, content_type),
        content_type=content_type,
        content=_leaf_payload(part),
        outcome='skipped',
        skip_reason=skip_reason,
        content_id=None,
    )


def _walk(part: EmailMessage, chosen_body: EmailMessage | None, leaves: list[_Leaf]) -> None:
    if part is chosen_body:
        return  # the body -- accounted for separately, never listed in parts

    if part.get_content_maintype() == 'multipart':
        payload = part.get_payload()
        if not isinstance(payload, list) or not payload:
            leaves.append(_container_leaf(part, len(leaves), skip_reason='unsupported_container'))
            return

        if part.get_content_subtype() == 'alternative':
            for child in payload:
                if child is chosen_body:
                    continue
                if chosen_body is not None and child.get_content_type() in ('text/plain', 'text/html'):
                    # An un-chosen text/plain|text/html sibling of the
                    # rendering get_body() already picked -- a duplicate,
                    # not a separate part (see module docstring). Only
                    # applies once a body was actually found: if this
                    # message has no body at all, fall through and classify
                    # every child individually instead of silently dropping
                    # what might be its only content. Deliberately checks
                    # the exact content type (not get_content_maintype()=='text'),
                    # so other text/* alternatives -- text/calendar meeting
                    # invites, text/rtf, ... -- are NOT swallowed by this rule
                    # and instead fall through to _classify_leaf/_container_leaf
                    # and get a proper manifest entry (never silently dropped).
                    continue
                _walk(child, chosen_body, leaves)
            return

        # multipart/mixed, multipart/related, multipart/signed (S/MIME --
        # both the protected content AND the detached signature get
        # recursed into here, which is exactly what iter_attachments()
        # fails to do), multipart/report, ... -- recurse into every child
        # uniformly so nothing nested is ever lost.
        for child in payload:
            _walk(child, chosen_body, leaves)
        return

    if part.get_content_type() == 'message/rfc822':
        leaves.append(_container_leaf(part, len(leaves), skip_reason='nested_message'))
        return

    leaves.append(_classify_leaf(part, len(leaves)))


def _walk_tree(root: EmailMessage) -> tuple[EmailMessage | None, list[_Leaf]]:
    chosen_body = root.get_body(preferencelist=('html', 'plain'))
    leaves: list[_Leaf] = []
    _walk(root, chosen_body, leaves)
    return chosen_body, leaves


# --- body rendering ---------------------------------------------------------

_MARKDOWN_METACHAR_RE = re.compile(r'([\\`\[\]()#<>])')


def _escape_markdown_text(text: str) -> str:
    """Escape characters that carry structural meaning in Markdown source.
    `sanitize_filename` guarantees a storage-safe *filesystem* name but does
    NOT strip Markdown metacharacters, so an attacker-chosen attachment
    filename interpolated verbatim into the `cid:` placeholder below could
    otherwise close the placeholder's `[...]` early and open its own `(...)`
    group -- turning an intended-inert text placeholder into a real,
    attacker-controlled `![alt](https://...)` image link that `MarkdownView`
    would then render. Escaping keeps the placeholder pure text no matter
    what the sender puts in the filename. Deliberately excludes `*`/`_`:
    markdownify's own text-node escaping (`escape_asterisks`/
    `escape_underscores`, both on by default -- see confluence_markdown.py's
    `html_to_markdown`, run right after this) already neutralizes those via
    a blind `str.replace`, which would double up (and desync) a backslash
    we inserted ourselves ahead of it."""
    return _MARKDOWN_METACHAR_RE.sub(r'\\\1', text)


def _replace_cid_references(html: str, cid_to_filename: Mapping[str, str]) -> str:
    """Replace `<img src="cid:...">` with a literal `![inline attachment:
    <filename>]` text placeholder *before* markdownify runs (v1 -- see
    design doc: rewriting to real part-content URLs would need `MarkdownView`
    to allow a new URL scheme). Unmatched cids (no corresponding inline part
    -- e.g. a dangling reference) fall back to a generic placeholder rather
    than a broken `![alt](cid:...)` link markdownify would otherwise emit.
    The filename is Markdown-escaped (`_escape_markdown_text`) since it is
    attacker-controlled (mail-supplied) and must never be able to form new
    Markdown syntax of its own."""
    if not html or 'cid:' not in html.lower():
        return html
    soup = BeautifulSoup(html, 'html.parser')
    for img in soup.find_all('img'):
        src = (img.get('src') or '').strip()
        if not src.lower().startswith('cid:'):
            continue
        cid = _clean_content_id(src[len('cid:'):])
        filename = cid_to_filename.get(cid) if cid else None
        if filename:
            placeholder = f'![inline attachment: {_escape_markdown_text(filename)}]'
        else:
            placeholder = '![inline attachment]'
        img.replace_with(soup.new_string(placeholder))
    return str(soup)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _frontmatter_meta(envelope: Envelope, *, content_sha256: str, ingested_by: str, ingested_at: datetime) -> dict:
    return {
        'source': 'mail',
        'subject': envelope.subject,
        'from': envelope.from_address,
        'to': envelope.to,
        'date': _iso_utc(envelope.sent_at) if envelope.sent_at else None,
        'message_id': envelope.rfc_message_id,
        'content_sha256': content_sha256,
        'ingested_by': ingested_by,
        'ingested_at': _iso_utc(ingested_at),
    }


def _render_body(
    chosen_body: EmailMessage | None,
    leaves: list[_Leaf],
    *,
    envelope: Envelope,
    content_sha256: str,
    ingested_by: str,
    ingested_at: datetime,
) -> MailBody:
    if chosen_body is None:
        return MailBody(format=None, markdown=None)

    content_type = chosen_body.get_content_type()
    if content_type == 'text/html':
        cid_to_filename = {leaf.content_id: leaf.filename for leaf in leaves if leaf.outcome == 'inline' and leaf.content_id}
        html = chosen_body.get_content()
        if not isinstance(html, str):
            html = html.decode('utf-8', errors='replace')
        html = _replace_cid_references(html, cid_to_filename)
        # base_url='': no page origin exists for mail; capture_attachments=False
        # so confluence_markdown's Confluence-attachment image rewrite
        # (artifacts/{name}) never fires here -- external/embedded images
        # that aren't cid: references stay absolute image links.
        rendered, _images, _links = html_to_markdown(html, base_url='', capture_attachments=False)
    else:
        text = chosen_body.get_content()
        rendered = text if isinstance(text, str) else text.decode('utf-8', errors='replace')

    meta = _frontmatter_meta(envelope, content_sha256=content_sha256, ingested_by=ingested_by, ingested_at=ingested_at)
    return MailBody(format=content_type, markdown=render_frontmatter(meta) + rendered)


# --- public entry points ---------------------------------------------------

def parse_mail_message(raw: bytes, *, content_sha256: str, ingested_by: str, ingested_at: datetime) -> ParsedMail:
    """Full parse of one raw RFC-822 message: envelope, rendered body (incl.
    YAML frontmatter), and the ordered part manifest. `content_sha256` and
    `ingested_at` are caller-supplied (see module docstring) so this stays a
    pure function of its arguments; `ingested_by` is the free-form source
    label (`?source=` query param / request.source) that becomes the
    frontmatter's `ingested_by` key.

    Raises `MailParseError` if `raw` cannot be parsed at all."""
    msg = _parse_bytes(raw)
    envelope = _extract_envelope(msg)
    chosen_body, leaves = _walk_tree(msg)
    body = _render_body(
        chosen_body, leaves, envelope=envelope, content_sha256=content_sha256, ingested_by=ingested_by, ingested_at=ingested_at
    )
    parts = [
        MailPart(
            index=leaf.index,
            filename=leaf.filename,
            content_type=leaf.content_type,
            size_bytes=len(leaf.content),
            outcome=leaf.outcome,
            skip_reason=leaf.skip_reason,
        )
        for leaf in leaves
    ]
    return ParsedMail(envelope=envelope, body=body, parts=parts)


def extract_mail_part(raw: bytes, index: int) -> ExtractedPart | None:
    """Re-run the same walk against `raw` and return the part at `index`
    (whatever its outcome -- inline and skipped parts are downloadable too,
    only their bytes were never persisted separately). Returns None when
    `index` is out of range; the caller (part-content endpoint) additionally
    cross-checks filename/content_type against the stored manifest before
    serving, per the design doc.

    Raises `MailParseError` if `raw` cannot be parsed at all (should not
    happen in practice -- `raw` is `mail_messages.raw_content`, which only
    ever holds bytes that already parsed successfully at ingest time)."""
    msg = _parse_bytes(raw)
    _chosen_body, leaves = _walk_tree(msg)
    for leaf in leaves:
        if leaf.index == index:
            return ExtractedPart(filename=leaf.filename, content_type=leaf.content_type, content=leaf.content)
    return None
