# Quotation reader for the PR module — moved unchanged from the stand-alone PR app (quotation_scanner.py).
# Keep it free of server imports so it can be tested on its own (tests/test_pr_scanner.py).
"""Read a quotation file (PDF or image) and pull out the data the PR paper form needs.

Text comes from the PDF's own text layer when it has one; scanned PDFs and photos go
through Tesseract OCR (Thai + English). The parsing below is heuristic, so the form
treats every value it returns as a suggestion the user reviews before saving.
"""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import importlib.util
import io
import os
import re
import shutil
import statistics
import sys
import threading
import unicodedata

MAX_OCR_PAGES = 3
# Items read beyond the form's limit still count toward the totals check.
MAX_ITEMS_READ = 200
OCR_DPI = 300
# A PDF text layer shorter than this is treated as a scan (often just a page footer).
MIN_TEXT_LAYER_CHARS = 40
# A text layer with more stray symbols than this (a font with a broken character map,
# e.g. "Á¨\x85\x9a¸É") is read by OCR instead. Readable quotations stay under 0.05.
MAX_GARBAGE_RATIO = 0.15
NORMAL_SYMBOLS = set("®©°±×÷µ²³")
WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# OpenThai OCR (iApp, Apache-2.0) reads scans first when installed; Tesseract is its backup.
# PR_OCR_PRIMARY=tesseract swaps them. The model folder is OPENTHAI_OCR_DIR, or
# models/OpenThai-OCR-Text in the app.
OPENTHAI_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "models", "OpenThai-OCR-Text")


class ScannerUnavailable(RuntimeError):
    """OCR software is missing, so only PDFs with a text layer can be read."""


class ScanFailed(RuntimeError):
    """The file could not be opened or read."""


# --------------------------------------------------------------------------- text

def find_tesseract(configured=""):
    for candidate in (configured, os.environ.get("TESSERACT_CMD", ""), shutil.which("tesseract") or "",
                      WINDOWS_TESSERACT):
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def find_openthai(configured=""):
    for candidate in (configured, os.environ.get("OPENTHAI_OCR_DIR", ""), OPENTHAI_DEFAULT_DIR):
        if candidate and os.path.isfile(os.path.join(candidate, "runtime", "line_engine.py")):
            return candidate
    return ""


_OPENTHAI = {"tried": False, "engine": None}
_OPENTHAI_LOCK = threading.Lock()


def _openthai_engine():
    """The OpenThai line reader, loaded once; None when it is not installed.

    It is used only with OpenVINO: its other backend, onnxruntime, crashes the whole process
    on Windows with an old Visual C++ runtime, and that would take the server down with it.
    """
    with _OPENTHAI_LOCK:
        if _OPENTHAI["tried"]:
            return _OPENTHAI["engine"]
        _OPENTHAI["tried"] = True
        folder = find_openthai()
        if not folder or importlib.util.find_spec("openvino") is None:
            return None
        try:
            # Loaded under its own name: the model ships a package called just "runtime".
            runtime = os.path.join(folder, "runtime")
            spec = importlib.util.spec_from_file_location(
                "openthai_runtime", os.path.join(runtime, "__init__.py"), submodule_search_locations=[runtime])
            package = importlib.util.module_from_spec(spec)
            sys.modules["openthai_runtime"] = package
            spec.loader.exec_module(package)
            from openthai_runtime.line_engine import LineOCR
            _OPENTHAI["engine"] = LineOCR(folder, threads=min(4, os.cpu_count() or 1))
        except Exception:
            _OPENTHAI["engine"] = None
        return _OPENTHAI["engine"]


def _openthai_text(images):
    """Read the pages with OpenThai. Its rows come back as pieces with boxes; a wide gap
    between pieces on a row is a column break (3 spaces), as in layout_lines()."""
    import numpy as np

    engine = _openthai_engine()
    if engine is None:
        return ""
    rows_out = []
    for image in images:
        result = engine.read(np.ascontiguousarray(np.asarray(image.convert("RGB"))[:, :, ::-1]))
        rows = {}
        for line in result.get("lines", []):
            rows.setdefault(line["row"], []).append(line)
        for key in sorted(rows):
            parts = sorted(rows[key], key=lambda line: min(x for x, _ in line["box"]))
            height = statistics.median(max(y for _, y in line["box"]) - min(y for _, y in line["box"])
                                       for line in parts) or 1
            text, right = "", None
            for line in parts:
                left = min(x for x, _ in line["box"])
                if right is not None:
                    text += "   " if left - right > 1.2 * height else " "
                text += line["text"]
                right = max(x for x, _ in line["box"])
            rows_out.append(text)
    return fix_thai("\n".join(rows_out))


def _reading_quality(text):
    """(items and total found, date found) for choosing between two readings of a page."""
    lines = [line.strip() for line in normalize_text(text).splitlines() if line.strip()]
    items = find_items(lines, MAX_ITEMS_READ)
    subtotal = sum((Decimal(item["amount"]) for item in items), Decimal("0"))
    total = find_grand_total(lines, subtotal, find_vat_mode(lines))
    return bool(items) + bool(total), bool(find_date(lines))


def _read_openthai(images, tesseract_cmd):
    try:
        text = _openthai_text(images)
    except Exception as error:
        raise ScannerUnavailable("OpenThai OCR อ่านไฟล์ไม่ได้") from error
    if not text:
        raise ScannerUnavailable("ยังไม่ได้ติดตั้ง OpenThai OCR")
    return text, []


