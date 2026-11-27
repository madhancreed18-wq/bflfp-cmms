"""OneOCR-shaped Thai OCR: a DB text-line detector and a CTC line recognizer on ONNX Runtime.

Why this shape: the 0.8B VLM writes a page one token at a time (~1,100 tokens,
each a full read of its weights), which costs ~28 s per page on 8 x86 cores.
A detector runs once per page and a CTC recognizer reads every character of
a line in one forward pass, so cost follows image area, not text length, and
a runaway loop is impossible by construction.

Pipeline (see the plan in NT-ONPREM-OCR-0.8B.md / the line-OCR plan):
  image -> [orientation] -> detector (probability map -> DB boxes, unclipped)
        -> reading order (rows by vertical overlap, left to right)
        -> crops from the full-resolution image -> recognizer (H=48, width
           follows the line, batched by width) -> greedy CTC decode with a
           confidence per line -> thai_norm.canonical -> joined text.

Model directory layout:
  det.onnx, det.yml     detector + its PaddleX inference.yml (thresholds)
  rec.onnx, rec.yml     recognizer + inference.yml (character_dict), or
  rec.onnx, rec_dict.txt  our trained recognizer + its charset, one per line
  config.json           optional overrides, e.g. {"det": {"limit": 1600}}
  rec2.onnx             optional second recognizer (same charset), see "rec2" below
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pyclipper

from .thai_norm import canonical

_DIGITS = set("0123456789\u0e50\u0e51\u0e52\u0e53\u0e54\u0e55\u0e56\u0e57\u0e58\u0e59")
_LATIN = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_DECIMAL_COMMA = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})*),(\d{2})(?![\d.,])")

DEFAULTS: dict[str, dict[str, Any]] = {
    "det": {"limit": 1280, "thresh": 0.3, "box_thresh": 0.6, "unclip": 1.5, "min_size": 3,
            "max_candidates": 3000, "v_pad": 0.0,
            # short_pad: DB's unclip offset is area * unclip / perimeter, so a short box (one word in a table cell) gets
            # much less margin above and below than a long line (about 0.375 against 0.75 of its height at unclip
            # 1.5), and its top or bottom gets cut ("บาท" read as "บาV", public feedback 17 Nov 2026). With short_pad
            # k > 0 every horizontal box is grown, across the line only, to at least k * (1 + unclip) times its core
            # height: the height a long line gets. 0 = off.
            "short_pad": 0.0,
            # rescan_below (2.1.4): a page shrunk to `limit` whose lines are under this many detector pixels tall
            # (a Retina screenshot's 11-14 pt UI text) is detected again at a size that makes them about
            # rescan_target px, up to rescan_max on the long side. 0 = off.
            "rescan_below": 0.0, "rescan_target": 20.0, "rescan_max": 3200,
            # split_gap: a detected line is cut where it has a blank stretch at
            # least this many line heights wide — two columns' lines or two
            # table cells the detector merged across a narrow gutter. Word
            # spacing, even justified Thai, stays well under it. 0 = off.
            "split_gap": 1.2},
    # drop_below / min_chars: lines under this confidence or length are dropped —
    # stamps, seals, signatures and handwritten marks the source text never has.
    # Tune per model on the court dev pages (train/lineocr/sweep_dev.py): for
    # rec-tiny-v0 the optimum was 0.92 / 3, halving whole-document CER.
    # min_chars_digits: a line made only of digits (Arabic or Thai) may be this short instead; None = min_chars.
    # 2.1.x ships min_chars 2, which dropped single-digit table cells (a quantity "2" on a receipt; public feedback,
    # 17 Nov 2026). min_chars_digits_height: the digit exception needs the box to be at least this share of the page's
    # line height; real table digits measure 0.65-1.0 of it, while floating marks and ornaments that old letterpress
    # books read as "9" or "๘" measure 0.20-0.61 (90th percentile), dev pages, 18 Nov 2026. 0 = no height condition.
    "rec": {"height": 48, "max_w": 3200, "batch": 16, "drop_below": 0.0, "min_chars": 1, "min_chars_digits": None,
            "min_chars_digits_height": 0.0,
            # min_chars_latin (2.1.4): the same height-gated exception for one Latin letter (timetable day codes
            # M T W H F S U, a unit "g"). Thai letters are not included: marks and ornaments read as Thai letters.
            "min_chars_latin": False,
            # junk_below / junk_invalid: also drop a line under this confidence
            # whose Thai spelling is broken beyond this share (see invalid_share).
            "junk_below": 0.0, "junk_invalid": 1.0,
            # color: what a crop looks like before recognition. The recognizer
            # was trained on grey lines only (scans and greyscale synthetics), so
            # coloured text (slides, banners, coloured forms) reads as garbage
            # unless the crop is turned grey first. "gray" = luminance;
            # "gray_inv" also flips light-on-dark crops; "stretch" also spreads
            # low-contrast crops over the full range; "bgr" = unchanged.
            # A/B 2026-10-01 vs "bgr": stretch takes SEA-DocBench 0.521 -> 0.506
            # (25 empty pages -> 0), Wikisource 0.218 -> 0.212, scene text
            # 0.685 -> 0.240; court (0.0364) and docbench unchanged.
            # "flatten" subtracts each column's own background (from the
            # crop's top and bottom margins): any polarity, and gradients
            # (one decision per crop flips half a dark-to-light line wrong).
            # A/B on bench/robustness.py, 20 pages per condition: gradient
            # 0.0499 -> 0.0033 (worst page 0.97 -> 0.04), diagram, table and
            # colour better; court and docbench unchanged.
            "color": "flatten"},
    # ori: page orientation from the text itself, no extra model. A page whose
    # lines run vertically is turned 90 degrees, whichever way a few long lines
    # read with more confidence; a page that reads badly (median line
    # confidence under flip_below) is tried upside down on `sample` lines and
    # turned 180 if that reads better by `flip_margin`.
    "ori": {"enabled": True, "sample": 8, "flip_below": 0.8, "flip_margin": 0.15,
            # deskew_above: a page whose lines lean more than this (degrees) is
            # straightened and detected again; cutting each line out at an
            # angle loses thin marks (แต่ -> ต่ at 15-30 degrees).
            "deskew_above": 2.5},
    # marks: floating tone marks and vowels (old letterpress books) that the
    # detector boxes apart from their line are put back into it and the line
    # read again; see LineOCR._attach_marks. Distances are in line heights.
    "marks": {"enabled": True, "max_gap": 0.35, "max_ext": 0.9, "max_height": 0.9, "margin": 0.0,
              "page_share": 0.25, "page_min": 3, "keep_base": True},
    # cols: the list guard in the column logic (_list_pair); 0 = off. list_rep 0.6 (2.1.1): on the column-order dev
    # pages (bench/cols_order_dev.py) every value from 0.4 to 0.7 fixed the name grids (96.0 -> 99.7) and left every
    # prose kind unchanged; 0.6 is nearest the middle of the gap between prose (at most 0.35) and lists (0.81 up).
    "cols": {"list_rep": 0.6, "list_facing": 0.0, "list_max_w": 10.0, "list_enum": 0.0},
    # rec2 (2.1.2): a second recognizer trained with loopless Thai fonts as well (rec-tiny-v9). Loopless faces (Kanit,
    # Prompt, Sukhumvit Set) make the first one read Thai letters as Latin look-alikes (บ/ป -> U, ร -> S, ท -> n)
    # inside Thai words. A line whose reading has Latin letters in a word that also has Thai letters is read again
    # with rec2, and rec2's reading is kept only if it has fewer such letters. Other lines never reach rec2, so
    # court scans keep the first recognizer's reading (rec2 alone loses on them). "" = off.
    "rec2": {"file": "", "min_mixed": 1},
    # judge9: typewriters and old letterpress draw ๙ with a long tail bent over to the right, and rec reads that as ๕.
    # After every other step, this small classifier looks at each character read as ๕ in a kept line (a window of the
    # line around the place it was read from) and turns it into ๙ from the threshold up. It can change nothing else.
    # "" = off.
    "judge9": {"file": "", "threshold": 0.95},
    # pre.dewarp (2.2, optional, off): phone photos are dewarped before detection (dewarp.py: PaddlePaddle UVDoc by
    # default, or the MonkeyOCRv2 preprocessor; ONNX Runtime); flat pages (scans, born-digital) are left alone. Boxes
    # then refer to the dewarped image. dewarp_dir: the folder with dewarp_mask.onnx and inference.onnx (uvdoc; or
    # dewarp_grid.onnx for monkey), relative to the model folder unless absolute. The files are downloaded separately.
    "pre": {"dewarp": False, "dewarp_dir": "dewarp", "dewarp_backend": "uvdoc"},
    # decimal_comma (2.1.4): an amount whose last comma has exactly two digits after it ("88,226,21", "280,00") gets a
    # decimal point. paragraphs (2.1.4, opt-in): a blank line between rows more than paragraph_gap line heights apart,
    # or where reading moves back up the page (the next column).
    "join": {"thai_no_space": False, "decimal_comma": False, "paragraphs": False, "paragraph_gap": 1.0},
    # OpenVINO bf16 uses AMX on Sapphire Rapids+ and falls back to f32 on CPUs
    # without it. Measured on a Xeon 8468 core (16 lines x 48 x 1024):
    # PP-OCRv6 tiny rec 541 ms on ORT fp32 vs 162 ms on OpenVINO bf16.
    # ort_low_memory (2.1.4): ONNX Runtime without its memory arena and memory patterns. Every page size is a new input
    # shape, and the cached patterns grew to about 3 GB on macOS. The output is identical. OpenVINO is unaffected.
    "runtime": {"backend": "openvino", "precision": "bf16", "ort_low_memory": False},
}
DET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
DET_STD = np.array([0.229, 0.224, 0.225], np.float32)


def _yaml_postprocess(path: Path) -> dict:
    """PostProcess block of a PaddleX inference.yml, without a YAML dependency
    in the service image: only flat `key: value` pairs and the dict list."""
    out: dict[str, Any] = {}
    chars: list[str] = []
    in_post = in_dict = False
    for raw in path.read_text(encoding="utf-8").split("\n"):
        if raw.startswith("PostProcess:"):
            in_post = True
            continue
        if in_post and raw and not raw.startswith(" "):
            break
        if not in_post:
            continue
        line = raw.strip()
        if line.startswith("character_dict:"):
            in_dict = True
            continue
        if in_dict and line.startswith("- "):
            item = raw.split("- ", 1)[1]
            if item.startswith(("'", '"')) and item.endswith(item[0]) and len(item) >= 2:
                item = item[1:-1].replace("''", "'")
            chars.append(item)
            continue
        in_dict = False
        if ":" in line:
            k, v = (s.strip() for s in line.split(":", 1))
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    if chars:
        out["character_dict"] = chars
    return out


class _Model:
    """One network on OpenVINO (default) or ONNX Runtime; run(x) -> first output."""

    def __init__(self, path: Path, threads: int, backend: str, precision: str, low_memory: bool = False):
        self.backend = backend
        if backend == "openvino":
            import openvino as ov

            core = ov.Core()
            # No CPU pinning: OpenVINO pins the thread that runs a request, and
            # with one inference thread that is always core 0 — every service
            # worker thread (and every other process on the box) then queues on
            # one core. The OS scheduler spreads them; taskset still works.
            # The runtime cache keeps compiled kernels per input shape, and every page
            # size, crop width and batch size is a new shape: at the default 5,000
            # entries a worker grew from 0.75 to 1.3 GB over 300 images and kept going
            # (the line-ocr sidecar was OOM-killed). 200 entries holds it near 0.7 GB
            # for about 5% more time per image.
            self.compiled = core.compile_model(str(path), "CPU", {
                "INFERENCE_NUM_THREADS": threads, "NUM_STREAMS": 1,
                "PERFORMANCE_HINT": "LATENCY", "INFERENCE_PRECISION_HINT": precision,
                "ENABLE_CPU_PINNING": os.environ.get("OV_CPU_PINNING", "0") == "1",
                "CPU_RUNTIME_CACHE_CAPACITY": int(os.environ.get("OV_RUNTIME_CACHE", "200"))})
            # An infer request serves one call at a time; the compiled model is
            # shared, so each calling thread gets its own request.
            self.local = threading.local()
            self.input = self.compiled.inputs[0].get_any_name()
            return
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.log_severity_level = 3  # paddle2onnx exports trip "unused initializer" warnings by the dozen
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Idle threads must not spin: on a shared box (and for core-seconds per
        # page) busy-waiting shows up as work that was never done.
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        if low_memory:
            so.enable_cpu_mem_arena = False
            so.enable_mem_pattern = False
        self.sess = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        self.input = self.sess.get_inputs()[0].name

    def run(self, x: np.ndarray) -> np.ndarray:
        if self.backend == "openvino":
            req = getattr(self.local, "req", None)
            if req is None:
                req = self.local.req = self.compiled.create_infer_request()
            return req.infer({self.input: x})[0]
        return self.sess.run(None, {self.input: x})[0]


def order_points(pts: np.ndarray) -> np.ndarray:
    """4 points -> top-left, top-right, bottom-right, bottom-left."""
    pts = pts[np.argsort(pts[:, 0])]
    left, right = pts[:2][np.argsort(pts[:2, 1])], pts[2:][np.argsort(pts[2:, 1])]
    return np.array([left[0], right[0], right[1], left[1]], np.float32)


def _mini_box(contour: np.ndarray) -> tuple[np.ndarray, float]:
    rect = cv2.minAreaRect(contour)
    return order_points(cv2.boxPoints(rect)), min(rect[1])


def _box_score(prob: np.ndarray, box: np.ndarray) -> float:
    h, w = prob.shape
    xs, ys = box[:, 0], box[:, 1]
    x0, x1 = int(np.clip(np.floor(xs.min()), 0, w - 1)), int(np.clip(np.ceil(xs.max()), 0, w - 1))
    y0, y1 = int(np.clip(np.floor(ys.min()), 0, h - 1)), int(np.clip(np.ceil(ys.max()), 0, h - 1))
    mask = np.zeros((y1 - y0 + 1, x1 - x0 + 1), np.uint8)
    cv2.fillPoly(mask, [(box - [x0, y0]).astype(np.int32)], 1)
    return cv2.mean(prob[y0:y1 + 1, x0:x1 + 1], mask)[0]


def _unclip(box: np.ndarray, ratio: float) -> np.ndarray | None:
    poly = box.astype(np.float32)
    area, length = cv2.contourArea(poly), cv2.arcLength(poly, True)
    if length == 0:
        return None
    off = pyclipper.PyclipperOffset()
    off.AddPath([tuple(p) for p in poly.tolist()], pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    out = off.Execute(area * ratio / length)
    return np.array(out[0], np.float32) if out else None


def crop_line(img: np.ndarray, box: np.ndarray, turn: int = 1) -> np.ndarray:
    """Perspective-rectify one quad into a line image. A vertical run of text
    is turned by np.rot90(k=turn); turn=0 leaves it as cut."""
    w = int(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3])))
    h = int(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2])))
    w, h = max(w, 1), max(h, 1)
    m = cv2.getPerspectiveTransform(box.astype(np.float32),
                                    np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32))
    out = cv2.warpPerspective(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if turn and h / w >= 1.5:  # a vertical run of text: read it rotated
        out = np.ascontiguousarray(np.rot90(out, turn))
    return out


def line_height(boxes: list[np.ndarray]) -> float:
    """Typical text-line height: the median over line-shaped boxes (at least
    twice as wide as tall). Bold text on a dark slide can shed more fragment
    boxes (detached marks, 8-21 px) than it has lines (80-100 px), and a plain
    median over all boxes then measures the fragments."""
    hs = [float(max(np.linalg.norm(b[0] - b[3]), 1.0)) for b in boxes]
    lines = [h for b, h in zip(boxes, hs) if np.linalg.norm(b[0] - b[1]) >= 2 * h]
    return float(np.median(lines if len(lines) >= 3 else hs)) if hs else 1.0


def _flatten(g: np.ndarray) -> np.ndarray:
    """Dark text on white from any line crop: each column's background is the
    median of the crop's top and bottom margin rows, smoothed along the line,
    and text is whatever differs from it, light or dark."""
    h, w = g.shape
    k = max(2, h // 6)
    bg = np.median(np.concatenate([g[:k], g[-k:]], 0), axis=0).astype(np.uint8)
    win = max(3, min(h | 1, 255))
    bg = cv2.medianBlur(np.ascontiguousarray(np.repeat(bg[None, :], 3, 0)), win)[1].astype(np.float32)
    d = np.abs(g.astype(np.float32) - bg[None, :])
    hist = np.cumsum(np.bincount(d.astype(np.uint8).ravel(), minlength=256))
    hi = max(float(np.searchsorted(hist, 0.98 * hist[-1])), 24.0)
    return (255 - np.clip(d * (255.0 / hi), 0, 255)).astype(np.uint8)


def _fragments(boxes: list[np.ndarray], rec: dict) -> set[int]:
    """Boxes that are pieces of a neighbouring line (a stacked mark or a tail
    detected on its own): at most 3 characters, under 35 % of the page's line
    height, and touching a full-height line above or below. Read alone they
    come out as junk ("9/", "9]")."""
    if len(boxes) < 3:
        return set()
    hs = [float(np.linalg.norm(b[0] - b[3])) for b in boxes]
    hmed = line_height(boxes)
    out = set()
    for i, b in enumerate(boxes):
        if hs[i] >= 0.35 * hmed or len(rec.get(i, ("", 0))[0].replace(" ", "")) > 3:
            continue
        x0, x1, y0, y1 = b[:, 0].min(), b[:, 0].max(), b[:, 1].min(), b[:, 1].max()
        for j, c in enumerate(boxes):
            if j == i or hs[j] < 0.6 * hmed:
                continue
            if c[:, 0].min() <= x1 and x0 <= c[:, 0].max() and \
                    min(abs(y0 - c[:, 1].max()), abs(c[:, 1].min() - y1)) <= 0.5 * hmed:
                out.add(i)
                break
    return out


def _vertical(box: np.ndarray, ratio: float) -> int:
    """+1 for a line at least `ratio` times taller than wide, -1 for one that
    much wider than tall, 0 otherwise (box corners as _mini_box orders them)."""
    w, h = np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[0] - box[3])
    return 1 if h >= ratio * max(w, 1.0) else -1 if w >= ratio * max(h, 1.0) else 0


def split_gaps(img: np.ndarray, boxes: list[np.ndarray], ratio: float) -> list[np.ndarray]:
    """Cut each line box at blank stretches >= ratio x its height, and trim
    such a stretch off either end (see split_one)."""
    return [p for b in boxes for p in split_one(img, b, ratio)]


def split_one(img: np.ndarray, b: np.ndarray, ratio: float) -> list[np.ndarray]:
    """One line box -> its pieces between blank stretches >= ratio x its height,
    with such a stretch trimmed off either end (the detector can run a line's
    box across an empty gutter up to the next column's first letter)."""
    if not ratio:
        return [b]
    c = crop_line(img, b, turn=0)
    h, w = c.shape[:2]
    if w < 4 * h or h < 4:
        return [b]
    g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY) if c.ndim == 3 else c
    ink = (_flatten(g) < 128).any(axis=0)
    segs, start, blank = [], None, 0
    for x, on in enumerate(ink):
        if on:
            if start is None:
                start = x
            elif blank >= ratio * h:
                segs.append((start, x - blank))
                start = x
            blank = 0
        elif start is not None:
            blank += 1
    if start is not None:
        segs.append((start, w - blank))
    if not segs or (len(segs) == 1 and segs[0][0] < ratio * h and w - segs[0][1] < ratio * h):
        return [b]
    top, bot = b[1] - b[0], b[2] - b[3]
    pad = 0.15 * h
    out = []
    for x0, x1 in segs:
        t0, t1 = max(0.0, (x0 - pad) / w), min(1.0, (x1 + pad) / w)
        out.append(np.array([b[0] + t0 * top, b[0] + t1 * top, b[3] + t1 * bot, b[3] + t0 * bot], np.float32))
    return out


