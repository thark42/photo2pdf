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
    kind: Optional[str] = None   # "card" for ID / bank / insurance / loyalty cards; None for letters
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"date": self.date.isoformat() if self.date else None, "sender": self.sender,
                "doc_type": self.doc_type, "year": self.year, "kind": self.kind}

    @classmethod
    def from_json(cls, d: dict) -> "DocFields":
        return cls(date=date.fromisoformat(d["date"]) if d.get("date") else None,
                   sender=d.get("sender"), doc_type=d.get("doc_type"), year=d.get("year"),
                   kind=d.get("kind"))

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
        out.kind = out.kind or f.kind
    return out


def suggest_name(f: DocFields, fallback: str) -> str:
    """File name (with .pdf) from fields, or `fallback` when nothing useful was found."""
    if f.kind == "card":
        return suggest_card_name(f, fallback)
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


# ==========================================================================
# Cards: ID cards, passports, driving licences, insurance, bank and loyalty cards
# ==========================================================================
#
# Name format: "<Card type> - <Issuer> - valid until <YYYY-MM-DD>.pdf", with
# missing parts left out, e.g. "Health insurance card - Zilveren Kruis - valid
# until 2026-12-31.pdf" or "Passport - Netherlands - valid until 2031-05-01.pdf".
#
# Deliberately NOT used in names: the holder's name, document numbers, BSN,
# card numbers. ID documents are sensitive; a file name gets copied, synced
# and shown in places the PDF itself does not.

CARD_TYPES = [
    Rule(r"\bP<[A-Z<]{3}|\bpaspoort\b|\bpassport\b|\breisepass\b", "Passport"),
    Rule(r"identiteitskaart|identity card|\bID[- ]?kaart\b|\bI[<D][A-Z<]{3}\d|carte d.identit|personalausweis",
         "ID card"),
    Rule(r"rijbewijs|driving licen[cs]e|driver.?s licen[cs]e|f[üu]hrerschein|permis de conduire", "Driving licence"),
    Rule(r"verblijfs(?:document|vergunning)|residence (?:permit|document|card)", "Residence permit"),
    Rule(r"european health insurance card|europese ziekteverzekeringskaart|\bEHIC\b|\bGHIC\b",
         "European Health Insurance Card"),
    Rule(r"zorgpas|zorgverzekering|zorgverzekeraar|health insurance|insurance card|verzekerdenpas|"
         r"polisnummer|zilveren kruis|\bmenzis\b|(?-i:\b(?:VGZ|ONVZ|DSW|CZ)\b)", "Health insurance card"),
    Rule(r"ov-?chipkaart", "OV-chipkaart"),
    Rule(r"museumkaart", "Museumkaart"),
    Rule(r"bibliotheek|library card", "Library card"),
    Rule(r"\bdebit\b|maestro|mastercard|\bvisa\b|v pay|bankpas|betaalpas|creditcard|credit card", "Bank card"),
    Rule(r"bonuskaart|bonus ?card|airmiles|air miles|klantenkaart|spaarkaart|loyalty|rewards? card|"
         r"member(?:ship)? card|lidmaatschap|ledenpas|lidkaart|klantenpas", "Membership card"),
]

CARD_ISSUERS = [
    # health insurers
    Rule(r"zilveren kruis", "Zilveren Kruis"), Rule(r"\bmenzis\b", "Menzis"), Rule(r"(?-i:\bVGZ\b)", "VGZ"),
    Rule(r"(?-i:\bONVZ\b)", "ONVZ"), Rule(r"(?-i:\bDSW\b)", "DSW"), Rule(r"(?-i:\bCZ\b)", "CZ"), Rule(r"(?-i:\bFBTO\b)", "FBTO"),
    Rule(r"interpolis", "Interpolis"), Rule(r"univ[ée]", "Univé"), Rule(r"\bzekur\b", "Zekur"),
    Rule(r"\ba\.?s\.?r\.", "a.s.r."), Rule(r"(?-i:\bOHRA\b)", "OHRA"), Rule(r"(?-i:\bIZA\b)", "IZA"),
    # banks
    Rule(r"(?-i:\bING\b)", "ING"), Rule(r"rabobank", "Rabobank"), Rule(r"abn ?amro", "ABN AMRO"), Rule(r"(?-i:\bSNS\b)", "SNS"),
    Rule(r"(?-i:\bASN\b)", "ASN Bank"), Rule(r"triodos", "Triodos"), Rule(r"\bbunq\b", "bunq"), Rule(r"knab", "Knab"),
    # shops and memberships
    Rule(r"albert heijn|(?-i:\bAH\b)", "Albert Heijn"), Rule(r"\bjumbo\b", "Jumbo"), Rule(r"(?-i:\bHEMA\b)", "HEMA"),
    Rule(r"kruidvat", "Kruidvat"), Rule(r"\betos\b", "Etos"), Rule(r"ikea", "IKEA"), Rule(r"(?-i:\bANWB\b)", "ANWB"),
    Rule(r"gall ?& ?gall", "Gall & Gall"), Rule(r"\bpraxis\b", "Praxis"), Rule(r"\bgamma\b", "GAMMA"),
    Rule(r"(?-i:\bNS\b)", "NS"), Rule(r"\blidl\b", "Lidl"), Rule(r"(?-i:\bPLUS\b)", "PLUS"),
]

MRZ_COUNTRIES = {"NLD": "Netherlands", "BEL": "Belgium", "DEU": "Germany", "D<<": "Germany", "GBR": "United Kingdom",
                 "FRA": "France", "USA": "United States", "ESP": "Spain", "ITA": "Italy", "POL": "Poland",
                 "TUR": "Turkey", "MAR": "Morocco", "SUR": "Suriname"}

