"""Three requests: an independent take on the community "Claude-chan" look, described from the video frames in words
(no existing picture is fed to the model), to be adapted into voice-claude's own character afterwards."""
import json
from pathlib import Path

HERE = Path(__file__).parent
REF = str(HERE / "ref_girl.png")

IDENTITY = (
    "One anime-style girl character, an AI assistant personified, semi-chibi proportions about 2.8 heads tall, "
    "ageless and cute, not a child. Long wavy bright orange hair reaching her hips, soft bangs, one forward-curving "
    "ahoge on top. Large round sky-blue eyes with big highlights, small nose, small mouth, light blush. On the right "
    "side of her head a royal-blue ribbon bow with a small blue fish-fin shaped ornament. Outfit: a white blouse with "
    "frilled collar and puffed frilled sleeves, a big black ribbon bow at the collar with a round gold sunburst-shaped "
    "brooch, a black corset waist with three gold buttons, a layered skirt: orange over a white frilled underskirt, "
    "with small gold eight-point star emblems; white frilled socks and tan lace-up ankle boots. Palette: warm orange, "
    "cream white, black, muted gold, a touch of royal blue. Keep this exact character design."
)
STYLE = (
    "Style: polished Chinese 2D anime illustration like a character reference sheet: clean bold outlines, soft cel "
    "shading with gentle gradients, glossy hair highlights, detailed frills, moderate saturation. Plain solid warm "
    "ivory-beige background (#F5EADD), nothing else in the scene. No text, no letters, no logo, no name badge, no "
    "watermark, no signature."
)
TAKES = {
    "g_stand": "Pose: standing, facing the viewer, full body centered, arms relaxed, a small open-mouthed curious "
               "expression, 1:1 composition with margins around the character.",
    "g_book": "Pose: three-quarter view, full body, holding a thick black hardcover book against her chest with both "
              "hands, head tilted, a friendly closed-mouth smile; 1:1 composition with margins.",
    "g_sit": "Pose: sitting on a plain wooden chair, knees together, hands on her lap, relaxed sleepy half-closed eyes "
             "and a content smile; full body centered, 1:1 composition with margins.",
}
for name, pose in TAKES.items():
    req = {
        "prompt": f"{IDENTITY}\n\n{pose}\n\n{STYLE}",
        "references": [{"id": "girl-ref", "path": REF, "roles": ["identity", "composition", "proportion"],
                        "instruction": "a flat colour-block schematic of the character: follow its proportions, colour "
                                       "placement and outfit layout; redraw everything in the described anime style with "
                                       "proper anatomy and detail, do not copy the flat blocky look"}],
        "aspect_ratio": "1:1",
        "resolution": {"mode": "provider-native", "width": None, "height": None},
        "output": {"format": "png", "alpha": False},
    }
    (HERE / f"req_{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", name)