def _words(chars: list | None, box: np.ndarray, text: str, conf: float) -> list[dict]:
    """Words of one line with their boxes, cut from the character positions the
    recognizer reported (space-separated tokens; Thai runs without spaces stay
    one token). Without positions, the whole line is one word.

    CTC emits a character somewhere inside its glyph, so a token's frames stop
    short of its ink on both sides. Each edge is pushed out by half a glyph
    advance (the line's median pitch), but kept clear of the middle of the space
    by a fifth of an advance: the boxes leave a visible gap where the space is,
    as OneOCR's do, and a PDF text layer built from them gets its spaces back
    from that gap. The line's first word starts one advance before its first
    frame, the last ends half an advance after its last."""
    whole = [{"text": text, "confidence": round(conf, 4), "box": np.asarray(box, np.float64)}]
    if not chars:
        return whole
    tokens, cur, gaps = [], [], []  # gaps[i]: centre of the space after token i
    for ch in chars:
        if ch[0].isspace() or ch[0] == "":
            if cur:
                tokens.append(cur)
                gaps.append((ch[1] + ch[2]) / 2)
                cur = []
            elif gaps:
                gaps[-1] = (gaps[-1] + (ch[1] + ch[2]) / 2) / 2  # a run of spaces: its middle
        else:
            cur.append(ch)
    if cur:
        tokens.append(cur)
    adv = _advance(tokens)
    b = np.asarray(box, np.float64)
    out = []
    for n, tok in enumerate(tokens):
        t = fix_scripts(canonical("".join(c[0] for c in tok)))
        if not t:
            continue
        first, last = tok[0][1], tok[-1][2]
        if n > 0:
            x0 = min(first, max(gaps[n - 1] + 0.2 * adv, first - 0.5 * adv))
        else:
            x0 = max(0.0, first - adv)
        if n < len(gaps) and n < len(tokens) - 1:
            x1 = max(last, min(gaps[n] - 0.2 * adv, last + 0.5 * adv))
        else:
            x1 = min(1.0, last + 0.5 * adv)
        quad = np.array([b[0] + x0 * (b[1] - b[0]), b[0] + x1 * (b[1] - b[0]),
                         b[3] + x1 * (b[2] - b[3]), b[3] + x0 * (b[2] - b[3])])
        out.append({"text": t, "confidence": round(float(np.mean([c[3] for c in tok])), 4), "box": quad})
    return out or whole


