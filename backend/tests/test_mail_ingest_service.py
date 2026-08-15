"""Mail ingestion service tests (docs/integrations/mail-ingestion.md).

Pure parsing/rendering, exercised with hand-built `email.message.EmailMessage`
fixtures -- no DB, no FastAPI. Covers: simple text, html+plain alternative
(and that the discarded alternative is NOT listed as a part), mixed with a
supported + an unsupported attachment, the `multipart/signed` (S/MIME) trap
where a bare `iter_attachments()` would lose the nested PDF, top-level
`multipart/related` inline-image classification (incl. the cid: ->
placeholder body rewrite), `message/rfc822` forwards, missing Message-ID, and
walk-order determinism across repeated parses.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
import yaml

from app.services.mail_ingest import (
    MailParseError,
    compute_content_sha256,
    extract_mail_part,
    parse_mail_message,
)

INGESTED_AT = datetime(2026, 8, 15, 10, 2, 11, tzinfo=timezone.utc)
INGESTED_BY = 'mail-gateway'
SHA = 'deadbeef' * 8


def _parse(raw: bytes):
    return parse_mail_message(raw, content_sha256=SHA, ingested_by=INGESTED_BY, ingested_at=INGESTED_AT)


def _split_frontmatter(markdown: str) -> tuple[dict, str]:
    assert markdown.startswith('---\n')
    end = markdown.index('\n---\n', 3)
    meta = yaml.safe_load(markdown[4 : end + 1])
    body = markdown[end + len('\n---\n') :]
    return meta, body


# --- fixtures ----------------------------------------------------------------

def _simple_text_mail() -> bytes:
    msg = EmailMessage()
    msg['Subject'] = 'Simple report'
    msg['From'] = 'Alice Example <alice@partner.example>'
    msg['To'] = 'billing@firma.example'
    msg['Cc'] = 'audit@firma.example'
    msg['Date'] = 'Sat, 15 Aug 2026 09:12:00 +0000'
    msg['Message-ID'] = '<20260815091200.abc@partner.example>'
    msg.set_content('Plain body text, umlaut: äöü.')
    return msg.as_bytes()


def _html_plain_alternative_mail() -> bytes:
    msg = EmailMessage()
    msg['Subject'] = 'Quarterly report'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('Plain mirror of the html body.')
    msg.add_alternative('<p>HTML <strong>body</strong>.</p>', subtype='html')
    return msg.as_bytes()


def _mixed_with_pdf_and_zip_mail() -> bytes:
    msg = EmailMessage()
    msg['Subject'] = 'Quarterly report'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('See attached.')
    msg.add_attachment(
        b'%PDF-1.4 fake pdf bytes', maintype='application', subtype='pdf', filename='bericht-q3.pdf'
    )
    msg.add_attachment(b'PK\x03\x04 fake zip bytes', maintype='application', subtype='zip', filename='archiv.zip')
    return msg.as_bytes()


def _signed_mixed_with_pdf_mail() -> bytes:
    """Hand-built multipart/signed (S/MIME) wrapping a multipart/mixed with
    a text body + a PDF, plus the detached pkcs7 signature. Constructed as a
    raw string since EmailMessage has no `make_signed()` convenience -- this
    is exactly the S/MIME shape the design doc warns `iter_attachments()`
    mishandles."""
    inner = (
        'Content-Type: multipart/mixed; boundary="innerBoundary"\r\n'
        '\r\n'
        '--innerBoundary\r\n'
        'Content-Type: text/plain; charset="utf-8"\r\n'
        'Content-Transfer-Encoding: 7bit\r\n'
        '\r\n'
        'Signed body text.\r\n'
        '\r\n'
        '--innerBoundary\r\n'
        'Content-Type: application/pdf\r\n'
        'Content-Transfer-Encoding: base64\r\n'
        'Content-Disposition: attachment; filename="contract.pdf"\r\n'
        '\r\n'
        + base64.b64encode(b'%PDF-1.4 fake pdf bytes').decode()
        + '\r\n'
        '--innerBoundary--\r\n'
    )
    signature = base64.b64encode(b'fake-signature-bytes').decode()
    raw = (
        'Subject: Signed report\r\n'
        'From: alice@partner.example\r\n'
        'To: billing@firma.example\r\n'
        'MIME-Version: 1.0\r\n'
        'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
        'micalg=sha-256; boundary="outerBoundary"\r\n'
        '\r\n'
        '--outerBoundary\r\n' + inner + '--outerBoundary\r\n'
        'Content-Type: application/pkcs7-signature; name="smime.p7s"\r\n'
        'Content-Transfer-Encoding: base64\r\n'
        'Content-Disposition: attachment; filename="smime.p7s"\r\n'
        '\r\n' + signature + '\r\n'
        '--outerBoundary--\r\n'
    )
    return raw.encode('utf-8')


def _related_inline_png_mail() -> bytes:
    msg = EmailMessage()
    msg['Subject'] = 'Newsletter'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('<p>See <img src="cid:logo123"></p>', subtype='html')
    msg.add_related(
        b'\x89PNG fake bytes', maintype='image', subtype='png', filename='logo.png', cid='<logo123>', disposition='inline'
    )
    return msg.as_bytes()


def _forwarded_message_mail() -> bytes:
    inner = EmailMessage()
    inner['Subject'] = 'Original mail'
    inner['From'] = 'sender@partner.example'
    inner['To'] = 'someone@partner.example'
    inner.set_content('Original body text.')

    outer = EmailMessage()
    outer['Subject'] = 'Fwd: Original mail'
    outer['From'] = 'forwarder@partner.example'
    outer['To'] = 'billing@firma.example'
    outer.set_content('See attached original.')
    outer.add_attachment(inner)
    return outer.as_bytes()


def _no_message_id_mail() -> bytes:
    msg = EmailMessage()
    msg['Subject'] = 'No id here'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('Body.')
    return msg.as_bytes()


# --- tests ---------------------------------------------------------------------

def test_simple_text_mail():
    result = _parse(_simple_text_mail())

    assert result.envelope.subject == 'Simple report'
    assert result.envelope.from_address == 'alice@partner.example'
    assert result.envelope.to == ['billing@firma.example']
    assert result.envelope.cc == ['audit@firma.example']
    assert result.envelope.rfc_message_id == '<20260815091200.abc@partner.example>'
    assert result.envelope.sent_at == datetime(2026, 8, 15, 9, 12, 0, tzinfo=timezone.utc)

    assert result.body.format == 'text/plain'
    meta, body = _split_frontmatter(result.body.markdown)
    assert meta == {
        'source': 'mail',
        'subject': 'Simple report',
        'from': 'alice@partner.example',
        'to': ['billing@firma.example'],
        'date': '2026-08-15T09:12:00Z',
        'message_id': '<20260815091200.abc@partner.example>',
        'content_sha256': SHA,
        'ingested_by': 'mail-gateway',
        'ingested_at': '2026-08-15T10:02:11Z',
    }
    assert 'Plain body text, umlaut: äöü.' in body

    assert result.parts == []


def test_html_plain_alternative_picks_html_and_drops_plain_sibling():
    result = _parse(_html_plain_alternative_mail())

    assert result.body.format == 'text/html'
    assert '**body**' in result.body.markdown or 'body' in result.body.markdown
    # The un-chosen plain-text mirror is a duplicate rendering, not a
    # separate part -- must not show up as a bogus "skipped" attachment.
    assert result.parts == []


def test_mixed_with_pdf_and_zip():
    result = _parse(_mixed_with_pdf_and_zip_mail())

    assert result.body.format == 'text/plain'
    assert len(result.parts) == 2

    pdf, zip_part = result.parts
    assert pdf.index == 0
    assert pdf.filename == 'bericht-q3.pdf'
    assert pdf.content_type == 'application/pdf'
    assert pdf.outcome == 'job'
    assert pdf.skip_reason is None

    assert zip_part.index == 1
    assert zip_part.filename == 'archiv.zip'
    assert zip_part.outcome == 'skipped'
    assert zip_part.skip_reason == 'unsupported_type'


def test_signed_message_pdf_is_found_not_lost():
    """The S/MIME trap: a bare iter_attachments() would yield the inner
    multipart/mixed as one opaque, unclassified pseudo-attachment and lose
    the PDF entirely. The walk must recurse through multipart/signed into
    its protected multipart/mixed content and find the PDF."""
    result = _parse(_signed_mixed_with_pdf_mail())

    assert result.body.format == 'text/plain'
    assert 'Signed body text.' in result.body.markdown

    outcomes = {part.filename: part.outcome for part in result.parts}
    assert outcomes['contract.pdf'] == 'job'
    # The detached pkcs7 signature is also recursed into (not skipped
    # silently) -- it just fails extension validation like any other
    # unrecognized type.
    assert outcomes['smime.p7s'] == 'skipped'
    pdf_part = next(p for p in result.parts if p.filename == 'contract.pdf')
    assert pdf_part.content_type == 'application/pdf'


def test_related_inline_png_is_inline_not_attachment_and_body_has_cid_placeholder():
    result = _parse(_related_inline_png_mail())

    assert result.body.format == 'text/html'
    assert len(result.parts) == 1
    png_part = result.parts[0]
    assert png_part.filename == 'logo.png'
    assert png_part.content_type == 'image/png'
    assert png_part.outcome == 'inline'
    assert png_part.skip_reason is None

    assert '![inline attachment: logo.png]' in result.body.markdown
    assert 'cid:' not in result.body.markdown


def test_cid_placeholder_escapes_markdown_metachars_in_attacker_filename():
    """Regression: the inline attachment's filename is attacker-controlled
    (mail-supplied) and was interpolated verbatim into the `![inline
    attachment: <filename>]` cid: placeholder -- a filename crafted as
    `foo](evil-target)[bar` closed the placeholder's `[...]` early and
    opened a real `(...)` group, turning the intended-inert placeholder
    into a live Markdown image/link `MarkdownView` would then parse. The
    escaped filename must never form a live `](...)` sequence in the
    rendered body markdown. (Deliberately slash-free: a `/`-containing
    payload -- e.g. a real `https://host/path` URL -- gets its `://host/`
    prefix stripped by `sanitize_filename`'s path-basename behavior before
    it ever reaches the placeholder, which incidentally defeats that
    specific shape of payload; a bare `(evil-target)` has no such escape
    hatch and reaches `_replace_cid_references` fully intact, so it is the
    shape that actually exercises the escaping fix.)"""
    msg = EmailMessage()
    msg['Subject'] = 'Newsletter'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('<p>See <img src="cid:logo123"></p>', subtype='html')
    evil_filename = 'foo](evil-target)[bar'
    msg.add_related(
        b'\x89PNG fake bytes',
        maintype='image',
        subtype='png',
        filename=evil_filename,
        cid='<logo123>',
        disposition='inline',
    )
    result = _parse(msg.as_bytes())

    assert '](evil-target)' not in result.body.markdown
    assert 'evil-target' in result.body.markdown  # filename text itself still present, just inert
    assert r'\]\(evil-target\)' in result.body.markdown  # brackets/parens actually escaped, not stripped


def test_ocr_inline_images_setting_routes_inline_part_to_job(monkeypatch):
    """`ocr_inline_images` (default False) is the opt-out valve documented in
    core/config.py: flipping it on must route an otherwise-'inline'
    Content-ID part through the normal attachment validate/Job path instead
    of unconditionally classifying it 'inline'."""
    from app.core.config import settings

    monkeypatch.setattr(settings, 'ocr_inline_images', True)
    result = _parse(_related_inline_png_mail())

    assert len(result.parts) == 1
    png_part = result.parts[0]
    assert png_part.filename == 'logo.png'
    # .png is a supported attachment extension, so with the inline
    # short-circuit disabled it falls through to _validate_attachment and
    # qualifies as an ordinary job -- unlike the default (outcome=='inline').
    assert png_part.outcome == 'job'
    assert png_part.skip_reason is None


def test_message_rfc822_forward_is_skipped_nested_message():
    raw = _forwarded_message_mail()
    result = _parse(raw)

    assert len(result.parts) == 1
    part = result.parts[0]
    assert part.content_type == 'message/rfc822'
    assert part.outcome == 'skipped'
    assert part.skip_reason == 'nested_message'

    # Bytes stay downloadable via the re-extraction helper even though the
    # nested message is not recursed into / ingested itself.
    extracted = extract_mail_part(raw, part.index)
    assert extracted is not None
    assert extracted.content_type == 'message/rfc822'
    assert b'Subject: Original mail' in extracted.content


def test_no_message_id_is_none():
    result = _parse(_no_message_id_mail())
    assert result.envelope.rfc_message_id is None


def test_walk_order_is_deterministic_across_reparses():
    raw = _mixed_with_pdf_and_zip_mail()

    first = _parse(raw)
    second = _parse(raw)
    assert first.parts == second.parts

    for part in first.parts:
        a = extract_mail_part(raw, part.index)
        b = extract_mail_part(raw, part.index)
        assert a == b
        assert a.filename == part.filename
        assert a.content_type == part.content_type


def test_extract_mail_part_out_of_range_returns_none():
    raw = _simple_text_mail()  # no parts at all
    assert extract_mail_part(raw, 0) is None


def test_attachment_only_message_has_no_body():
    msg = EmailMessage()
    msg['Subject'] = 'Attachment only'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.make_mixed()
    msg.add_attachment(b'%PDF-1.4 fake pdf bytes', maintype='application', subtype='pdf', filename='doc.pdf')

    result = _parse(msg.as_bytes())
    assert result.body.format is None
    assert result.body.markdown is None
    assert len(result.parts) == 1
    assert result.parts[0].outcome == 'job'


def test_oversized_attachment_is_skipped_too_large(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, 'max_upload_bytes', 10)
    msg = EmailMessage()
    msg['Subject'] = 'Big attachment'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('body')
    msg.add_attachment(b'0123456789ABCDEF', maintype='application', subtype='pdf', filename='big.pdf')

    result = _parse(msg.as_bytes())
    assert len(result.parts) == 1
    assert result.parts[0].outcome == 'skipped'
    assert result.parts[0].skip_reason == 'too_large'


def test_unparseable_input_raises_mail_parse_error():
    with pytest.raises(MailParseError):
        parse_mail_message(b'', content_sha256=SHA, ingested_by=INGESTED_BY, ingested_at=INGESTED_AT)


def test_compute_content_sha256_matches_hashlib():
    import hashlib

    raw = b'some raw bytes'
    assert compute_content_sha256(raw) == hashlib.sha256(raw).hexdigest()
