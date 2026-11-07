# sindex

A Windows service that indexes the text content of local files and lets you search it from a local web page.
It keeps CPU and memory use low, even if that makes indexing slower.

- **Formats:** about 165 text extensions (txt, md, csv, json, yaml, toml, ini, log, source code, ...), HTML and
  XML-family markup (xml, svg, rss, xaml, csproj, ...), docx/xlsx/pptx, odt/ods/odp, rtf, and pdf (text layer,
  including AES/RC4-encrypted PDFs that open without a password; PDFs that need one are skipped). The list is
  `text_extensions` in `config.toml` (Settings shows all of it): take out what you do not want, add your own.
  An old `extra_text_extensions` entry is merged into it. Only text between XML tags is indexed, not attribute values.
  Files are matched by extension, so extension-less files such as `README` or `Makefile` are not indexed.
- **Photos and videos (off by default):** camera make/model, GPS coordinates, the date taken, and free text
  (title, description, keywords, author) from EXIF, XMP and IPTC in jpg/jpeg/png/tiff/webp/gif/bmp and RAW
  cr2/nef/arw/dng, and from container tags in mp4/mov/m4v/mkv/webm/avi/wmv/3gp (via `pymediainfo`, which needs
  `MediaInfo.dll`; the Windows wheel bundles it). Only headers are read, never pixels or video frames. Turn it on
  with `index_media_metadata` in `config.toml`, or the *Index photo and video metadata* checkbox on the Settings
  page's *What to index* tab; like the other rules there, turning it on makes the indexer rescan, so files that
  were skipped before are picked up without a manual rebuild. The free text is searchable like any other file;
  camera, GPS, resolution, duration and date taken are filters in the *Photos & videos* section of the search
  page, which also works with no search words typed at all (browse by filter alone).
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
# index a folder once, in recycled indexer processes like the service uses, then exit
.venv\Scripts\sindex --data-dir .\data index --root D:\Download
.venv\Scripts\sindex --data-dir .\data search "sample" --ext csv
# --names also matches folder and file names (a scan of the file list, so slower)
.venv\Scripts\sindex --data-dir .\data search "invoice" --names

