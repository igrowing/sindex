# AGENTS.md - Project Context & Operational Rules for AI Agents

This file provides non-negotiable architectural context, programming standards, and verification workflows for autonomous agents working on this Python codebase (`sindex`, a Windows service that full-text indexes local files). Read and internalize this file entirely before executing any tasks. `README.md` explains what the project does and how to run it; this file explains how to change it.

---

## 1. Project Overview & Architecture Blueprint

`sindex` targets **Windows 11 only** and Python 3.12+. Its defining requirement is **minimal CPU/RAM (and SSD-write) usage, even at the price of slower indexing**. Every change is judged against that first.

### Module Layer Convention

Do not mix parsing, storage, and presentation. The boundaries that must hold:
* `sindex/extract/` and `sindex/textnorm.py` : Pure content handling. `extract/*` turns a binary stream into text pieces (generators) and raises `ExtractError` / `NotText`; `textnorm` is pure text-in/text-out. Neither may import `db`, `search`, `scanner`, `indexer`, `service`, `web`, `control`, or `cli` (`extract` may use `config` and `throttle`).
* `sindex/config.py`, `filters.py`, `throttle.py`, `usn.py`, `scanner.py` : Configuration, path rules, resource control, NTFS change journal, directory walking.
* `sindex/db.py`, `sindex/search.py`, `sindex/control.py` : Schema and connection settings, query building, and read-only statistics. SQL lives in these files plus `scanner.py` and `indexer.py`. The web layer contains no SQL of its own.
* `sindex/indexer.py` : The indexer process is the **only writer** of the database (it drives `scanner.py` and the `db.py` helpers). It runs as a child process that the supervisor recycles; all work is persisted in the `queue` table so a restart loses nothing.
* `sindex/service.py`, `sindex/web/`, `sindex/cli.py` : Supervisor (never parses files), local web UI (read-only DB connection; controls the indexer only through files: `paused`, `requests/`, `status.json`), and entry points.

### Core Design Patterns
* **Explicit dependencies:** Pass `Config` (and connections, throttles) as arguments. No module-level singletons or mutable globals; the one exception is `throttle._job_handle`, which must stay alive for the whole process. Construct long-lived objects only at entry points (`cli.py`, `service.py`, `indexer.main`).
* **Windows isolation:** `ctypes`/pywin32 code lives only in `throttle.py`, `usn.py`, `service.py`, and the service commands of `cli.py`. Import pywin32 lazily where it is not always needed. Do not add cross-platform shims, and do not scatter `sys.platform` checks through business logic.
* **Streaming and bounded memory:** Never read a whole file, archive member, or result set into memory when it can be streamed (64 KB blocks, incremental decoders, `iterparse`/expat, one PDF page at a time, one directory at a time). New extractors must cooperate with `Throttle` (`checkpoint()` / `consume()`).
* **One writer, batched writes:** Only `indexer.py` writes to `index.db`. The web layer reads through `db.connect(..., readonly=True)` and controls the indexer only through files. See the write-batching notes in `indexer.py` and the README before touching any write path.
* **Text goes through `textnorm`:** Everything stored in the FTS index goes through `textnorm.normalize()`, and every user query goes through `search.build_match()`, or matching silently breaks (CJK bigrams, Greek/Hebrew/Arabic marks).

---

## 2. Non-Negotiable Coding Standards & Modern Syntax

### Anti-Hallucination & Modern API Rules
Agents must use current Python 3.12+ patterns. Do not copy legacy (Python 3.8 era) examples found in general training sets:
* **Typing:** Use `X | None`, `list[str]`, `dict[str, int]`; never `typing.Optional/List/Dict`. Keep `from __future__ import annotations` at the top of modules, as the existing files do.
* **Standard library first:** Prefer `tomllib`, `pathlib`, `dataclasses`, f-strings, `zip(..., strict=True)`. Use `pathlib` for configuration and CLI paths; use `os.scandir`/`os.path` in hot loops such as the scanner. Do not use `datetime.utcnow()`. Do not add a third-party dependency for something the standard library does; this project is judged on resident footprint.
* **Text encodings:** Always pass `encoding=` to `open()`, `read_text()`, `write_text()`. The Windows default is cp1252 and silently corrupts non-Latin text. Files being indexed are opened in binary mode. Console output of non-ASCII text needs UTF-8 (`cli.py` reconfigures stdout; run scripts with `PYTHONIOENCODING=utf-8`).
* **Invisible characters:** Write private-use, combining, zero-width, and other invisible characters in source as `\uXXXX` escapes, never as literals (a literal `U+E000` is unreadable and unreviewable). If a test needs lone surrogates, build them with `chr(0xD83D)`.
* **SQL:** Parameterized queries only. An f-string is acceptable only for a `PRAGMA` value that was cast with `int()`. FTS5 `MATCH` strings are built only by `search.build_match()`.
* **ctypes:** Declare `argtypes` and `restype` for every foreign function (handles are 64-bit), check return values, and report `ctypes.get_last_error()`.
* **Value objects:** Use `@dataclass` (with `slots=True`; `frozen=True` unless mutation is intended) rather than dicts or tuples that cross module boundaries. `Config` is mutable on purpose because tests override it.

