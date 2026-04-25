"""Pipeline step bodies, grouped by phase.

Each module exposes one ``step_NN_xxx(ctx)`` function per pipeline step.
``xscore.shared.pipeline_steps.wire_step_fns`` imports these modules and binds
each step's ``fn`` field in the ``STEPS`` registry.
"""