# or run everything (indexer + web UI at http://127.0.0.1:8765/) until Ctrl+C
.venv\Scripts\sindex --data-dir .\data run --root D:\Download  # for scanning of specific folder
.venv\Scripts\sindex --data-dir .\data run   # for scanning using data/config.toml
```

Reading the USN journal needs administrator rights. Without them, sindex falls back to periodic rescans.

## Install as a service (elevated prompt)

```powershell
.venv\Scripts\sindex install     # auto start (delayed), runs as LocalSystem
.venv\Scripts\sindex start
start http://127.0.0.1:8765/
```

**Searching.** A search starts when you press Enter, or by itself `search_complete_after_s` seconds (default 1,
`[web]`; 0 = Enter only) after your last keystroke. Changing the text stops a running search, and a moving line under
the search box shows that one is running. A search has three steps, in this order of ranking:

1. the word index: whole words and prefixes (`kyo`, `kyo*`), accents ignored, ranked by relevance; instant;
2. a scan of the stored text of every file for the same words *inside* other words, so `kyo` also finds `tokyo` and
   `kyoto`. `*` and `?` inside a word are wildcards (`k?o`, `ky*to`). The stored text keeps its original letters, so
   this needs no second index and no extra disk space, but it reads all of it: on a 921 MB index about 2-3 s, and
   up to about 8 s for a complicated regular expression, growing in step with the index;
3. folder and file names, only if the names box is ticked.

**Sort by** (above the results) orders the list by path, file name, size or date, ascending or descending. The default,
Relevance, keeps the order of the steps above and, for two or more words, puts the files in which these words are
closest to each other first. A sort orders the files that were found, at most 2,000 (`2000+` in the summary says the
search stopped there), so with that many matches it does not reach the rest. The server keeps the files a browser tab
found for two minutes, so choosing another sort or turning a page only re-orders them (milliseconds) instead of reading
all the text again; a new search, or any change of the words or filters, searches again. The menu is part of the page address
(`sort=`), and `/api/search` takes the same values.

Below the count, two dropdowns with checkboxes narrow the found files by extension: **Show only files with
extension(s)** (generic choice ALL) and **Exclude files with extension(s)** (generic choice NONE). They list the
extensions of all found files with their counts, not just those of the page on screen. Choosing an extension in one
menu greys the other out until the first is back at its generic choice (or its last extension is unchecked). Like a
sort, this only re-orders the kept search, so it is instant; a new search starts again with ALL and NONE. The choices
are part of the page address (`only_ext=pdf,txt`, `skip_ext=md`; a dot stands for files without an extension, which
are only indexed when `text_extensions` lists an empty one).

Each result has **Open file** (in the app Windows associates with it) and **Open folder** (the folder in Explorer,
with the file selected). A web page cannot start programs, so both ask the sindex server to do it. Like Open file,
Open folder therefore only works while sindex runs in your own desktop session (`sindex run`); the installed
service runs in Windows' isolated session 0, where the request fails with a message instead of doing nothing.

Punctuation in a word is part of the search: `kyo)` finds only text that has those four characters, and `c++` does
not find every `c`. (The word index itself throws punctuation away, so what it finds for such a word is checked
against the stored text.) To search for a `*` or `?` itself, tick Use RegEx and write `\*` or `\?`.

An exclusion (`-kyo`) drops every file whose text has the term, also inside other words (`tokyo`, `kyoto`), and
`-kyo` alone lists the files with indexed text that do not have it, newest first, without snippets (the names box
also excludes files with it in their path). Files without indexed text (binary, too big, failed) are left out of
that list, because it is not known what they contain.

Three independent checkboxes sit under the search box: **Also in folder- and file-names**, **Use RegEx** and **Case
sensitive** (`sindex search --names --regex --case` on the command line). Names adds the folder and file names to the
search. Case sensitive compares the typed letters exactly, in contents and in names (`kyo` no longer finds `KYO`).
Use RegEx turns the whole query into one Python regular expression for the contents (`^` and `$` match at line
starts and ends) and, with the names box, for the full path (`D:\folder\file.txt`); case is ignored unless Case
sensitive is also ticked. A pattern is limited to 200 characters.

The web server answers requests on threads, at most two searches at a time. A newer search of the same browser tab
cancels the older one inside SQLite, editing the box cancels the running one, and a search that runs longer than 30 s
is stopped. Regular expressions run on the third-party `regex` module, the only regular-expression library the
program uses: unlike the standard `re` it can stop a match that has run away (a pattern that needs more than 0.5 s
on one path or 1 s on one piece of text is refused with a message) and lets other threads work meanwhile, so a bad
pattern cannot freeze the service. `requirements.txt` lists every third-party module. The Explorer search connector
(`/api/search.rss`) asks on every keystroke and therefore uses the word index only, without the scan.

`sindex install` also puts a "sindex search" shortcut to the web UI on the all-users Desktop and in the Start menu
(`sindex remove` deletes them). They open `http://<web_host>:<web_port>/` from `config.toml`; the UI is served over
plain HTTP, not HTTPS. It also wires up three more ways to reach the same search without opening that shortcut:

- **Folder and drive right-click menu:** "Search contents with sindex" on any folder, folder background or drive
  opens the web UI scoped to that folder (`sindex ui <path>` behind the scenes). On Windows 11 this classic verb
  lives under "Show more options" (or Shift+right-click) rather than the top-level menu.
- **Explorer address bar and Win+R:** typing `sindex` followed by words (for example `sindex overdue invoice`)
  opens the web UI already running that search. This works through the same "App Paths" registry mechanism that
  lets you type `notepad` or `calc`; typing an actual subcommand name (`sindex status`, `sindex install`, ...)
  still runs that subcommand instead, exactly as it would in a console.
- **Explorer search connector:** a "sindex search.searchConnector-ms" file is placed in the installing user's
  `%USERPROFILE%\Searches` folder. Opening it once adds sindex as a saved search in Explorer's navigation pane;
  typing in Explorer's search box while it is selected returns real file results (icon, preview, double-click to
  open) from `/api/search.rss`, an RSS feed generated from the same query language as the web UI's search box.

`sindex remove` undoes all three.

The service keeps its data in `%ProgramData%\sindex`:

| File | Purpose |
|---|---|
| `config.toml` | settings (roots, exclusions, throttling, web port/token); see below for how it is edited |
| `index.db` | SQLite FTS5 index |
| `logs\` | `service.log`, `indexer.log` (with a `progress: N files indexed, M files in queue` line after every 1000 files), `indexer-stderr.log` |
| `status.json`, `paused`, `requests\`, `settings-requests\` | control files the web UI and CLI use to talk to the indexer and the service |

To uninstall, run `sindex remove`. The service host is copied to `.venv\sindex-service\`, so keep the venv in place
while the service is installed.

## Windows executable (no Python needed)

Build it once on a machine that has the project (Python 3.12+):

```powershell
.venv\Scripts\pip install -e ".[build]"
.venv\Scripts\python packaging\build.py       # -> dist\sindex\sindex.exe (a folder of about 25 MB)
```

Copy the whole `dist\sindex` folder to the target machine and **double-click `sindex.exe`** (or run
`sindex.exe install --start`). It asks for administrator rights and then installs itself as the service: it copies
the program to `%ProgramFiles%\sindex`, registers the `sindex` service (automatic, delayed start, LocalSystem),
creates the Desktop and Start menu shortcuts, and starts the service. The web UI is at `http://127.0.0.1:8765/`.