def _advance(tokens: list) -> float:
    """Median distance between consecutive spacing characters inside words, as a
    fraction of the line width: the glyph pitch. Marks above and below sit on
    their base character and are skipped."""
    steps = []
    for tok in tokens:
        xs = [c[1] for c in tok if c[0] and not unicodedata.combining(c[0]) and unicodedata.category(c[0][0]) != "Mn"]
        steps += [b - a for a, b in zip(xs, xs[1:]) if b > a]
    if steps:
        return float(np.median(steps))
    n = sum(len(t) for t in tokens)
    return (tokens[-1][-1][2] - tokens[0][0][1]) / max(n, 1) if tokens else 0.0


def _rot90_matrix(k: int, shape: tuple[int, int]) -> np.ndarray:
    """3x3 map from an image of `shape` to np.rot90(image, k) coordinates."""
    H, W = shape
    if k == 1:   # counter-clockwise
        return np.array([[0, 1, 0], [-1, 0, W - 1], [0, 0, 1]], np.float64)
    if k == -1:  # clockwise
        return np.array([[0, -1, H - 1], [1, 0, 0], [0, 0, 1]], np.float64)
    return np.array([[-1, 0, W - 1], [0, -1, H - 1], [0, 0, 1]], np.float64)  # 180


def _deskew(img: np.ndarray, angle: float) -> tuple[np.ndarray, np.ndarray]:
    """Rotate so lines leaning by `angle` degrees run level; canvas grown to
    fit, new area filled with the page's own border colour."""
    H, W = img.shape[:2]
    m = cv2.getRotationMatrix2D((W / 2, H / 2), angle, 1.0)
    c, s_ = abs(m[0, 0]), abs(m[0, 1])
    nW, nH = int(round(H * s_ + W * c)), int(round(H * c + W * s_))
    m[0, 2] += nW / 2 - W / 2
    m[1, 2] += nH / 2 - H / 2
    edge = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]], 0)
    fill = tuple(int(v) for v in np.median(edge, axis=0)) if img.ndim == 3 else int(np.median(edge))
    out = cv2.warpAffine(img, m, (nW, nH), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=fill)
    return out, np.vstack([m, [0, 0, 1]])


def _unrotate(box: np.ndarray, rotation: int, shape: tuple[int, int]) -> np.ndarray:
    """Box corners on the page turned clockwise by `rotation` -> original image."""
    k = {90: -1, 270: 1, 180: 2}.get(rotation)
    return box if k is None else _apply(np.linalg.inv(_rot90_matrix(k, shape)), box)


def _apply(m: np.ndarray, box: np.ndarray) -> np.ndarray:
    pts = np.hstack([np.asarray(box, np.float64), np.ones((len(box), 1))]) @ m.T
    return pts[:, :2]


def page_skew(boxes: list[np.ndarray]) -> float:
    """Median text-line angle in degrees, from boxes at least 3x wider than tall."""
    angles = []
    for b in boxes:
        dx, dy = b[1][0] - b[0][0], b[1][1] - b[0][1]
        h = np.linalg.norm(b[0] - b[3])
        if np.hypot(dx, dy) >= 3 * max(h, 1.0):
            angles.append(math.degrees(math.atan2(dy, dx)))
    return float(np.median(angles)) if angles else 0.0


def reading_order(boxes: list[np.ndarray], angle: float | None = None, columns: bool = True,
                  texts: list[str] | None = None, rules: dict | None = None) -> list[list[int]]:
    """Rows top to bottom, each left to right, in DESKEWED coordinates, block by
    block: text columns are read one after the other (see _blocks).

    On a page scanned 1 degree off, a table row's cells sit 20 px apart in y
    across an A4 width — enough for plain y-grouping to put a row's numbers on
    the next row. Box corners are rotated by -skew first, then boxes grouped
    into rows by vertical overlap with the row's running centre. `texts` (the
    recognised line texts, by box) lets the column test reject number columns."""
    if not boxes:
        return []
    a = math.radians(-(page_skew(boxes) if angle is None else angle))
    rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]], np.float32)
    pts = [b.astype(np.float32) @ rot.T for b in boxes]
    ext = [(float(p[:, 0].min()), float(p[:, 0].max()), float(p[:, 1].min()), float(p[:, 1].max())) for p in pts]
    hmed = line_height(boxes)
    idx = list(range(len(boxes)))
    blocks = _blocks(idx, ext, hmed, texts, rules=rules) if columns else [idx]
    if columns and a:
        # The skew is the median angle of the line boxes' top edges, which can be
        # 1-2 degrees off on a straight page; rotated by it, a gutter about one em
        # wide is covered by the column's line ends, and the page is read across.
        # The unrotated frame is used for the blocks when it finds more of them
        # (same tests); rows are still grouped in the rotated frame.
        flat = [(float(b[:, 0].min()), float(b[:, 0].max()), float(b[:, 1].min()), float(b[:, 1].max())) for b in boxes]
        alt = _blocks(idx, flat, hmed, texts, rules=rules)
        if len(alt) > len(blocks):
            blocks = alt
    cy = [(e[2] + e[3]) / 2 for e in ext]
    rx = [(e[0] + e[1]) / 2 for e in ext]
    hs = [float(max(np.linalg.norm(b[0] - b[3]), 1.0)) for b in boxes]
    tol = 0.5 * hmed
    rows: list[list[int]] = []
    for blk in blocks:
        # Rows from full-height lines only; small boxes (detached marks, a
        # stray symbol) join the nearest row afterwards without moving it.
        # Otherwise a chain of mark fragments between two lines, each within
        # tolerance of the next, merges the two lines into one row.
        full = [i for i in blk if hs[i] >= 0.6 * hmed] or list(blk)
        small = [i for i in blk if i not in set(full)]
        brows: list[list[int]] = []
        for i in sorted(full, key=lambda k: cy[k]):
            if brows and abs(cy[i] - np.mean([cy[j] for j in brows[-1]])) <= tol:
                brows[-1].append(i)
            else:
                brows.append([i])
        centre = [float(np.mean([cy[j] for j in r])) for r in brows]
        for i in small:
            d = [abs(cy[i] - c) for c in centre]
            if d and min(d) <= tol + 0.5 * hs[i]:
                brows[int(np.argmin(d))].append(i)
            else:
                brows.append([i])
                centre.append(cy[i])
        order = sorted(range(len(brows)), key=lambda r: centre[r])
        rows += [sorted(brows[r], key=lambda k: rx[k]) for r in order]
    return rows


def _gaps(iv: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]:
    """Empty bands between intervals, at least min_gap wide."""
    iv = sorted(iv)
    out, reach = [], iv[0][1]
    for lo, hi in iv[1:]:
        if lo - reach >= min_gap:
            out.append((reach, lo))
        reach = max(reach, hi)
    return out