### Code Style & Formatting Constraints
* **Linting:** `ruff check sindex tests` must pass with the rules configured in `pyproject.toml`. Do not weaken that configuration to make code pass.
* **Formatting:** Lines must not exceed **120 characters**. No auto-formatter is enforced: match the surrounding style and never reformat a whole file you are not otherwise changing.
* **Type hints:** Annotate all new or changed function signatures. No type checker is configured, so do not claim that types are verified.
* **Dependencies:** Declare them in `pyproject.toml` with a lower bound. Keep `dependencies` and each `optional-dependencies` list sorted alphabetically.
* **Versions:** Automatically increase the patch number (the last segment of `X.Y.Z`) of `version` in `pyproject.toml` on every commit. That is the only place the version is stored; do not add a `__version__` constant (read it with `importlib.metadata.version("sindex")` if code ever needs it). Change the major or minor number only when explicitly requested by the developer.

### Lint Hygiene Rules (zero-warning policy)

`ruff check sindex tests` must return **zero issues** before any commit. When in doubt, run `ruff check --fix` first (it sorts imports and modernizes syntax), then resolve the remaining issues manually.

**Import ordering (I)**
* Standard library, then third-party, then `sindex`, each group alphabetical. `ruff check --fix` sorts them.

**Boolean parameters (FBT001, FBT002)**
* Never use positional `bool` parameters. Make them keyword-only: `def save(*, enabled: bool)` not `def save(enabled: bool)`. Update all call sites: `save(enabled=True)`.

**Blind exceptions (BLE001)**
* No bare `except:` and no blind `except Exception` / `except BaseException` unless the handler re-raises (for example after a transaction rollback) or logs with `log.exception(...)`. See section 3.

**Debug output (T20)**
* No `print()` outside `cli.py`, where it is the user-facing output. Everywhere else use `log = logging.getLogger(__name__)`.

**Resources and threads**
* Open files, connections, and archives with `with`. Every `Popen` is waited on or terminated. Every thread is either joined or `daemon=True` with a comment saying why that is safe.

**Asserts**
* In production code `assert` is only for internal invariants and must carry a message: `assert seq >= 0, "negative chunk sequence"`. Never use `assert` to validate input (it disappears under `-O`); raise a typed exception instead. Plain asserts are fine in tests.

**Suppression directives (document_ignores)**
* Prefer fixing over suppressing. Every `# noqa: CODE` must list the specific code and have a `#` comment on the line immediately above explaining why the suppression is intentional. No blanket `# noqa`.

**Comments and docstrings**
* Comment the non-obvious *why*: Windows quirks, measured trade-offs, invariants. Do not write docstrings or comments that merely restate the code. When a comment states a measured result, keep the number accurate or drop it.

**Adjacent strings**
* When splitting a string across adjacent literals, ensure the join point has whitespace. Do not split at a hyphen.

**Deprecated APIs (DeprecationWarning)**
* `pytest` treats `DeprecationWarning` as an error (`filterwarnings` in `pyproject.toml`). Use the current replacement API; never add a warning filter or suppression to production code.

**Unused code (F401, F841)**
* Remove any unused import, variable, or function. Do not rename an unused variable to `_name` to hide it (a bare `_` for an unused unpacked value is fine).

---

