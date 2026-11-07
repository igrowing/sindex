# sindex architecture

`sindex` is a Windows 11 service that full-text indexes local files and serves a local search page. Every design
decision is judged against one requirement: **minimal CPU, RAM and SSD writes, even if indexing gets slower.**
`README.md` explains what it does and how to run it; `AGENTS.md` explains how to change it. This file explains how
the pieces fit together.

The diagrams are [Mermaid](https://mermaid.js.org/) and render on GitHub and in VS Code's Markdown preview.

## Contents

1. [Process view](#1-process-view)
2. [Module layers](#2-module-layers)
3. [Class diagram](#3-class-diagram)
4. [Database schema](#4-database-schema)
5. [Indexing pipeline](#5-indexing-pipeline-sequence)
6. [Change detection](#6-change-detection)
7. [Search flow](#7-search-flow-sequence)
8. [Control plane (web UI to indexer)](#8-control-plane)
9. [Indexer lifecycle](#9-indexer-lifecycle-state-diagram)
10. [Resource control](#10-resource-control)
11. [Design rules that the diagrams rely on](#11-design-rules-that-the-diagrams-rely-on)
12. [Why not FastAPI and asyncio](#12-why-not-fastapi-and-asyncio)
13. [Security model](#13-security-model)

---

## 1. Process view

At run time sindex is a small tree of OS processes. Only the **indexer** writes the database; only the **service**
talks to the network; only the **extraction workers** parse untrusted file content.

```mermaid
flowchart TB
    SCM["Windows Service Control Manager"]
    CLI["sindex CLI<br/>(cli.py)"]
    Browser["Browser / Explorer search connector"]

    subgraph SVC["Service process: sindex.exe service (LocalSystem)"]
        direction TB
        Sup["Supervisor<br/>spawns, restarts, applies settings"]
        Web["WebServer<br/>(ThreadingHTTPServer, 127.0.0.1)"]
    end

    subgraph IDX["Indexer process (child, recycled): IDLE priority, Job Object 256 MB / 10% CPU"]
        direction TB
        Ind["Indexer<br/>the ONLY database writer"]
        Pool["ProcessPool"]
    end

    subgraph WRK["Extraction workers x extract_workers (default 3)"]
        W1["worker 1"]
        W2["worker 2"]
        W3["worker n"]
    end

    DB[("index.db<br/>SQLite WAL + FTS5")]
    Files["Control files in the data dir<br/>paused, requests/, settings-requests/, status.json"]
    Cfg["config.toml"]
    FS[("NTFS files + USN journal")]

    SCM -->|start / stop| Sup
    CLI -->|"install / run / index"| SCM
    Browser -->|HTTP| Web
    Sup -->|"Popen + stdin pipe (EOF = stop)"| Ind
    Ind -->|"spawn, pipes: Task out, Chunk and Result back"| Pool
    Pool --- W1
    Pool --- W2
    Pool --- W3

    Ind -->|"read + write"| DB
    Web -.->|"read-only"| DB
    CLI -.->|"read-only (search, status)"| DB
    Web -->|"writes requests"| Files
    Ind -->|"reads requests, writes status"| Files
    Sup -->|"writes"| Cfg
    Web -.->|"reads"| Files
    W1 & W2 & W3 -->|"stream 64 KB blocks"| FS
    Ind -->|"scandir, FSCTL_READ_USN_JOURNAL"| FS
```

Why this shape:

| Decision | Reason |
|---|---|
| Parsing is in a child process, not the service | Only a new process returns memory to Windows. The supervisor recycles the indexer every `recycle_after_files` files or when it grows past `recycle_rss_mb`. |
| Parsing is in worker processes, not the indexer | One Python process can use one core at most, and PDF parsing is where the time goes. The indexer stays the single writer, so workers send text back over pipes. |
| `stdin` pipe from supervisor to indexer | EOF on stdin means "finish the current file and exit". A killed service still ends the indexer, and the indexer ends the workers. |
| Web UI controls the indexer through files | The web layer needs no write access to the database and no IPC protocol. |

---

## 2. Module layers

Arrows mean "may import". The layering is enforced by convention and by review (see `AGENTS.md` section 1).

```mermaid
flowchart TB
    subgraph L5["Entry points"]
        cli["cli.py"]
        service["service.py"]
        webs["web/server.py + static/"]
    end

    subgraph L4["Orchestration"]
        indexer["indexer.py<br/>(sole writer)"]
    end

    subgraph L3["Storage, query, read-only control"]
        db["db.py"]
        search["search.py"]
        control["control.py"]
        scanner["scanner.py"]
    end

    subgraph L2["Rules and OS access"]
        filters["filters.py"]
        usn["usn.py (ctypes)"]
        throttle["throttle.py (ctypes)"]
        config["config.py"]
    end

    subgraph L1["Pure content handling"]
        extract["extract/*<br/>text, office, pdf, rtf,<br/>image_meta, video_meta,<br/>job, pool"]
        textnorm["textnorm.py"]
    end

    cli --> service
    cli --> indexer
    cli --> search
    cli --> control
    service --> indexer
    service --> webs
    service --> control
    webs --> search
    webs --> control
    webs --> db
    indexer --> scanner
    indexer --> db
    indexer --> control
    indexer --> extract
    indexer --> usn
    indexer --> filters
    indexer --> throttle
    scanner --> db
    scanner --> filters
    search --> db
    search --> textnorm
    control --> db
    db --> usn
    db --> textnorm
    filters --> config
    extract --> config
    extract --> throttle
    extract --> textnorm
```

Hard boundaries:

* `extract/*` and `textnorm.py` never import `db`, `search`, `scanner`, `indexer`, `service`, `web`, `control` or
  `cli`. `extract` may use `config` and `throttle`.
* SQL lives only in `db.py`, `search.py`, `control.py`, `scanner.py` and `indexer.py`. The web layer has none.
* `ctypes` and pywin32 code lives only in `throttle.py`, `usn.py`, `service.py` and the service commands of `cli.py`.
* No module-level singletons. `Config`, connections and `Throttle` are passed as arguments and constructed at entry
  points (`cli.py`, `service.py`, `indexer.main`).

### Module responsibilities

| Module | Responsibility |
|---|---|
| `config.py` | `Config` dataclass, TOML load / validate / render / apply, `Spec`, logging setup. |
| `filters.py` | `PathFilter`: roots, exclusion globs, hidden files, `file_kind()`. |
| `scanner.py` | One-directory-at-a-time walk that reconciles a subtree with the `files` table and fills `queue`. |
| `usn.py` | NTFS change journal (`Journal`, `Change`, `JournalError`), drive discovery. |
| `throttle.py` | `Throttle` (duty cycle, pause rules, read rate), `ThrottledReader`, Job Object and priority setup. |
| `indexer.py` | The indexer process: queue loop, write batching, volumes, requests, maintenance, status file. |
| `extract/__init__.py` | `kind_of`, `max_bytes`, `Context`, `MediaFields`, dispatch `extract()`. |
| `extract/job.py` | `Task`, `Chunk`, `Result` messages and `run_job()`, which turns a file into normalized chunks. |
| `extract/pool.py` | `InlineRunner` and `ProcessPool`, behind one interface. |
| `textnorm.py` | Case/accent folding, CJK bigrams, Greek/Hebrew/Arabic marks, chunking. |
| `db.py` | Schema, `connect()`, queue and media helpers, location check. |
| `search.py` | Query language, `build_match()`, `search()`, snippets. |
| `control.py` | Control-file writers (pause, requests, settings), status and statistics readers, log reader. |
| `service.py` | `Supervisor`, `SindexService` (pywin32), install helpers, `run_core`. |
| `web/server.py` | HTTP routes, token and Host checks, `SearchRegistry` for cancelling superseded searches. |
| `cli.py` | Subcommands, shortcuts, context menu, App Paths, Explorer search connector, elevation. |

---

## 3. Class diagram

The main types and who owns whom. Dataclasses are marked `<<dataclass>>`.

```mermaid
classDiagram
    direction LR

    class Config {
        <<dataclass>>
        +Path data_dir
        +list roots
        +float duty_factor
        +int extract_workers
        +int sqlite_cache_mb
        +db_path() Path
        +text_exts() frozenset
    }

    class PathFilter {
        +list roots
        +root_of(path) str
        +skip_dir_entry() bool
        +skip_file_entry() bool
        +dir_excluded(path) bool
        +file_kind(path) str
    }

    class Throttle {
        +checkpoint()
        +consume(nbytes)
        +sleep(seconds)
        +pause_reason() str
        +wait_while_paused()
    }

    class ThrottledReader {
        +read(n) bytes
        +readinto(buf) int
    }

    class Supervisor {
        +Popen proc
        +int restarts
        +run()
        -_spawn() Popen
        -_terminate(proc)
        -_apply_settings() bool
    }

    class WebServer {
        +start()
        +stop()
    }

    class SearchRegistry {
        +begin(tab, seq, timeout) SearchTicket
        +end(tab, ticket)
    }

    class SearchTicket {
        +stopped() bool
        +should_stop() int
    }

    class Indexer {
        +Connection conn
        +dict jobs
        +run() int
        -_loop() int
        -_process_queue() bool
        -_poll_journals() bool
        -_full_scans() bool
        -_commit()
        -_prepare_file()
        -_on_message(msg)
    }

    class Volume {
        <<dataclass>>
        +str letter
        +list roots
        +int journal_id
        +int next_usn
        +bool needs_scan
    }

    class Journal {
        +query() tuple
        +read(start_usn, journal_id, wanted) Iterator
        +close()
    }

    class Change {
        <<dataclass>>
        +str path
        +bool is_dir
        +int reason
    }

    class Runner {
        <<interface>>
        +has_slot() bool
        +submit(task)
        +poll(timeout) list
        +close()
        +wants_recycle bool
    }

    class InlineRunner
    class ProcessPool
    class Worker {
        <<dataclass>>
        +Process process
        +Connection conn
        +int job
    }

    class Task {
        <<dataclass>>
        +int job
        +str path
        +str kind
        +int max_chunks
    }

    class Chunk {
        <<dataclass>>
        +int job
        +str text
    }

    class Result {
        <<dataclass>>
        +int job
        +str status
        +str error
        +MediaFields media
    }

    class MediaFields {
        <<dataclass>>
        +str kind
        +int width
        +int height
        +str camera
    }

    class Context {
        +Throttle throttle
        +str encoding
        +MediaFields media
    }

    class ExtractError
    class NotText

    Supervisor --> Config
    Supervisor ..> Indexer : spawns as child process
    WebServer --> Supervisor : reads state
    WebServer --> SearchRegistry
    SearchRegistry o-- SearchTicket

    Indexer --> Config
    Indexer *-- PathFilter
    Indexer *-- Throttle
    Indexer *-- Runner
    Indexer o-- Volume
    Volume o-- Journal
    Journal ..> Change : yields

    Runner <|.. InlineRunner
    Runner <|.. ProcessPool
    ProcessPool o-- Worker
    Indexer ..> Task : submits
    Chunk ..> Indexer : stored by
    Result ..> Indexer : closes a job
    InlineRunner ..> Result
    ProcessPool ..> Chunk
    Worker ..> Context : run_job()
    Context --> Throttle
    Context --> MediaFields
    Result --> MediaFields
    Throttle ..> ThrottledReader : paces
    Context ..> ExtractError : may raise
    Context ..> NotText : may raise
```

---

## 4. Database schema

One SQLite file (`index.db`) in WAL mode. `chunks` is an FTS5 table; a file's chunks occupy a reserved rowid range, so
no separate mapping table is needed: `file_id = chunks.rowid >> 16`.

```mermaid
erDiagram
    files ||--o{ chunks : "rowid >> CHUNK_BITS = id"
    files ||--o| media : "file_id"
    files {
        INTEGER id PK
        TEXT dir "NOCASE"
        TEXT name "NOCASE, UNIQUE with dir"
        INTEGER size
        INTEGER mtime_ns
        TEXT status "indexing, ok, truncated, empty, binary, too_big, error"
        TEXT encoding
        TEXT error
        REAL indexed_at
    }
    chunks {
        INTEGER rowid "file_id << 16 + seq"
        TEXT body "FTS5, normalized text"
        TEXT source "UNINDEXED: text, meta, ocr"
    }
    media {
        INTEGER file_id PK
        TEXT kind
        INTEGER width
        INTEGER height
        REAL duration_s
        TEXT taken_at
        TEXT camera
        REAL lat
        REAL lon
        TEXT codec
    }
    queue {
        TEXT path PK "with stage"
        TEXT stage PK "dir, text, meta, ocr"
        INTEGER priority "lower runs first"
        TEXT reason "scan, usn, force"
        REAL enqueued_at
    }
    volumes {
        TEXT letter PK
        TEXT roots "JSON list"
        TEXT fs_type
        INTEGER journal_id
        INTEGER next_usn
        REAL last_full_scan
    }
    meta {
        TEXT key PK
        TEXT value "JSON: rules, maintenance_pending, last_maintenance"
    }
```

`queue`, `volumes` and `meta` carry the indexer's persistent state, which is why a recycle or crash loses nothing.
`ocr_*` columns and the `ocr` stage are reserved for a later phase.

**Queue priorities** (lower number first): forced reindex 1, directory scan 5, easy text 10, photo/video metadata 12,
hard formats (PDF, Office, RTF) 15. Cheap files become searchable first.

---

## 5. Indexing pipeline (sequence)

How one queued file becomes searchable text. The indexer never parses; it prepares the file, hands a `Task` to a
runner, and stores the `Chunk` messages that come back inside one long transaction.

```mermaid
sequenceDiagram
    autonumber
    participant Q as queue table
    participant I as Indexer (single writer)
    participant R as Runner (ProcessPool)
    participant W as Worker process
    participant X as extract(...)
    participant D as index.db

    I->>Q: SELECT ... ORDER BY priority, enqueued_at LIMIT n
    Q-->>I: path, stage, reason
    I->>I: throttle.checkpoint() (duty cycle, pause rules)
    alt stage is dir
        I->>I: scanner.scan(top) - queue changed files
    else stage is text or meta
        I->>D: BEGIN (if no open batch)
        I->>I: _prepare_file: stat, unchanged? too big? gone?
        I->>D: INSERT/UPSERT files row (status = indexing)
        I->>D: DELETE old chunks of this source
        I->>R: submit(Task(job, path, kind, max_chunks))
        R->>W: Task over pipe
        W->>X: extract(path, kind, ctx)
        loop one piece at a time
            X-->>W: text piece (64 KB reads, one PDF page, ...)
            W->>W: textnorm.chunks() then normalize()
            W-->>R: Chunk(job, text)
            R-->>I: Chunk
            I->>D: INSERT INTO chunks(rowid, body, source)
        end
        W-->>R: Result(job, status, error, encoding, media)
        R-->>I: Result
        I->>D: UPDATE files SET status, encoding, error
        opt image or video
            I->>D: upsert media row
        end
        I->>Q: DELETE queue row (same transaction)
    end
    Note over I,D: COMMIT when the batch reaches 200 files, 60 s,<br/>about sqlite_cache_mb/2 MB of text, or when idle, paused, recycling or stopping.<br/>A commit first waits for files in flight (_settle), so a half file is never visible.
```

Failure handling inside this flow:

* `run_job()` turns every failure into a `Result` with an explicit status: `binary` (`NotText`), `error`
  (`ExtractError`, `OSError`, `MemoryError`, or a logged parser bug), `empty`, `truncated`, `too_big`. An unreadable
  document is never stored as "indexed, empty".
* If a worker dies, only its file is recorded as `error` and the worker is replaced.
* On stop, `_abandon_jobs()` deletes the half-indexed `files` rows. Their queue rows are still present, so they are
  redone. There is deliberately no `SAVEPOINT` per file: FTS5 would write a new segment on every savepoint.

### Extractors

```mermaid
flowchart LR
    ext["extract(path, kind, ctx)"] --> open["_open: ThrottledReader over a binary file"]
    open --> k{kind}
    k -->|text| T["text.py<br/>BOM, UTF-8/16, cp125x, CJK sets<br/>HTML/XML via expat"]
    k -->|ooxml| O1["office.py: docx, xlsx, pptx<br/>zipfile + expat iterparse"]
    k -->|odf| O2["office.py: odt, ods, odp"]
    k -->|rtf| R["rtf.py"]
    k -->|pdf| P["pdf.py: pypdf, one page at a time"]
    k -->|image| IM["image_meta.py: EXIF, XMP, IPTC headers"]
    k -->|video| V["video_meta.py: pymediainfo container tags"]
    T & O1 & O2 & R & P & IM & V --> out["Iterator of str pieces"]
    out --> chunks["textnorm.chunks + normalize"]
```

`kind_of(ext, cfg)` decides the kind from the extension and the configuration. Files without a configured extension
are not indexed, and media kinds are only produced when `index_media_metadata` is on.

---

## 6. Change detection

Two mechanisms feed the same `queue` table. Everything downstream (the pipeline in section 5) is identical.

```mermaid
flowchart TD
    start(["Indexer._loop iteration"]) --> fs{"Volume needs a full scan?<br/>(first run, rules changed,<br/>journal wrapped, or no journal<br/>and rescan_hours passed)"}
    fs -->|yes| scan["scanner.scan(root)<br/>os.scandir, one directory at a time<br/>compare size + mtime_ns with files table"]
    scan --> enq["db.enqueue(path, stage, priority, reason)"]
    fs -->|no| poll{"Volume has a USN journal<br/>and usn_poll_seconds elapsed?"}
    poll -->|yes| q["Journal.query(): id, lowest, next USN"]
    q --> wrap{"journal id changed or<br/>next_usn below lowest?"}
    wrap -->|yes| flag["needs_scan = True"]
    wrap -->|no| read["Journal.read(): changes since next_usn<br/>filtered by _wanted()"]
    read --> chg["_on_change: skip excluded parents<br/>dir change: stage dir<br/>file change: stage from queue_target()"]
    chg --> enq
    chg --> save["save next_usn in the same transaction"]
    poll -->|no| qproc
    enq --> qproc["_process_queue()"]
    flag --> start
    save --> qproc
```

* The journal position is recorded **before** a full scan starts, so a change made during the scan is not missed.
* Position and queued changes are saved in one transaction.
* Reading the journal needs administrator rights. Without them, or on a volume without a journal, the indexer falls
  back to a periodic rescan (`rescan_hours`).
* A deleted or newly excluded file is queued too; `_prepare_file` then finds it missing or filtered and removes its
  rows.
* When indexing rules change (`exclude_globs`, extensions, hidden files, size limits, media switch), the new rules
  are stored in `meta` only after the rescan they cause has finished, so an interrupted rescan restarts.

---

## 7. Search flow (sequence)

Search is read-only and runs on web server threads. The query always passes through `search.build_match()` and the
index text through `textnorm.normalize()`, so both sides fold case, accents and CJK bigrams identically.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant B as Browser (index.html)
    participant H as web _Handler
    participant S as SearchRegistry
    participant Q as search.search()
    participant D as index.db (read-only)

    U->>B: types
    B->>H: GET /api/search?q=...&quick=1&tab=T&seq=n (after 0.2 s)
    H->>H: Host header check (DNS-rebinding guard)
    H->>S: begin(tab, seq, 30 s)
    Note over S: a newer seq of the same tab<br/>cancels the older ticket
    H->>D: db.connect(readonly=True)
    H->>Q: search(conn, query, hit_limit=200)
    Q->>Q: _parse + _to_match (FTS5 MATCH string)
    Q->>D: chunks MATCH ? JOIN files ORDER BY rank LIMIT
    Note over D,S: SQLite progress handler polls ticket.should_stop()
    D-->>Q: rowids
    Q->>D: snippet() for the page of results
    Q-->>H: results, total, capped
    H->>S: end(tab, ticket)
    H-->>B: JSON
    B->>H: GET /api/search?... (full limit 2000, after search_complete_after_s)
    H-->>B: JSON (replaces the quick result)
```

Search features, all in `search.py`:

* Words, phrases, prefixes, OR groups and negation, built into one FTS5 `MATCH` string.
* Optional `names` mode that also scans folder and file names (a table scan, so opt-in).
* Filters: path, extensions, modified date range, and the media filters (camera, GPS, size, duration, date taken).
  With media filters and no words it browses by filter alone, newest first.
* At most two searches run at once; a search over 30 s is stopped.

The same query language is exposed three more ways: `sindex search` (CLI), `/api/search.rss` (Explorer search
connector, described by `/opensearch.xml`), and the `sindex <words>` shortcut through the registry's App Paths entry.

### HTTP surface

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/`, `/settings`, `/log` and `/style.css` | GET | none | Static pages, with `{{TOKEN}}` and `{{VERSION}}` substituted |
| `/api/search`, `/api/search.rss`, `/opensearch.xml` | GET | none | Search |
| `/api/export.csv` | GET | none | All files of the page's search as CSV (same parameters as `/api/search`) |
| `/api/status`, `/api/settings`, `/api/health` | GET | none | State, current settings, problem files |
| `/api/browse`, `/api/log` | GET | token | Folder picker, log viewer (they reveal local paths) |
| `/api/pause`, `/api/resume`, `/api/reindex`, `/api/retry`, `/api/rebuild`, `/api/maintenance`, `/api/settings`, `/api/open` | POST | token | Control actions |

All requests must carry a `Host` of `127.0.0.1`, `localhost` or `[::1]` with the configured port. POSTs are
serialized by a lock and verified with a constant-time token comparison.

---

## 8. Control plane

The web UI and CLI never call into the indexer. They drop small files into the data directory and the indexer (or the
supervisor, for settings) picks them up.

```mermaid
flowchart LR
    subgraph writers["Writers (web UI, CLI)"]
        wp["control.pause / resume"]
        wr["control.request_reindex / retry / rebuild / maintenance"]
        ws["control.request_settings"]
    end

    subgraph files["Data directory"]
        paused["paused (flag file)"]
        req["requests/*.json"]
        sreq["settings-requests/*.json"]
        cfgf["config.toml (+ .bak)"]
        stat["status.json"]
    end

    subgraph readers["Readers"]
        Ind["Indexer<br/>_take_requests every loop"]
        Sup["Supervisor<br/>_apply_settings every second"]
        UI["web /api/status<br/>control.indexer_status"]
    end

    wp --> paused
    wr --> req
    ws --> sreq

    paused --> Ind
    req --> Ind
    sreq --> Sup
    Sup -->|"config.apply(): validate, render, write"| cfgf
    Sup -->|"settings changed: stop and restart"| Ind
    cfgf -->|"read at indexer start"| Ind
    Ind -->|"at most every 10 s"| stat
    stat --> UI
```

| Request | Handled by | Effect |
|---|---|---|
| `paused` file | `Throttle` via `Indexer._user_pause` | Indexer commits and idles until the file is removed. Worker processes check the same flag. |
| `{"path": ...}` | `Indexer._handle_request` | Enqueue a forced `dir` or file entry. |
| `{"status": ...}` | `Indexer._retry_status` | Requeue every file that ended with that status. |
| `{"rebuild": true}` | `Indexer._handle_request` | Enqueue a forced scan of every root. |
| `{"maintenance": ...}` | `Indexer._run_maintenance` | `optimize` (FTS merge) or `compact` (VACUUM), run only when the queue is empty. |
| settings request | `Supervisor._apply_settings` | Validated by the web layer, written to `config.toml` by the service, then the indexer restarts. A rejected change leaves the file as it was and is shown in the status. |

The page validates before it writes a request, and the supervisor validates again, because `config.toml` may have been
edited by hand in between.

---

## 9. Indexer lifecycle (state diagram)

States follow `status.json` (`state`) plus the process-level outcomes the supervisor reacts to.

```mermaid
stateDiagram-v2
    [*] --> Starting
    Starting --> Error: db location invalid (EXIT_CONFIG 2)
    Starting --> Idle: db opened, rules checked, volumes loaded

    Idle --> Scanning: a volume needs a full scan,<br/>or a dir queue entry is due
    Scanning --> Indexing: files queued
    Idle --> Indexing: USN journal changes queued
    Indexing --> Indexing: next queued file
    Indexing --> Scanning: dir entry reached
    Indexing --> Idle: queue empty (commit, small FTS merge)
    Idle --> Maintenance: request pending and queue empty
    Maintenance --> Idle: optimize or compact finished

    Idle --> Paused: user flag, battery, outside schedule, system busy
    Indexing --> Paused
    Scanning --> Paused
    Paused --> Idle: pause reasons cleared (commit done first)

    Indexing --> Recycling: files_done >= recycle_after_files,<br/>RSS > recycle_rss_mb, or runner asks
    Recycling --> [*]: drain jobs, commit, exit 3 (EXIT_RECYCLE)

    Idle --> Stopped: stdin EOF / stop event
    Indexing --> Stopped: Stopped raised at a checkpoint
    Stopped --> [*]: abandon half files, commit, exit 0
```

The supervisor's view of the same process:

```mermaid
stateDiagram-v2
    [*] --> Spawning
    Spawning --> Watching: Popen ok
    Watching --> Spawning: exit 3 (recycle), restart at once
    Watching --> Restarting: settings applied, terminate and restart
    Restarting --> Spawning
    Watching --> Backoff: any other exit code
    Backoff --> Spawning: wait 30 s, doubling to 900 s;<br/>cut short by a settings change
    Backoff --> [*]: service stop
    Watching --> [*]: service stop, close stdin, wait 30 s, then taskkill /T
```

---

## 10. Resource control

Because the goal is a tiny footprint, throttling is part of the architecture rather than a tuning knob.

```mermaid
flowchart TB
    subgraph OS["Windows-level limits (throttle.py, set once per process)"]
        A["IDLE priority class"]
        B["Background mode: low I/O and memory priority"]
        C["EcoQoS (Efficiency mode)"]
        D["Job Object: 256 MB memory cap,<br/>10% CPU hard cap for indexer + workers"]
    end

    subgraph COOP["Cooperative pacing (Throttle)"]
        E["checkpoint(): after 0.1 s of work,<br/>sleep duty_factor x work"]
        F["consume(n): read_mb_per_sec budget,<br/>shared across workers"]
        G["pause rules: battery, CPU busy %,<br/>schedule windows, user flag"]
    end

    subgraph MEM["Bounded memory"]
        H["streaming: 64 KB blocks,<br/>incremental decoders, expat,<br/>one PDF page, one directory"]
        I["process recycling:<br/>indexer and workers"]
        J["SQLite: 8 MB page cache, no mmap"]
    end

    subgraph SSD["Bounded SSD writes"]
        K["one transaction per ~200 files or 60 s"]
        L["batch size capped to half the page cache"]
        M["no SAVEPOINT per file"]
        N["status.json at most every 10 s"]
    end

    GOAL(("minimal<br/>CPU, RAM,<br/>SSD wear"))
    OS --> GOAL
    COOP --> GOAL
    MEM --> GOAL
    SSD --> GOAL
```

Every extractor reads through `ThrottledReader`, which calls `Throttle.consume()` on each read, so pacing needs no
cooperation from the parser beyond using the reader it is given. `ProcessPool` divides `read_mb_per_sec` by the worker
count so the limit holds for the whole indexer.

---

## 11. Design rules that the diagrams rely on

1. **One writer.** Only `indexer.py` (with `scanner.py` and the `db.py` helpers it drives) writes `index.db`. The web
   layer and CLI open it with `db.connect(..., readonly=True)`.
2. **The queue is the source of truth for pending work.** Everything the indexer wants to do later is a row in
   `queue`, so recycling, crashes and restarts are safe.
3. **Never half a file.** A file's rows are visible only after its `Result` arrives and its batch commits. Commits wait
   for files in flight.
4. **Explicit failure over default data.** Extraction failures become a stored status (`error`, `binary`, `too_big`,
   `truncated`, `empty`), never an empty "indexed" file.
5. **Typed failures at boundaries.** Third-party exceptions (`pypdf`, `expat`, `zipfile`) become `ExtractError` or
   `NotText` in `extract/*`; the journal raises `JournalError`; throttling raises `Stopped`.
6. **Text goes through `textnorm`.** Stored text uses `normalize()`; queries use `search.build_match()`. Bypassing
   either silently breaks matching.
7. **Windows code is isolated.** `ctypes` and pywin32 stay in `throttle.py`, `usn.py`, `service.py` and the service
   commands of `cli.py`; there are no cross-platform shims.
8. **Dependencies are explicit.** `Config`, connections and throttles are passed as arguments; long-lived objects are
   built only at entry points.

### Packaging

`packaging/build.py` builds a PyInstaller **folder** (not a one-file executable, because a service must answer the
service control manager within seconds). `sindex.exe` is one binary with several roles, selected by subcommand:

```mermaid
flowchart LR
    exe["sindex.exe"] --> svc["service<br/>(started by SCM)"]
    exe --> idx["indexer<br/>(started by the service)"]
    exe --> wrk["multiprocessing spawn<br/>(parsing workers, via freeze_support)"]
    exe --> cmds["install, remove, start, stop,<br/>run, index, search, status,<br/>pause, resume, reindex, ui"]
    svc -->|Popen| idx
    idx -->|spawn| wrk
```

`sindex install` copies the program folder to `%ProgramFiles%\sindex`, registers the service (delayed automatic start,
LocalSystem), and creates the Desktop and Start menu shortcuts, the folder context-menu verb, the App Paths entry and
the Explorer search connector. `sindex remove` undoes all of them.

---

## 12. Why not FastAPI and asyncio

The web UI is `http.server.ThreadingHTTPServer` with a handler class, and the whole program uses threads and
processes. There is no `asyncio` and no web framework. This is deliberate. The reasons are below; they are
arguments from the design, not benchmark results.

```mermaid
flowchart TB
    subgraph chosen["Chosen: stdlib threaded server"]
        direction TB
        c1["HTTP request"] --> c2["handler thread (stdlib)"]
        c2 --> c3["sqlite3 read-only connection<br/>(blocking, in this thread)"]
        c3 --> c4["JSON response"]
    end
    subgraph rejected["Rejected: FastAPI + asyncio"]
        direction TB
        r1["HTTP request"] --> r2["uvicorn + starlette + pydantic<br/>(3rd-party, resident in the service)"]
        r2 --> r3["event loop"]
        r3 --> r4["run_in_executor / threadpool<br/>because sqlite3 blocks"]
        r4 --> r5["same sqlite3 call, in a thread anyway"]
    end
```

### 1. Footprint is the primary requirement

The project is judged on resident memory and CPU (`AGENTS.md`: "Do not add a third-party dependency for something
the standard library does; this project is judged on resident footprint"). The service process stays resident all
day. FastAPI would bring `starlette`, `pydantic` (with a compiled core) and an ASGI server such as `uvicorn`, and all
of them would be imported at service start and kept in memory. `http.server` is already in the standard library and
costs nothing extra. The same reasoning is behind `pypdf[cryptodome]` instead of the heavier `crypto` extra, noted in
`pyproject.toml`.

### 2. The work is blocking and local, so async buys nothing

`asyncio` helps when one process waits on many slow network connections. Here:

* The only client is a browser on `127.0.0.1`, with a handful of concurrent requests at most.
* The expensive part of a request is a SQLite query, and the `sqlite3` module is blocking. An async handler would
  have to push every query into a thread pool, so the code would run in a thread anyway, with an event loop added
  on top.
* SQLite can already run one query per connection per thread in parallel. The server opens one read-only
  connection per request and limits searches to two at a time (`SEARCH_SLOTS`), which is the real concurrency limit
  and is independent of the I/O model.

### 3. Cancelling a search needs a thread-level hook, not a task

A newer search in the same browser tab must cancel the older one, and a search over 30 s must stop
(`SearchRegistry`, `SearchTicket`). This works by a SQLite progress handler that polls `ticket.should_stop()` inside
the running query, and it stops the query at the SQLite level. `asyncio` task cancellation cannot interrupt a
blocking C call: it would cancel the `await`, leave the query running in the executor thread, and still hold a
connection and a slot. The thread-plus-progress-handler design is both simpler and correct.

### 4. Parallelism comes from processes, not from an event loop

The heavy work is parsing, mainly PDFs, and it is CPU-bound. Neither threads nor `asyncio` run Python code on several
cores at once. The architecture uses separate processes for that: the indexer child, and the `ProcessPool` of
extraction workers (section 1). The pool is already an explicit, bounded message-passing design: `has_slot()`,
`submit()`, `poll()` over `multiprocessing` pipes, where a worker blocks on `send` until the indexer reads. Replacing
it with coroutines would add nothing, and it would remove the process boundary that returns memory to Windows when a
process is recycled.

### 5. Throttling is synchronous by design

`Throttle.checkpoint()` and `consume()` implement the duty cycle, the read-rate limit and the pause rules by
**sleeping the calling thread** (`stop.wait()`), and they raise `Stopped` to unwind the stack. Every extractor is a
plain generator that calls into a `ThrottledReader`. Making that cooperative with `async`/`await` would turn every
extractor, the scanner and `textnorm` into coroutines, or would need a sync-to-async bridge at each read. Sleeping is
what we want here: a sleeping thread uses no CPU. In addition, `extract/*` and `textnorm.py` are meant to be pure and
framework-free (section 2); `async` would spread through them.

### 6. Windows service and Job Object constraints

The service host (`pywin32`'s `ServiceFramework`) is a blocking callback model: `SvcDoRun` runs until the service
ends, and `SvcStop` signals it with a `threading.Event`. The `Supervisor`, `Popen` handling, `stdin` EOF shutdown and the `multiprocessing` `spawn` workers
all use blocking calls and threads. A running event loop would be one more thing to start, stop and tear down inside
the 30 s service stop window, and it would make the interplay with `ctypes` calls (USN journal, Job Object) harder to
reason about, because those calls block the loop too.

### 7. What the API actually needs

The API is a few JSON routes and three static pages (section 7). FastAPI's strengths are not used here:

| FastAPI feature | Why it is not needed |
|---|---|
| Pydantic request validation | Bodies are small flat dicts. Settings are validated by `config.validate_changes()`, which also serves the CLI and the supervisor, so a second schema would duplicate it. |
| OpenAPI and docs UI | The only client is the project's own static pages and the Explorer search connector. |
| Dependency injection | `Config` is passed explicitly (the project rule is no singletons), and the server object already holds it. |
| Async I/O, WebSockets | The page polls `/api/status` and the log; nothing streams. |
| Middleware, routing | A dozen routes are an `if` chain in `do_GET` / `_post`. Host-header and token checks are a few lines each, and there is one place to review them. |

### Trade-offs accepted

* **Thread per connection.** `ThreadingHTTPServer` creates a thread per request. That is acceptable for a
  localhost UI with one user. Searches are capped by `SEARCH_SLOTS`; every other route is quick.
* **Hand-written routing and header handling.** More code than a framework would need, but it is small, has no
  parsing surface beyond the standard library, and `MAX_BODY` bounds request size.
* **No HTTPS or HTTP/2.** Not needed for a loopback-only server; the Host check and the token cover the realistic
  threat (other web pages reaching `localhost`).

### When to revisit

Reconsider only if requirements change in a way that makes async I/O the bottleneck: remote access by many users,
streaming results over WebSockets, or a long-lived push feed. None of these is planned, and the process and
threading model above would still be needed for parsing and database writes.

---

## 13. Security model

The index contains the text of the files of every account on the PC, so reading it is the asset to protect. The
service runs as LocalSystem and so does the indexer: `index.db` belongs to SYSTEM, and the app has no notion of a
"user who created the index". Protection covers the **HTTP connection only**, by design, to keep install and
operation simple.

```mermaid
flowchart TB
    net["Another computer"] -->|"TCP to port 8765"| nolisten["Nothing listens on a network address:<br/>connection refused"]
    local["Any program on this PC,<br/>any Windows account"] -->|"127.0.0.1:8765"| host

    subgraph srv["Web server (listens on 127.0.0.1 only)"]
        host{"Host header is 127.0.0.1,<br/>localhost or [::1] with our port?"}
        route["GET routes: search, status, pages"]
        tok{"Token valid?<br/>(constant-time compare)"}
        priv["POST routes, /api/browse, /api/log:<br/>control files and settings"]
        host -->|yes| route
        host -->|"no (DNS rebinding)"| deny["403 forbidden"]
        route --> tok
        tok -->|"needed by POST, browse, log"| priv
        tok -->|"invalid"| deny
    end
```

| Threat | Stopped by |
|---|---|
| Another computer reads the index | The server listens on `127.0.0.1`. `config.check_web_host` accepts only an IPv4 loopback address: `config.load` raises `ConfigError` (the service does not start) and `WebServer.start` refuses to start on anything else or on a socket bound elsewhere. |
| A web page in a browser reaches the server under a foreign name (DNS rebinding) | The `Host` header must be `127.0.0.1`, `localhost` or `[::1]` with the configured port. |
| A web page or program pauses the indexer, changes settings, browses folders or reads logs | The token (`X-Sindex-Token`, compared with `hmac.compare_digest`) on every POST, `/api/browse` and `/api/log`. Settings are validated before they are written. |
| Oversized or malformed requests | `MAX_BODY` bounds the body; bodies must be a JSON object; at most two searches run at once and a search stops after 30 s. |

**Not protected, on purpose:**

* **Other accounts on the same PC.** Loopback is open to every account (and the Windows firewall does not filter it),
  so another user, or malware running under one, can open `http://127.0.0.1:8765/` and search. The search and status
  routes need no token.
* **The files themselves.** `index.db`, `config.toml` (with the token) and the logs sit in `%ProgramData%\sindex`,
  which inherits read access for ordinary users from `%ProgramData%`. A local administrator can always read them.
* **Results are not filtered by the caller's file permissions.** The indexer reads every account's files as SYSTEM,
  so anyone who can search can see text of files they could not open themselves.

**Options considered and not implemented**, in case the requirement changes: a Windows Firewall block rule for the
port added at install; an allow-list of Windows accounts, checked by looking up the owner of each loopback connection
(TCP table, then process token); restricting the data folder to SYSTEM and Administrators; a token on the read routes;
a named pipe with an ACL (browsers cannot talk to one); a per-user index. Each adds install steps, moving parts or
breaks a way of using the search (CLI `search` reads the database directly, the Explorer search connector and the
right-click verb use plain URLs).