- **Update:** run the new `sindex.exe` the same way. It stops the service, replaces the program folder (settings and
  index in `%ProgramData%\sindex` stay), and starts the service again.
- **Remove:** `"%ProgramFiles%\sindex\sindex.exe" remove` from an administrator prompt, then delete the folder
  `%ProgramFiles%\sindex`. Delete `%ProgramData%\sindex` too if you do not want to keep the index and settings.
- **Everything else works as with the Python install:** `sindex.exe status`, `search`, `pause`, `run` (in a console,
  with `--data-dir` for a test folder), and so on.
- **If the self-install does not work** (for example, a policy blocks the UAC prompt), copy the folder to
  `C:\Program Files\sindex` yourself and register it from an administrator prompt:

  ```powershell
  sc.exe create sindex binPath= "\"C:\Program Files\sindex\sindex.exe\" service" start= delayed-auto DisplayName= "sindex content indexer"
  sc.exe start sindex
  ```

It is a folder, not a single file, on purpose: Windows expects a service to answer within seconds of starting, and a
one-file build unpacks itself into a temporary folder first, every time. The service starts `sindex.exe service`, which
starts the indexer as `sindex.exe indexer` and the parsing workers as further copies of `sindex.exe`; you will see
several `sindex.exe` processes.

## How it stays light

- The service process only supervises and serves the web UI. A separate child process does all parsing. It runs at
  IDLE priority in background mode (low I/O and memory priority) with EcoQoS ("Efficiency mode" in Task Manager),
  inside a Job Object with a hard memory cap (256 MB) and a hard CPU cap (10% of the machine).
- The child is restarted every 1000 files or when it grows past 150 MB. Work is persisted in the `queue` table.
  Only a new process gives memory back to Windows, which is why `sindex index` also runs the indexer as recycled
  children. PDFs are the heavy part, because pypdf parses whole pages: a page can need 30 to 130 MB while it is
  decoded. After each page the decoded objects are dropped, and a page that does not fit under the 256 MB cap is
  skipped without losing the rest of the document.
- Parsing runs in `extract_workers` extra processes (3 by default), because a Python process can use one core at most
  and PDF parsing is where nearly all the time goes. The indexer hands each queued file to a free worker and only
  stores the text that comes back, so it stays the single writer of the database. Workers are started when first
  needed, replaced when they grow past `recycle_rss_mb` or after `recycle_after_files` files (and if one dies, only its
  file is recorded as an error), and end with the indexer, even if the service is killed. `extract_workers = 0`
  parses in the indexer itself, one file at a time.
- A duty cycle sleeps as long as it works (`duty_factor`) and reads are rate-limited (`read_mb_per_sec`). Indexing
  pauses on battery, when the system is busy, or when you pause it.
- Everything is streamed: 64 KB reads, incremental decoders, expat for Office XML, one PDF page at a time, and one
  directory at a time while scanning. SQLite uses an 8 MB page cache and no mmap.

## Settings page

The web UI has a **Settings** page (the gear link at the top right of the search page), in five tabs: Speed,
Schedule, What to index, Health and Maintenance. One *Save changes* button stores everything changed on the first
three; a tab with a rejected value is marked and opened. It covers:

- **Speed:** the presets Background, Balanced and Fast (the measured steps below: 22 s, 9 s and 3.8 s for the
  test files), the two pause rules, and all the other `[throttle]` values under *Advanced*.
- **Schedule and freshness:** `schedule_windows` (for example `22:00-06:00`; the indexer pauses outside them),
  `rescan_hours` and `usn_poll_seconds`.
