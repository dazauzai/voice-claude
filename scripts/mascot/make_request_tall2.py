"""The tall standard sheet again, this time with a proportion reference (the user's own ChatGPT render of the same
design, 4.5 heads) so the model does not snap back to the chibi body."""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
ASSETS = ROOT / "voiceclaude" / "assets"
DESIGN = ROOT / "docs" / "mascot" / "design.md"
CHIBI = str(ASSETS / "mascot_chibi.png")
TALL_REF = str(ASSETS / "mascot_tall.png")  # 4-4.5 heads: the body proportions
FISH = str(ASSETS / "fish.png")
doc = DESIGN.read_text("utf-8")
block = re.search(r"```\n(Full-body illustration.*?)```", doc, re.S).group(1).strip()
block = block.replace("a semi-chibi anime girl, about 2.8 heads tall,", "an anime girl drawn at a slender 4.5-heads-tall proportion (see reference image 2 for the body proportions),")
block = block.replace("plain cream background, standing pose, three-quarter or front view, character and fish fully in frame.",
                      "plain solid warm ivory-beige background (#F5EADD).")
prompt = block + (
    "\n\nSTANDARD REFERENCE SHEET, tall version: front view, standing straight, arms slightly away from the body, feet "
    "together, small smile, even lighting, every costume element visible. BODY PROPORTIONS exactly like reference "
    "image 2: 4.5 heads tall, long legs, the bell skirt ending just below the knee, hair to the hips; youthful, not an "
    "adult model. FACE, hair colours, headphones and costume exactly like reference image 1 (do not copy reference "
    "image 2's face or its hem pattern layout). The lace is rendered finely: eyelet holes, scalloped edges, the "
    "seigaiha embroidery readable. Ahoge: one single question-mark-shaped strand, hook open toward the left side of "
    "the picture. Finish: soft frosted, matte, slightly grainy texture along outlines and shaded edges like coloured "
    "pencil on textured paper. The fish companion from reference image 3 SITS on the ground beside her right foot "
    "(viewer's left), the same size as in reference image 1, so it reaches her mid-calf; small navy bow at its tail. "
    "1:1 square, generous margins. No text, no letters, no logos, no watermark, no signature."
)
req = {
    "prompt": prompt,
    "references": [
        {"id": "locked-design", "path": CHIBI, "roles": ["identity", "style", "costume"],
         "instruction": "face, hair, headphones and every costume element come from this image"},
        {"id": "tall-proportions", "path": TALL_REF, "roles": ["proportion"],
         "instruction": "ONLY the body proportions (4.5 heads, long legs) come from this image; not its face, not its hem layout"},
        {"id": "fish", "path": FISH, "roles": ["identity"], "instruction": "her companion: this exact blue fish"},
    ],
    "aspect_ratio": "1:1",
    "resolution": {"mode": "provider-native", "width": None, "height": None},
    "output": {"format": "png", "alpha": False},
}
(HERE / "req_u3_tall.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
print("wrote u3_tall")