## 3. Strict Error Handling & Failure Protocols
* **No Silent Swallowing:** Never write `except: pass` or `except Exception: pass`. Catch the narrowest exception type, and either handle it, log it (`log.debug` at least), or add a comment saying why ignoring it is safe. Broad handlers are allowed only when they re-raise (transaction rollback in `indexer.py` and `scanner.py`) or log with `log.exception` (`Indexer.index_file` records a parser bug as `status='error'`, so one bad file never stops the indexer).
* **Typed failures at boundaries:** Translate third-party errors into the project's exceptions (`ExtractError`, `NotText`, `JournalError`, `Stopped`) where they enter our code, as `extract/pdf.py` and `extract/office.py` do. Do not leak `pypdf`/`expat`/`zipfile` exceptions upward.
* **Explicit Failure Over Default Data:** When parsing files, configuration, or Windows API results, **never fall back to implicit placeholder or default values** (an empty string, an empty result, `time.time()`) if validation fails. Raise, or record an explicit status such as `error`, `binary`, `too_big`. An unreadable document must never be stored as "indexed, empty". It is better to fail loudly and traceably than to hold corrupted baseline data.
* **Unit test per bug:** On every reported bug, create at least one (or more, as needed) unit test first to confirm the bug (expect the unit test to fail). On the bug fix finish, verify and confirm with the newly written unit test that the bug is fixed.

---

## 4. Operational Steps & Verification Loop

When assigned a development ticket or bug fix, you must execute the task according to this chronological checklist. Do not skip steps.

### Step 1: Establish Environment Baseline
Before changing a single line of production code:
A. If the repository has a git remote, pull the current changes first; in team coding we must take peer changes into account. (If there is no repository or remote yet, skip this.)
B. Verify the codebase is in a functional state to isolate future errors:
  1. Use the project virtual environment (`.venv`). If it is missing: `py -3.12 -m venv .venv`. Then `.venv\Scripts\python -m pip install -e ".[dev]"`.
  2. Run `.venv\Scripts\ruff check sindex tests` to guarantee the starting code is lint-clean.
  3. Run `.venv\Scripts\python -m pytest` to ensure existing tests pass. The two tests in `tests/test_usn.py` skip unless the shell is elevated; if you change `usn.py` or the journal handling in `indexer.py`, ask the user to run them from an administrator prompt.

### Step 2: Implement Changes
1. Make your modifications in the module layer where they belong (section 1).
2. On changes in production code follow the rules:
  * Add unit tests for added functions and classes.
  * On removal of obsolete, unused, or merged functions, remove the corresponding unit tests.
  * On changes in functions, adapt the related unit tests accordingly.
3. **Don't repeat yourself:** Before implementing any functionality, review existing modules for similar functionality; often what is needed already exists, so just call and reuse it. If a slight modification to an existing function is needed and it has only 1-2 call sites, enhance that function and adapt the call sites. If a significant modification is needed, or there are many call sites, add a function with the larger functional span and let the older, narrower one call it. Add a new function only if nothing similar exists.
4. **Resource budget (for anything touching indexing, extraction, or storage):** memory stays bounded (stream, do not load), work goes through `Throttle`, and write volume is not increased by accident. If you change how or when the database is written, measure it instead of assuming, for example by comparing `psutil.Process().io_counters().write_bytes` before and after indexing the same folder (`psutil` is not a project dependency; install it in the venv for measurement only). Do not put `SAVEPOINT`s around each file, and keep write batches within the SQLite page cache (see the README section on batched writes).

### Step 3: Self-Correction & Automated Cleaning
Before presenting your changes for human review, clean your workspace of transient tracking code:
* Review your code diff and verify that NO `print()` statements remain outside `cli.py`. Use `logging` if logging is necessary.
* Remove unused imports, leftover temporary code, and commented-out code.
* Keep scratch scripts and experiment data outside the repository tree. Tests and experiments must use a temporary data directory (the `cfg` fixture, or `--data-dir <tmp>`), never `%ProgramData%\sindex`, which belongs to the installed service.

### Step 4: Verification Testing Pipeline

Confirm your adjustments have not created regression breakages elsewhere:
1. Run `.venv\Scripts\ruff check sindex tests` to ensure the new code satisfies the lint rules.
2. Run the tests for the module you modified, then the whole suite:

`.venv\Scripts\python -m pytest tests\test_<module>.py`

`.venv\Scripts\python -m pytest`

3. For changes to the service, indexer loop, or web UI, also run it end to end against a temporary data directory with a non-default port, so it cannot clash with an installed service on 8765. Then check `/api/status` and a search:

`.venv\Scripts\sindex --data-dir <tmp> run --root <small test folder>`

Create `<tmp>\config.toml` first with a `[web]` section (`web_port = 8799`, `token = "test"`); otherwise the default port 8765 is used.

4. Do not run `sindex install`, `remove`, `start`, or `stop` unless the user explicitly asks; they change the machine's services and need administrator rights.

If errors are flagged, read the stack trace, apply a corrective fix, and re-run the verification suite until the console output reports a clean, successful completion.
