# ldm — Linux download manager (v1, Python)

Working name. Design: [../notes/10-linux-design.md](../notes/10-linux-design.md).

**Milestone 1 (this):** command-line engine — multi-part HTTP(S) downloads into one pre-allocated
file, dynamic segmentation, pause (Ctrl-C) and resume, retries, changed-file detection, SQLite state.

```sh
uv venv .venv && uv pip install -e '.[dev]'
.venv/bin/ldm add https://example.com/file.iso -c 8        # Ctrl-C pauses
.venv/bin/ldm resume 1
.venv/bin/ldm list
.venv/bin/python -m pytest -q                               # tests use a local server only
```

State lives in `$XDG_DATA_HOME/ldm/ldm.db` (`--db` to override); downloads go to `~/Downloads`
(`-o` to override). While downloading, data is in `<name>.<id>.part` next to the target.
