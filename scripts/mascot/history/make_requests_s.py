"""Round 3: deeper orange hair, a frosted/matte grain along the edges, the ahoge leaning left and shaped like a question
mark. Clothes unchanged for now. Two takes."""
import json
from pathlib import Path

HERE = Path(__file__).parent
REF = str(HERE / "out_r1_stand.png")
FISH = str(HERE / "out_c_sleepy.png")

COMMON = (
    "Same girl as in reference image 1: same face, eyes, semi-chibi proportions (about 2.8 heads), the chunky navy "
    "headphones with cornflower-blue ear cups, and exactly the same classic Lolita outfit (white high-collar frilled "
    "blouse with princess sleeves, navy collar bow with the round silver-blue sound-wave brooch, navy corset bodice "
    "with three silver buttons, high-waisted cornflower-blue knee-length bell skirt with sound-wave marks over two "
    "tiers of white lace petticoat, white knee socks with lace tops, navy Mary Jane shoes). Change only these three "
    "things:\n"
    "1. Hair colour: a deeper, richer orange, like burnt orange / dark tangerine (#D9661C) at the roots and crown, "
    "still fading gradually from shoulder level down to pale peach-cream tips.\n"
    "2. Ahoge: one single strand on top of the head, curled in the shape of a question mark, leaning toward the left "
    "side of the picture; it is the only ahoge.\n"
    "3. Finish: a soft frosted, matte, slightly grainy texture along all outlines and shaded edges, like coloured "
    "pencil or pastel crayon on textured paper: edges feel velvety and a little rough instead of glossy and sharp; "
    "the flat colour areas stay clean. No gloss highlights on the hair except a few muted ones.\n"
    "Her companion from reference image 2 sits beside her feet: the small chubby round blue fish with headphones, "
    "about knee height, same frosted finish. Plain solid warm ivory-beige background (#F5EADD), nothing else. 1:1 "
    "square, margins around both characters. No text, no letters, no logo, no watermark, no signature."
)
TAKES = {
    "s1_stand": "Pose: standing, facing the viewer, full body centered, one hand lightly on a headphone cup as if "
                "listening, the other holding the skirt's edge; a small open-mouthed curious expression.",
    "s2_turn": "Pose: three-quarter view, full body centered, hands clasped in front of the skirt, head tilted a "
               "little, a gentle closed-mouth smile; the hair gradient visible along the back.",
}
for name, pose in TAKES.items():
    req = {
        "prompt": f"{COMMON}\n\n{pose}",
        "references": [
            {"id": "base-girl", "path": REF, "roles": ["identity", "style", "costume", "proportion"],
             "instruction": "our character and outfit: keep everything except the three listed changes"},
            {"id": "fish", "path": FISH, "roles": ["identity"],
             "instruction": "her companion: this exact blue fish with headphones, drawn small beside her feet"},
        ],
        "aspect_ratio": "1:1",
        "resolution": {"mode": "provider-native", "width": None, "height": None},
        "output": {"format": "png", "alpha": False},
    }
    (HERE / f"req_{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", name)
