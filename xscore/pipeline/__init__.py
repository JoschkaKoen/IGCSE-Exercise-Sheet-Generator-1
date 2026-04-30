"""Pipeline orchestration glue.

Modules:
    runner       — run_pipeline() main loop and kick_off_render_bg.
    resume       — --from-step bootstrap from prior runs and artifact copying.
    cost_table   — pretty-printed per-model cost breakdown for the ai_costs step.
"""
