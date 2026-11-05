"""Suggest a file name from a document's OCR text: date, sender, document type.

Rule-based, Dutch + English, aimed at tax and government post. No model;
every rule is a regular expression in this file (or in an optional
%APPDATA%\\Photo2PDF\\naming.json, see load_extra_rules). Suggestions are
only made when at least two of date / sender / type are found; otherwise the
dated fallback name is kept. Anything missing is left out, and the
review screen lets the user edit every name before saving.

Name format:  "<YYYY-MM-DD> <Sender> - <Document type>.pdf"
e.g.          "2025-03-12 Belastingdienst - Voorlopige aanslag inkomstenbelasting 2025.pdf"

Date choice, in order:
  1. a date right after a label: Datum, Dagtekening, Date, Dated, Date of issue
  2. otherwise the first date in the top 40% of the text
Years outside 1990..next year are ignored. Numeric dates are read day-first
(dd-mm-yyyy), which is the Dutch/UK convention.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

MONTHS = {
    # Dutch
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7,
    "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "okt": 10, "nov": 11, "dec": 12,
    # English (only the ones that differ)
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7, "august": 8,
    "october": 10, "mar": 3, "oct": 10,
}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))

_DATE_PATTERNS = [
    # 12 maart 2025 / 12 March 2025 / 12 mrt. 2025
    (re.compile(rf"\b(\d{{1,2}})\s+({_MON})\.?\s+((?:19|20)\d{{2}})\b", re.I), "dmy_word"),
    # March 12, 2025
    (re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}}),?\s+((?:19|20)\d{{2}})\b", re.I), "mdy_word"),
    # 2025-03-12
    (re.compile(r"\b((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    # 12-03-2025 / 12/03/2025 / 12.03.2025
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)\d{2})\b"), "dmy"),
]

_DATE_LABEL = re.compile(
    r"\b(datum|dagtekening|verzenddatum|date(?: of issue)?|dated|issue date)\b\s*[:\-]?\s*", re.I)


@dataclass
class Rule:
    pattern: str
    label: str  # may reference groups, e.g. "Gemeente {1}"

    def __post_init__(self):
        self.rx = re.compile(self.pattern, re.I)

    def match(self, text: str) -> Optional[str]:
        m = self.rx.search(text)
        if not m:
            return None
        groups = [g.strip().title() if g else "" for g in m.groups()]
        try:
            return self.label.format(m.group(0), *groups).strip()
        except (IndexError, KeyError):
            return self.label


# Order matters: first match wins, so specific before general.
SENDERS = [
    Rule(r"belastingdienst\s*/?\s*toeslagen|dienst\s+toeslagen", "Toeslagen"),
    Rule(r"belastingdienst\s*/?\s*douane", "Douane"),
    Rule(r"belastingdienst", "Belastingdienst"),
    # Proper-name parts are matched case-sensitively and never cross a line break.
    Rule(r"\bgemeente[ \t]+((?-i:[A-Z][\w'\-]+)(?:[ \t]+(?-i:[A-Z][\w'\-]+))?)", "Gemeente {1}"),
    Rule(r"\bwaterschap[ \t]+((?-i:[A-Z][\w'\-]+)(?:[ \t]+(?-i:[A-Z][\w'\-]+))?)", "Waterschap {1}"),
    Rule(r"hoogheemraadschap[ \t]+(?:van[ \t]+)?((?-i:[A-Z][\w'\-]+)(?:[ \t]+(?-i:[A-Z][\w'\-]+))?)", "Hoogheemraadschap {1}"),
    Rule(r"\buwv\b", "UWV"),
    Rule(r"\bsvb\b|sociale verzekeringsbank", "SVB"),
    Rule(r"\bduo\b|dienst uitvoering onderwijs", "DUO"),
    Rule(r"\bcjib\b|centraal justitieel incassobureau", "CJIB"),
    Rule(r"\bcak\b", "CAK"),
    Rule(r"\brdw\b", "RDW"),
    Rule(r"\bind\b.{0,40}naturalisatie|immigratie- en naturalisatiedienst", "IND"),
    Rule(r"kamer van koophandel|\bkvk\b", "KVK"),
    Rule(r"pensioenfonds[ \t]+((?-i:[A-Z][\w\-]+))", "Pensioenfonds {1}"),
    Rule(r"\babp\b", "ABP"),
    Rule(r"\bhmrc\b|hm revenue", "HMRC"),
    Rule(r"internal revenue service|\birs\b", "IRS"),
]

DOC_TYPES = [
    Rule(r"voorlopige\s+aanslag\s+inkomstenbelasting", "Voorlopige aanslag inkomstenbelasting"),
    Rule(r"voorlopige\s+aanslag", "Voorlopige aanslag"),
    Rule(r"definitieve\s+aanslag\s+inkomstenbelasting|aanslag\s+inkomstenbelasting", "Aanslag inkomstenbelasting"),
    Rule(r"aanslag\s+gemeentelijke\s+belastingen|aanslagbiljet", "Aanslag gemeentelijke belastingen"),
    Rule(r"woz[-\s]?beschikking|beschikking\s+woz", "WOZ-beschikking"),
    Rule(r"aanslag\s+motorrijtuigenbelasting|motorrijtuigenbelasting", "Motorrijtuigenbelasting"),
    Rule(r"aanslag\s+(?:waterschapsbelasting|zuiveringsheffing)", "Aanslag waterschapsbelasting"),
    Rule(r"definitieve\s+berekening\s+(zorgtoeslag|huurtoeslag|kinderopvangtoeslag|kindgebonden budget)",
         "Definitieve berekening {1}"),
    Rule(r"(zorgtoeslag|huurtoeslag|kinderopvangtoeslag|kindgebonden budget)", "{1}"),
    Rule(r"uitnodiging\s+(?:tot\s+het\s+doen\s+van\s+)?aangifte", "Uitnodiging aangifte"),
    Rule(r"aangifte\s+inkomstenbelasting", "Aangifte inkomstenbelasting"),
    Rule(r"jaaropgaaf|jaaropgave", "Jaaropgaaf"),
    Rule(r"dwangbevel", "Dwangbevel"),
    Rule(r"aanmaning", "Aanmaning"),
    Rule(r"betalingsherinnering", "Betalingsherinnering"),
    Rule(r"kwijtschelding", "Kwijtschelding"),
    Rule(r"uitspraak\s+(?:op\s+)?bezwaar", "Uitspraak op bezwaar"),
    Rule(r"bezwaarschrift", "Bezwaarschrift"),
    Rule(r"beschikking", "Beschikking"),
    Rule(r"betalingsregeling", "Betalingsregeling"),
    Rule(r"\bfactuur\b", "Factuur"),
    Rule(r"self assessment", "Self Assessment"),
    Rule(r"notice of assessment", "Notice of assessment"),
    Rule(r"\bP60\b", "P60"),
    Rule(r"\btax return\b", "Tax return"),
    Rule(r"\binvoice\b", "Invoice"),
]

# Tax/benefit year, appended to the type when found and not already there.
_YEAR = re.compile(
    r"\b(?:inkomstenbelasting|belastingjaar|zorgtoeslag|huurtoeslag|kinderopvangtoeslag|budget|over\s+(?:het\s+)?(?:jaar\s+)?|jaar|tax\s+year|year)\s*"
    r"((?:19|20)\d{2})(?:\s*[-/]\s*(?:19|20)?\d{2})?\b", re.I)

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class DocFields:
    date: Optional[date] = None
    sender: Optional[str] = None
    doc_type: Optional[str] = None
    year: Optional[int] = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"date": self.date.isoformat() if self.date else None, "sender": self.sender,
                "doc_type": self.doc_type, "year": self.year}

    @classmethod
    def from_json(cls, d: dict) -> "DocFields":
        return cls(date=date.fromisoformat(d["date"]) if d.get("date") else None,
                   sender=d.get("sender"), doc_type=d.get("doc_type"), year=d.get("year"))

    def empty(self) -> bool:
        return not (self.date or self.sender or self.doc_type)


def _mk_date(kind: str, g: tuple[str, ...], today: date) -> Optional[date]:
    try:
        if kind == "dmy_word":
            d, m, y = int(g[0]), MONTHS[g[1].lower()], int(g[2])
        elif kind == "mdy_word":
            m, d, y = MONTHS[g[0].lower()], int(g[1]), int(g[2])
        elif kind == "ymd":
            y, m, d = int(g[0]), int(g[1]), int(g[2])
        else:
            d, m, y = int(g[0]), int(g[1]), int(g[2])
        dt = date(y, m, d)
    except (ValueError, KeyError):
        return None
    return dt if 1990 <= dt.year <= today.year + 1 else None


def find_dates(text: str, today: Optional[date] = None) -> list[tuple[int, date]]:
    """(character offset, date) for every plausible date, in text order."""
    today = today or date.today()
    found: dict[int, date] = {}
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            dt = _mk_date(kind, m.groups(), today)
            if dt and not any(abs(m.start() - k) < 4 for k in found):
                found[m.start()] = dt
    return sorted(found.items())


def pick_date(text: str, today: Optional[date] = None) -> Optional[date]:
    dates = find_dates(text, today)
    if not dates:
        return None
    for lm in _DATE_LABEL.finditer(text):
        for pos, dt in dates:
            if 0 <= pos - lm.end() <= 40:
                return dt
    cutoff = max(200, int(len(text) * 0.4))
    top = [dt for pos, dt in dates if pos <= cutoff]
    return top[0] if top else None


def load_extra_rules() -> tuple[list[Rule], list[Rule]]:
    """Optional user rules, tried before the built-in ones:
    {"senders": [["regex", "Label"]], "doc_types": [["regex", "Label"]]}"""
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    f = (Path(base) if base else Path.home() / ".config") / "Photo2PDF" / "naming.json"
    try:
        data = json.loads(f.read_text("utf-8"))
        return ([Rule(p, lbl) for p, lbl in data.get("senders", [])],
                [Rule(p, lbl) for p, lbl in data.get("doc_types", [])])
    except (OSError, ValueError, re.error, TypeError):
        return [], []


def extract(text: str, today: Optional[date] = None, extra=None) -> DocFields:
    text = re.sub(r"[ \t]+", " ", text or "")
    ex_s, ex_t = extra if extra is not None else load_extra_rules()
    f = DocFields(date=pick_date(text, today))
    # Letterhead and title sit at the top; footers repeat generic words
    # ("bezwaar", "beschikking") on almost every letter. Search the top first.
    head = text[: max(400, len(text) // 3)]

    def first(rules):
        for scope in (head, text):
            for rule in rules:
                if (v := rule.match(scope)):
                    return v
        return None

    f.sender = first(ex_s + SENDERS)
    if (t := first(ex_t + DOC_TYPES)):
        f.doc_type = t[:1].upper() + t[1:]
    if (m := _YEAR.search(text)):
        y = int(m.group(1))
        if 1990 <= y <= (today or date.today()).year + 1:
            f.year = y
    return f


def merge(fields: list[DocFields]) -> DocFields:
    """Combine per-page fields for a multi-page document: first page that has each field wins."""
    out = DocFields()
    for f in fields:
        out.date = out.date or f.date
        out.sender = out.sender or f.sender
        out.doc_type = out.doc_type or f.doc_type
        out.year = out.year or f.year
    return out


def suggest_name(f: DocFields, fallback: str) -> str:
    """File name (with .pdf) from fields, or `fallback` when nothing useful was found."""
    # Only name from content when it's clear: at least two of date / sender / type.
    if sum(x is not None for x in (f.date, f.sender, f.doc_type)) < 2:
        return fallback
    doc_type = f.doc_type
    if doc_type and f.year and str(f.year) not in doc_type:
        doc_type = f"{doc_type} {f.year}"
    parts = []
    if f.date:
        parts.append(f.date.isoformat())
    if f.sender:
        parts.append(f.sender)
    head = " ".join(parts)
    name = f"{head} - {doc_type}" if head and doc_type else (head or doc_type)
    return sanitize(name) + ".pdf"


def sanitize(name: str, max_len: int = 120) -> str:
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:max_len].rstrip() or "Document"


def text_from_tsv(tsv: str) -> str:
    """Rebuild reading-order text (one line per Tesseract line) from TSV output."""
    lines: dict[tuple, list[str]] = {}
    order: list[tuple] = []
    for row in tsv.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        key = (c[1], c[2], c[3], c[4])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(c[11].strip())
    return "\n".join(" ".join(lines[k]) for k in order)
