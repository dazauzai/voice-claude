"""A flat colour-block schematic of the community "Claude-chan" look, as a proportion/composition/colour reference
for the image model (about 2.8 heads tall, semi-chibi). Drawn with Pillow only; no existing artwork is used."""
from pathlib import Path

from PIL import Image, ImageDraw

W = 1024
img = Image.new("RGB", (W, W), "#F5EADD")
d = ImageDraw.Draw(img)
ORANGE, HAIR_DARK, SKIN, WHITE, BLACK, GOLD, BLUE, EYE = "#F08A3C", "#D2691E", "#FFE3CF", "#FFFFFF", "#2B2B33", "#C9A227", "#4D7CFE", "#2F55C4"
cx = 512
# hair back mass (long, down to the hips)
d.rounded_rectangle((cx - 190, 150, cx + 190, 700), radius=170, fill=ORANGE)
# head
d.ellipse((cx - 150, 160, cx + 150, 460), fill=SKIN)
# bangs
d.chord((cx - 152, 150, cx + 152, 420), start=180, end=360, fill=ORANGE)
d.polygon([(cx - 150, 285), (cx - 110, 330), (cx - 60, 280), (cx, 335), (cx + 60, 280), (cx + 110, 330), (cx + 150, 285), (cx + 150, 240), (cx - 150, 240)], fill=ORANGE)
# ahoge
d.line([(cx + 10, 165), (cx + 40, 95), (cx + 90, 120)], fill=HAIR_DARK, width=14, joint="curve")
# blue bow with fin ornament on the right side of the head
d.polygon([(cx + 140, 230), (cx + 230, 190), (cx + 215, 260)], fill=BLUE)
d.polygon([(cx + 140, 230), (cx + 230, 290), (cx + 200, 230)], fill=BLUE)
d.ellipse((cx + 128, 218, cx + 156, 246), fill=EYE)
# eyes (big, blue)
for ex in (cx - 62, cx + 62):
    d.ellipse((ex - 34, 330, ex + 34, 410), fill=EYE)
    d.ellipse((ex - 18, 342, ex + 2, 368), fill=WHITE)
# blush + small mouth
d.ellipse((cx - 118, 395, cx - 70, 420), fill="#FFB3B3")
d.ellipse((cx + 70, 395, cx + 118, 420), fill="#FFB3B3")
d.ellipse((cx - 10, 425, cx + 10, 440), fill="#C04A4A")
# body: white frilled blouse, black corset with gold buttons, big black bow with gold brooch
d.rounded_rectangle((cx - 120, 455, cx + 120, 620), radius=40, fill=WHITE)
d.rounded_rectangle((cx - 100, 560, cx + 100, 660), radius=20, fill=BLACK)
for y in (585, 615, 645):
    d.ellipse((cx - 8, y - 8, cx + 8, y + 8), fill=GOLD)
d.polygon([(cx - 70, 470), (cx, 500), (cx + 70, 470), (cx + 50, 530), (cx, 505), (cx - 50, 530)], fill=BLACK)
d.ellipse((cx - 16, 484, cx + 16, 516), fill=GOLD)
# sleeves and hands
d.rounded_rectangle((cx - 210, 470, cx - 120, 650), radius=40, fill=WHITE)
d.rounded_rectangle((cx + 120, 470, cx + 210, 650), radius=40, fill=WHITE)
d.ellipse((cx - 200, 630, cx - 140, 690), fill=SKIN)
d.ellipse((cx + 140, 630, cx + 200, 690), fill=SKIN)
# skirt: layered orange over white, with a sunburst-like emblem placeholder (plain gold circle)
d.polygon([(cx - 110, 650), (cx + 110, 650), (cx + 230, 880), (cx - 230, 880)], fill=ORANGE)
d.polygon([(cx - 160, 800), (cx + 160, 800), (cx + 230, 880), (cx - 230, 880)], fill=WHITE)
d.ellipse((cx - 70, 700, cx - 30, 740), fill=GOLD)
d.ellipse((cx + 30, 700, cx + 70, 740), fill=GOLD)
# legs, socks, lace-up boots
for lx in (cx - 60, cx + 60):
    d.rounded_rectangle((lx - 30, 870, lx + 30, 930), radius=16, fill=SKIN)
    d.rounded_rectangle((lx - 34, 920, lx + 34, 950), radius=12, fill=WHITE)
    d.rounded_rectangle((lx - 40, 945, lx + 40, 1000), radius=18, fill="#C58B5A")
out = Path(__file__).with_name("ref_girl.png")
img.save(out)
print(out)
