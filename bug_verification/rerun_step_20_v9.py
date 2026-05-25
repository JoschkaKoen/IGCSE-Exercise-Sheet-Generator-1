"""Re-run step 20 (extract_exam_questions) on an existing s23_12 run with the v9 prompt.

Bypasses the pipeline runner because step 20 isn't marked resumable. Loads the
cached scaffold from step 19, re-creates the Gemini client, and writes a fresh
20_extract_exam_questions/ folder using whatever version of the prompt is on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eXercise.ai_client import make_gemini_native_client  # noqa: E402
from eXercise.env_load import load_project_env  # noqa: E402
from xscore.scaffold.formats.base import ScaffoldFormat, _parse_yaml_scaffold_node  # noqa: E402
from xscore.scaffold.scaffold_fill import extract_exam_questions  # noqa: E402
from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown  # noqa: E402
from xscore.scaffold.scaffold_prompts import extract_questions_model_config  # noqa: E402

load_project_env()

RUN_DIR = Path("output/xscore/s23_12/2026-05-03_15-47-55")
SCAFFOLD_PATH = RUN_DIR / "19_extract_exam_question_numbers" / "exam_scaffold.yaml"
EXAM_PDF = RUN_DIR / "09_cut_exam" / "exam_input.pdf"
OUT_DIR = RUN_DIR / "20_extract_exam_questions"

assert SCAFFOLD_PATH.exists(), SCAFFOLD_PATH
assert EXAM_PDF.exists(), EXAM_PDF

scaffold_doc = yaml.safe_load(SCAFFOLD_PATH.read_text())
# Convert raw YAML nodes (with `type` field) into the in-memory shape with
# `question_type` that the merger and serializer expect.
scaffold_nodes = [_parse_yaml_scaffold_node(q) for q in scaffold_doc["questions"]]
raw_layout = {"rows": scaffold_doc.get("rows", 1), "cols": scaffold_doc.get("cols", 1)}

fmt = ScaffoldFormat()
fill_model, fill_thinking, fill_max_tokens = extract_questions_model_config()
print(f"Model: {fill_model}, thinking={fill_thinking}, max_tokens={fill_max_tokens}")

client = make_gemini_native_client()
print(f"Client: {type(client).__name__}")

OUT_DIR.mkdir(parents=True, exist_ok=True)
# Wipe the old per-page artifacts so we can see clean output
for p in OUT_DIR.glob("exam_questions_p*"):
    p.unlink()

raw_questions = extract_exam_questions(
    client,
    fill_model,
    fill_thinking,
    fill_max_tokens,
    actual_exam_pdf=EXAM_PDF,
    scaffold_nodes=scaffold_nodes,
    artifact_dir=RUN_DIR,
    fmt=fmt,
    is_cs=True,  # s23_12 is Computer Science
)

# Write the merged YAML + markdown (mirrors what the step body does)
out_yaml = OUT_DIR / "exam_questions.yaml"
out_yaml.write_text(fmt.serialize_exam(raw_questions, raw_layout), encoding="utf-8")
write_raw_exam_markdown(RUN_DIR, raw_questions)
print(f"\nWrote {out_yaml}")
print(f"Total questions written: {len(raw_questions)}")
