"""Round 4: the Cadence outfit from outfit_design.md (its English prompt block), plus the ahoge direction fix and the
frosted finish. Three takes: front, back three-quarter, hem close-up."""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
REF = str(HERE / "out_s1_stand.png")
FISH = str(HERE / "out_c_sleepy.png")
doc = (HERE / "outfit_design.md").read_text("utf-8")
block = re.search(r"```\n(Full-body illustration.*?)```", doc, re.S).group(1).strip()
# the pose line is set per take; the finish and the ahoge direction are ours
block = block.replace("plain cream background, standing pose, three-quarter or front view, character and fish fully in frame.",
                      "plain solid warm ivory-beige background (#F5EADD).")
EXTRA = (
    "\n\nAhoge: the single question-mark-shaped strand opens toward the left side of the picture, its hook pointing "
    "left. Finish: a soft frosted, matte, slightly grainy texture along all outlines and shaded edges, like coloured "
    "pencil on textured paper; flat colour areas stay clean; only a few muted highlights on the hair. The fish "
    "companion wears the same frosted finish and a small navy flat bow at the base of its tail. 1:1 square. No text, "
    "no letters, no logos, no watermark, no signature."
)
TAKES = {
    "t1_front": "Pose: standing, facing the viewer, full body centered with margins, one hand lightly touching a "
                "headphone cup, the other resting on the skirt; a small open-mouthed curious expression; the fish "
                "sits beside her feet looking up.",
    "t2_back": "Pose: standing, seen from behind at a three-quarter angle, looking back over her shoulder with a "
               "gentle smile; full body centered with margins, so the large navy waist-tie bow with its long tails, "
               "the corset lacing, the ribbon streamers from the headphones and the hair gradient are all visible; the "
               "fish sits beside her feet.",
    "t3_hem": "Composition: a close-up from the waist down to the shoes, filling the frame, slightly from below, to "
              "show the bodice buttons, the skirt's three border rows (zigzag ribbon with buttons, embroidered band, "
              "scalloped lace), the two petticoat tiers, the socks with ribbon bows and the Mary Janes; the fish sits "
              "at the lower left corner.",
}
for name, pose in TAKES.items():
    req = {
        "prompt": f"{block}\n\n{pose}{EXTRA}",
        "references": [
            {"id": "base-girl", "path": REF, "roles": ["identity", "style", "proportion"],
             "instruction": "our character: keep face, hair colours, headphones, proportions and drawing style; the "
                            "outfit is redesigned exactly as the prompt describes"},
            {"id": "fish", "path": FISH, "roles": ["identity"],
             "instruction": "her companion: this exact blue fish with headphones"},
        ],
        "aspect_ratio": "1:1",
        "resolution": {"mode": "provider-native", "width": None, "height": None},
        "output": {"format": "png", "alpha": False},
    }
    (HERE / f"req_{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", name, len(req["prompt"]), "chars")
