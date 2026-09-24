# sindex

A Windows service that indexes the text content of local files and lets you search it from a local web page.
It keeps CPU and memory use low, even if that makes indexing slower.

- **Formats:** about 165 text extensions (txt, md, csv, json, yaml, toml, ini, log, source code, ...), HTML and
  XML-family markup (xml, svg, rss, xaml, csproj, ...), docx/xlsx/pptx, odt/ods/odp, rtf, and pdf (text layer,
  including AES/RC4-encrypted PDFs that open without a password; PDFs that need one are skipped). Add your own
  with `extra_text_extensions` in `config.toml`. Only text between XML tags is indexed, not attribute values.
  Files are matched by extension, so extension-less files such as `README` or `Makefile` are not indexed.
- **Languages and encodings:** it detects BOMs, UTF-8/16, cp125x, KOI8-R, cp866, ISO-8859-x, Shift-JIS, EUC-JP,
  GB18030, Big5 and EUC-KR. Search ignores accents and case: Latin accents, Greek tonos, and Hebrew/Arabic vowel
  marks don't affect matching. CJK text is indexed as bigrams.
- **Change tracking:** the NTFS USN journal. Volumes without one are rescanned periodically.

## Setup

```powershell
py -3.12 -m venv .venv            # Python 3.12+ (tested with 3.14)
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m pytest    # run tests/test_usn.py from an elevated prompt to test the journal
```

## Try it without installing the service

```powershell
# index a folder once, in this process, then exit
.venv\Scripts\sindex --data-dir .\data index --root D:\Download
.venv\Scripts\sindex --data-dir .\data search "sample" --ext csv

# or run everything (indexer + web UI at http://127.0.0.1:8765/) until Ctrl+C
.venv\Scripts\sindex --data-dir .\data run --root D:\Download
```

Reading the USN journal needs administrator rights. Without them, sindex falls back to periodic rescans.

## Install as a service (elevated prompt)

```powershell
.venv\Scripts\sindex install     # auto start (delayed), runs as LocalSystem
.venv\Scripts\sindex start
start http://127.0.0.1:8765/
```

The service keeps its data in `%ProgramData%\sindex`:

| File | Purpose |
|---|---|
| `config.toml` | settings (roots, exclusions, throttling, web port/token); restart the service after editing |
| `index.db` | SQLite FTS5 index |
| `logs\` | `service.log`, `indexer.log`, `indexer-stderr.log` |
| `status.json`, `paused`, `requests\` | control files the web UI and CLI use to talk to the indexer |

To uninstall, run `sindex remove`. The service host is copied to `.venv\sindex-service\`, so keep the venv in place
while the service is installed.

## How it stays light

- The service process only supervises and serves the web UI. A separate child process does all parsing. It runs at
  IDLE priority in background mode (low I/O and memory priority) with EcoQoS ("Efficiency mode" in Task Manager),
  inside a Job Object with a hard memory cap (256 MB) and a hard CPU cap (10% of the machine).
- The child is restarted every 1000 files or when it grows past 150 MB. Work is persisted in the `queue` table.
- A duty cycle sleeps as long as it works (`duty_factor`) and reads are rate-limited (`read_mb_per_sec`). Indexing
  pauses on battery, when the system is busy, or when you pause it.
- Everything is streamed: 64 KB reads, incremental decoders, expat for Office XML, one PDF page at a time, and one
  directory at a time while scanning. SQLite uses an 8 MB page cache and no mmap.

## Tuning how hard it is throttled

All of these are in the `[throttle]` section of `config.toml` (restart the service after editing). Indexing 2,506
files on an otherwise idle 8-core machine took 2.8 s unthrottled and 22 s with the defaults:

| Setting (default) | What it does | Cost |
|---|---|---|
| `efficiency_mode` (true) | Windows "Efficiency mode" (EcoQoS): low clock speeds, efficiency cores | the biggest: about 2x slower on its own |
| `background_io` (true) | low I/O and memory priority | about 1.5x slower on its own |
| `duty_factor` (1.0) | after each 0.1 s of work, sleep this many times as long: CPU share is 1 / (1 + factor) | 1.0 is 50%, 0.5 is 67%, 0.25 is 80% |
| `read_mb_per_sec` (4) | cap on file reading; only matters for large files | small |
| `cpu_rate_percent` (10) | hard CPU cap as a percentage of the *whole machine* | rarely binding on 8 cores, but 10% of a 4-core machine is 0.4 of one core |
| `idle_priority` (true) | lowest CPU priority | free on an idle machine; keep it so foreground work always wins |
| `pause_on_battery`, `pause_when_cpu_busy_percent` (true, 70) | stop entirely instead of running slowly | none while running |

Measured through the real indexer process, same files: defaults 22 s, `duty_factor = 0.5` 16 s, plus
`efficiency_mode = false` 9 s, plus `background_io = false` 6.5 s, and additionally `duty_factor = 0.25` and
`read_mb_per_sec = 16` 3.8 s. Numbers will differ on your machine, so try one change at a time.

## SSD wear: batched writes

The index is written in batches: one transaction spans up to 200 files (`commit_every_files`) or 60 s
(`commit_every_seconds`), and it is also committed when the indexer goes idle, pauses, recycles or stops. A file's
queue entry is removed in the same transaction as its index rows, so a crash loses at most the open batch, which is
redone from the queue. Never half a file.

Bytes written by the indexer process to build a 56 MB index of 2,506 files (`C:\Python314\Lib`, measured with
`psutil` write counters, so a proxy for physical writes):

| Setting | Written |
|---|---|
| one commit per file (2 MB cache) | 330 MB |
| batches of 200 files, 2 MB cache | 159 MB |
| **default:** batches of 200 files, 8 MB cache | **146 MB** |
| 1000 files, 64 MB cache | 129 MB |

Two things matter, and both were found by measuring:

- **The batch must fit in SQLite's page cache** (`sqlite_cache_mb`, 8 MB by default; a batch is capped at about half
  that many MB of text). Otherwise dirty pages spill to the log mid-transaction and hot pages are written over and
  over. Beyond 8 MB there is little further gain.
- **No savepoint per file.** FTS5 writes a new index segment on every savepoint, which brings the small-segment
  write amplification straight back.

The floor is about twice the database size, because WAL mode writes each page once to the log and once when the log
is checkpointed into the database. Reading is unaffected. The trade-off is that a new or changed file can take up to a
minute to show up in search. The indexer's status file is rewritten at most every 10 s.

## CLI

`sindex [--data-dir DIR] run | index | search | status | pause | resume | reindex PATH | install | remove | start | stop`
