"""One canonical form for Thai text: training labels, model output, PDF text layers.

A CTC recognizer learns exactly the code-point sequences in its labels, so any
Thai string that has two spellings for one rendering (and Thai has many) splits
the training signal and scores as an error at eval time. Everything that feeds
or leaves the line-OCR model goes through canonical():

  * NFC. Never NFKC: it decomposes sara am (U+0E33) into nikhahit + aa.
  * Marks after a base in one order: vowel (ั ิ ี ึ ื ุ ู ฺ ็), then tone /
    thanthakhat / yamakkan, then nikhahit. NFC alone does not do this, since
    most Thai marks have combining class 0.
  * Sara am composed: nikhahit (+ tone) + aa -> tone + ำ, in either typed order
    ("นํ้า", "น้ํา" -> "น้ำ").
  * Repeated identical marks collapsed; ำำ -> ำ; เเ (two sara e) -> แ.
  * Thai glyph-variant private-use code points (U+F700-F71A, the shifted
    marks legacy fonts emit; 95 % of sampled Royal Gazette text-layer pages
    carry them) mapped back to standard Thai.
  * Zero-width space/joiners, soft hyphen and BOM dropped; NBSP -> space;
    runs of spaces/tabs collapsed; CR/LF unified; line ends trimmed.
Thai digits stay Thai digits: they are what the page shows.
"""
from __future__ import annotations

import re
import unicodedata

# Legacy Thai glyph variants (shifted/lowered marks, tail-less ฐ ญ) -> standard code points.
PUA = {
    0xF700: 0x0E10, 0xF701: 0x0E34, 0xF702: 0x0E35, 0xF703: 0x0E36, 0xF704: 0x0E37,
    0xF705: 0x0E48, 0xF706: 0x0E49, 0xF707: 0x0E4A, 0xF708: 0x0E4B, 0xF709: 0x0E4C,
    0xF70A: 0x0E48, 0xF70B: 0x0E49, 0xF70C: 0x0E4A, 0xF70D: 0x0E4B, 0xF70E: 0x0E4C,
    0xF70F: 0x0E0D, 0xF710: 0x0E31, 0xF711: 0x0E4D, 0xF712: 0x0E47, 0xF713: 0x0E48,
    0xF714: 0x0E49, 0xF715: 0x0E4A, 0xF716: 0x0E4B, 0xF717: 0x0E4C, 0xF718: 0x0E38,
    0xF719: 0x0E39, 0xF71A: 0x0E3A,
}
DROP = dict.fromkeys(map(ord, "​‌‍⁠­﻿"))
TRANSLATE = {**PUA, **DROP, 0x00A0: 0x20}

VOWEL_MARKS = set("ัิีึืฺุู็")
TONE_MARKS = set("่้๊๋์๎")
NIKHAHIT = "ํ"
MARKS = VOWEL_MARKS | TONE_MARKS | {NIKHAHIT}
MARK_RUN = re.compile("[" + "".join(sorted(MARKS)) + "]{2,}")
SARA_AM = re.compile("([่-๋]?)ํ([่-๋]?)า")
SPACES = re.compile(r"[ \t\f\v]+")


def _order(m: re.Match) -> str:
    run = m.group(0)
    out: list[str] = []
    for ch in sorted(run, key=lambda c: 0 if c in VOWEL_MARKS else 1 if c in TONE_MARKS else 2):
        if not out or out[-1] != ch:  # a doubled mark renders as one; keep one
            out.append(ch)
    return "".join(out)


def canonical(text: str) -> str:
    text = unicodedata.normalize("NFC", (text or "").translate(TRANSLATE))
    text = MARK_RUN.sub(_order, text)
    text = SARA_AM.sub(lambda m: m.group(1) + m.group(2) + "ำ", text)
    text = text.replace("\u0E40\u0E40", "\u0E41").replace("\u0E33\u0E33", "\u0E33")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(SPACES.sub(" ", line).strip() for line in text.split("\n")).strip()