def _blocks(idx: list[int], ext: list[tuple], hmed: float, texts: list[str] | None = None,
            depth: int = 0, rules: dict | None = None) -> list[list[int]]:
    """Recursive XY-cut into reading-order blocks.

    A vertical cut (a gutter no line crosses) is taken first, but only between
    two columns of PROSE: the column on each side of the gutter needs at least
    12 lines, lines long for their height (median width >= 7 line heights), at
    least half its lines filling the column width, as wrapped text does, and
    (when texts are known) at most 30 % digits. Tables and grids fail: on the
    docbench budget and committee pages that a looser test split, the columns
    had 4-12 lines, ragged item labels (9-21 % full-width lines, against
    54-95 % for prose columns) or digit amounts, so they stay in row order
    with each amount next to its label. A list numbered across the row
    (๑๔๗. left, ๑๔๘. right) is not split either. Otherwise the widest
    horizontal gap of at least 0.8 line heights splits top from bottom (a
    full-width title above the columns)."""
    if len(idx) < 6 or depth > 12:
        return [idx]
    cuts = []
    for g0, g1 in _gaps([(ext[i][0], ext[i][1]) for i in idx], 0.8 * hmed):
        left = [i for i in idx if ext[i][1] <= g0]
        right = [i for i in idx if ext[i][0] >= g1]
        lcol, rcol = _next_to(left, ext, hmed, True), _next_to(right, ext, hmed, False)
        if (_prose(lcol, ext, hmed, texts) and _prose(rcol, ext, hmed, texts)
                and not _numbered_across(lcol, rcol, ext, hmed, texts)
                and not _list_pair(lcol, rcol, ext, hmed, texts, rules)):
            cuts.append((g1 - g0, left, right))
    if cuts:
        _, left, right = max(cuts, key=lambda c: c[0])
        return _blocks(left, ext, hmed, texts, depth + 1, rules) + _blocks(right, ext, hmed, texts, depth + 1, rules)
    hg = _gaps([(ext[i][2], ext[i][3]) for i in idx], 0.8 * hmed)
    if not hg:
        return _banded_columns(idx, ext, hmed, texts, depth, rules) or [idx]
    g0, g1 = max(hg, key=lambda g: g[1] - g[0])
    top = [i for i in idx if ext[i][3] <= g0]
    bottom = [i for i in idx if ext[i][2] >= g1]
    return _blocks(top, ext, hmed, texts, depth + 1, rules) + _blocks(bottom, ext, hmed, texts, depth + 1, rules)


def _banded_columns(idx: list[int], ext: list[tuple], hmed: float, texts: list[str] | None,
                    depth: int, rules: dict | None = None) -> list[list[int]] | None:
    """Prose columns that a few full-width lines cross: a title or a heading
    over both columns, a footer, a caption between two column sections. The
    plain cut finds no gutter (those lines cross it) and often no horizontal
    gap either (a title set close to the text), so the page was read straight
    across both columns. Tried only where no horizontal gap splits the block
    (the horizontal cut comes first, as before). Lines at least 0.55 of the
    block's width, at most 15 % of its lines, are set aside. In the rest a gutter is an x band in the middle
    70 % of the block, at least 0.4 line heights wide, that at most 3 % of the
    lines cross (a narrow CSS column gap, or a line the detector ran across it).
    If it lies between two prose columns (the same test as the plain cut), the
    wide lines and the lines crossing the whole gutter cut the page into bands,
    and each band is read left of the gutter, then right of it, before the
    crossing line under it (a line that only touches the gutter goes to the
    side of its centre).
    Single-column pages (mostly wide lines, no gutter) and tables (no prose
    column) are not touched."""
    x0, x1 = min(ext[i][0] for i in idx), max(ext[i][1] for i in idx)
    wide = [i for i in idx if ext[i][1] - ext[i][0] >= 0.55 * (x1 - x0)]
    if len(wide) > 0.15 * len(idx):
        return None
    rest = [i for i in idx if i not in set(wide)]
    if len(rest) < 24:
        return None
    lo, n = int(x0 + 0.15 * (x1 - x0)), int(0.7 * (x1 - x0)) + 1
    cover = np.zeros(n, np.int32)
    for i in rest:
        a, b = max(0, int(ext[i][0]) - lo), min(n, int(math.ceil(ext[i][1])) - lo)
        if b > a:
            cover[a:b] += 1
    free = cover <= max(1, int(0.03 * len(rest)))
    runs, s = [], None
    for x, f in enumerate(list(free) + [False]):
        if f and s is None:
            s = x
        elif not f and s is not None:
            if x - s >= 0.4 * hmed:
                runs.append((lo + s, lo + x))
            s = None
    best = None
    for k, (g0, g1) in enumerate(runs):
        left = [i for i in rest if ext[i][1] <= g0]
        right = [i for i in rest if ext[i][0] >= g1]
        # the column on each side ends at the neighbouring gutter (three columns: two gutters)
        lcol = [i for i in left if k == 0 or ext[i][0] >= runs[k - 1][1]]
        rcol = [i for i in right if k == len(runs) - 1 or ext[i][1] <= runs[k + 1][0]]
        if (_prose(lcol, ext, hmed, texts) and _prose(rcol, ext, hmed, texts)
                and not _numbered_across(lcol, rcol, ext, hmed, texts) and not _list_pair(lcol, rcol, ext, hmed, texts, rules)
                and (best is None or g1 - g0 > best[0])):
            best = (g1 - g0, g0, g1)
    if best is None:
        return None
    _, g0, g1 = best
    mid = [i for i in rest if ext[i][0] < g0 and ext[i][1] > g1]  # narrower lines that still cross the whole gutter
    seps = sorted(wide + mid, key=lambda i: (ext[i][2] + ext[i][3]) / 2)
    cy = {i: (ext[i][2] + ext[i][3]) / 2 for i in idx}
    cx = {i: (ext[i][0] + ext[i][1]) / 2 for i in idx}  # a line that only touches the gutter goes by its centre
    out: list[list[int]] = []
    for b in range(len(seps) + 1):
        lo = cy[seps[b - 1]] if b else -math.inf
        hi = cy[seps[b]] if b < len(seps) else math.inf
        band = [i for i in rest if i not in set(mid) and lo < cy[i] <= hi]
        for side in ([i for i in band if cx[i] < (g0 + g1) / 2], [i for i in band if cx[i] >= (g0 + g1) / 2]):
            if side:
                out += _blocks(side, ext, hmed, texts, depth + 1, rules)
        if b < len(seps):
            out.append([seps[b]])
    return out


def _next_to(side: list[int], ext: list[tuple], hmed: float, gutter_on_right: bool) -> list[int]:
    """The column of `side` that touches the gutter (a side can hold several)."""
    if not side:
        return side
    g = _gaps([(ext[i][0], ext[i][1]) for i in side], 0.8 * hmed)
    if not g:
        return side
    if gutter_on_right:
        return [i for i in side if ext[i][0] >= g[-1][1]]
    return [i for i in side if ext[i][1] <= g[0][0]]


_DIGITS = set("0123456789๐๑๒๓๔๕๖๗๘๙")
# Script fix-ups for look-alikes a line recognizer cannot tell apart without
# context. Neither pattern occurs in real Thai text: a lone Latin W glued to
# Thai letters (Prompt/Kanit Bold พ is a W with a loop: "W.ศ.", "wฤษภาคม"), and
# a Latin O or an Arabic 0 inside a number written in Thai digits ("๗o.๘๒").
_LONE_W = re.compile(r"(?<![A-Za-z])[Ww](?=\.?[\u0E01-\u0E2E\u0E40-\u0E44])")
_ZERO_IN_THAI = re.compile(r"(?<=[๐-๙])[Oo0]|[Oo0](?=[๐-๙])")


def fix_scripts(text: str) -> str:
    text = _LONE_W.sub("พ", text)
    return _ZERO_IN_THAI.sub("๐", text)
_ENUM = re.compile(r"^\s*\(?([0-9๐-๙]{1,4})\s*[.)]")


def _enum(text: str) -> int | None:
    m = _ENUM.match(text)
    return int(m.group(1).translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))) if m else None


def _numbered_across(lcol: list[int], rcol: list[int], ext: list[tuple], hmed: float,
                     texts: list[str] | None) -> bool:
    """Most facing lines numbered n (left) and n+1 (right): a list that reads across."""
    if texts is None:
        return False
    pairs = 0
    for i in lcol:
        a = _enum(texts[i])
        if a is None:
            continue
        cy = (ext[i][2] + ext[i][3]) / 2
        if any(_enum(texts[j]) == a + 1 and abs((ext[j][2] + ext[j][3]) / 2 - cy) <= 0.5 * hmed for j in rcol):
            pairs += 1
    return pairs >= 0.5 * min(len(lcol), len(rcol))


_DIGIT_RUN = re.compile(r"[0-9\u0E50-\u0E59]+")


def _start_rep(lines: list[str]) -> float:
    """Share of lines whose first 3 characters (a digit run counted as one) are one of the 2 most common starts."""
    st = [_DIGIT_RUN.sub("D", x.strip())[:3] for x in lines if x.strip()]
    if not st:
        return 0.0
    counts: dict[str, int] = {}
    for x in st:
        counts[x] = counts.get(x, 0) + 1
    return sum(sorted(counts.values(), reverse=True)[:2]) / len(st)


