"""Round 2 on variant 2 (orange hair, blue outfit): the user's refinements — hair fading at the tips, a longer skirt,
the waistline about 5 % higher, the chin about 3 % pointier, and a classic Lolita outfit. Three takes to choose from."""
import json
from pathlib import Path

HERE = Path(__file__).parent
REF = str(HERE / "out_v2_orange.png")
FISH = str(HERE / "out_c_sleepy.png")

COMMON = (
    "Same girl as in reference image 1 (same face type, eye shape and colour, semi-chibi proportions of about 2.8 "
    "heads, hair silhouette with the forward-curving ahoge, chunky navy headphones with cornflower-blue ear cups, the "
    "same polished 2D anime illustration style), with these exact refinements:\n"
    "1. Hair: bright orange at the roots and crown, fading gradually toward the tips into a pale peach-cream; the "
    "gradient starts around shoulder level and the last third of every lock is noticeably lighter.\n"
    "2. Face: the chin very slightly pointier than in the reference (about 3 percent), still soft and round-cheeked, "
    "not sharp.\n"
    "3. Outfit, classic Lolita style in cornflower blue (#4D7CFE), white and navy with silver details: a white blouse "
    "with a high frilled collar and long princess sleeves ending in lace cuffs; a navy ribbon bow at the collar with "
    "a round silver-blue sound-wave emblem (three curved lines) as the brooch; a cornflower-blue jumper-skirt dress "
    "with a high waistline (about 5 percent higher than the reference), a fitted navy corset-style bodice with three "
    "silver buttons; the skirt longer than the reference, reaching the knees, bell-shaped over a full white "
    "petticoat with two tiers of lace, small sound-wave marks embroidered along the hem; white knee-high socks with "
    "lace tops; navy round-toe Mary Jane shoes with a strap and a small bow. No headpiece except the headphones; a "
    "small navy ribbon on the headphone band is allowed.\n"
    "4. Her companion from reference image 2 sits beside her feet: the small chubby round blue fish with headphones, "
    "about knee height, tail to the side, looking up at her.\n"
    "Plain solid warm ivory-beige background (#F5EADD), nothing else. 1:1 square, margins around both characters. "
    "No text, no letters, no logo, no watermark, no signature."
)
TAKES = {
    "r1_stand": "Pose: standing, facing the viewer, full body centered, one hand lightly on a headphone cup as if "
                "listening, the other hand holding the skirt's edge slightly; a small open-mouthed curious expression.",
    "r2_turn": "Pose: three-quarter view, full body centered, hands clasped in front of the skirt, head tilted a "
               "little, a gentle closed-mouth smile; the hair gradient clearly visible along the back.",
    "r3_curtsy": "Pose: a light curtsy, holding the skirt out to both sides so the bell shape and petticoat lace show, "
                 "eyes closed in a happy smile.",
}
for name, pose in TAKES.items():
    req = {
        "prompt": f"{COMMON}\n\n{pose}",
        "references": [
            {"id": "base-girl", "path": REF, "roles": ["identity", "style", "proportion"],
             "instruction": "our character: keep face type, eyes, proportions and drawing style; apply exactly the "
                            "refinements in the prompt"},
            {"id": "fish", "path": FISH, "roles": ["identity"],
             "instruction": "her companion: this exact blue fish with headphones, drawn small beside her feet"},
        ],
        "aspect_ratio": "1:1",
        "resolution": {"mode": "provider-native", "width": None, "height": None},
        "output": {"format": "png", "alpha": False},
    }
    (HERE / f"req_{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", name)
