---
license: apache-2.0
language:
- th
- en
tags:
- ocr
- thai
- onnx
- openvino
- text-recognition
- text-detection
pipeline_tag: image-to-text
---

<p align="center"><img src="https://huggingface.co/datasets/iapp/OpenThai2.0-model-card-assets/resolve/main/openthai-iapp-logos.png" alt="OpenThai · iApp Technology" width="460"></p>

# OpenThai2.2-OCR-Text-20261127

**Fast Thai + English printed-text OCR that runs on a CPU, no GPU needed.** 0.002B (2.02M parameters) in all, about 8 MB of
ONNX files. It is the engine behind iApp Document OCR v3.0, from OpenThai (formerly OpenThaiGPT). Developed by iApp,
launched with AIEAT. It returns a page's text in reading order, with a box and a confidence for every line. For tables
and Markdown, use [OpenThai2.2-OCR-Markdown-20261127](https://huggingface.co/iapp/OpenThai2.2-OCR-Markdown-20261127).

## Against Typhoon OCR V2 0.8B

Same pages, same scoring (character accuracy; higher is better), Typhoon run as its own card says.

| | OpenThai2.2-OCR-Text 0.002B (2.02M) | Typhoon OCR V2 0.8B | Result |
|---|---|---|---|
| **Average of 12 printed Thai and English sets** | **88.2** | 85.1 | **Win, +3.1** |
| Administrative Court judgments | **97.0** | 95.6 | |
| Royal Gazette | **97.8** | 96.0 | |
| Parliament budget documents | **99.6** | 85.9 | |
| Old books (Thai Wikisource) | **78.8** | 76.3 | |
| OCR104 Thai+English crops | **87.9** | 82.1 | |
| ThaiOCRBench full pages | 83.8 | **86.1** | |
| **Runs on** | **a CPU** | an NVIDIA GPU | |
| **Seconds per page** ‡ | **0.39** on a CPU | 1.71 on an H100 (with its EAGLE-3 head) | **No GPU needed** |
| **Throughput** | **about 57,000 pages an hour on 8 CPU cores** | needs a GPU | |

**Win** means we are ahead even at the low end of the 95% confidence range (+1.4 to +4.9, from re-sampling the test
pages). A page a model fails on counts as empty.

‡ One page at a time; not the same pages (ours: 24 printed pages on an Intel Xeon CPU; Typhoon: Thai document pages on
an H100, 27 November 2026), so read them as a rough guide, not a ratio.

**Where Typhoon OCR V2 0.8B is ahead:** handwriting (69.8 against 10.3; this model does not target handwriting), receipts
(77.8 against 68.3), scene text, and ThaiOCRBench full pages.

## What is new in 2.2

- **2.2.1: old-style ๙ is read as ๕ far less often** (typewriters and old letterpress). On 918 unseen old pages, ๙ read as
  ๕ fell from 19 to 6; a small classifier (`judge9.onnx`) can only change ๕ to ๙, and nothing else moved on the benchmark.
- **2.2.0: optional flattening of phone photos** (off by default; scans and PDFs are left alone). On 200 simulated phone
  photos, accuracy 86.7 → 90.5.

## Use

```python
# pip install openvino opencv-python-headless pyclipper numpy huggingface_hub   (or onnxruntime instead of openvino)
from huggingface_hub import snapshot_download
snapshot_download("iapp/OpenThai2.2-OCR-Text-20261127", local_dir="OpenThai2.2-OCR-Text")

import sys; sys.path.insert(0, "OpenThai2.2-OCR-Text")
from runtime.line_engine import LineOCR

ocr = LineOCR("OpenThai2.2-OCR-Text", threads=1)
r = ocr.read("page.png")          # path, bytes, BGR ndarray or PIL image
print(r["text"])                  # lines in reading order (columns one after another)
r["lines"]                        # [{"text", "confidence", "row", "box"}], boxes in the input image's coordinates
```

- **Throughput:** run one worker process per core with `threads=1` (threads in one process stop scaling at about 2 cores).
- **Phone photos:** `pip install onnxruntime`, then
  `hf download PaddlePaddle/UVDoc_onnx inference.onnx --local-dir OpenThai2.2-OCR-Text/dewarp` and
  `LineOCR(..., pre={"dewarp": True})`. `r["dewarp"]` reports what was done.
- **Step-by-step guide** for Windows, Mac and Linux, with a script for pictures and PDFs:
  [English](https://iapp.co.th/en/blog/openthai2p0-ocr-how-to-run) · [ภาษาไทย](https://iapp.co.th/blog/openthai2p0-ocr-how-to-run) · [中文](https://iapp.co.th/zh-CN/blog/openthai2p0-ocr-how-to-run)
  (written for 2.0; use this repository's name).
- **iApp API:** Document OCR (`https://iapp.co.th/docs/ocr/document`) is built on this engine.

The runtime turns sideways and upside-down pages upright, deskews, reads two- and three-column pages column by column
while keeping tables and lists in row order, handles light-on-dark and coloured text, and puts floating tone marks of old
letterpress back on their line.

## Model at a glance

<table>
<tr><td><b>Parameters</b></td><td>2.02M: detector 428K (PP-OCRv6-tiny), recognizer 587K, second recognizer for loopless fonts 587K, ๙ classifier 422K</td></tr>
<tr><td><b>Files</b></td><td><code>det.onnx</code>, <code>rec.onnx</code>, <code>rec2.onnx</code>, <code>judge9.onnx</code> (about 8 MB), with the Python runtime in <code>runtime/</code></td></tr>
<tr><td><b>Hardware</b></td><td>CPU only; about 450 MB of RAM per worker process; OpenVINO (bfloat16 on CPUs with AMX) or ONNX Runtime</td></tr>
<tr><td><b>Training data</b></td><td>410,270 real text lines (court scans, parliament budget PDFs, Wikisource old books) and synthetic lines</td></tr>
<tr><td><b>Versions</b></td><td>2.2.1 = tag <code>v2.2.1</code>; 2.2.0 = <code>v2.2.0</code>; 2.1.4 = <code>v2.1.4</code>; 2.0 = <code>v2.0.0</code>. Earlier names (OpenThai2.0-OCR-Text, OpenThai2.1-OCR-Text-20261118) redirect here.</td></tr>
</table>

## Limits

- **Printed text only:** handwriting, receipts photographed at an angle and scene text read poorly.
- **No tables or Markdown:** use OpenThai2.2-OCR-Markdown for those.
- **A ๙ the detector or recognizer loses** is not recovered by the classifier.

## Acknowledgements

Built on PaddlePaddle PP-OCRv6-tiny (Apache-2.0). Trained on NVIDIA H100 GPUs provided by Siam AI. The dewarping page mask
is iApp's ONNX export of the MonkeyOCRv2 preprocessor's page mask (zenosai/MonkeyOCRv2-S-Parsing, Apache-2.0).

## Licence

Apache-2.0.
