---
name: parse_exam_pdf_yaml
version: v1
description: Step 18 — parse_exam_pdf. System-only prompt for exam-paper structure extraction in YAML format (the user prompt for this path is built dynamically from layout data by xscore.scaffold.formats.yaml_format._build_user_exam_prompt_yaml, so no USER section here). No substitutions. Used by xscore.scaffold.formats.yaml_format.YamlScaffoldFormat.system_exam_prompt.
---
## SYSTEM

You are an expert at reading Cambridge IGCSE exam papers. Extract every question and sub-question as structured YAML.
