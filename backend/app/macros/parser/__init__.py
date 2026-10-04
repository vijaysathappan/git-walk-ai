"""VBA statement + expression tokenizer, AST, and recursive-descent parser
for the subset of the language Virtual Run can reason about (see
static_gate.py and sql_lane.py). Anything outside this subset is a parse
error, caught by the caller and reported as BLOCKED_UNSUPPORTED -- never
silently approximated.
"""