def _list_pair(lcol: list[int], rcol: list[int], ext: list[tuple], hmed: float, texts: list[str] | None,
               rules: dict | None) -> bool:
    """Two sides of a gap that are the columns of a list, read row by row: a name and a position, a label and its
    value, two names a row. 2.1 read a Royal Gazette committee list ("๑. นาย… | กรรมการและ…") as two text columns,
    every name before every position. A list pairs its lines one to one across the gap (`list_facing`: the share
    of the shorter side's lines with a line of the other side on the same row) and both sides are narrow
    (median line width under `list_max_w` line heights); prose columns of a two-column page are twice as wide.
    `list_enum`: a left side that much numbered ("๑.", "2.") facing at least 0.6 is a list at any width."""
    if not rules or not (rules.get("list_rep") or rules.get("list_facing") or rules.get("list_enum")) or not lcol or not rcol:
        return False
    if rules.get("list_rep") and texts is not None:
        # The columns of a list start their lines alike ("นาย…/นาง…", "กรรมการ…", ": …", "๑./๒./…"); prose
        # columns start them on any word. Share of a side's lines whose first 3 characters (digits as one class)
        # are one of its 2 most common starts: name grids 0.84-1.0, the 2.1 Gazette list 1.0 / 0.81, a budget
        # list 0.93 / 0.93; prose columns of the dev pages at most 0.35.
        if float(np.mean([_start_rep([texts[i] for i in col]) for col in (lcol, rcol)])) >= rules["list_rep"]:
            return True
    small, other = (lcol, rcol) if len(lcol) <= len(rcol) else (rcol, lcol)
    cy = [(ext[j][2] + ext[j][3]) / 2 for j in other]
    face = float(np.mean([any(abs((ext[i][2] + ext[i][3]) / 2 - c) <= 0.5 * hmed for c in cy) for i in small]))
    wide = max(float(np.median([ext[i][1] - ext[i][0] for i in col])) for col in (lcol, rcol)) / hmed
    if rules.get("list_facing") and face >= rules["list_facing"] and wide < rules.get("list_max_w", 10.0):
        return True
    if rules.get("list_enum") and texts is not None and face >= 0.6:
        return float(np.mean([_enum(texts[i]) is not None for i in lcol])) >= rules["list_enum"]
    return False


def _prose(col: list[int], ext: list[tuple], hmed: float, texts: list[str] | None = None) -> bool:
    if len(col) < 12:
        return False
    widths = [ext[i][1] - ext[i][0] for i in col]
    span = max(ext[i][1] for i in col) - min(ext[i][0] for i in col)
    if float(np.median(widths)) < 7 * hmed or float(np.mean([w >= 0.75 * span for w in widths])) < 0.35:
        return False
    if texts is not None:
        chars = [c for i in col for c in texts[i] if not c.isspace()]
        if chars and sum(c in _DIGITS for c in chars) > 0.3 * len(chars):
            return False
        # prose never repeats a line; a list's value column does ("เป็นตุลาการ…" on every row)
        if len({texts[i].strip() for i in col}) < 0.8 * len(col):
            return False
    return True


_MARK_RE = re.compile(r"[ัิ-ฺ็-๎\s]")


def _nine_window(line: np.ndarray, x0: float, x1: float) -> np.ndarray:
    """48x80 grey window of a line image centred on the character read from x0..x1 (shares of its width)."""
    g = cv2.cvtColor(line, cv2.COLOR_BGR2GRAY) if line.ndim == 3 else line
    h, w = g.shape
    cx, half = (x0 + x1) / 2 * w, 2.0 * h / 2
    a, b = int(round(cx - half)), int(round(cx + half))
    left, right = max(0, -a), max(0, b - w)
    g = g[:, max(0, a):min(w, b)]
    if left or right:
        g = cv2.copyMakeBorder(g, 0, 0, left, right, cv2.BORDER_REPLICATE)
    g = cv2.resize(g, (96, 48), interpolation=cv2.INTER_AREA if h > 48 else cv2.INTER_LINEAR)
    return g[:, 8:88]


def _only_nines(old: str, new: str) -> bool:
    return len(old) == len(new) and all(x == y or (x, y) == ("๕", "๙") for x, y in zip(old, new))


