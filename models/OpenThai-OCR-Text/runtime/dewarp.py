"""Optional page dewarping for phone photos (OpenThai 2.2, 18 Nov 2026), off by default.

Two backends, both Apache-2.0, run with ONNX Runtime (CPU or GPU). uvdoc is the default: on our dev sets it is as good
as or better than monkey, 3.5x faster on CPU and 36 MB instead of 293 MB.
  monkey  the MonkeyOCRv2 document preprocessor (github.com/Yuliang-Liu/MonkeyOCRv2; weights preprocessor1/2.pth in
          zenosai/MonkeyOCRv2-S-Parsing, Apache-2.0): a U2NETP page mask, then a Swin-based net that predicts a backward
          sampling grid at 512x512, upsampled to the image and applied with cv2.remap. ONNX files: dewarp_mask.onnx
          (exported by iApp from preprocessor1.pth) and dewarp_grid.onnx (from preprocessor2.pth).
  uvdoc   PaddlePaddle UVDoc text-image unwarping (PaddlePaddle/UVDoc_onnx, Apache-2.0): the network takes the image at its
          own size and returns it unwarped. The page-coverage rule below still needs dewarp_mask.onnx. The network file
          may be named uvdoc.onnx or keep its download name, inference.onnx.

The page mask, dewarp_mask.onnx (4.6 MB), ships in the dewarp/ folder of the OpenThai 2.2 model repositories (revision
v2.2.0). UVDoc is downloaded from PaddlePaddle into the same folder:

    hf download PaddlePaddle/UVDoc_onnx inference.onnx --local-dir dewarp

backend="monkey" also needs dewarp_grid.onnx (288 MB), which is not distributed with OpenThai.

Flat pages are left alone: if the page mask covers at least `skip_coverage` of the image (scans and born-digital pages
fill it), the image is returned unchanged. Resampling a flat page only loses detail.

    d = Dewarper("dewarp")                               # folder holding dewarp_mask.onnx and inference.onnx (uvdoc)
    img, info = d(img_bgr)                                # info: applied, coverage, seconds
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np

S = 512


def _session(path: Path, threads: int, providers: list[str] | None):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    avail = ort.get_available_providers()
    prov = [p for p in (providers or ["CUDAExecutionProvider", "CPUExecutionProvider"]) if p in avail] or ["CPUExecutionProvider"]
    return ort.InferenceSession(str(path), so, providers=prov)


def _resize_align_corners(a: np.ndarray, H: int, W: int) -> np.ndarray:
    """Bilinear resize of a (h, w) map with align_corners=True semantics (torch F.interpolate)."""
    h, w = a.shape
    xs = np.linspace(0, w - 1, W, dtype=np.float32)
    ys = np.linspace(0, h - 1, H, dtype=np.float32)
    mx, my = np.meshgrid(xs, ys)
    return cv2.remap(a.astype(np.float32), mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


class Dewarper:
    def __init__(self, model_dir: str | os.PathLike, backend: str = "uvdoc", threads: int = 4,
                 skip_coverage: float = 0.9, mask_thresh: float = 0.8, providers: list[str] | None = None):
        d = Path(model_dir)
        if backend not in ("monkey", "uvdoc"):
            raise ValueError("backend must be monkey or uvdoc")
        self.backend, self.skip_coverage, self.mask_thresh = backend, skip_coverage, mask_thresh
        self.mask = _session(d / "dewarp_mask.onnx", threads, providers)
        if backend == "monkey":
            self.net = _session(d / "dewarp_grid.onnx", threads, providers)
        else:
            self.net = _session(d / "uvdoc.onnx" if (d / "uvdoc.onnx").exists() else d / "inference.onnx", threads, providers)
        self.net_in = self.net.get_inputs()[0].name

    def page_mask(self, img_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
        """(x at 512x512 RGB in [0,1], page mask 512x512 uint8 (largest component), coverage)."""
        rgb = img_bgr[:, :, ::-1].astype(np.float32) / 255.0
        x = cv2.resize(rgb, (S, S), interpolation=cv2.INTER_AREA).transpose(2, 0, 1)[None].copy()
        m = (self.mask.run(None, {"x": x})[0][0, 0] > self.mask_thresh).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        if n > 1:
            m = (labels == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
        return x, m, float(m.mean())

    def __call__(self, img_bgr: np.ndarray) -> tuple[np.ndarray, dict]:
        t0 = time.perf_counter()
        H, W = img_bgr.shape[:2]
        x, m, cov = self.page_mask(img_bgr)
        info = {"backend": self.backend, "coverage": round(cov, 3), "applied": False}
        if cov >= self.skip_coverage or (cov < 0.05 and self.skip_coverage <= 1.0):  # flat page fills the image; none found
            info["seconds"] = round(time.perf_counter() - t0, 3)
            return img_bgr, info
        if self.backend == "monkey":
            g = self.net.run(None, {self.net_in: x * m[None, None].astype(np.float32)})[0][0]
            if not np.isfinite(g).all():
                info["seconds"] = round(time.perf_counter() - t0, 3)
                return img_bgr, info
            gx, gy = _resize_align_corners(g[0], H, W), _resize_align_corners(g[1], H, W)
            out = cv2.remap(img_bgr, (gx + 1) * 0.5 * (W - 1), (gy + 1) * 0.5 * (H - 1), cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
        else:
            xin = (img_bgr[:, :, ::-1].astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
            y = self.net.run(None, {self.net_in: xin})[0][0]
            out = np.ascontiguousarray(np.clip(y.transpose(1, 2, 0) * 255.0, 0, 255).astype(np.uint8)[:, :, ::-1])
        info["applied"] = True
        info["seconds"] = round(time.perf_counter() - t0, 3)
        return out, info


def main() -> None:
    """Dewarp an items file ({id, image, ...}) or a folder of images into OUT, writing OUT/items.jsonl (same records,
    image -> the written file) and OUT/dewarp_log.jsonl (applied, coverage, seconds)."""
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--backend", default="uvdoc", choices=["monkey", "uvdoc"])
    ap.add_argument("--items", help="items.jsonl with an image path per record")
    ap.add_argument("--images", help="a folder of images (written with the same names)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--skip-coverage", type=float, default=0.9, help="leave pages whose mask covers this share alone; >1 = always dewarp")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    dw = Dewarper(a.models, a.backend, threads=a.threads, skip_coverage=a.skip_coverage)
    if a.items:
        recs = [json.loads(line) for line in open(a.items, encoding="utf-8")]
    else:
        recs = [{"id": p.stem, "image": str(p)} for p in sorted(Path(a.images).iterdir())
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp")]
    with open(out / "items.jsonl", "w", encoding="utf-8") as fi, open(out / "dewarp_log.jsonl", "w", encoding="utf-8") as fl:
        for r in recs:
            img = cv2.imread(r["image"], cv2.IMREAD_COLOR)
            res, info = dw(img)
            dst = out / "img" / Path(r["image"]).name
            if info["applied"]:
                cv2.imwrite(str(dst), res, [cv2.IMWRITE_JPEG_QUALITY, 95] if dst.suffix.lower() in (".jpg", ".jpeg") else [])
            elif not dst.exists():
                os.symlink(os.path.abspath(r["image"]), dst)
            fi.write(json.dumps({**r, "image": str(dst)}, ensure_ascii=False) + "\n")
            fl.write(json.dumps({"id": r.get("id"), **info}) + "\n")
    print(f"{len(recs)} images -> {out}")


if __name__ == "__main__":
    main()
