"""Stage 2 - per-page text acquisition: native text layer when usable, OCR otherwise.

Produces, per page:
  lines - gap-split text segments with bounding boxes (PDF points)
  rows  - full horizontal lines (used for table rows)
"""
from __future__ import annotations

import math
import statistics
import threading
from typing import Optional

import pymupdf

from src.models import PageText, TextLine

OCR_DPI = 300
_ocr_engine = None
_ocr_error: Optional[str] = None
_lock = threading.Lock()


def ocr_available() -> tuple[bool, Optional[str]]:
    global _ocr_engine, _ocr_error
    with _lock:
        if _ocr_engine is None and _ocr_error is None:
            try:
                from rapidocr_onnxruntime import RapidOCR  # optional dependency
                _ocr_engine = RapidOCR()
            except Exception as e:  # pragma: no cover - depends on environment
                _ocr_error = f"OCR engine unavailable ({type(e).__name__}: {e}). Install rapidocr-onnxruntime."
    return _ocr_engine is not None, _ocr_error


def text_quality(text: str) -> float:
    """0..1 score of how usable a native text layer is."""
    t = text.strip()
    if len(t) < 30:
        return 0.0
    bad = sum(1 for ch in t if ch == "�" or 0xE000 <= ord(ch) <= 0xF8FF or (ord(ch) < 32 and ch not in "\n\t\r"))
    non_space = [ch for ch in t if not ch.isspace()]
    alnum = sum(1 for ch in non_space if ch.isalnum())
    ratio_alnum = alnum / max(1, len(non_space))
    score = ratio_alnum - 5 * (bad / max(1, len(t)))
    return max(0.0, min(1.0, score / 0.6))


def _segments_from_words(words, page_no, method, gap=18.0):
    """words: list of (x0,y0,x1,y1,text,conf). Returns (segments, rows)."""
    if not words:
        return [], []
    heights = [w[3] - w[1] for w in words]
    tol = max(2.0, statistics.median(heights) * 0.5)
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in words:
        yc = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - yc) <= tol:
            lines[-1][1].append(w)
            n = len(lines[-1][1])
            lines[-1][0] = (lines[-1][0] * (n - 1) + yc) / n
        else:
            lines.append([yc, [w]])
    segments, rows = [], []

    def mk(ws):
        ws = sorted(ws, key=lambda w: w[0])
        confs = [w[5] for w in ws if w[5] is not None]
        return TextLine(page=page_no, text=" ".join(w[4] for w in ws),
                        bbox=[min(w[0] for w in ws), min(w[1] for w in ws), max(w[2] for w in ws), max(w[3] for w in ws)],
                        method=method, confidence=min(confs) if confs else None,
                        size=max(w[3] - w[1] for w in ws))

    for _, ws in lines:
        ws = sorted(ws, key=lambda w: w[0])
        rows.append(mk(ws))
        cur = [ws[0]]
        for w in ws[1:]:
            if w[0] - cur[-1][2] > gap:
                segments.append(mk(cur))
                cur = [w]
            else:
                cur.append(w)
        segments.append(mk(cur))
    return segments, rows


def _native(page, page_no) -> PageText:
    words = [(w[0], w[1], w[2], w[3], w[4], None) for w in page.get_text("words")]
    text = page.get_text()
    segs, rows = _segments_from_words(words, page_no, "native_text")
    return PageText(page=page_no, method="native_text", lines=segs, rows=rows, full_text=text,
                    quality=text_quality(text), width=page.rect.width, height=page.rect.height)


def _ocr(page, page_no) -> PageText:
    ok, err = ocr_available()
    if not ok:
        return PageText(page=page_no, method="ocr", lines=[], rows=[], full_text="", quality=0.0,
                        width=page.rect.width, height=page.rect.height, note=err)
    import numpy as np
    pix = page.get_pixmap(dpi=OCR_DPI)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    with _lock:
        result, _ = _ocr_engine(img)
    result = result or []
    scale = 72.0 / OCR_DPI
    # estimate skew from long boxes and correct y in coordinate space (cheap deskew)
    angles = []
    for box, txt, sc in result:
        (x0, y0), (x1, y1) = box[0], box[1]
        if x1 - x0 > 150:
            angles.append(math.atan2(y1 - y0, x1 - x0))
    skew = statistics.median(angles) if angles else 0.0
    words = []
    for box, txt, sc in result:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        cx = (min(xs) + max(xs)) / 2
        dy = -math.tan(skew) * cx
        words.append((min(xs) * scale, (min(ys) + dy) * scale, max(xs) * scale, (max(ys) + dy) * scale, txt, float(sc)))
    segs, rows = _segments_from_words(words, page_no, "ocr", gap=25.0)
    text = "\n".join(r.text for r in rows)
    confs = [w[5] for w in words]
    return PageText(page=page_no, method="ocr", lines=segs, rows=rows, full_text=text, quality=text_quality(text),
                    ocr_confidence=round(statistics.mean(confs), 3) if confs else None,
                    width=page.rect.width, height=page.rect.height,
                    note=f"OCR @ {OCR_DPI} dpi, deskew {math.degrees(skew):.2f} deg")


def acquire(data: bytes, min_quality: float = 0.5) -> tuple[list[PageText], list[str]]:
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages, notes = [], []
    for i, page in enumerate(doc, start=1):
        native = _native(page, i)
        if native.quality >= min_quality:
            pages.append(native)
            notes.append(f"p{i}: native text layer (quality {native.quality:.2f})")
            continue
        reason = "no text layer" if not native.full_text.strip() else f"garbled text layer (quality {native.quality:.2f})"
        o = _ocr(page, i)
        pages.append(o)
        notes.append(f"p{i}: {reason} -> OCR" + (f" ({o.note})" if o.note else "") +
                     (f", mean confidence {o.ocr_confidence}" if o.ocr_confidence is not None else ""))
    return pages, notes


def render_page_png(data: bytes, page_no: int, dpi: int = 110, highlights: Optional[list[list[float]]] = None) -> bytes:
    doc = pymupdf.open(stream=data, filetype="pdf")
    page = doc[page_no - 1]
    for bb in highlights or []:
        r = pymupdf.Rect(bb[0] - 2, bb[1] - 2, bb[2] + 2, bb[3] + 2)
        annot = page.add_rect_annot(r)
        annot.set_colors(stroke=(1, 0.55, 0), fill=(1, 0.85, 0.2))
        annot.set_opacity(0.35)
        annot.update()
    return page.get_pixmap(dpi=dpi).tobytes("png")
