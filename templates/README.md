# Report templates — one folder per factory (FP / BFL / PC)

The app fills THESE files when a report is downloaded — the template IS the
official format. Same layout across factories; header/form code differ.

templates/FP/repair_form.xlsx   F-SP-ENG02-03 Rev.01  (ใบแจ้งซ่อม)
templates/FP/daily_report.xlsx  Maintenance daily report

To add a factory: copy the folder, swap logo/company/form codes.
NOTE: FP repair_form.xlsx currently missing its logo — replace with the
original from the working system before the filler module is built.
Cell mappings will live next to each template as <name>.map.json (later).
