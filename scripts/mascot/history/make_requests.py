"""Three request files for the Codex image adapter: the same voice-claude mascot, three takes."""
import json
from pathlib import Path

HERE = Path(__file__).parent
REF = str(HERE / "ref_fish.png")

IDENTITY = (
    "The mascot of voice-claude, a voice assistant: one chubby, round, blue fish, drawn as a cute meme-style sticker "
    "character. Egg-shaped plump body in cornflower blue (#4D7CFE) with a lighter pale-blue belly, a small darker "
    "dorsal fin, two tiny side fins, and a short heart-shaped tail. Huge round dark-navy eyes with big white "
    "highlights, round pink blush spots, a tiny happy mouth. It wears a pair of chunky dark-navy over-ear headphones "
    "with cornflower-blue ear cups, the band across the top of its head. No arms, no legs, no humanoid features, "
    "not a whale, not a mermaid. Keep this exact character design."
)
STYLE = (
    "Style: soft Chinese internet sticker / meme illustration: bold near-black-navy outer contours, finer interior "
    "lines, clean rounded shapes, gentle cel shading, a few glossy highlights, moderate saturation, friendly and a "
    "little silly. Plain solid warm ivory-beige background (#F5EADD), nothing else in the scene. No text, no "
    "letters, no logo, no watermark, no signature."
)
TAKES = {
    "a_listening": "Pose: facing the viewer, centered, body slightly tilted, eyes wide open and attentive as if "
                   "listening carefully, one tiny fin raised to the headphone cup. Full body, 1:1 composition with "
                   "margins around the character.",
    "b_talking": "Pose: three-quarter view, mouth open mid-sentence, cheerful, with three small curved sound-wave "
                 "marks drawn beside each cheek like it is speaking. Full body, centered, 1:1 composition with margins.",
    "c_sleepy": "Pose: facing the viewer, eyes as relaxed happy arcs, a small content smile, headphones slightly "
                "askew, looking cozy and lazy. Full body, centered, 1:1 composition with margins.",
}
for name, pose in TAKES.items():
    req = {
        "prompt": f"{IDENTITY}\n\n{pose}\n\n{STYLE}",
        "references": [{"id": "voice-claude-fish-ref", "path": REF, "roles": ["identity", "composition"],
                        "instruction": "a flat schematic of the character: follow its shapes, proportions, colours and the "
                                       "headphones exactly; redraw it in the described soft illustrated style, do not copy "
                                       "the flat vector look"}],
        "aspect_ratio": "1:1",
        "resolution": {"mode": "provider-native", "width": None, "height": None},
        "output": {"format": "png", "alpha": False},
    }
    (HERE / f"req_{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), "utf-8")
    print("wrote", name)
