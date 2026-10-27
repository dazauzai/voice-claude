"""voice-claude's mascot, as a flat reference drawing (shape, proportions, colours) for the image model: a round fat
blue fish with big eyes, blush, and headphones. Drawn with Pillow only."""
from pathlib import Path

from PIL import Image, ImageDraw

W = 1024
img = Image.new("RGB", (W, W), "#F5EADD")
d = ImageDraw.Draw(img)
BLUE, LIGHT, DARK, NAVY, PINK, WHITE = "#4D7CFE", "#A9C6FF", "#2F55C4", "#1F2A44", "#FF9AA8", "#FFFFFF"
# body: a fat egg, belly lighter
d.ellipse((150, 260, 790, 820), fill=BLUE)
d.ellipse((230, 460, 710, 790), fill=LIGHT)
# tail
d.polygon([(780, 540), (940, 420), (900, 540), (940, 660)], fill=BLUE)
# fins
d.ellipse((330, 700, 450, 790), fill=DARK)
d.ellipse((520, 700, 640, 790), fill=DARK)
# top fin
d.polygon([(430, 270), (470, 150), (560, 250)], fill=DARK)
# eyes
for cx in (340, 600):
    d.ellipse((cx - 58, 430, cx + 58, 560), fill=NAVY)
    d.ellipse((cx - 30, 452, cx + 6, 500), fill=WHITE)
    d.ellipse((cx + 14, 515, cx + 34, 535), fill=WHITE)
# blush
d.ellipse((190, 580, 290, 640), fill=PINK)
d.ellipse((650, 580, 750, 640), fill=PINK)
# mouth: small happy curve
d.arc((420, 560, 520, 640), start=20, end=160, fill=NAVY, width=14)
# headphones: band + two cups
d.arc((170, 220, 770, 720), start=200, end=340, fill=NAVY, width=36)
for x0 in (120, 740):
    d.rounded_rectangle((x0, 450, x0 + 150, 650), radius=60, fill=NAVY)
    d.rounded_rectangle((x0 + 35, 490, x0 + 115, 610), radius=36, fill=BLUE)
out = Path(__file__).with_name("ref_fish.png")
img.save(out)
print(out)