def _mixed(text: str) -> int:
    """Latin letters inside words that also hold Thai letters: "lละพลNSะทU" -> 4. A loopless Thai face read as Latin
    look-alikes; a Thai word next to an English one (separated by a space) does not count."""
    n = 0
    for tok in text.split():
        if any("\u0e01" <= ch <= "\u0e4e" for ch in tok):
            n += sum(1 for ch in tok if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
    return n


def _digits(text: str) -> str:
    """The Arabic and Thai digits of a reading, in order."""
    return "".join(ch for ch in text if ch.isdigit())


def _base(text: str) -> str:
    """A reading without its above/below marks (sara am as its aa) and spaces:
    what a floating-mark re-read must leave unchanged."""
    return _MARK_RE.sub("", text.replace("ำ", "า"))


def _is_thai(ch: str) -> bool:
    return "\u0e00" <= ch <= "\u0e7f"


_MARKS = set("\u0E31\u0E34\u0E35\u0E36\u0E37\u0E38\u0E39\u0E3A\u0E47\u0E48\u0E49\u0E4A\u0E4B\u0E4C\u0E4D\u0E4E")
_LEADING = set("\u0E40\u0E41\u0E42\u0E43\u0E44")


def _cons(ch: str) -> bool:
    return "\u0E01" <= ch <= "\u0E2E"


def invalid_share(text: str) -> float:
    """Share of Thai characters that break Thai spelling: a mark with no consonant
    under it, or a leading vowel (เ แ โ ใ ไ) not followed by a consonant.
    Stamps, seals and signatures read as strings like "ูี้ซ์ ริ ร้ร้ ไ้" score
    high; real text in an unfamiliar font scores near zero."""
    thai = bad = 0
    for i, ch in enumerate(text):
        if not _is_thai(ch):
            continue
        thai += 1
        prev = text[i - 1] if i else " "
        if ch in _MARKS and not (_cons(prev) or (prev in _MARKS and i >= 2 and _cons(text[i - 2]))):
            bad += 1
        elif ch in _LEADING and not (i + 1 < len(text) and _cons(text[i + 1])):
            bad += 1
    return bad / thai if thai else 0.0


class LineOCR:
    def __init__(self, model_dir: str | os.PathLike, threads: int = 1, **overrides: dict):
        d = Path(model_dir)
        self.cfg = json.loads(json.dumps(DEFAULTS))
        det_pp = _yaml_postprocess(d / "det.yml") if (d / "det.yml").exists() else {}
        for src, dst in (("thresh", "thresh"), ("box_thresh", "box_thresh"), ("unclip_ratio", "unclip")):
            if isinstance(det_pp.get(src), float):
                self.cfg["det"][dst] = det_pp[src]
        user = json.loads((d / "config.json").read_text()) if (d / "config.json").exists() else {}
        for section in (user, overrides):
            for k, v in section.items():
                self.cfg.setdefault(k, {}).update(v)
        # CTC classes: 0 = blank, then the dictionary, then the space PaddleOCR
        # appends. Our trained models ship rec_dict.txt; stock ones rec.yml.
        if (d / "rec_dict.txt").exists():
            chars = [c for c in (d / "rec_dict.txt").read_text(encoding="utf-8").split("\n") if c]
        else:
            chars = _yaml_postprocess(d / "rec.yml")["character_dict"]
        self.chars = [""] + chars + [" "]
        # OpenCV's pool defaults to every core: each crop/resize would fan out
        # over the whole box, which oversubscribes it as soon as pages run in
        # parallel. Match the engine's own thread budget.
        cv2.setNumThreads(threads)
        rt = self.cfg["runtime"]
        backend = rt["backend"]
        if backend == "openvino":
            try:
                import openvino  # noqa: F401
            except ImportError:
                backend = "onnxruntime"
        self.backend = backend
        lm = bool(rt.get("ort_low_memory", False))
        self.det = _Model(d / "det.onnx", threads, backend, rt["precision"], low_memory=lm)
        self.rec = _Model(d / "rec.onnx", threads, backend, rt["precision"], low_memory=lm)
        f2 = self.cfg["rec2"]["file"]
        self.rec2 = _Model(d / f2, threads, backend, rt["precision"], low_memory=lm) if f2 and (d / f2).exists() else None
        f9 = self.cfg["judge9"]["file"]
        self.judge9 = _Model(d / f9, threads, backend, rt["precision"], low_memory=lm) if f9 and (d / f9).exists() else None
        self.dewarper = None
        if self.cfg.get("pre", {}).get("dewarp"):
            from .dewarp import Dewarper

            dd = Path(self.cfg["pre"]["dewarp_dir"])
            self.dewarper = Dewarper(dd if dd.is_absolute() else d / dd, backend=self.cfg["pre"]["dewarp_backend"],
                                     threads=max(1, threads))

    # -- detection -----------------------------------------------------------
    def detect(self, img: np.ndarray) -> list[np.ndarray]:
        c = self.cfg["det"]
        boxes = self._detect_at(img, c["limit"])
        if c.get("rescan_below") and c["limit"] and boxes:
            H0, W0 = img.shape[:2]
            pad = max(8, int(0.05 * min(H0, W0)))
            s = min(c["limit"] / max(H0 + 2 * pad, W0 + 2 * pad), 4.0)
            hdet = line_height(boxes) * s  # the page's line height at the detector's working size
            if s < 1.0 and hdet < c["rescan_below"]:
                limit = min(float(c["rescan_max"]), c["limit"] * c["rescan_target"] / max(hdet, 1.0))
                if limit > c["limit"] * 1.15:
                    boxes = self._detect_at(img, limit)
        return boxes

    def _detect_at(self, img: np.ndarray, limit: float) -> list[np.ndarray]:
        c = self.cfg["det"]
        H0, W0 = img.shape[:2]
        # A white border so text touching the edge still gets a closed region,
        # then the long side scaled to the detector's working size in BOTH
        # directions (as PaddleOCR's resize_long does): a 200x50 crop of one
        # word is otherwise too small for the DB head to find anything.
        pad = max(8, int(0.05 * min(H0, W0)))
        img = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        h, w = img.shape[:2]
        s = min(limit / max(h, w), 4.0) if limit else 1.0
        rh, rw = max(32, int(round(h * s / 32)) * 32), max(32, int(round(w * s / 32)) * 32)
        x = cv2.resize(img, (rw, rh)).astype(np.float32) / 255.0
        x = ((x - DET_MEAN) / DET_STD).transpose(2, 0, 1)[None]
        prob = self._run(self.det, x)[0, 0]
        bitmap = (prob > c["thresh"]).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        sx, sy = w / prob.shape[1], h / prob.shape[0]
        boxes = []
        for cnt in contours[: int(c["max_candidates"])]:
            box, side = _mini_box(cnt)
            if side < c["min_size"] or _box_score(prob, box) < c["box_thresh"]:
                continue
            core = side
            big = _unclip(box, c["unclip"])
            if big is None:
                continue
            box, side = _mini_box(big.reshape(-1, 1, 2))
            if side < c["min_size"] + 2:
                continue
            grow = 0.0
            if c["short_pad"] and np.linalg.norm(box[0] - box[1]) >= np.linalg.norm(box[0] - box[3]):
                grow = max(0.0, c["short_pad"] * (1 + c["unclip"]) * core - side) / 2  # per side, detector pixels
            box[:, 0] = np.clip(box[:, 0] * sx - pad, 0, W0 - 1)
            box[:, 1] = np.clip(box[:, 1] * sy - pad, 0, H0 - 1)
            if grow > 0:  # across the line: along the left edge's direction (top-left -> bottom-left)
                v = box[3] - box[0]
                v = v / max(float(np.linalg.norm(v)), 1e-6) * grow * sy
                box[[0, 1]] -= v
                box[[2, 3]] += v
                box[:, 0] = np.clip(box[:, 0], 0, W0 - 1)
                box[:, 1] = np.clip(box[:, 1], 0, H0 - 1)
            if c["v_pad"]:  # Thai marks above/below the core band: grow height only
                hh = max(np.linalg.norm(box[0] - box[3]), 1.0) * c["v_pad"] / 2
                box[[0, 1], 1] = np.clip(box[[0, 1], 1] - hh, 0, H0 - 1)
                box[[2, 3], 1] = np.clip(box[[2, 3], 1] + hh, 0, H0 - 1)
            if box[:, 0].max() - box[:, 0].min() >= 2 and box[:, 1].max() - box[:, 1].min() >= 2:
                boxes.append(box)
        return boxes

    # -- recognition ---------------------------------------------------------
    def prep_crop(self, crop: np.ndarray) -> np.ndarray:
        mode = self.cfg["rec"]["color"]
        if mode == "bgr" or crop.ndim != 3 or not crop.size:
            return crop
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if mode == "gray":
            return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
        if mode == "flatten":
            return cv2.cvtColor(_flatten(g), cv2.COLOR_GRAY2BGR)
        # Levels from a 256-bin histogram: linear time (np.percentile sorts
        # every crop and cost ~8% of page time).
        c = np.cumsum(np.bincount(g.ravel(), minlength=256))
        lo, med, hi = (int(np.searchsorted(c, q * c[-1])) for q in (0.02, 0.5, 0.98))
        lut = np.arange(256, dtype=np.float32)
        if mode == "stretch" and hi - lo >= 16:
            lut = np.clip((lut - lo) * (255.0 / (hi - lo)), 0, 255)
        if lut[med] < 128:  # the background dominates a line crop: dark background = light text
            lut = 255 - lut
        return cv2.cvtColor(cv2.LUT(g, lut.astype(np.uint8)), cv2.COLOR_GRAY2BGR)

    def recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        return [(t, c) for t, c, _ in self.recognize_detail(crops)]

    def recognize_detail(self, crops: list[np.ndarray], model: "_Model | None" = None) -> list[tuple[str, float, list]]:
        """(text, confidence, chars) per crop; chars are (char, x0, x1, prob)
        with x0..x1 the share of the crop's width the character was read from
        (the CTC frames of its run). Word boxes are cut from these."""
        c = self.cfg["rec"]
        if c["color"] == "flatten":  # cheaper at the recognizer's height
            crops = [self.prep_crop(cv2.resize(cr, (max(1, int(math.ceil(int(c["height"]) * cr.shape[1] / max(cr.shape[0], 1)))),
                                                    int(c["height"])))) for cr in crops]
        else:
            crops = [self.prep_crop(cr) for cr in crops]
        H, max_w, bs = int(c["height"]), int(c["max_w"]), int(c["batch"])
        ratios = [cr.shape[1] / max(cr.shape[0], 1) for cr in crops]
        out: list[tuple[str, float, list]] = [("", 0.0, [])] * len(crops)
        order = sorted(range(len(crops)), key=lambda i: ratios[i])
        for b in range(0, len(order), bs):
            idx = order[b:b + bs]
            W = min(max_w, max(16, int(math.ceil(H * max(ratios[i] for i in idx)))))
            batch = np.zeros((len(idx), 3, H, W), np.float32)  # 0 = mid-grey after normalising
            widths = []
            for n, i in enumerate(idx):
                rw = min(W, max(1, int(math.ceil(H * ratios[i]))))
                widths.append(rw)
                im = cv2.resize(crops[i], (rw, H)).astype(np.float32) / 255.0
                batch[n, :, :, :rw] = ((im - 0.5) / 0.5).transpose(2, 0, 1)
            probs = self._run(model or self.rec, batch)
            for n, i in enumerate(idx):
                out[i] = self._ctc_detail(probs[n], W / probs[n].shape[0], widths[n])
        return out

    @staticmethod
    def _run(model: _Model, x: np.ndarray) -> np.ndarray:
        return model.run(x)

    def _ctc_detail(self, p: np.ndarray, stride: float, width: int) -> tuple[str, float, list]:
        ids, conf = p.argmax(1), p.max(1)
        chars, probs, t, T = [], [], 0, len(ids)
        while t < T:
            end = t + 1
            while end < T and ids[end] == ids[t]:
                end += 1
            if ids[t] != 0:
                ch = self.chars[ids[t]] if ids[t] < len(self.chars) else ""
                chars.append((ch, min(1.0, t * stride / width), min(1.0, end * stride / width), float(conf[t])))
                probs.append(float(conf[t]))
            t = end
        return "".join(c[0] for c in chars), (float(np.mean(probs)) if probs else 0.0), chars

    def _ctc(self, p: np.ndarray) -> tuple[str, float]:
        ids, conf = p.argmax(1), p.max(1)
        keep = np.ones_like(ids, bool)
        keep[1:] = ids[1:] != ids[:-1]
        keep &= ids != 0
        chars = [self.chars[i] if i < len(self.chars) else "" for i in ids[keep]]
        return "".join(chars), float(conf[keep].mean()) if keep.any() else 0.0

    # -- page ----------------------------------------------------------------
    def read(self, image: Any) -> dict:
        img = self._load(image)
        dewarp_info = None
        if self.dewarper is not None:
            img, dewarp_info = self.dewarper(img)
        shape = img.shape[:2]
        t0 = time.perf_counter()
        boxes = self.detect(img)
        h, w = shape
        if not boxes and h <= 200 and w >= 1.5 * h:
            # Nothing found in a small, line-shaped image: callers send single
            # line/field crops too — read the whole image as one line.
            boxes = [np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)]
        oc, rotation, skew = self.cfg["ori"], 0, 0.0
        tf = np.eye(3)  # original image -> working image
        if oc["enabled"]:
            k = self._sideways(img, boxes)
            if k:  # np.rot90 k: +1 turns the page counter-clockwise, -1 clockwise
                tf = _rot90_matrix(k, img.shape[:2]) @ tf
                img = np.ascontiguousarray(np.rot90(img, k))
                boxes, rotation = self.detect(img), 90 if k == -1 else 270
            skew = page_skew(boxes)
            if abs(skew) >= oc["deskew_above"]:
                img, m = _deskew(img, skew)
                tf = m @ tf
                boxes = self.detect(img)
        t1 = time.perf_counter()
        page = self._page(img, boxes)
        if oc["enabled"] and not rotation and self._upside_down(img, page["boxes"], page):
            tf = _rot90_matrix(2, img.shape[:2]) @ tf
            img = np.ascontiguousarray(img[::-1, ::-1])
            boxes, rotation = self.detect(img), 180
            page = self._page(img, boxes)
        frag = _fragments(page["boxes"], page["rec"])
        if self.cfg["marks"]["enabled"]:
            frag |= self._attach_marks(img, page, frag)
        boxes = page["boxes"]
        nine = self._nine_pass(img, page, frag) if self.judge9 is not None else {}
        back = np.linalg.inv(tf)
        t2 = time.perf_counter()
        lines, row_texts, row_geom = [], [], []
        # What the filter threw away, and why: a hybrid caller decides from this
        # whether the page is one this model reads badly (handwriting, scene
        # text, stylised display type) rather than one with a stamp or a
        # signature on it. Fragments of marks are not counted.
        dropped = []
        hmed = line_height(boxes) if boxes else 1.0
        for ri, r in enumerate(page["rows"]):
            parts = []
            for i in r:
                text, conf = page["rec"][i]
                text = fix_scripts(canonical(text))
                if self.cfg["join"].get("decimal_comma"):
                    text = _DECIMAL_COMMA.sub(r"\1.\2", text)
                if i in frag:
                    continue
                reason = self._drop_reason(text, conf, float(np.linalg.norm(boxes[i][0] - boxes[i][3])) / hmed)
                box = _apply(back, boxes[i])
                entry = {"text": text, "confidence": round(conf, 4), "row": ri,
                         "box": [[round(float(x), 1), round(float(y), 1)] for x, y in box]}
                if reason:
                    dropped.append({**entry, "reason": reason})
                    continue
                chars = page["chars"].get(i)
                if i in nine:
                    raw, chars9 = nine[i]
                    new = fix_scripts(canonical(raw))
                    if self.cfg["join"].get("decimal_comma"):
                        new = _DECIMAL_COMMA.sub(r"\1.\2", new)
                    if _only_nines(text, new):
                        text = entry["text"] = new
                        chars = chars9
                parts.append(text)
                entry["words"] = [{**w, "box": [[round(float(x), 1), round(float(y), 1)] for x, y in _apply(back, w["box"])]}
                                  for w in _words(chars, boxes[i], text, conf)]
                lines.append(entry)
            if parts:
                row_texts.append(" ".join(parts))
                ys = [float(y) for j in r if j not in frag for y in np.asarray(boxes[j])[:, 1]]
                row_geom.append((min(ys), max(ys)) if ys else None)
        straightened = skew if abs(skew) >= oc["deskew_above"] else 0.0
        return {"text": self._join(row_texts, row_geom, hmed), "lines": lines, "dropped": dropped,
                "text_angle": round(page["angle"] + straightened, 2),
                "rotation": rotation, "dewarp": dewarp_info,
                "timing": {"det_ms": round((t1 - t0) * 1000, 1), "rec_ms": round((t2 - t1) * 1000, 1),
                           "n_lines": len(page["rec"])}}

    def _nine_pass(self, img: np.ndarray, page: dict, frag: set[int]) -> dict[int, tuple[str, list]]:
        """Lines holding a ๕ that the judge takes for ๙: {index: (raw text, chars) with those characters changed}."""
        at, wins, out = [], [], {}
        for i, (t, _) in page["rec"].items():
            ch = page["chars"].get(i)
            if i in frag or "๕" not in t or not ch or "".join(c[0] for c in ch) != t:
                continue
            line = crop_line(img, page["boxes"][i], turn=0)
            if line.shape[0] >= 1.5 * line.shape[1]:  # a vertical run: read turned, its places are not these
                continue
            for k, c in enumerate(ch):
                if c[0] == "๕":
                    at.append((i, k))
                    wins.append(_nine_window(line, c[1], c[2]))
        for b in range(0, len(at), 64):
            x = np.stack(wins[b:b + 64])[:, None].astype(np.float32) / 127.5 - 1.0
            for (i, k), p in zip(at[b:b + 64], np.asarray(self.judge9.run(x)).reshape(-1)):
                if p >= float(self.cfg["judge9"]["threshold"]):
                    ch = list(out[i][1] if i in out else page["chars"][i])
                    ch[k] = ("๙", *ch[k][1:])
                    out[i] = ("".join(c[0] for c in ch), ch)
        return out

    def _page(self, img: np.ndarray, boxes: list[np.ndarray]) -> dict:
        """Recognition, then reading order (the column test looks at the text).

        Each detected line is first cut at wide blank stretches, or trimmed of
        a blank end (split_one). The pieces replace the line only if every
        piece reads well enough to survive the line filter AND together they
        read at least 90 % of the characters the whole line does; otherwise
        the line is read whole. Lines merged across a column gutter split into
        long, confident pieces; a widely spaced header ("ตอนที่ ๕๓   ง") would
        lose its short pieces (๕๓ at 0.79, ง as "oZ"), and on a dark-to-light
        gradient the ink test can take real text for a blank end."""
        groups = [split_one(img, b, self.cfg["det"]["split_gap"]) for b in boxes]
        changed = [len(g) > 1 or g[0] is not b for b, g in zip(boxes, groups)]
        pieces = [p for g in groups for p in g]
        whole = [b for b, ch in zip(boxes, changed) if ch]
        res = self._read_boxes(img, pieces + whole)
        final, out, k, e = [], [], 0, len(pieces)
        for b, g, ch in zip(boxes, groups, changed):
            r = res[k:k + len(g)]
            k += len(g)
            if ch:
                alt = res[e]
                e += 1
                n_pieces = sum(len(x[0].replace(" ", "")) for x in r)
                if (min(x[1] for x in r) < self.cfg["rec"]["drop_below"]
                        or n_pieces < 0.9 * len(alt[0].replace(" ", ""))):
                    final.append(b)
                    out.append(alt)
                    continue
            final += g
            out += r
        angle = page_skew(final)
        rows = reading_order(final, angle, texts=[x[0] for x in out], rules=self.cfg["cols"])
        return {"rows": rows, "angle": angle, "rec": {i: (x[0], x[1]) for i, x in enumerate(out)},
                "chars": {i: x[2] for i, x in enumerate(out)}, "boxes": final}

    def _drop_reason(self, text: str, conf: float, hratio: float | None = None) -> str | None:
        """Why the line filter drops a (canonical, script-fixed) reading, or None. hratio: the box's height over the
        page's line height, for the digit exception (min_chars_digits_height)."""
        rc = self.cfg["rec"]
        if not text:
            return "empty"
        if conf < rc["drop_below"]:
            return "low_confidence"
        bare = text.replace(" ", "")
        short = rc["min_chars"]
        cls = _DIGITS | (_LATIN if rc.get("min_chars_latin") else set())
        if rc.get("min_chars_digits") is not None and bare and all(ch in cls for ch in bare) and (
                rc.get("min_chars_digits_height", 0.0) <= 0 or (hratio is not None and hratio >= rc["min_chars_digits_height"])):
            short = rc["min_chars_digits"]  # a lone quantity in a table cell ("2") is not junk
        if len(bare) < short:
            return "too_short"
        if conf < rc["junk_below"] and invalid_share(text) > rc["junk_invalid"]:
            return "invalid_thai"
        return None

    def _attach_marks(self, img: np.ndarray, page: dict, frag: set[int]) -> set[int]:
        """Floating Thai marks (old letterpress books, some display fonts): tone
        marks and upper vowels sit so far above the consonants that the detector
        boxes the line without them and finds the marks as boxes of their own,
        which read as junk and are dropped (page 324 of a 1930s book: 128 of 159
        tone marks lost; black-and-white conversion cannot bring them back).

        Only boxes the line filter would drop anyway are candidates, so a box
        that contributes text is never absorbed and a page without them is read
        exactly as before. A candidate within `max_gap` line heights above (or
        below) a kept line it overlaps along the line, no taller than
        `max_height` of it, is absorbed into the nearest such line (a tie goes
        to the line below: upper marks outnumber lower ones). That line's box
        grows by the marks' reach plus `margin` and the line is read again; the
        new reading replaces the old one if it still passes the line filter.
        When at least `page_share` of a page's lines (and `page_min`) had marks
        attached, the typeface floats its marks, so every other line grows by
        the median reach too (as far as the room to its neighbour allows): a
        line whose marks were not detected at all loses them otherwise.
        A re-read is kept only if it changes no digit and, unless the page
        floats its marks, no letter either: only marks (keep_base). Returns the
        absorbed box indices (skipped like fragments) of lines read again."""
        mc = self.cfg["marks"]
        boxes, rec = page["boxes"], page["rec"]
        n = len(boxes)
        hmed = line_height(boxes) if boxes else 1.0
        junk = [i in frag or self._drop_reason(fix_scripts(canonical(rec[i][0])), rec[i][1],
                                               float(np.linalg.norm(boxes[i][0] - boxes[i][3])) / hmed) is not None
                for i in range(n)]
        if not any(junk) or all(junk):
            return set()
        hmed = line_height([boxes[i] for i in range(n) if not junk[i]])
        frame = {}  # kept full-height line -> (origin, along, across, width, height)
        for a in range(n):
            if junk[a]:
                continue
            b = boxes[a].astype(np.float64)
            wa, ha = float(np.linalg.norm(b[1] - b[0])), float(np.linalg.norm(b[3] - b[0]))
            if wa >= 1 and ha >= 0.5 * hmed:
                frame[a] = (b[0], (b[1] - b[0]) / wa, (b[3] - b[0]) / ha, wa, ha)
        if not frame:
            return set()

        def proj(a: int, j: int) -> tuple[float, float, float, float]:
            o, u, v, _, _ = frame[a]
            p = boxes[j].astype(np.float64) - o
            s, t = p @ u, p @ v
            return float(s.min()), float(s.max()), float(t.min()), float(t.max())

        reach = {a: [0.0, 0.0] for a in frame}  # [above, below], in pixels
        owner = {}  # mark box -> the line it belongs to
        for j in range(n):
            if not junk[j]:
                continue
            best = None
            for a, (_, _, _, wa, ha) in frame.items():
                s0, s1, t0, t1 = proj(a, j)
                if t1 - t0 > mc["max_height"] * ha or min(s1, wa) - max(s0, 0.0) < 0.5 * (s1 - s0):
                    continue
                if t1 <= 0.5 * ha:
                    gap, side, ext = max(0.0, -t1), 0, -t0
                elif t0 >= 0.5 * ha:
                    gap, side, ext = max(0.0, t0 - ha), 1, t1 - ha
                else:
                    continue  # across the line's middle: part of the line, not a mark
                if gap > mc["max_gap"] * ha or ext > mc["max_ext"] * ha:
                    continue
                if best is None or (gap, side) < best[0]:
                    best = ((gap, side), a, side, ext)
            if best:
                _, a, side, ext = best
                owner[j] = a
                reach[a][side] = max(reach[a][side], ext)
        if not owner:
            return set()
        floating = False  # the page's typeface floats its marks
        for side in (0, 1):
            lifted = [a for a in frame if reach[a][side] > 0]
            if len(lifted) < max(mc["page_min"], mc["page_share"] * len(frame)):
                continue
            floating = True
            typ = float(np.median([reach[a][side] / frame[a][4] for a in lifted]))
            for a in frame:
                if reach[a][side] > 0:
                    continue
                wa, ha = frame[a][3], frame[a][4]
                room = typ * ha
                for b in frame:  # stop short of the neighbouring line's core
                    if b == a:
                        continue
                    s0, s1, t0, t1 = proj(a, b)
                    if min(s1, wa) - max(s0, 0.0) <= 0:
                        continue
                    if side == 0 and t1 <= 0.5 * ha:
                        room = min(room, max(0.0, -t1 * 0.9))
                    elif side == 1 and t0 >= 0.5 * ha:
                        room = min(room, max(0.0, (t0 - ha) * 0.9))
                reach[a][side] = room
        grown, new_boxes = [], []
        H, W = img.shape[:2]
        for a, (_, _, v, _, ha) in frame.items():
            up, down = reach[a]
            if up <= 0 and down <= 0:
                continue
            b = boxes[a].astype(np.float64).copy()
            if up > 0:
                b[[0, 1]] -= v * (up + mc["margin"] * ha)
            if down > 0:
                b[[2, 3]] += v * (down + mc["margin"] * ha)
            b[:, 0], b[:, 1] = np.clip(b[:, 0], 0, W - 1), np.clip(b[:, 1], 0, H - 1)
            grown.append(a)
            new_boxes.append(b.astype(np.float32))
        done = set()
        for a, b, r in zip(grown, new_boxes, self._read_boxes(img, new_boxes) if new_boxes else []):
            new = fix_scripts(canonical(r[0]))
            if self._drop_reason(new, r[1]) is not None:
                continue
            # A speck of noise taken for a mark (a receipt) must not change the
            # line: a tax ID's "0" became "ฉ". On a page whose typeface floats
            # its marks, the marks also settle letters the old reading guessed
            # wrong (กัน read as ก็น without its mai han-akat), so the letters
            # may change there, but never a digit; anywhere else only marks may.
            old = fix_scripts(canonical(rec[a][0]))
            if mc["keep_base"] and (_digits(new) != _digits(old) if floating else _base(new) != _base(old)):
                continue
            boxes[a], rec[a], page["chars"][a] = b, (r[0], r[1]), r[2]
            done.add(a)
        # Marks count as absorbed only where their line was read again; elsewhere
        # the page (and its dropped list, which the hybrid engine reads) is as before.
        return {j for j, a in owner.items() if a in done}

    def _read_boxes(self, img: np.ndarray, boxes: list[np.ndarray]) -> list[tuple[str, float, list | None]]:
        """Recognise every box. A box taller than wide is either a vertical run
        of text (a turned table header, a margin note) or just one tall
        character (a consonant under stacked marks), so it is read as cut and
        turned both ways, and the most confident reading kept."""
        raw = [crop_line(img, b, turn=0) for b in boxes]
        tall = [n for n, c in enumerate(raw) if c.shape[0] >= 1.5 * c.shape[1]]
        res = self.recognize_detail(raw + [np.ascontiguousarray(np.rot90(raw[n], k)) for k in (1, -1) for n in tall])
        out = res[:len(raw)]
        for j, n in enumerate(tall):
            for alt in (res[len(raw) + j], res[len(raw) + len(tall) + j]):
                if alt[1] > out[n][1]:
                    out[n] = (alt[0], alt[1], None)  # read turned: no word positions along the box
        if self.rec2 is not None:
            sus = [n for n, r in enumerate(out) if r[2] is not None and _mixed(r[0]) >= self.cfg["rec2"]["min_mixed"]]
            if sus:
                for n, alt in zip(sus, self.recognize_detail([raw[n] for n in sus], model=self.rec2)):
                    if _mixed(alt[0]) < _mixed(out[n][0]):
                        out[n] = alt
        return out

    def _sideways(self, img: np.ndarray, boxes: list[np.ndarray]) -> int:
        """np.rot90 k that turns a sideways page upright, 0 if it is not
        sideways: most text lines run vertically. The direction is whichever
        way the longest vertical lines read with more confidence."""
        votes = [_vertical(b, 2.0) for b in boxes]
        vert = [b for b, v in zip(boxes, votes) if v == 1]
        if len(vert) < 3 or len(vert) <= votes.count(-1):
            return 0
        vert.sort(key=lambda b: -np.linalg.norm(b[0] - b[3]))
        raw = [crop_line(img, b, turn=0) for b in vert[: self.cfg["ori"]["sample"]]]
        res = self.recognize([np.ascontiguousarray(np.rot90(c, k)) for k in (1, -1) for c in raw])
        ccw = sum(c for _, c in res[:len(raw)])
        cw = sum(c for _, c in res[len(raw):])
        return 1 if ccw > cw else -1

    def _upside_down(self, img: np.ndarray, boxes: list[np.ndarray], page: dict) -> bool:
        """Only for a page that reads badly: do its longest lines read better
        turned 180 degrees?"""
        oc = self.cfg["ori"]
        confs = [c for t, c in page["rec"].values() if len(t) >= 2]
        if len(confs) < 3 or float(np.median(confs)) >= oc["flip_below"]:
            return False
        idx = sorted((i for i in page["rec"] if _vertical(boxes[i], 2.0) == -1),
                     key=lambda i: -np.linalg.norm(boxes[i][0] - boxes[i][1]))[: oc["sample"]]
        if len(idx) < 3:
            return False
        up = float(np.mean([page["rec"][i][1] for i in idx]))
        flipped = self.recognize([np.ascontiguousarray(crop_line(img, boxes[i], turn=0)[::-1, ::-1]) for i in idx])
        return float(np.mean([c for _, c in flipped])) > up + oc["flip_margin"]

    def _join(self, rows: list[str], geom: list | None = None, hline: float = 1.0) -> str:
        jc = self.cfg["join"]
        if jc.get("paragraphs") and geom and len(geom) == len(rows) and not jc["thai_no_space"]:
            out, prev = [], None
            for r, g in zip(rows, geom):
                if out and prev and g and (g[0] - prev[1] > jc["paragraph_gap"] * hline or g[1] < prev[0]):
                    out.append("")
                out.append(r)
                prev = g or prev
            return "\n".join(out)
        if not jc["thai_no_space"]:
            return "\n".join(rows)
        # A Thai line wrap has no space at the break (word processors wrap
        # Thai between words without one), so Thai-Thai joins get nothing.
        out = rows[0] if rows else ""
        for r in rows[1:]:
            out += ("" if out and _is_thai(out[-1]) and _is_thai(r[0]) else "\n") + r
        return out

    @staticmethod
    def _load(image: Any) -> np.ndarray:
        if isinstance(image, np.ndarray):
            return image if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if isinstance(image, (str, os.PathLike)):
            img = cv2.imread(str(image), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError(f"cannot read image {image}")
            return img
        if isinstance(image, (bytes, bytearray)):
            img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("cannot decode image bytes")
            return img
        return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)  # PIL


