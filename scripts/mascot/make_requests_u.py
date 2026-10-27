"""Standard sheets: the locked Cadence design in two proportions — chibi (2.8 heads: icon, avatar, stickers) and tall
(4.5 heads: splash, README, whiteboard). Both absorb the ChatGPT version's two strengths: a seigaiha (青海波) silver
embroidered band on the hem and finer lace rendering. Also writes the seigaiha note into outfit_design.md."""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
ASSETS = ROOT / "voiceclaude" / "assets"
DESIGN = ROOT / "docs" / "mascot" / "design.md"
REF = str(ASSETS / "mascot_chibi.png")  # identity, style and outfit
FISH = str(ASSETS / "fish.png")

doc_path = DESIGN
doc = doc_path.read_text("utf-8")
if "青海波" not in doc:
    doc = doc.replace(
        "  2. ⬜ **声纹刺绣带**：缎带下方 2 cm，一条 2.5 cm 高的**银线 `#C9CED8` 刺绣带**：重复单元为\"三道同心弧 + 一个小圆点\"（声纹 + 发声点），单元宽 4 cm，首尾相接成环；弧线方向统一向右（像声音往一个方向传）。这是\"品牌印花\"的主位置。",
        "  2. ⬜ **声纹刺绣带（定稿：青海波式）**：缎带下方 2 cm，一条 3 cm 高的**银线 `#C9CED8` 刺绣带**，纹样为**青海波（seigaiha）**——层层叠起的扇形同心弧，每个扇形三道弧，弧心一个小圆点（发声点）；扇形相互叠压排成一行，首尾相接成环，读作\"连续的声波\"。这是\"品牌印花\"的主位置。（2026-10-25 定稿：采纳 ChatGPT 对照版里把这一带做成独立青海波刺绣带的做法。）")
    doc = doc.replace(
        "below it a 2.5 cm silver-thread embroidered band of repeating triple concentric arcs with a dot;",
        "below it a 3 cm silver-thread embroidered band in a seigaiha pattern: overlapping fan-shaped triple concentric arcs, each fan with a small dot at its center, reading as continuous sound waves;")
    doc = doc.replace("## 5. English image-generation prompt", "## 4.12 定稿备注\n\n- 2026-10-25 定稿。两套头身比并存：**Q 版 2.8 头身**用于图标、窗口头像、表情包；**等身版 4.5 头身**用于启动页、README 头图、白板空白页。同一张脸、同一套衣服。\n- 裙摆刺绣带定为青海波式（见 4.5）；蕾丝按\"扇边 + 稀疏孔眼\"画但允许比 Q 版更细。\n- 胖鱼伙伴蹲在脚边，不悬浮；尾鳍根部一个海军蓝小结。\n\n## 5. English image-generation prompt")
    doc_path.write_text(doc, "utf-8")
    print("design doc updated")

block = re.search(r"```\n(Full-body illustration.*?)```", doc, re.S).group(1).strip()
block = block.replace("plain cream background, standing pose, three-quarter or front view, character and fish fully in frame.",
                      "plain solid warm ivory-beige background (#F5EADD).")
COMMON = (
    "\n\nThis is the character's STANDARD REFERENCE SHEET: front view, standing straight, arms slightly away from the "
    "body, feet together, neutral friendly expression with a small smile, even lighting, every costume element "
    "clearly visible and nothing hidden by the hands or hair. The lace is rendered finely: eyelet holes, scalloped "
    "edges and the seigaiha embroidery readable. Ahoge: one single question-mark-shaped strand, the hook open toward "
    "the left side of the picture. Finish: a soft frosted, matte, slightly grainy texture along outlines and shaded "
    "edges like coloured pencil on textured paper; flat colour areas clean. The fish companion SITS on the ground "
    "beside her right foot (viewer's left), not floating: a round chubby blue ball-shaped fish with tiny side fins, a "
    "short tail with a small navy flat bow at its base, mini matching headphones, same frosted finish. 1:1 square, "
    "generous margins. No text, no letters, no logos, no watermark, no signature."
)
SHEETS = {
    "u1_chibi": "Proportions: semi-chibi, about 2.8 heads tall, exactly as the reference image; big head, short "
                "body, the skirt about as wide as the hair mass.",
    "u2_tall": "Proportions: this time a taller, slender 4.5-heads-tall version of the SAME character (same face, "
               "hair, headphones and outfit): longer legs and arms, the bell skirt reaching just below the knee, the "
               "hair reaching the hips, still youthful and cute, not an adult model; the fish companion stays the same "
               "size as before, so it now reaches her mid-calf.",
}
for name, prop in SHEETS.items():
    req = {
        "prompt": f"{block}\n\n{prop}{COMMON}",
        "references": [
            {"id": "locked-design", "path": REF, "roles": ["identity", "style", "costume"],
             "instruction": "the locked character and outfit: keep face, hair colours, headphones and every costume "
                            "element; proportions as the prompt says"},
            {"id": "fish", "path": FISH, "roles": ["identity"], "instruction": "her companion: this exact blue fish"},
        ],
        "aspect_ratio": "1:1",
        "resolution": {"mode": "provider-native", "width": None, "height": None},
        "output": {"format": "png", "alpha": False},
    }
    (HERE / f"req_{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", name)
