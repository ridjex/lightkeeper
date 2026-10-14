# Contributing

- Python 3.11+. The core uses the standard library only; Rich and Textual appear only under `lightkeeper/ui/`.
- `make test` (or `python3 -m unittest discover -s tests`) must pass. Tests never call GitHub or a model.
- Code, tests and README change in the same commit. Record why a non-obvious choice was made in `docs/decisions.md`.
- Keep it simple and transparent: the model proposes, the code decides what may be posted, the journal records everything.
