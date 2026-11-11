"""Deterministic normalisation: text, digits, numbers, dates, currency.

Rules: never guess. Every function returns the normalised value *and* whether the
interpretation was ambiguous, so the policy engine can hold instead of guessing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

# Devanagari, Arabic-Indic and Extended Arabic-Indic digits -> ASCII
_DIGIT_MAP = {}
for base in (0x0966, 0x0660, 0x06F0, 0x09E6, 0x0AE6):  # Devanagari, Arabic-Indic, Ext. Arabic-Indic, Bengali, Gujarati
    for i in range(10):
        _DIGIT_MAP[base + i] = str(i)
_DIGIT_MAP[0x066B] = "."  # Arabic decimal separator
_DIGIT_MAP[0x066C] = ","  # Arabic thousands separator


def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_DIGIT_MAP)
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def detect_scripts(text: str) -> list[str]:
    scripts = set()
    for ch in text:
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:
            scripts.add("Devanagari")
        elif 0x0600 <= cp <= 0x06FF:
            scripts.add("Arabic")
        elif ch.isalpha() and cp < 0x0250:
            scripts.add("Latin")
    return sorted(scripts)


# ---------------------------------------------------------------- locale
_DE_WORDS = re.compile(r"\b(rechnung|rechnungsnummer|rechnungsdatum|gesamtbetrag|zwischensumme|mwst|ust|menge|einzelpreis|bestellnummer|zahlbar|steuerfrei|bezeichnung)\b", re.I)
_EN_WORDS = re.compile(r"\b(invoice|total|amount|quantity|qty|description|subtotal|payment|terms|due)\b", re.I)
_COMMA_DEC = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+,\d{2}(?![\d,])|(?<![\d.,])\d+,\d{2}(?![\d.,])")
_DOT_DEC = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{2,3})+\.\d{2}(?![\d.])|(?<![\d.,])\d+\.\d{2}(?![\d.,])")
_SLASH_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_DOT_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


@dataclass
class LocaleInfo:
    language: Optional[str] = None
    scripts: list[str] = field(default_factory=list)
    number_style: Optional[str] = None      # 'dot_decimal' | 'comma_decimal' | None
    date_order: Optional[str] = None        # 'DMY' | 'MDY' | None
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"language": self.language, "scripts": self.scripts, "number_style": self.number_style,
                "date_order": self.date_order, "evidence": self.evidence}


def detect_locale(text: str) -> LocaleInfo:
    t = normalize_text(text)
    info = LocaleInfo(scripts=detect_scripts(text))
    de, en = len(_DE_WORDS.findall(t)), len(_EN_WORDS.findall(t))
    if de or en:
        info.language = "de" if de > en else "en"
        info.evidence.append(f"language keywords de={de} en={en}")
    c, d = len(_COMMA_DEC.findall(t)), len(_DOT_DEC.findall(t))
    if c > d:
        info.number_style = "comma_decimal"
    elif d > c:
        info.number_style = "dot_decimal"
    info.evidence.append(f"amount patterns comma-decimal={c} dot-decimal={d}")
    # date order evidence from the document itself: any slash date with an unambiguous component
    dmy = mdy = 0
    for a, b, _ in _SLASH_DATE.findall(t):
        a, b = int(a), int(b)
        if a > 12 >= b:
            dmy += 1
        elif b > 12 >= a:
            mdy += 1
    if _DOT_DATE.search(t) and (info.language == "de" or info.number_style == "comma_decimal"):
        dmy += 1
        info.evidence.append("dot-separated dates in a German/comma-decimal document -> day-first")
    if dmy and not mdy:
        info.date_order = "DMY"
    elif mdy and not dmy:
        info.date_order = "MDY"
    info.evidence.append(f"date-order evidence DMY={dmy} MDY={mdy}")
    return info


# ---------------------------------------------------------------- numbers
@dataclass
class Parsed:
    value: Optional[str]
    ambiguous: bool = False
    note: Optional[str] = None


_CURRENCY_TOKENS = re.compile(r"(INR|EUR|USD|GBP|SGD|Rs\.?|₹|€|\$|£)", re.I)


def parse_amount(raw: Optional[str], number_style: Optional[str] = None) -> Parsed:
    if raw is None:
        return Parsed(None, note="missing")
    s = normalize_text(raw)
    s = _CURRENCY_TOKENS.sub("", s).replace(" ", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if not re.fullmatch(r"\d[\d.,]*", s or ""):
        return Parsed(None, note=f"not a number: {raw!r}")
    dots, commas = s.count("."), s.count(",")
    if dots and commas:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        grp = "," if dec == "." else "."
        intpart, frac = s.rsplit(dec, 1)
        num = intpart.replace(grp, "") + "." + frac
        note = "lakh/crore grouping" if dec == "." and re.search(r"\d,\d{2},\d{3}", intpart) else None
    elif commas:
        parts = s.split(",")
        if commas > 1:
            num = s.replace(",", "")
            note = "comma grouping"
        elif len(parts[1]) == 2:
            num, note = parts[0] + "." + parts[1], "decimal comma"
        elif len(parts[1]) == 3:
            if number_style == "dot_decimal":
                num, note = s.replace(",", ""), "comma grouping (dot-decimal locale)"
            else:
                return Parsed(None, ambiguous=True, note=f"'{raw}' could be thousands or decimal comma")
        else:
            num, note = parts[0] + "." + parts[1], "decimal comma"
    elif dots:
        parts = s.split(".")
        if dots > 1:
            num, note = s.replace(".", ""), "dot grouping"
        elif len(parts[1]) == 3:
            if number_style == "comma_decimal":
                num, note = s.replace(".", ""), "dot grouping (comma-decimal locale)"
            else:
                return Parsed(None, ambiguous=True, note=f"'{raw}' could be thousands or decimal point")
        else:
            num, note = s, None
    else:
        num, note = s, None
    try:
        v = Decimal(num)
    except InvalidOperation:
        return Parsed(None, note=f"not a number: {raw!r}")
    if neg:
        v = -v
    return Parsed(str(v.quantize(Decimal("0.01"))) if v == v.quantize(Decimal("0.01")) else str(v), note=note)


def to_decimal(s: Optional[str]) -> Optional[Decimal]:
    return Decimal(s) if s not in (None, "") else None


# ---------------------------------------------------------------- dates
_MONTHS = {
    "jan": 1, "january": 1, "januar": 1, "feb": 2, "february": 2, "februar": 2, "mar": 3, "march": 3, "märz": 3, "maerz": 3,
    "apr": 4, "april": 4, "may": 5, "mai": 5, "jun": 6, "june": 6, "juni": 6, "jul": 7, "july": 7, "juli": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "okt": 10, "oktober": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12, "dez": 12, "dezember": 12,
}


def _iso(y: int, m: int, d: int) -> Optional[str]:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_date(raw: Optional[str], date_order: Optional[str] = None) -> Parsed:
    if not raw:
        return Parsed(None, note="missing")
    s = normalize_text(raw)
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        v = _iso(int(m[1]), int(m[2]), int(m[3]))
        return Parsed(v, note="ISO 8601") if v else Parsed(None, note="invalid date")
    m = re.search(r"\b(\d{1,2})\.?\s+([A-Za-zä]+)\.?,?\s+(\d{4})\b", s)  # 12 Oct 2026 / 31. Oktober 2026
    if m and m[2].lower() in _MONTHS:
        v = _iso(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
        return Parsed(v, note="month name") if v else Parsed(None, note="invalid date")
    m = re.search(r"\b([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\b", s)  # Oct 12, 2026
    if m and m[1].lower() in _MONTHS:
        v = _iso(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
        return Parsed(v, note="month name") if v else Parsed(None, note="invalid date")
    m = re.search(r"\b(\d{1,2})([./-])(\d{1,2})\2(\d{4})\b", s)
    if m:
        a, b, y = int(m[1]), int(m[3]), int(m[4])
        if a > 12 and b <= 12:
            return Parsed(_iso(y, b, a), note="day-first (day > 12)")
        if b > 12 and a <= 12:
            return Parsed(_iso(y, a, b), note="month-first (day > 12)")
        if a == b:
            return Parsed(_iso(y, a, b), note="day equals month")
        if date_order == "DMY":
            return Parsed(_iso(y, b, a), note="day-first from document locale evidence")
        if date_order == "MDY":
            return Parsed(_iso(y, a, b), note="month-first from document locale evidence")
        return Parsed(None, ambiguous=True, note=f"'{m[0]}' could be {a:02d}-{b:02d} or {b:02d}-{a:02d}; no locale evidence")
    return Parsed(None, note=f"unrecognised date: {raw!r}")


# ---------------------------------------------------------------- currency
def detect_currency(text: str) -> Parsed:
    t = normalize_text(text)
    codes = set(re.findall(r"\b(INR|EUR|USD|GBP|SGD|AUD|CAD)\b", t))
    if len(codes) == 1:
        return Parsed(codes.pop(), note="ISO code printed")
    if len(codes) > 1:
        return Parsed(None, ambiguous=True, note=f"multiple currency codes printed: {sorted(codes)}")
    if "€" in t:
        return Parsed("EUR", note="€ symbol")
    if "₹" in t or re.search(r"\bRs\.?\s*\d", t):
        return Parsed("INR", note="Rs/₹ symbol")
    if "£" in t:
        return Parsed("GBP", note="£ symbol")
    if "$" in t:
        return Parsed(None, ambiguous=True, note="'$' alone is ambiguous (USD/SGD/AUD/CAD)")
    return Parsed(None, note="no currency evidence")


def norm_invoice_number(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return re.sub(r"[^A-Z0-9]", "", normalize_text(s).upper())


def norm_account(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return re.sub(r"[^A-Z0-9]", "", normalize_text(s).upper())


def mask_account(s: Optional[str]) -> Optional[str]:
    n = norm_account(s)
    if not n:
        return None
    return "•" * max(0, len(n) - 4) + n[-4:]
