@AGENTS.md

The file above is the project schema; treat it as loaded. Two additions for coding sessions:

- Never write to `knowledge/concepts/` directly; concept promotion goes through `scripts/compile.py` and its filing gate. Write only to your own `knowledge/agents/<role>/` subdirectory.
- Run `python -m pytest hooks/ scripts/` before committing changes to hooks or scripts; both trees have test coverage that must stay green.
