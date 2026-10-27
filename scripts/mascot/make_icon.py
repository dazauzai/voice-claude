"""From the chibi standard sheet: a round avatar (head + headphones on the ivory background, circular, transparent
corners) and a Windows .ico with every size the taskbar and title bar use. Pillow only."""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
src = Image.open(HERE.parents[1] / "voiceclaude" / "assets" / "mascot_chibi.png").convert("RGBA")
W, H = src.size
# the head: eyes around y=270/1254, headband top around y=60, chin around y=360 -> a square around the face
cx, cy, half = int(W * 0.515), int(H * 0.2), int(W * 0.2)
box = (cx - half, cy - half, cx + half, cy + half)
head = src.crop(box)
size = head.size[0]
mask = Image.new("L", (size, size), 0)
ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
avatar.paste(head, (0, 0), mask)
avatar.save(HERE / "avatar_round.png")
# a soft navy ring so the circle reads on light and dark title bars
ring = avatar.copy()
d = ImageDraw.Draw(ring)
d.ellipse((2, 2, size - 3, size - 3), outline=(28, 43, 90, 255), width=max(6, size // 64))
ring.save(HERE / "avatar_ring.png")
sizes = [(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
ring.resize((256, 256), Image.LANCZOS).save(HERE / "voice-claude.ico", sizes=sizes)
for s in (16, 32, 64, 256):
    ring.resize((s, s), Image.LANCZOS).save(HERE / f"icon_{s}.png")
print("crop box", box, "->", HERE / "voice-claude.ico")
