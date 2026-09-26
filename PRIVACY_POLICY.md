# Privacy Policy — sindex

**Last updated: September 25, 2026**

sindex is an open-source Windows service that indexes the text content of
local files and lets you search it from a local web page. It is developed by
[igrowing](https://github.com/igrowing). This policy explains what data the
software accesses, why, and what it does (and does not) do with it.

---

## 1. Summary (TL;DR)

- sindex **does not collect, transmit, or share any data**. Everything it
  reads and stores stays on your computer.
- It contains **no code that connects to the Internet**. The only network
  endpoint is a small web page served **from your own machine**, bound to
  `127.0.0.1` (localhost) by default.
- To make files searchable, the indexer **reads the files under the folders
  it is configured to index** and stores their text in a local database.
  This is the whole purpose of the software.
- There are **no ads, no analytics, no tracking, no telemetry, no accounts,
  no crash reporting, and no update checks**.
- The software is **open source** — you can inspect every line of code in
  the project repository.

---

## 2. What sindex Accesses and Why

### Your files

The indexer reads files in the folders listed as `roots` in `config.toml`.
If `roots` is empty, it covers **all fixed drives**. Files are matched by
extension (plain text and source code, HTML/XML, Office and OpenDocument
files, RTF, and PDF). Files are read **read-only**; sindex never modifies,
moves, or deletes them.

Some locations are skipped by default: the Windows and Program Files
folders, `ProgramData`, the recycle bin, `System Volume Information`, common
developer cache folders (`node_modules`, `.git`, `.venv`, ...), and sindex's
own data folder. You can add more (folders, names,
patterns such as `*.pem`, and file extensions) on the Settings page or with `exclude_paths`, `exclude_globs`
and `exclude_extensions` in `config.toml`.

Password-protected PDFs that require a password are skipped.

### The NTFS change journal

To notice new and changed files without rescanning everything, sindex reads
the NTFS USN change journal, which lists the names of recently changed
files. This needs administrator rights (the installed service runs as
`LocalSystem`). The journal is used only to decide what to re-index.

### Network

sindex contains **no networking code that contacts any other computer**. It
makes no Internet requests and cannot "phone home". The local web UI listens
on `127.0.0.1:8765` by default. It also rejects requests whose `Host` header
is not a localhost name, which protects against DNS-rebinding attacks from
web pages open in your browser.

If you change `web_host` in `config.toml` to a non-local address, you are
deliberately exposing your index to your network — see Section 5.

---

## 3. What Is Stored

sindex keeps its data in a single folder, `%ProgramData%\sindex` by default
(or the folder given by `--data-dir` / `SINDEX_HOME`):

| File | Contents |
|------|----------|
| `index.db` | SQLite full-text index: for each indexed file its folder, name, size, modification time, status and detected encoding, plus **the extracted text of the file**, normalized for searching (case and accents folded, CJK split into bigrams). Also a queue of files waiting to be indexed. |
| `config.toml` | Your settings, including the folders to index and a randomly generated token that protects the web UI's pause/resume/reindex actions |
| `logs\` | `service.log`, `indexer.log`, `indexer-stderr.log` — rotating logs (about 1 MB each, a few copies kept) containing file paths and error messages, for example when a file cannot be parsed |
| `status.json`, `paused`, `requests\` | Small control files used by the web UI and command line to talk to the indexer |

**Important:** because the index contains the extracted text of your
documents, it can contain anything those documents contain — including
personal or confidential information. Treat `index.db` with the same care as
the original files. sindex does not encrypt it.

No data is stored in the cloud. No account or registration is required.
Nothing is stored outside the data folder.

### Removing your data

- To delete the index, stop the service and delete the data folder (or just
  `index.db`); sindex will rebuild it if you start it again.
- `sindex remove` uninstalls the Windows service. It does **not** delete the
  data folder; delete it yourself if you want the data gone.
- Files removed from disk are removed from the index on the next change
  detection or rescan.

---

## 4. The Local Web UI and Command Line

The web UI and the `sindex search` command query the index and display file
paths and short text excerpts (snippets) from matching files. The web UI
loads no external scripts, fonts, or images.

Pause, resume, and reindex actions require the token stored in
`config.toml`. **Searching does not require the token**: any program or
user on the same computer that can reach `127.0.0.1:8765` can search the
index and read the paths and snippets it returns, including content from
files that other Windows accounts would not normally be able to open,
because the indexer runs as `LocalSystem`. Install and use sindex only on
machines where you are comfortable with that. On shared computers, consider
restricting `roots` to folders that all users may read.

---

## 5. Sharing

sindex never sends anything anywhere on its own. The only ways data can
leave your computer are ones you choose:

- copying search results from the web page or command line and pasting them
  elsewhere;
- changing `web_host` to an address reachable from other machines (not the
  default, and not recommended — the search endpoint has no authentication);
- copying or backing up the data folder yourself.

---

## 6. Third-Party Services and Libraries

sindex contacts **no third-party services** of any kind.

It uses the following **open-source libraries** (in addition to the Python
standard library, including its SQLite). None of them are configured to
collect or transmit data. All are distributed under permissive open-source
licenses.

| Library | License | Purpose |
|---------|---------|---------|
| [Python](https://www.python.org) | PSF License | Runtime and standard library (SQLite, HTTP server, expat XML parser) |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/) | MIT | Detect the text encoding of files |
| [pypdf](https://pypi.org/project/pypdf/) | BSD 3-Clause | Extract text from PDF files |
| [pycryptodome](https://pypi.org/project/pycryptodome/) | BSD 2-Clause / Public Domain | Open AES-encrypted PDFs that need no password (installed via `pypdf[cryptodome]`) |
| [pywin32](https://pypi.org/project/pywin32/) | PSF License | Windows service and Windows API access |

Development-only tools (`pytest`, `ruff`) are not part of an installed
service.

There are **no third-party advertising SDKs**, **no crash reporting
services**, and **no analytics platforms** integrated in sindex.

---

## 7. Children's Privacy

sindex is a general-audience utility. It does not target children and does
not knowingly collect any data from anyone.

---

## 8. Changes to This Policy

If the software ever changes in a way that affects privacy (for example, a
network feature or a third-party service is added), this policy will be
updated and the "Last updated" date will change. The current version is
always the `PRIVACY_POLICY.md` file in the project repository.

---

## 9. Contact

Questions about this privacy policy: open an issue or discussion in the
project repository, or write to support.plaything728@silomails.com.
