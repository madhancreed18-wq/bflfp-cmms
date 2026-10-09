from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import textwrap

OUT = Path('workflow-images')
OUT.mkdir(exist_ok=True)
W, H = 1800, 2150
INK, BG, BOX, DECISION = '#17212B', '#FFFFFF', '#F8FAFC', '#FFFBEA'
FONT = r'C:\Windows\Fonts\tahoma.ttf'
BOLD = r'C:\Windows\Fonts\tahomabd.ttf'

def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)

def center_text(draw, box, text, size=34, bold=False, lines=None):
    f = font(size, bold)
    if lines is None:
        max_chars = max(12, int((box[2] - box[0]) / (size * .58)))
        lines = []
        for paragraph in text.split('\n'):
            lines.extend(textwrap.wrap(paragraph, width=max_chars) or [''])
    spacing = int(size * .32)
    heights = [draw.textbbox((0, 0), line, font=f)[3] for line in lines]
    total = sum(heights) + spacing * (len(lines) - 1)
    y = (box[1] + box[3] - total) / 2
    for line, h in zip(lines, heights):
        bb = draw.textbbox((0, 0), line, font=f)
        x = (box[0] + box[2] - (bb[2] - bb[0])) / 2
        draw.text((x, y), line, font=f, fill=INK)
        y += h + spacing

def rounded(draw, rect, text, fill=BOX, size=34):
    draw.rounded_rectangle(rect, radius=26, fill=fill, outline=INK, width=3)
    center_text(draw, rect, text, size=size, bold=True)

def diamond(draw, cx, cy, text):
    pts = [(cx, cy-88), (cx+190, cy), (cx, cy+88), (cx-190, cy)]
    draw.polygon(pts, fill=DECISION, outline=INK)
    draw.line(pts+[pts[0]], fill=INK, width=3)
    center_text(draw, (cx-145, cy-58, cx+145, cy+58), text, size=29, bold=True)

def arrow(draw, points):
    draw.line(points, fill=INK, width=4, joint='curve')
    x1, y1 = points[-2]; x2, y2 = points[-1]
    if x2 == x1:
        sign = 1 if y2 > y1 else -1
        head = [(x2, y2), (x2-13, y2-24*sign), (x2+13, y2-24*sign)]
    else:
        sign = 1 if x2 > x1 else -1
        head = [(x2, y2), (x2-24*sign, y2-13), (x2-24*sign, y2+13)]
    draw.polygon(head, fill=INK)

def label(draw, x, y, text):
    f = font(27, True)
    bb = draw.textbbox((0,0), text, font=f)
    draw.rectangle((x-12,y-7,x+(bb[2]-bb[0])+12,y+(bb[3]-bb[1])+7),fill=BG)
    draw.text((x,y), text, font=f, fill=INK)

def make(path, title, subtitle, s):
    im = Image.new('RGB', (W,H), BG); d = ImageDraw.Draw(im)
    center_text(d, (80, 45, W-80, 112), title, 46, True, [title])
    center_text(d, (80, 115, W-80, 160), subtitle, 26, False, [subtitle])
    x, bw, bh = 490, 820, 105
    y = 215
    rounded(d,(x,y,x+bw,y+bh),s['a']); arrow(d,[(900,y+bh),(900,y+bh+55)])
    y += 160; rounded(d,(x,y,x+bw,y+bh),s['b']); arrow(d,[(900,y+bh),(900,y+bh+55)])
    cy = y+bh+150; diamond(d,900,cy,s['c']); label(d,570,cy-25,s['yes']); label(d,1160,cy-25,s['no'])
    # left branch
    arrow(d,[(710,cy),(290,cy),(290,cy+140)])
    rounded(d,(90,cy+140,560,cy+285),s['d'], '#FFF6F6', 27)
    arrow(d,[(325,cy+285),(325,cy+350),(900,cy+350)])
    # right branch
    arrow(d,[(1090,cy),(1510,cy),(1510,cy+140)])
    rounded(d,(1240,cy+140,1710,cy+285),s['e'], '#F7FAFC', 27)
    arrow(d,[(1475,cy+285),(1475,cy+350),(900,cy+350)])
    y2 = cy+350
    rounded(d,(x,y2,x+bw,y2+bh),s['f']); arrow(d,[(900,y2+bh),(900,y2+bh+55)])
    y2 += 160; rounded(d,(x,y2,x+bw,y2+bh),s['g']); arrow(d,[(900,y2+bh),(900,y2+bh+55)])
    cy2=y2+bh+150; diamond(d,900,cy2,s['h']); label(d,590,cy2-25,s['stop_yes']); label(d,1160,cy2-25,s['stop_no'])
    # left pause/restart branch
    arrow(d,[(710,cy2),(280,cy2),(280,cy2+125)])
    rounded(d,(60,cy2+125,560,cy2+280),s['i'], '#FFFBEA', 31)
    arrow(d,[(310,cy2+280),(310,cy2+340),(450,cy2+340),(450,y2+55)])
    # right finish
    arrow(d,[(1090,cy2),(1520,cy2),(1520,cy2+125)])
    rounded(d,(1210,cy2+125,1740,cy2+280),s['j'], '#F7FAFC', 30)
    arrow(d,[(1475,cy2+280),(1475,cy2+340),(900,cy2+340)])
    y3=cy2+340
    rounded(d,(x,y3,x+bw,y3+bh),s['k']); arrow(d,[(900,y3+bh),(900,y3+bh+55)])
    cy3=y3+bh+150; diamond(d,900,cy3,s['l']); label(d,620,cy3-25,s['verify_no']); label(d,1160,cy3-25,s['verify_yes'])
    arrow(d,[(710,cy3),(280,cy3),(280,cy3+120)])
    rounded(d,(60,cy3+120,560,cy3+265),s['m'], '#FFF6F6', 31)
    arrow(d,[(310,cy3+265),(310,cy3+330),(900,cy3+330),(900,y2)])
    arrow(d,[(1090,cy3),(1520,cy3),(1520,cy3+120)])
    rounded(d,(1210,cy3+120,1740,cy3+265),s['n'], '#F2FBF4', 27)
    im.save(path, optimize=True)

