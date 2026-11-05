"""Decide what kind of thing a photo shows, from features measured in detect.py.

Kinds and where they go:
  page          letters, forms, statements           -> <year>/ (or per sender)
  card          ID, passport, licence, insurance,
                bank, loyalty, business cards         -> Cards/
  receipt       till receipts (kassabonnen)           -> Receipts/<year>/
  confirmation  payment / booking screenshots         -> Confirmations/<year>/
  ticket        boarding passes, tickets, parcel
                labels (QR code or barcode + words)   -> Tickets/<year>/
  note          handwritten notes, whiteboards        -> Notes/<year>/
  label         appliance / product type plates       -> Product labels/
Not saved by default (shown under "Not documents" with the reason):
  envelope, other screenshots, signs/posters/menus, duplicates, plain photos.

Every threshold here is a heuristic checked on synthetic images only; the
review screen is where wrong calls get fixed, and the log records each
correction so the thresholds can be tuned on real archives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from photo2pdf import naming

ID1_TOL = 0.07
RECEIPT_ASPECT = 2.0        # long side / short side of a till receipt outline
SIGN_TEXT_HEIGHT = 0.03     # median word height / image height above which text is "sign-sized"
BLUR_SHARPNESS = 60.0       # Laplacian variance at 1000 px below which a document is "hard to read"
BLUR_CONF = 55.0            # mean OCR confidence below which a document is "hard to read"


@dataclass
class Decision:
    kind: Optional[str]           # see module docstring; None = not a document
    is_document: bool
    reason: Optional[str] = None  # shown under "Not documents"
    fields: Optional[naming.DocFields] = None


def classify(f: dict, text: str, min_words: int) -> Decision:
    words = f.get("words", 0)
    quad = f.get("quad", False)
    aspect = f.get("aspect", 1.0)
    codes = f.get("qr", False) or f.get("barcode", False)

    # 1. Screenshots: only the useful ones become documents.
    if f.get("screenshot"):
        if codes and naming.is_ticket_text(text):
            return Decision("ticket", True, fields=naming.extract_ticket(text))
        conf = naming.extract_confirmation(text)
        if conf is not None and words >= 4:
            return Decision("confirmation", True, fields=conf)
        return Decision(None, False, "Screenshot")

    # 2. Cards by outline.
    if f.get("card_shape") and words >= 2:
        fields = naming.extract_card(text)
        if not fields.doc_type:
            fields = naming.business_card_fields(text) or fields
        return Decision("card", True, fields=fields)

    # 3. Receipts: a long thin slip, or plenty of till vocabulary and prices.
    rscore = naming.receipt_score(text)
    if words >= 4 and ((quad and aspect >= RECEIPT_ASPECT and rscore >= 2) or rscore >= 5):
        return Decision("receipt", True, fields=naming.extract_receipt(text))

    # 4. Tickets and parcel labels on paper: a code plus ticket words.
    if codes and naming.is_ticket_text(text):
        return Decision("ticket", True, fields=naming.extract_ticket(text))

    # 5. Envelopes: postal markings, an address and little else.
    if naming.is_envelope_text(text, words) and f.get("long_lines", 0) < 3:
        return Decision(None, False, "Envelope")

    # 6. Cards without a visible outline: keyword / passport strip and little text.
    if 2 <= words < 40 and naming.looks_like_card_text(text):
        return Decision("card", True, fields=naming.extract_card(text))

    # 7. Appliance type plates.
    if not quad and 3 <= words < 40 and naming.label_score(text) >= 2:
        return Decision("label", True, fields=naming.extract_label(text))

    # 8. Letters and forms, with a guard against signs, posters and menus.
    if words >= min_words:
        if (not quad and f.get("median_h_ratio", 0) > SIGN_TEXT_HEIGHT and words < 60
                and f.get("long_lines", 0) < 4):
            return Decision(None, False, "Sign or poster")
        return Decision("page", True, fields=naming.extract(text))

    # 9. A sheet too blurred to read: keep it as a page so it is seen (flagged "hard to read"),
    #    and so a sharp copy elsewhere can replace it as a duplicate.
    if quad and not f.get("card_shape") and f.get("sharpness", 999) < BLUR_SHARPNESS:
        return Decision("page", True, fields=naming.DocFields())

    # 10. Handwritten notes / whiteboards: a sheet was found, ink on it, but little printed text.
    if quad and not f.get("card_shape") and 0.01 <= f.get("ink", 0) <= 0.2:
        return Decision("note", True, fields=naming.DocFields(kind="note", doc_type="Note"))

    return Decision(None, False, None)


def hard_to_read(f: dict) -> bool:
    return f.get("sharpness", 999) < BLUR_SHARPNESS or (
        f.get("words", 0) >= 4 and f.get("mean_conf", 100) < BLUR_CONF)


FOLDERS = {"card": "Cards", "receipt": "Receipts", "confirmation": "Confirmations",
           "ticket": "Tickets", "note": "Notes", "label": "Product labels"}
YEARLESS = {"card", "label"}
LABELS = {"page": "", "card": "card", "receipt": "receipt", "confirmation": "screenshot",
          "ticket": "ticket", "note": "handwritten note", "label": "product label"}
