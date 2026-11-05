"""Stage 1 - intake & security. Validates bytes, never executes embedded content."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from src.models import ValidationFinding

MAX_BYTES = 15 * 1024 * 1024
MAX_PAGES = 20
STORAGE = Path(__file__).resolve().parents[2] / "storage" / "files"


@dataclass
class IntakeResult:
    document_id: str
    correlation_id: str
    file_name: str
    sha256: str
    size: int
    page_count: int = 0
    stored_path: str | None = None
    findings: list[ValidationFinding] = field(default_factory=list)
    ok: bool = True


def _f(rule, sev, status, msg, **kw):
    return ValidationFinding(rule_id=rule, stage="intake", severity=sev, status=status, message=msg, **kw)


def intake(file_name: str, data: bytes) -> IntakeResult:
    sha = hashlib.sha256(data).hexdigest()
    doc_id = "DOC-" + uuid.uuid4().hex[:10].upper()
    res = IntakeResult(document_id=doc_id, correlation_id="RUN-" + uuid.uuid4().hex[:12].upper(),
                       file_name=file_name, sha256=sha, size=len(data))
    STORAGE.mkdir(parents=True, exist_ok=True)
    path = STORAGE / f"{doc_id}.bin"
    path.write_bytes(data)                      # stored inert; never opened by anything but the PDF parser
    res.stored_path = str(path)

    if len(data) > MAX_BYTES:
        res.findings.append(_f("INTAKE_LIMITS", "ERROR", "FAIL", f"File is {len(data)} bytes; limit is {MAX_BYTES}."))
        res.ok = False
        return res
    head = data[:1024]
    if b"%PDF-" not in head:
        kind = "PNG image" if data.startswith(b"\x89PNG") else "JPEG image" if data.startswith(b"\xff\xd8") else "unknown binary"
        res.findings.append(_f("INTAKE_FILE_TYPE", "ERROR", "FAIL",
                               f"File signature is not PDF (detected: {kind}) even though the name is '{file_name}'.",
                               expected="%PDF- header", actual=kind))
        res.ok = False
        return res
    res.findings.append(_f("INTAKE_FILE_TYPE", "INFO", "PASS", "PDF signature verified from magic bytes."))
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # corrupt
        res.findings.append(_f("INTAKE_CORRUPT", "ERROR", "FAIL", f"PDF could not be parsed: {type(e).__name__}."))
        res.ok = False
        return res
    if doc.needs_pass or doc.is_encrypted:
        res.findings.append(_f("INTAKE_ENCRYPTED", "ERROR", "FAIL", "PDF is password-protected; ask the vendor for an unprotected copy."))
        res.ok = False
        return res
    if doc.page_count == 0:
        res.findings.append(_f("INTAKE_CORRUPT", "ERROR", "FAIL", "PDF has no readable pages."))
        res.ok = False
        return res
    try:
        for p in doc:
            p.get_text("words")
    except Exception as e:
        res.findings.append(_f("INTAKE_CORRUPT", "ERROR", "FAIL", f"PDF page content is damaged: {type(e).__name__}."))
        res.ok = False
        return res
    if doc.page_count > MAX_PAGES:
        res.findings.append(_f("INTAKE_LIMITS", "ERROR", "FAIL", f"{doc.page_count} pages exceeds limit of {MAX_PAGES}."))
        res.ok = False
        return res
    res.page_count = doc.page_count
    res.findings.append(_f("INTAKE_CORRUPT", "INFO", "PASS", f"PDF parsed: {doc.page_count} page(s), not encrypted."))
    return res
