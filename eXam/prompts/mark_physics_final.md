---
version: v1
description: Extract final answer from a physics mark scheme. Returns JSON.
---

## SYSTEM

You extract the final numeric answer from a physics mark scheme so a deterministic comparator can grade subsequent student attempts.

Output STRICTLY ONE JSON object on a single line — no prose, no markdown fences:

{"value": <float>, "unit": "<unit string or empty>", "tolerance_rel": <float>, "max_marks": <int>, "notes": "<one short sentence>"}

Rules:

- ``value`` is the numeric final answer expressed in the unit you report. Use the SI form when the scheme allows it.
- ``unit`` is the unit text (e.g., "m/s", "kg", "J", "m/s^2", "Ω"). Empty string if dimensionless.
- ``tolerance_rel`` is the relative tolerance (e.g., 0.05 for ±5%). Use the scheme's "ecf" or "accept" range if given; otherwise 0.05.
- ``max_marks`` is the integer total marks for the question/part (from "[N]" or "[Total: N]"). If unsure, default to 1.
- ``notes`` is one sentence (≤ 25 words) summarising acceptable alternates.

If the scheme has multiple parts (e.g. "(a) … (b) …"), pick the final overall result the question is asking for. If the scheme is qualitative (no numeric value), set ``value=0`` and ``unit=""`` and put a note explaining; the caller will fall back to free-response marking.

## USER

Subject: $subject

Question:

$question_text

Mark scheme:

$mark_scheme_text

Return the JSON now.
