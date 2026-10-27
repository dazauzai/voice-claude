"""Three adaptations of our Claude-chan base into voice-claude's own character. Reference: our own out_g_stand.png
(identity, style, composition); the changes are spelled out per variant."""
import json
from pathlib import Path

HERE = Path(__file__).parent
REF = str(HERE / "out_g_stand.png")
FISH = str(HERE / "out_c_sleepy.png")

COMMON = (
    "Same girl as in reference image 1: same face, eye shape, semi-chibi proportions (about 2.8 heads), hair "
    "silhouette with the forward-curving ahoge, the same outfit cut (white frilled blouse with puffed sleeves, big "
    "ribbon bow at the collar with a round brooch, corset waist with three buttons, layered skirt over a white frilled "
    "underskirt, frilled socks, lace-up ankle boots) and the same polished 2D anime illustration style. "
    "She is the mascot of voice-claude, a voice assistant, so: she wears chunky dark-navy over-ear headphones with "
    "cornflower-blue ear cups (band over the top of her head, the ahoge sticking up over the band); the brooch at her "
    "collar is a round silver-blue sound-wave emblem (three curved lines) instead of a sunburst; the skirt emblems are "
    "small sound-wave marks. Beside her feet sits her companion from reference image 2: the small chubby round blue "
    "fish with headphones, about knee height, looking up at her. "
    "Pose: standing, facing the viewer, full body centered, one hand resting on a headphone cup as if listening, a "
    "small open-mouthed curious expression. 1:1 square, margins around both characters. Plain solid warm ivory-beige "
    "background (#F5EADD), nothing else. No text, no letters, no logo, no watermark, no signature."
)
VARIANTS = {
    "v1_blue": "Colour change: her hair becomes cornflower blue (#4D7CFE) with slightly lighter pale-blue tips; eyes "
               "warm amber-orange; outfit palette navy, white, cornflower blue and silver (the skirt navy over the white "
               "underskirt, the collar bow navy). No fins, no tail, no maid headpiece.",
    "v2_orange": "Keep her bright orange hair and blue eyes exactly as in the reference. Outfit palette changes to "
                 "cornflower blue (#4D7CFE), white, navy and silver: the skirt cornflower blue over the white underskirt, "
                 "the collar bow navy, the corset navy with silver buttons. The orange hair against the blue outfit is "
                 "the point.",
    "v3_silver": "Colour change: her hair becomes silver-white with cornflower-blue tips; eyes cornflower blue; outfit "
                 "palette white, cornflower blue and navy with silver details (skirt cornflower blue over white). No "
                 "fins, no tail.",
}
for name, change in VARIANTS.items():
    req = {
        "prompt": f"{COMMON}\n\n{change}",
        "references": [
            {"id": "base-girl", "path": REF, "roles": ["identity", "style", "composition", "proportion"],
             "instruction": "our base character: keep face, proportions, outfit cut and drawing style; apply only the "
                            "changes described in the prompt"},
            {"id": "fish", "path": FISH, "roles": ["identity"],
             "instruction": "her companion: this exact blue fish with headphones, drawn small beside her feet"},
        ],
        "aspect_ratio": "1:1",
        "resolution": {"mode": "provider-native", "width": None, "height": None},
        "output": {"format": "png", "alpha": False},
    }
    (HERE / f"req_{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", name)