# -- service engines -----------------------------------------------------------
# Same duck-typed API as engine.OcrEngine (ready / transcribe(png) / close), so
# main.process() drives any of them unchanged.

_WORKER: "LineOCR | None" = None


def _worker_init(model_dir: str, threads: int) -> None:
    global _WORKER
    _WORKER = LineOCR(model_dir, threads=threads)


def _worker_read(png: bytes) -> dict:
    return _WORKER.read(png)


class LineEngine:
    """LineOCR behind the service's async engine API.

    Pages run in a pool of worker PROCESSES, one LineOCR each. Threads cannot
    scale: detection post-processing, reading order and the line filter are
    Python and hold the GIL, so 8 threads on 8 cores kept only ~2 busy
    (350 pages/min against ~1,300 for 8 processes)."""

    def __init__(self, model_dir: str, workers: int = 2, threads: int = 1):
        import asyncio
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        self.url, self.model = f"line:{model_dir}", Path(model_dir).name
        # spawn, not fork: the server process already runs threads (event loop,
        # executors) and forking those is unsafe.
        self.pool = ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"),
                                        initializer=_worker_init, initargs=(str(model_dir), threads))
        self.sem = asyncio.Semaphore(workers)

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    async def transcribe(self, png: bytes) -> dict:
        import asyncio

        async with self.sem:
            t0 = time.perf_counter()
            r = await asyncio.get_running_loop().run_in_executor(self.pool, _worker_read, png)
        lines = r["lines"]
        chars = sum(len(l["text"]) for l in lines)
        conf = sum(l["confidence"] * len(l["text"]) for l in lines) / chars if chars else 0.0
        return {"text": r["text"], "truncated": False, "looped": False, "engine": "line",
                "confidence": round(conf, 4), "lines": lines,
                "ms": round((time.perf_counter() - t0) * 1000)}


