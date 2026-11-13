"""Langfuse as the results store: `projection` turns a run directory into what is recorded (pure),
`sink` writes and reads it, `upload` holds a finished run until Langfuse provably has all of it,
`rows` reads a run directory back, and `compare` judges regressions between recorded runs.
"""