def _read_scan(images, tesseract_cmd):
    """Read scanned pages with the main OCR; when its reading misses the items, the total or
    the date, the other OCR reads them too and the better reading wins (the main one on a tie).
    A date line only the other found is added, since OpenThai skips hand-filled dotted lines.
    Returns (text, method, warnings)."""
    readers = [("ocr_openthai", _read_openthai), ("ocr", _ocr_with_retry)]
    if os.environ.get("PR_OCR_PRIMARY", "").strip().lower() == "tesseract":
        readers.reverse()
    readings, missing = [], None
    for method, read in readers:
        try:
            text, warnings = read(images, tesseract_cmd)
        except ScannerUnavailable as error:
            missing = missing or error
            continue
        quality = _reading_quality(text)
        readings.append((quality, method, text, warnings))
        if quality == (2, True):
            break
    if not readings:
        raise missing
    best = readings[0]
    if len(readings) == 2 and readings[1][0] > best[0]:
        best = readings[1]
    quality, method, text, warnings = best
    other = next((reading for reading in readings if reading is not best), None)
    if other and other[0][1] and not quality[1]:
        text += "\n" + "\n".join(line for line in other[2].splitlines() if DATE_LABEL.search(line))
    return text, method, warnings


def _ocr_images(images, tesseract_cmd):
    try:
        import pytesseract
    except ImportError as error:
        raise ScannerUnavailable("ยังไม่ได้ติดตั้งแพ็กเกจ pytesseract") from error
    command = find_tesseract(tesseract_cmd)
    if not command:
        raise ScannerUnavailable(
            "ยังไม่ได้ติดตั้งโปรแกรม Tesseract OCR จึงอ่านได้เฉพาะไฟล์ PDF ที่มีข้อความในตัว"
        )
    pytesseract.pytesseract.tesseract_cmd = command
    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception as error:  # pytesseract wraps many process errors
        raise ScannerUnavailable("ไม่สามารถเรียกใช้โปรแกรม Tesseract OCR ได้") from error
    languages = "+".join(code for code in ("tha", "eng") if code in installed) or "eng"
    warnings = [] if "tha" in installed else [
        "Tesseract ยังไม่มีชุดภาษาไทย (tha) จึงอ่านได้เฉพาะภาษาอังกฤษและตัวเลข"
    ]
    pages = []
    for image in images:
        # psm 6 keeps each printed row on one line, which is what the item parser needs.
        pages.append(pytesseract.image_to_string(image, lang=languages, config="--psm 6"))
    return fix_thai(join_thai_letters("\n".join(pages))), warnings


THAI_LETTER_GAP = re.compile(r"(?<=[฀-๿]) (?=[฀-๿])")

# Thai fonts made for Windows/Mac draw shifted tone marks and tail-less letters from the
# Private Use Area (U+F700-U+F71A). Their text layer copies out as these codes, which look
# like garbage and match no word; map each one back to the ordinary Thai character.
THAI_PUA = {
    0xF700: "ฐ", 0xF701: "ิ", 0xF702: "ี", 0xF703: "ึ", 0xF704: "ื",
    0xF705: "่", 0xF706: "้", 0xF707: "๊", 0xF708: "๋", 0xF709: "์",
    0xF70A: "่", 0xF70B: "้", 0xF70C: "๊", 0xF70D: "๋", 0xF70E: "์",
    0xF70F: "ญ", 0xF710: "ั", 0xF711: "ํ", 0xF712: "็",
    0xF713: "่", 0xF714: "้", 0xF715: "๊", 0xF716: "๋", 0xF717: "์",
    0xF718: "ุ", 0xF719: "ู", 0xF71A: "ฺ",
}
THAI_TONES = "่้๊๋"
THAI_UPPER_LOWER = "ัิีึื็ุู"
TONE_BEFORE_VOWEL = re.compile(f"([{THAI_TONES}์])([{THAI_UPPER_LOWER}])")
NIKHAHIT_TONE_AA = re.compile(f"ํ([{THAI_TONES}]?)า")
REPEATED_MARK = re.compile(f"([{THAI_TONES}{THAI_UPPER_LOWER}์ํ])\\1+")


def fix_thai(text):
    """Put Thai text read from a PDF or OCR back into the standard spelling.

    - Private Use Area glyphs become the ordinary characters.
    - Sara am written as nikhahit + sara aa ("จํากัด", "นํ้า") becomes one "ำ" after the tone.
    - Two sara e ("เเ") typed for sara ae become "แ".
    - A tone mark stored before the upper/lower vowel ("ก่ี") moves after it ("กี่").
    - A mark repeated by overlapping text runs is kept once.
    """
    if not text:
        return text or ""
    text = text.translate(THAI_PUA)
    text = NIKHAHIT_TONE_AA.sub(lambda m: m.group(1) + "ำ", text)
    text = text.replace("เเ", "แ")
    text = TONE_BEFORE_VOWEL.sub(r"\2\1", text)
    text = REPEATED_MARK.sub(r"\1", text)
    return unicodedata.normalize("NFC", text)