def downscale_png(png: bytes, max_pixels: int) -> bytes:
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    if w * h <= max_pixels:
        return png
    s = (max_pixels / (w * h)) ** 0.5
    img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 1])[1].tobytes()


class HybridEngine:
    """Line engine first; a page whose confidence falls below min_conf is re-read
    by the VLM — capped at max_share of pages, since one VLM page costs as much
    CPU/GPU as dozens of line pages. Over the cap the line text is kept and the
    page is flagged low_confidence rather than silently degrading throughput."""

    def __init__(self, line: LineEngine, vlm, min_conf: float = 0.85, max_share: float = 0.05,
                 vlm_max_pixels: int = 896 * 32 * 32):
        self.line, self.vlm = line, vlm
        self.min_conf, self.max_share, self.vlm_max_pixels = min_conf, max_share, vlm_max_pixels
        self.url, self.model = f"hybrid({line.url} | {vlm.url})", f"{line.model}+{vlm.model}"
        self.pages = self.fallbacks = 0

    async def ready(self) -> bool:
        return await self.line.ready() and await self.vlm.ready()

    async def close(self) -> None:
        await self.line.close()
        await self.vlm.close()

    async def transcribe(self, png: bytes) -> dict:
        r = await self.line.transcribe(png)
        self.pages += 1
        if r["confidence"] >= self.min_conf or not r["text"] and not r["lines"]:
            return r
        if self.fallbacks + 1 > self.max_share * max(self.pages, 20):
            return {**r, "low_confidence": True}
        self.fallbacks += 1
        v = await self.vlm.transcribe(downscale_png(png, self.vlm_max_pixels))
        return {**v, "engine": "vlm-fallback", "line_confidence": r["confidence"]}
