TH Sarabun PSK — the face every filed form is printed in
========================================================

Put these four files in THIS folder:

    THSarabunPSK.ttf
    THSarabunPSK Bold.ttf
    THSarabunPSK Italic.ttf        (optional)
    THSarabunPSK BoldItalic.ttf    (optional)

Only the first two are used. On a Windows machine that already has the font they
are at  C:\Windows\Fonts\  — copy them here, do not move them.

WHY THEY LIVE HERE AND NOT IN C:\WINDOWS\FONTS
----------------------------------------------
F-SP-ENG02-03 (repair notification), F-SP-ENG02-05 (PM checklist) and the daily
report are controlled documents. They have to come out identical on the planner's
PC, on the live server and on a Linux box. Reading whatever face each machine
happens to have installed is exactly how the same form ends up in three different
fonts with nobody noticing, so the app looks HERE first and only falls back to the
installed copies.

The plan sheet (HTML) loads them from /fonts/ for the same reason: it used to pull
Prompt from Google Fonts, which silently falls back to a system face on a plant LAN
with no route to the internet.

IF THIS FOLDER IS EMPTY
-----------------------
Nothing breaks. The app falls back, in order, to an installed TH Sarabun, then
Tahoma / Leelawadee on Windows, then Garuda / Loma / Noto Thai on Linux. The forms
still print — just not in the standard face.

TO CHECK WHICH FACE IS ACTUALLY BEING USED
------------------------------------------
Open  /api/reports/font  while signed in. It reports the file that was registered,
whether it came from this folder, and the size scale in use.

TEXT SIZE
---------
TH Sarabun sets much smaller than Tahoma at the same point size, so every size in
the PDFs is multiplied by PDF_FONT_SCALE (default 1.35) to keep the printed result
the same height it has always been. Set the environment variable to change it;
1.0 means "use the literal point sizes". A one-page form that would spill onto a
second sheet is rebuilt a step smaller automatically, per form.

The font is free to redistribute (SIPA / Thai national font project).