def join_thai_letters(text):
    """Tesseract's Thai model puts a space after nearly every character ("ม อ เต อ ร ์").

    Thai does not separate words with spaces, so single spaces between Thai characters
    are dropped; wider gaps (column breaks) and spaces next to Latin text or digits stay.
    """
    # It also writes sara am as nikhahit + sara aa ("จํากัด"); use the single character.
    return THAI_LETTER_GAP.sub("", text).replace("ํา", "ำ")


def garbage_ratio(text):
    """Share of visible characters that are control codes or stray Latin-1 symbols."""
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return 0.0
    garbage = sum(
        1 for character in visible
        if (0x80 <= ord(character) < 0x300 and character not in NORMAL_SYMBOLS)
        or unicodedata.category(character) in ("Cc", "Co", "Cn")
        or character == "�"
    )
    return garbage / len(visible)


def layout_lines(textpage):
    """The page's text as printed rows, top to bottom and left to right.

    A PDF's text stream often lists table columns or headers out of reading order, so
    pdfium's own runs are re-sorted by position. Thai vowel and tone marks sit above or
    below the line, so only base characters decide where a run belongs.
    """
    count = textpage.count_chars()
    text = textpage.get_text_range(0, count)
    runs, current, previous = [], [], None
    for index in range(min(count, len(text))):
        character = text[index]
        if character in "\r\n":
            if current:
                runs.append(current)
            current, previous = [], None
            continue
        box = textpage.get_charbox(index)
        if not character.isspace() and unicodedata.category(character) != "Mn":
            # One run can hold several table cells, even out of order ("1,800 1,200" where
            # 1,200 is printed further left); split it wherever the text jumps sideways.
            if previous is not None:
                height = max(box[3] - box[1], previous[3] - previous[1], 1)
                if box[0] > previous[2] + 2 * height or box[2] < previous[0] - height / 2:
                    if current:
                        runs.append(current)
                    current = []
            previous = box
        current.append((character, box))
    if current:
        runs.append(current)
    pieces = []
    for run in runs:
        boxes = [box for character, box in run
                 if not character.isspace() and unicodedata.category(character) != "Mn"]
        if not boxes:
            continue
        pieces.append({
            "text": "".join(character for character, _ in run).strip(),
            "left": min(box[0] for box in boxes),
            "right": max(box[2] for box in boxes),
            "base": statistics.median(box[1] for box in boxes),
            "height": statistics.median(box[3] - box[1] for box in boxes) or 1,
        })
    rows = []
    for piece in sorted(pieces, key=lambda piece: (-piece["base"], piece["left"])):
        for row in rows:
            if abs(row["base"] - piece["base"]) <= 0.4 * min(row["height"], piece["height"]):
                row["pieces"].append(piece)
                break
        else:
            rows.append({"base": piece["base"], "height": piece["height"], "pieces": [piece]})
    lines = []
    for row in sorted(rows, key=lambda row: -row["base"]):
        parts, right = [], None
        for piece in sorted(row["pieces"], key=lambda piece: piece["left"]):
            if right is not None:
                # A wide gap is a column break; keep it visible for the parser.
                parts.append("   " if piece["left"] - right > 1.5 * row["height"] else " ")
            parts.append(piece["text"])
            right = piece["right"]
        lines.append("".join(parts))
    return lines


def _prepare_image(image):
    from PIL import ImageOps

    image = ImageOps.exif_transpose(image).convert("L")
    # Tesseract wants text roughly 30px tall; phone photos and small scans need upscaling.
    if image.width < 2000:
        scale = 2000 / image.width
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    return ImageOps.autocontrast(image)


def _ocr_with_retry(images, tesseract_cmd):
    """OCR the pages; when no item row comes out, try again with table borders removed.

    Removing lines everywhere would also eat bold text on clean scans, so it is only the
    fallback for forms whose boxed rows Tesseract skipped.
    """
    images = [_prepare_image(image) for image in images]
    text, warnings = _ocr_images(images, tesseract_cmd)
    if _has_items(text):
        return text, warnings
    retry, _ = _ocr_images([remove_table_lines(image) for image in images], tesseract_cmd)
    return (retry, warnings) if _has_items(retry) else (text, warnings)


def _has_items(text):
    lines = [line.strip() for line in normalize_text(text).splitlines() if line.strip()]
    return bool(find_items(lines, 1))