en = {
 'a':'1. Operator finds a machine problem','b':'2. Open CMMS and scan/select machine QR','c':'Machine stopped?','yes':'Yes — Breakdown','no':'No — Corrective work',
 'd':'3A. Report urgent BD job\nAll technicians receive notification','e':'3B. Report CM job\nPlanner assigns technician and schedule',
 'f':'4. Technician starts work','g':'5. Timer starts automatically','h':'Needs to stop or wait?','stop_yes':'Yes','stop_no':'No',
 'i':'Technician pauses\nParts / production / break','j':'6. Technician completes repair\nProblem, cause, solution, photos',
 'k':'7. Operator gets completion notification','l':'Operator checks machine\nWorking correctly?','verify_no':'Not fixed','verify_yes':'Working correctly',
 'm':'Reject with reason\nJob becomes Rework','n':'Approve and sign\nJob is Done\nHistory, downtime and KPIs update'}
th = {
 'a':'1. ผู้ปฏิบัติงานพบปัญหาเครื่องจักร','b':'2. เปิด CMMS และสแกน/เลือก QR เครื่องจักร','c':'เครื่องจักรหยุดหรือไม่?','yes':'ใช่ — Breakdown','no':'ไม่ใช่ — งานแก้ไข',
 'd':'3A. แจ้งงานด่วน BD\nช่างทุกคนได้รับการแจ้งเตือน','e':'3B. แจ้งงาน CM\nPlanner มอบหมายช่างและกำหนดเวลา',
 'f':'4. ช่างเริ่มทำงาน','g':'5. ระบบเริ่มจับเวลาอัตโนมัติ','h':'ต้องหยุดหรือรอหรือไม่?','stop_yes':'ใช่','stop_no':'ไม่ใช่',
 'i':'ช่างพักงาน\nรออะไหล่ / รอผลิต / พัก','j':'6. ช่างบันทึกงานเสร็จ\nปัญหา สาเหตุ วิธีแก้ รูปภาพ',
 'k':'7. ผู้ปฏิบัติงานได้รับแจ้งงานเสร็จ','l':'ผู้ปฏิบัติงานตรวจเครื่อง\nใช้งานได้ถูกต้องหรือไม่?','verify_no':'ใช้ไม่ได้','verify_yes':'ใช้งานได้',
 'm':'ปฏิเสธและระบุเหตุผล\nงานกลับไป Rework','n':'อนุมัติและลงชื่อ\nงานเสร็จสิ้น\nประวัติ Downtime และ KPI อัปเดต'}

make(OUT/'operator-workflow-english.png', 'Operator Workflow — CMMS', 'From reporting a machine problem to verified completion', en)
make(OUT/'operator-workflow-thai.png', 'ขั้นตอนการทำงานของผู้ปฏิบัติงาน — CMMS', 'ตั้งแต่แจ้งปัญหาเครื่องจักรจนถึงตรวจรับงานเสร็จ', th)