_EXPIRY_LABEL = re.compile(
    r"\b(geldig\s+tot(?:\s+en\s+met)?|geldig\s+t/?m|vervaldatum|verloopt|einddatum|valid\s+(?:until|thru|through|to)|"
    r"expir(?:y|es|ation)(?:\s+date)?|date\s+of\s+expiry|exp\.?|4b\.?)\s*[:\-]?\s*", re.I)
_MMYY = re.compile(r"\b(0[1-9]|1[0-2])\s*/\s*(\d{2})\b")


def _mrz_check(field: str) -> int:
    weights = (7, 3, 1)
    total = 0
    for i, ch in enumerate(field):
        if ch.isdigit():
            v = int(ch)
        elif ch.isalpha():
            v = ord(ch.upper()) - 55
        else:
            v = 0
        total += v * weights[i % 3]
    return total % 10


def parse_mrz(text: str, today: Optional[date] = None) -> dict:
    """Card type, issuing country and expiry from a machine-readable zone, if one reads cleanly.

    Supports TD3 (passports, 2x44) and TD1 (ID cards, 3x30). Dates are only
    trusted when their ICAO 9303 check digit matches, which rejects most OCR
    misreads. Returns {} when nothing verifiable is found.
    """
    today = today or date.today()
    lines = [re.sub(r"\s+", "", ln).upper().replace("«", "<") for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.count("<") >= 3 and re.fullmatch(r"[A-Z0-9<]{25,46}", ln)]
    out: dict = {}

    def exp_date(yymmdd: str, check: str) -> Optional[date]:
        if not (yymmdd.isdigit() and check.isdigit()) or _mrz_check(yymmdd) != int(check):
            return None
        yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
        try:
            return date(2000 + yy, mm, dd)   # expiry dates are always in this century
        except ValueError:
            return None

    for i, ln in enumerate(lines):
        if ln.startswith("P") and len(ln) >= 40 and i + 1 < len(lines):      # TD3 line 1 / line 2
            out["type"], out["country"] = "Passport", ln[2:5]
            l2 = lines[i + 1]
            if len(l2) >= 28:
                out["expiry"] = exp_date(l2[21:27], l2[27])
            break
        if ln[0] in "IAC" and 28 <= len(ln) <= 32 and i + 1 < len(lines):    # TD1 line 1 / line 2
            out["type"], out["country"] = "ID card", ln[2:5]
            l2 = lines[i + 1]
            if len(l2) >= 15:
                out["expiry"] = exp_date(l2[8:14], l2[14])
            break
    if out.get("country"):
        out["country"] = MRZ_COUNTRIES.get(out["country"], out["country"].replace("<", ""))
    return {k: v for k, v in out.items() if v}


def pick_expiry(text: str, today: Optional[date] = None) -> Optional[date]:
    """A date right after an expiry label, including MM/YY on bank cards (end of that month)."""
    today = today or date.today()
    for lm in _EXPIRY_LABEL.finditer(text):
        tail = text[lm.end(): lm.end() + 40]
        for rx, kind in _DATE_PATTERNS:
            m = rx.match(tail.lstrip()) or rx.search(tail)
            if m:
                dt = _mk_date(kind, m.groups(), date(today.year + 30, 1, 1))  # expiry may be far ahead
                if dt:
                    return dt
        m = _MMYY.search(tail)
        if m:
            import calendar

            y, mo = 2000 + int(m.group(2)), int(m.group(1))
            return date(y, mo, calendar.monthrange(y, mo)[1])
    return None


def extract_card(text: str, today: Optional[date] = None, extra=None) -> DocFields:
    text = re.sub(r"[ \t]+", " ", text or "")
    mrz = parse_mrz(text, today)
    f = DocFields(kind="card")
    f.doc_type = mrz.get("type")
    if not f.doc_type:
        for rule in CARD_TYPES:
            if (v := rule.match(text)):
                f.doc_type = v
                break
    if f.doc_type in ("Passport", "ID card", "Driving licence", "Residence permit"):
        f.sender = mrz.get("country")
        if not f.sender and re.search(r"nederland|netherlands|koninkrijk der", text, re.I):
            f.sender = "Netherlands"
    else:
        ex_s = (extra if extra is not None else load_extra_rules())[0]
        for rule in ex_s + CARD_ISSUERS:
            if (v := rule.match(text)):
                f.sender = v
                break
    f.date = mrz.get("expiry") or pick_expiry(text, today)
    return f


def looks_like_card_text(text: str) -> bool:
    """A card type keyword or a machine-readable zone is present."""
    return bool(parse_mrz(text) or any(r.match(text) for r in CARD_TYPES))


def extract_any(text: str, card_shape: bool = False, today: Optional[date] = None, extra=None) -> DocFields:
    """Card fields when the photo has card proportions or reads like a card, else letter fields."""
    words = len(re.findall(r"\w{3,}", text or ""))
    if card_shape or (words < 40 and looks_like_card_text(text)):
        return extract_card(text, today, extra)
    return extract(text, today, extra)


def suggest_card_name(f: DocFields, fallback: str) -> str:
    if not f.doc_type:
        return fallback
    parts = [f.doc_type]
    if f.sender:
        parts.append(f.sender)
    if f.date:
        parts.append(f"valid until {f.date.isoformat()}")
    return sanitize(" - ".join(parts)) + ".pdf"