def remove_table_lines(image, span=60, dark=110):
    """Whiten the ruling lines of a bordered table; Tesseract skips rows boxed in by them.

    A line is a solid dark run at least `span` pixels long. Averaging the image over
    span-wide (or span-tall) boxes keeps only such runs dark, which marks them without
    needing an image-processing library beyond Pillow. Text strokes are far shorter.
    """
    from PIL import Image, ImageChops, ImageFilter

    width, height = image.size

    def runs(size):
        averaged = image.resize(size, Image.BOX).resize((width, height), Image.NEAREST)
        return averaged.point(lambda value: 255 if value < dark else 0)

    mask = ImageChops.lighter(runs((max(1, width // span), height)),
                              runs((width, max(1, height // span))))
    mask = mask.filter(ImageFilter.MaxFilter(5))  # cover the line's anti-aliased edges
    return Image.composite(Image.new("L", image.size, 255), image, mask)


def extract_text(data, suffix, tesseract_cmd=""):
    """Return (text, method, warnings). method is "text_layer" or "ocr"."""
    suffix = suffix.lower()
    if suffix == ".pdf":
        try:
            import pypdfium2 as pdfium
        except ImportError as error:
            raise ScannerUnavailable("ยังไม่ได้ติดตั้งแพ็กเกจ pypdfium2") from error
        try:
            pdf = pdfium.PdfDocument(data)
        except Exception as error:
            raise ScanFailed("ไม่สามารถเปิดไฟล์ PDF ได้ ไฟล์อาจเสียหายหรือมีรหัสผ่าน") from error
        try:
            pages = [pdf[index] for index in range(min(len(pdf), MAX_OCR_PAGES))]
            text = fix_thai("\n".join("\n".join(layout_lines(page.get_textpage())) for page in pages))
            usable = len(re.sub(r"\s", "", text)) >= MIN_TEXT_LAYER_CHARS
            if usable and garbage_ratio(text) < MAX_GARBAGE_RATIO:
                return text, "text_layer", []
            images = [page.render(scale=OCR_DPI / 72).to_pil() for page in pages]
        finally:
            pdf.close()
        if usable:
            # The text layer is unreadable (broken font encoding); OCR the page instead,
            # but keep the text layer when OCR software is missing.
            try:
                return _read_scan(images, tesseract_cmd)
            except ScannerUnavailable:
                return text, "text_layer", []
        return _read_scan(images, tesseract_cmd)
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as error:
        raise ScannerUnavailable("ยังไม่ได้ติดตั้งแพ็กเกจ Pillow") from error
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ScanFailed("ไม่สามารถเปิดไฟล์รูปภาพได้ ไฟล์อาจเสียหาย") from error
    return _read_scan([image], tesseract_cmd)


# ------------------------------------------------------------------------ parsing

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5, "มิถุนายน": 6,
    "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}
ENGLISH_MONTHS = {
    name: number for number, names in enumerate((
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
    ), start=1) for name in names
}
# A standalone number; parts of codes such as "Mix-04", "M185" or "201x120" are not numbers.
NUMBER = re.compile(
    r"(?<![0-9A-Za-z.,/])(?<![0-9A-Za-z]-)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?![0-9A-Za-z,/]|\.\d|-[0-9A-Za-z])"
)
NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})(?!\d)")
MONTH_NAMES = sorted([*THAI_MONTHS, *ENGLISH_MONTHS], key=len, reverse=True)
WORD_DATE = re.compile(
    r"(\d{1,2})\s*(" + "|".join(re.escape(name) for name in MONTH_NAMES) + r")\.?\s*,?\s*(\d{4})",
    re.IGNORECASE,
)
ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")


def fold(text):
    """Text reduced for keyword matching.

    Many Thai PDFs map glyphs wrongly in their text layer: "ราคา" comes out as "รำคำ",
    vowels and tone marks land on the wrong consonant, and spaces appear mid-word. Dropping
    the floating marks and spaces, and treating ำ as า, makes those variants compare equal.
    """
    text = unicodedata.normalize("NFC", (text or "").lower()).replace("ำ", "า")
    return "".join(
        character for character in text
        if not character.isspace() and character != "�" and unicodedata.category(character) != "Mn"
    )


class Keywords:
    """Thai words matched on folded text, English words matched as whole words."""

    def __init__(self, thai=(), english=""):
        self.thai = tuple(fold(word) for word in thai)
        self.english = re.compile(english, re.IGNORECASE) if english else None

    def search(self, line):
        folded = fold(line)
        return any(word in folded for word in self.thai) or bool(
            self.english and self.english.search(line)
        )


DATE_LABEL = Keywords(["วันที่"], r"\bdate\b")
# Thai month names as they look after fold(), for text layers that garble them.
FOLDED_MONTHS = {fold(name): number for name, number in THAI_MONTHS.items()}
FOLDED_DATE = re.compile(
    r"(\d{1,2})(" + "|".join(re.escape(name) for name in sorted(FOLDED_MONTHS, key=len, reverse=True))
    + r")(\d{4})"
)
# Words in an item table's header row, grouped by column meaning.
TABLE_HEADER_GROUPS = (
    Keywords(["รายการ", "รายละเอียด"], r"\b(?:description|items?|particulars?)\b"),
    Keywords(["จำนวน", "ปริมาณ"], r"\b(?:qty|quantity)\b"),
    Keywords(["ราคา", "หน่วยละ"], r"\b(?:price|amount|unit\s*price)\b"),
)
# The first summary row below the items ends the item table.
TABLE_END = Keywords(
    ["รวมเงิน", "รวมราคา", "รวมทั้งสิ้น", "รวมทั้งหมด", "ราคารวม", "ยอดรวม", "ภาษีมูลค่าเพิ่ม", "ส่วนลดรวม"],
    r"\b(?:sub[\s-]*total|total|totle|vat)\b",
)
# Lines that carry totals or terms, never an item row.
NOT_ITEM = Keywords(
    ["รวม", "ภาษี", "ส่วนลด", "มัดจำ", "ชำระ", "ยืนราคา", "เงื่อนไข", "เลขประจำตัว",
     # Not bare "โทร": it is also inside product words such as "คอนโทรล" (control).
     "โทร.", "โทร:", "โทรศัพท์", "โทรสาร",
     "วันที่", "ใบเสนอราคา", "เลขที่", "ราคาสุทธิ", "ยอดสุทธิ"],
    r"\b(?:vat|discount|sub\s*total|total|tel|fax|tax\s*id|page\s*\d|date|quotation)\b",
)
ITEM_NUMBER_PREFIX = re.compile(r"^\s*(?:\d{1,2}\s*[.)]?\s+|[-•*]\s*)")
VAT_LINE = Keywords(["ภาษีมูลค่าเพิ่ม", "ภาษีขาย"], r"\bvat\b")
VAT_INCLUSIVE = Keywords(
    ["รวมภาษีมูลค่าเพิ่มแล้ว", "รวมภาษีแล้ว", "รวมvatแล้ว", "ราคารวมภาษี", "ราคารวมvat"],
    r"\b(?:incl(?:ude[sd]?|\.)?\s*vat|vat\s*(?:included|inclusive))\b",
)
GRAND_TOTAL = Keywords(
    ["รวมทั้งสิ้น", "ยอดสุทธิ", "ราคาสุทธิ", "รวมสุทธิ", "เงินสุทธิ", "รวมเงินทั้งสิ้น", "ยอดรวมทั้งหมด",
     "รวมทั้งหมด", "จำนวนเงินทั้งสิ้น"],
    r"\b(?:grand\s*total|net\s*total|total\s*amount|net\s*amount)\b",
)
ANY_TOTAL = Keywords(["รวม"], r"\b(?:total|totle)\b")
SUPPLIER_LINE = Keywords(
    ["บริษัท", "บจก", "ห้างหุ้นส่วน", "หจก", "ร้าน"],
    r"\b(?:co\.?,?\s*ltd|company|limited|corporation|part\.?\s*ltd)\b",
)
CUSTOMER_LINE = Keywords(
    ["บลูฟาโล", "ลูกค้า", "เรียน", "ถึง", "ชื่อลูกค้า"],
    r"blue\s*falo|\b(?:customer|attn|to\s*:|bill\s*to|ship\s*to|sold\s*to)",
)
# Legal-form words dropped when comparing company names (applied to fold()ed text).
LEGAL_WORDS = re.compile(
    "|".join(re.escape(fold(word)) for word in (
        "ห้างหุ้นส่วนจำกัด", "ห้างหุ้นส่วน", "บริษัท", "บจก.", "บจก", "จำกัด", "(มหาชน)", "มหาชน",
        "หจก.", "หจก", "ร้าน", "สำนักงานใหญ่",
    )) + r"|co\.?|ltd\.?|limited|company|corporation|corp\.?|inc\.?|public|part\.?|\(.*?\)"
)
CENT = Decimal("0.01")


def to_decimal(token):
    try:
        return Decimal(token.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def normalize_text(text):
    text = fix_thai(text).translate(THAI_DIGITS).replace("\u200b", "").replace("\ufffe", "")
    # OCR often reads the Thai baht sign or a stray "฿" glued to numbers.
    text = text.replace("฿", " ")
    # Some text layers split the satang off a price: "2,000. 00".
    text = re.sub(r"(\d)\. (\d{2})(?![\d.,])", r"\1.\2", text)
    text = re.sub(r"(?<![\d,])(\d{1,3}), (\d{3}\.\d{2})(?!\d)", r"\1,\2", text)  # "1, 880.00"
    # OCR often reads a thousands comma as a dot: "1.680.00".
    text = re.sub(r"(?<![\d.,])(\d{1,3})\.(\d{3}\.\d{2})(?![\d.])", r"\1,\2", text)
    # Fill-in forms write the total on a dotted line, where OCR splits the thousands:
    # ".........1 500......" or ".........1.200......" mean 1,500 and 1,200.
    text = re.sub(r"(\.{2,}\s*)([1-9]\d{0,2})[ .](\d{3})(?=\s*\.{2,})", r"\1\2,\3", text)
    text = "\n".join(_dot_thousands(line) for line in text.split("\n"))
    # Dot leaders glued to a number hide it from NUMBER: ".....1,500....." -> " 1,500 ".
    text = re.sub(r"\.{2,}(?=\d)|(?<=\d)\.{2,}", " ", text)
    # Single spaces stay single; wider gaps (column breaks from layout_lines) become 3 spaces.
    text = re.sub(r"[ \t\xa0]{3,}", "   ", text)
    return re.sub(r"(?<=\S)[ \t\xa0]{2}(?=\S)", " ", text)


THOUSANDS_DOT = re.compile(r"(?<![\d.,])([1-9]\d{0,2})\.(\d{3})(?![\d.,])")


def _dot_thousands(line):
    """OCR reads a thousands comma as a dot: "1.200 1.200" on a price row means 1,200.

    Only when a row holds two or more such figures and no figure with satang, so a
    weight such as "1.500 kg" next to a price "450.00" is left alone.
    """
    if len(THOUSANDS_DOT.findall(line)) < 2 or re.search(r"\d\.\d{2}(?!\d)", line):
        return line
    return THOUSANDS_DOT.sub(r"\1,\2", line)


def _make_date(day, month, year):
    year = int(year)
    if year < 100:
        year += 2500 if year >= 60 else 2000  # two-digit years: 69 → 2569 (BE), 26 → 2026
    if year > 2400:
        year -= 543
    try:
        return date(year, int(month), int(day))
    except ValueError:
        return None


FORM_DATE_WORDS = re.compile(r"(?:เดือน|เคือน|พ\s*\.\s*ศ\s*\.?|ค\s*\.\s*ศ\s*\.?)")


def _undot(line):
    """A fill-in form's date without its labels and dot leaders: "27 พฤศจิกายน 2569"."""
    line = FORM_DATE_WORDS.sub(" ", line)
    return re.sub(r"\s*(?:\.\s*){2,}", " ", line)


def find_date(lines):
    """The quotation date: prefer one on a line labelled วันที่/Date, else the first date."""
    found = []
    for index, line in enumerate(lines):
        labelled = bool(DATE_LABEL.search(line)) or (index and DATE_LABEL.search(lines[index - 1]) and
                                                      not DATE_LABEL.search(line))
        for match in NUMERIC_DATE.finditer(line):
            value = _make_date(*match.groups())
            if value:
                found.append((not labelled, index, value))
        for match in [*WORD_DATE.finditer(line), *WORD_DATE.finditer(_undot(line))]:
            month_name = match.group(2).lower().rstrip(".")
            month = THAI_MONTHS.get(match.group(2)) or THAI_MONTHS.get(month_name + ".") \
                or ENGLISH_MONTHS.get(month_name)
            value = _make_date(match.group(1), month, match.group(3)) if month else None
            if value:
                found.append((not labelled, index, value))
        for match in FOLDED_DATE.finditer(fold(line)):
            value = _make_date(match.group(1), FOLDED_MONTHS[match.group(2)], match.group(3))
            if value:
                found.append((not labelled, index, value))
        for match in ISO_DATE.finditer(line):
            value = _make_date(match.group(3), match.group(2), match.group(1))
            if value:
                found.append((not labelled, index, value))
    plausible = [entry for entry in found if 2000 <= entry[2].year <= date.today().year + 1]
    return min(plausible)[2].isoformat() if plausible else ""


def _close(expected, actual):
    return abs(expected - actual) <= max(Decimal("1"), actual * Decimal("0.01"))


UNIT_PREFIX = re.compile(
    r"^(?:pcs?|ea|each|set|sets|unit|units|lot|job|box|roll|pac|pack|m|kg|ชิ้น|อัน|ตัว|ชุด|เครื่อง|"
    r"กล่อง|ม้วน|เมตร|แผ่น|เส้น|ใบ|คู่|หน่วย|งาน|ล็อต)\b\.?\s*",
    re.IGNORECASE,
)


def _discounted(gross, between, amount):
    """True when a discount column between the price and the amount explains the gap."""
    for value in between:
        if value is None:
            continue
        if value < 100 and _close(gross * (1 - value / 100), amount):  # discount %
            return True
        if value < gross and _close(gross - value, amount):  # discount amount
            return True
    return False


def _item_from_line(line):
    """(description, quantity, unit_price) for a priced row, else None.

    The amount is the last number. The unit price comes before it, possibly with a
    discount column in between, and the quantity somewhere before the price. Layouts
    differ on where the description sits: "desc qty unit price amount" or
    "no qty unit desc price amount".
    """
    matches = list(NUMBER.finditer(line))
    if len(matches) < 2:
        return None
    values = [to_decimal(match.group()) for match in matches]
    amount_i = len(values) - 1
    amount = values[amount_i]
    # The amount column holds money ("4,540.00", "2,560"), not a bare count like "5".
    amount_text = matches[amount_i].group()
    if not amount or amount <= 0 or not ("." in amount_text or "," in amount_text or amount >= 100):
        return None
    for price_i in range(amount_i - 1, max(amount_i - 4, 0) - 1, -1):
        price = values[price_i]
        if not price or price <= 0:
            continue
        between = values[price_i + 1:amount_i]
        # Nearest earlier number first, so item numbers and codes are not taken as quantity;
        # a number followed by a unit ("1 Pcs.") beats one that ends a product name ("mixer 1").
        candidates = range(price_i - 1, -1, -1)
        with_unit = [index for index in candidates
                     if UNIT_PREFIX.match(line[matches[index].end():].lstrip())]
        for qty_i in [*with_unit, *(index for index in candidates if index not in with_unit)]:
            quantity = values[qty_i]
            if not quantity or quantity <= 0 or quantity > 100000:
                continue
            gross = quantity * price
            if not (_close(gross, amount) or _discounted(gross, between, amount)):
                continue
            # Some templates print the price under "Qty" and the quantity under "@".
            if price == price.to_integral() and quantity != quantity.to_integral():
                quantity, price = price, quantity
            return _description(line, matches, qty_i, price_i), quantity, price, amount
        # Lump-sum rows print no quantity: "งานติดตั้ง 1 งาน" is often just "งานติดตั้ง  40,000  40,000".
        if not between and _close(price, amount) and price_i == amount_i - 1:
            return _description(line, matches, None, price_i), Decimal("1"), price, amount
    return None


def _description(line, matches, qty_i, price_i):
    if qty_i is None:
        before, middle = line[:matches[price_i].start()], ""
    else:
        before = line[:matches[qty_i].start()]
        middle = line[matches[qty_i].end():matches[price_i].start()]
    before = _keep_abbreviation_dot(ITEM_NUMBER_PREFIX.sub("", before))
    if NUMBER.fullmatch(before):  # only an item number
        before = ""
    # "no qty unit description price amount": the description follows the quantity.
    middle = UNIT_PREFIX.sub("", middle.strip(" :-|")).strip(" :-|")
    if before and len(re.sub(r"\W", "", before)) >= 3:
        return _clean_description(before)
    return _clean_description(middle or before)


# OCR reads table borders as "|", often with a stray letter beside them ("ไ | ประเก็น").
OCR_BORDER = re.compile(r"(?:^|(?<=\s))[^\sA-Za-z0-9|]{0,2}\s*\|\s*")


def _clean_description(text):
    if "|" not in text:
        return text
    return _keep_abbreviation_dot(re.sub(r" {2,}", " ", OCR_BORDER.sub(" ", text)))


def _keep_abbreviation_dot(text):
    """Trim edge punctuation, but keep the dot of a Thai abbreviation ("30 ซม.", "ม.ค.")."""
    text = text.strip(" :-|").lstrip(". ")
    return re.sub(r"(?<![฀-๿])[ .]+$", "", text).strip(" :-|")


def _money(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _is_item_row(line):
    return not NOT_ITEM.search(line) and _item_from_line(line) is not None


def _table_rows(lines):
    """The rows between an item table's header and its first summary row.

    Restricting items to the table keeps phone numbers, terms and remarks out. When no
    header is recognised the whole text is searched, as before.
    """
    for index, line in enumerate(lines):
        if sum(group.search(line) for group in TABLE_HEADER_GROUPS) < 2 or _is_item_row(line):
            continue
        end = len(lines)
        for row in range(index + 1, len(lines)):
            if TABLE_END.search(lines[row]) and _item_from_line(lines[row]) is None:
                end = row
                break
        rows = lines[index + 1:end]
        if any(_is_item_row(row) for row in rows):
            return rows
    return lines


# A description that starts mid-sentence continues the lines above it.
CONTINUATION = re.compile(r"^[,;)+/&]|^[a-z]")


def _counted_heading(line):
    """"Canon 6030w แก้ไข 1 เครื่อง" -> "Canon 6030w แก้ไข": a row naming the thing and its
    count, with no price, that the priced row below it belongs to. Else ""."""
    match = re.match(r"^(.*?\S)\s+\d{1,4}\s+(\S+)\s*$", line)
    if not match or not UNIT_PREFIX.match(match.group(2)) or len(re.sub(r"\W", "", match.group(1))) < 3:
        return ""
    return _clean_description(match.group(1))


def find_items(lines, limit):
    items = []
    previous = []  # description lines above the current row (it often wraps or sits above)
    for line in _table_rows(lines):
        if NOT_ITEM.search(line):
            previous = []
            continue
        parsed = _item_from_line(line)
        if parsed is None:
            stripped = ITEM_NUMBER_PREFIX.sub("", line).strip(" :-|.")
            if stripped and not NUMBER.fullmatch(stripped):
                previous = [*previous[-2:], stripped]
            else:
                previous = []
            continue
        description, quantity, price, amount = parsed
        if not description:
            description = previous[-1] if previous else ""
        elif previous and CONTINUATION.match(description):
            description = " ".join([*previous, description])
        elif previous and (head := _counted_heading(previous[-1])):
            # "Canon 6030w แก้ไข 1 เครื่อง" with the work and price on the row below.
            description = f"{head} - {description}"
        previous = []
        note = ""
        if not _close(quantity * price, amount):
            # A per-line discount: the form takes the net price so its total matches.
            note = f"ราคาก่อนส่วนลด {price:,.2f}"
            price = amount / quantity
        items.append({
            "description": description[:500],
            "quantity": _money(quantity),
            "unit_price": _money(price),
            "amount": _money(amount),
            "note": note,
        })
        if len(items) >= limit:
            break
    return items


def _amount_on(line):
    numbers = [to_decimal(match.group()) for match in NUMBER.finditer(line)]
    numbers = [value for value in numbers if value is not None and value != 7]
    return numbers[-1] if numbers else None


def _money_values(lines):
    for line in lines:
        for match in NUMBER.finditer(line):
            if "." in match.group() or "," in match.group():
                yield to_decimal(match.group())


MONEY_ONLY = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}")


def find_vat_amount(lines):
    """The VAT figure on the quotation, or None.

    Some layouts print the label and its value on separate rows ("VAT 7 %" then "161.00").
    """
    for index, line in enumerate(lines):
        if not VAT_LINE.search(line) or GRAND_TOTAL.search(line):
            continue
        amount = _amount_on(line)
        if not amount:
            following = lines[index + 1:index + 3]
            amount = next(
                (to_decimal(row) for row in following if MONEY_ONLY.fullmatch(row.strip())), None
            )
        if amount and amount > 0:
            return amount
    return None


def find_vat_mode(lines):
    """"1" when VAT is added on top of the item prices, "0" when prices already include it."""
    for line in lines:
        if VAT_INCLUSIVE.search(line):
            return "0"
    return "1" if find_vat_amount(lines) else ""


def find_grand_total(lines, items_total=None, vat_mode=""):
    """The amount payable.

    Labels vary a lot ("รวมทั้งสิ้น" is the subtotal on some forms, "TOTAL" on others), so
    when the items are known, the figure that equals their sum plus 7% VAT (or the sum
    itself when VAT is not added) is the strongest evidence.
    """
    if items_total:
        summary = [line for line in lines if not _is_item_row(line)]  # an item's own amount is no total
        values = [value for value in _money_values(summary) if value]
        with_vat = items_total * Decimal("1.07")
        targets = [with_vat, items_total]
        if vat_mode == "0":
            targets.reverse()
        for target in targets:
            for value in values:
                if abs(value - target) <= Decimal("0.05"):
                    return _money(value)
            if target is with_vat and vat_mode == "1":
                # OCR can lose the total row while the VAT row survives; add them up.
                vat = find_vat_amount(lines)
                if vat and _close(items_total * Decimal("0.07"), vat):
                    return _money(items_total + vat)
    for keywords in (GRAND_TOTAL, ANY_TOTAL):
        for line in reversed(lines):
            if keywords.search(line):
                amount = _amount_on(line)
                if amount:
                    return _money(amount)
    return ""


def normalize_name(name):
    name = LEGAL_WORDS.sub("", fold(name))
    return re.sub(r"[\W_]+", "", name)


NAME_END = re.compile(
    r"\s{3}|\||\.{3,}|เลขประจำตัว|เลขที่|เลขท|\btax\s*id|\btel\b|โทร(?:\.|ศัพท์|\s*:)|\d{2,}/|\bno\.|\bbranch\b",
    re.IGNORECASE,
)
DOCUMENT_TITLE = Keywords(["ใบเสนอราคา", "หน้า"], r"quot|qutation|\bpage\b")


FORM_COMPANY_LABEL = re.compile(r"^\s*บริษัท\s*/?\s*ห้าง\s*/?\s*ร้าน\s*(?:\.\s*){2,}")


def find_supplier_name(lines):
    top = lines[:15]
    for line in top:
        if SUPPLIER_LINE.search(line) and not CUSTOMER_LINE.search(line):
            # "บริษัทฯ ขอเสนอราคา..." is a sentence about the seller, not its name.
            if re.match(r"\s*(?:ทาง)?บริษัท\s*ฯ|\s*ทางบริษัท", line):
                continue
            # A fill-in form's label "บริษัท ห้าง ร้าน......" is not the name; what is written after it is.
            line = FORM_COMPANY_LABEL.sub("", line)
            # Take the column holding the legal name, skipping logo junk printed beside it.
            segments = [part.strip(" :-/") for part in NAME_END.split(line) if part and part.strip(" :-/")]
            name = next((part for part in segments if SUPPLIER_LINE.search(part)), segments[0])
            return name[:200]
    # Small shops print only a trade name ("DD factory") at the top of the page.
    for line in top[:4]:
        if DOCUMENT_TITLE.search(line) or CUSTOMER_LINE.search(line):
            continue
        name = NAME_END.split(line)[0].strip(" :-/")
        if not re.match(r"[A-Za-z฀-๿]", name):  # logo junk such as "®\x90n"
            continue
        if DATE_LABEL.search(name) or NOT_ITEM.search(name):
            continue
        if len(re.sub(r"[\W\d_]", "", name)) >= 2:
            return name[:200]
    return ""


def match_supplier(text, suppliers):
    """Pick the saved supplier whose name appears in the text (longest match wins)."""
    haystack = normalize_name(text)
    best = None
    for supplier_id, company_name in suppliers:
        needle = normalize_name(company_name)
        if len(needle) >= 3 and needle in haystack and (best is None or len(needle) > best[1]):
            best = (supplier_id, len(needle), company_name)
    return (best[0], best[2]) if best else (None, "")


def parse_quotation(text, suppliers=(), max_items=10):
    text = normalize_text(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    all_items = find_items(lines, MAX_ITEMS_READ)
    items = all_items[:max_items]
    supplier_id, supplier_name = match_supplier(text, suppliers)
    vat_mode = find_vat_mode(lines)
    subtotal = sum((Decimal(item["amount"]) for item in all_items), Decimal("0"))
    grand_total = find_grand_total(lines, subtotal, vat_mode)
    if not vat_mode and all_items and grand_total and _close(subtotal, Decimal(grand_total)):
        # No VAT row and the total equals the items: a seller that does not charge VAT.
        vat_mode = "0"
    elif vat_mode == "1" and all_items and grand_total and subtotal > 0 \
            and _close(subtotal, Decimal(grand_total)) \
            and not _close(subtotal * Decimal("1.07"), Decimal(grand_total)):
        # The total is the items alone and the "VAT" found is not 7% of them (OCR noise
        # such as "4 vat 4"): no VAT is charged.
        vat = find_vat_amount(lines)
        if not vat or not _close(subtotal * Decimal("0.07"), vat):
            vat_mode = "0"
    result = {
        "request_date": find_date(lines),
        "vat_included": vat_mode,
        "supplier_id": supplier_id,
        "supplier_name": supplier_name or find_supplier_name(lines),
        "supplier_matched": supplier_id is not None,
        "grand_total": grand_total,
        "items": items,
        "warnings": [],
    }
    if not items:
        result["warnings"].append("ไม่พบรายการสินค้าในใบเสนอราคา กรุณากรอกรายการเอง")
    elif len(all_items) > len(items):
        result["warnings"].append(
            f"ใบเสนอราคามี {len(all_items)} รายการ แต่ฟอร์มรับได้ {max_items} รายการ "
            f"กรอกให้เฉพาะ {max_items} รายการแรก"
        )
    elif result["grand_total"]:
        total = Decimal(result["grand_total"])
        expected = {subtotal, subtotal * Decimal("1.07")}
        if not any(_close(value, total) for value in expected):
            result["warnings"].append(
                "ยอดรวมจากรายการที่อ่านได้ไม่ตรงกับยอดรวมทั้งสิ้นในใบเสนอราคา อาจมีบางรายการที่อ่านไม่ครบ"
            )
    return result