- **What to index:** roots and folders to skip (picked in a folder browser, with the chosen folders listed beside it;
  the browser lists folders of this computer and needs the web token), then *Files and folders to skip anywhere* and
  *File types to skip* (side by side), the file types read as text, hidden files, and the size limits.
  The first list holds names and patterns, one per line (`exclude_globs`): a name (`node_modules`) or a pattern for
  names (`.*`, `tmp*`, `*.bak`) skips every file and folder with such a name, wherever it is; a pattern with a
  backslash is matched against the whole path (`*\.ssh\*`, `D:\Private\*`). `*` is any text, `?` one character, and
  case is ignored. It starts with the usual cache and trash folders (`node_modules`, `__pycache__`, `venv`, ...) and
  dotfiles (`.*`). The file types list takes extensions with or without a dot, separated by spaces, commas or lines,
  and may use the wildcards too (`env pem msi*`); it starts with a few binary and key/certificate extensions
  (`exe com bin dll msi* cer pem crt`). Saving a change to these rules makes the indexer rescan, so files that are
  excluded now leave the index and files that are allowed now enter it. An old `exclude_dir_names` entry in
  `config.toml` is merged into the patterns. A separate checkbox, *Index photo and video metadata*, turns on the
  EXIF/XMP/IPTC and container-tag reading described above, with its own *Skip media files larger than* limit; it
  is one more rule that triggers a rescan when changed.
- **Index storage:** where `index.db` is and how big it is. To move it, stop the service, move the `index.db*` files,
  set `db_dir` in `config.toml` and start the service. The folder must exist and be on a local fixed drive (SQLite's
  write-ahead log does not work on network shares); otherwise the indexer refuses to start and says why in its status.
  The index folder is never indexed.
- **Indexing health:** the files that ended as error, too big, not text, cut short or empty, with their error, and a
  retry for one file or for a whole group (after raising a size limit, for example).
- **Maintenance:** rebuild everything, optimize the search index, compact the database (both run when the queue is
  empty, rewrite much of the database and cannot be interrupted), and reindex a path.

The **Log** link in the status panel of the search page opens a read-only log viewer: the indexer log, the service
and web UI log, or the indexer's stderr, showing the last 64 KB to 1 MB, with a text filter, highlighted warnings and
errors, and a *Follow* switch that refreshes every 3 s. Reading a log needs the web token, because logs name the
files and folders that were indexed. (Searches are not logged.)

The page never writes `config.toml` itself: it validates the change (roots must exist, patterns and windows must be
well formed), then drops a request in `settings-requests\` and the service writes the file and restarts the indexer,
which reads the new values when it starts. A value that is out of range is refused with a message and the file stays
as it was. Every write needs the web token, like Pause and Reindex.

The service rewrites `config.toml` from a template with every key filled in, so comments you add by hand are lost;
the previous file is kept as `config.toml.bak`. Edit it by hand only while the service is stopped, or expect the
indexer to pick the change up at its next restart.

## Tuning how hard it is throttled

All of these are in the `[throttle]` section of `config.toml`, or under *Advanced* on the Settings page. Indexing 2,506
files on an otherwise idle 8-core machine took 2.8 s unthrottled and 22 s with the defaults:

| Setting (default) | What it does | Cost |
|---|---|---|
| `efficiency_mode` (true) | Windows "Efficiency mode" (EcoQoS): low clock speeds, efficiency cores | the biggest: about 2x slower on its own |
| `background_io` (true) | low I/O and memory priority | about 1.5x slower on its own |
| `duty_factor` (1.0) | after each 0.1 s of work, sleep this many times as long: CPU share is 1 / (1 + factor) | 1.0 is 50%, 0.5 is 67%, 0.25 is 80%. Applies to each worker on its own |
| `read_mb_per_sec` (4) | cap on file reading; only matters for large files | small. Shared by the workers |
| `cpu_rate_percent` (10) | hard CPU cap as a percentage of the *whole machine*, for the indexer and its workers together | rarely binding on 8 cores, but 10% of a 4-core machine is 0.4 of one core |
| `extract_workers` (3) | processes that parse files in parallel (0 = in the indexer) | 60 PDFs and 100 other files: 45 s with 0, 35 s with 3 (one 10 s PDF at the end of the queue set the floor). Each worker takes about 50 MB of RAM |
| `idle_priority` (true) | lowest CPU priority | free on an idle machine; keep it so foreground work always wins |
| `pause_on_battery`, `pause_when_cpu_busy_percent` (true, 70) | stop entirely instead of running slowly | none while running |

Measured through the real indexer process, same files: defaults 22 s, `duty_factor = 0.5` 16 s, plus
`efficiency_mode = false` 9 s, plus `background_io = false` 6.5 s, and additionally `duty_factor = 0.25` and
`read_mb_per_sec = 16` 3.8 s. Numbers will differ on your machine, so try one change at a time.

## SSD wear: batched writes

The index is written in batches: one transaction spans up to 200 files (`commit_every_files`) or 60 s
(`commit_every_seconds`), and it is also committed when the indexer goes idle, pauses, recycles or stops. A file's
queue entry is removed in the same transaction as its index rows, so a crash loses at most the open batch, which is
redone from the queue. Never half a file: a commit waits until the files being parsed by the workers are complete,
and the files in flight count towards the batch size.

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
