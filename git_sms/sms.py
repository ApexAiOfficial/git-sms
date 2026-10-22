"""SMS-aware text: GSM-7 normalisation, segment counting and pagination.

A message that stays in the GSM-7 alphabet costs one segment per 160 characters
(153 when concatenated); a single curly quote or emoji switches the whole message to
UCS-2 at 70 (67). Normalising typography first keeps replies cheap and readable on
basic phones.
"""

from __future__ import annotations

import math
import re
import unicodedata

GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = set("^{}\\[~]|€")

# Typography that would force UCS-2, mapped to GSM-7 equivalents (code points keep the source ASCII).
_REPLACEMENT_CODES = {
    "'": (0x2018, 0x2019, 0x201A, 0x201B, 0x2032, 0x0060, 0x00B4),
    '"': (0x201C, 0x201D, 0x201E, 0x201F, 0x2033),
    "-": (0x2010, 0x2011, 0x2013, 0x2014, 0x2015, 0x2212),
    "...": (0x2026,),
    "*": (0x2022, 0x00B7),
    "->": (0x2192,),
    "<-": (0x2190,),
    " ": (0x00A0, 0x2002, 0x2003, 0x2009, 0x202F, 0x0009),
}
REPLACEMENTS = {chr(code): text for text, codes in _REPLACEMENT_CODES.items() for code in codes}
ZERO_WIDTH = re.compile("[" + "".join(chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)) + "]")
PAGE_SUFFIX_RESERVE = 14  # room for " [10/10 MORE]"
MAX_PAGES = 10


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = ZERO_WIDTH.sub("", text)
    text = "".join(REPLACEMENTS.get(ch, ch) for ch in text)
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_gsm7(text: str) -> bool:
    return all(ch in GSM7_BASIC or ch in GSM7_EXTENDED for ch in text)


def _cost(text: str, gsm: bool) -> int:
    if gsm:
        return sum(2 if ch in GSM7_EXTENDED else 1 for ch in text)
    return len(text.encode("utf-16-le")) // 2


def segments(text: str) -> int:
    if not text:
        return 1
    gsm = is_gsm7(text)
    cost = _cost(text, gsm)
    single, multi = (160, 153) if gsm else (70, 67)
    return 1 if cost <= single else math.ceil(cost / multi)


def _fit(text: str, budget: int, gsm: bool) -> int:
    """Length of the longest prefix of text whose cost is within budget."""
    used = 0
    for index, ch in enumerate(text):
        used += _cost(ch, gsm)
        if used > budget:
            return index
    return len(text)


def paginate(text: str, max_segments: int) -> list[str]:
    """Split a reply into pages that each fit in max_segments, labelled for MORE."""
    text = normalize(text) or "(empty)"
    gsm = is_gsm7(text)
    single, multi = (160, 153) if gsm else (70, 67)
    capacity = single if max_segments == 1 else multi * max_segments
    if _cost(text, gsm) <= capacity:
        return [text]

    target = capacity - PAGE_SUFFIX_RESERVE
    pages: list[str] = []
    remaining = text
    while remaining and len(pages) < MAX_PAGES:
        if _cost(remaining, gsm) <= target:
            pages.append(remaining)
            remaining = ""
            break
        cut = _fit(remaining, target, gsm)
        window = remaining[:cut]
        boundary = max(window.rfind("\n"), window.rfind(" "))
        if boundary > cut // 2:
            cut = boundary
        pages.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        last = pages[-1]
        pages[-1] = last[: _fit(last, target - 4, gsm)].rstrip() + " ..."

    total = len(pages)
    return [f"{page} [{i}/{total}{' MORE' if i < total else ''}]" for i, page in enumerate(pages, start=1)]
