"""One extraction job: the messages and statuses that the indexer stores, whichever runner produced them."""

import threading

import pytest

from sindex import textnorm
from sindex.extract import job as job_module
from sindex.extract.job import Chunk, Result, Task, run_job
from sindex.extract.pool import InlineRunner
from sindex.throttle import Stopped, Throttle

from .conftest import make_docx


def run(cfg, path, kind="text", max_chunks=1000):
    return list(run_job(Task(7, str(path), kind, max_chunks), cfg, Throttle(cfg, threading.Event())))


def test_text_file_gives_normalized_chunks_then_a_result(cfg, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("Café crème", encoding="utf-8")
    *chunks, result = run(cfg, f)
    assert [type(m) for m in chunks] == [Chunk] and all(c.job == 7 for c in chunks)
    assert chunks[0].text == textnorm.normalize("Café crème"), "the text has been through textnorm.normalize()"
    assert result == Result(7, "ok", None, "utf-8", False)


def test_empty_file_is_reported_as_empty(cfg, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("   \n", encoding="utf-8")
    assert run(cfg, f) == [Result(7, "empty", None, "utf-8", False)]


def test_binary_content_is_not_an_error(cfg, tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(bytes(range(256)) * 40)
    assert run(cfg, f)[-1].status == "binary"


def test_unreadable_document_is_an_error_with_a_message(cfg, tmp_path):
    f = tmp_path / "broken.docx"
    f.write_bytes(b"this is not a zip archive")
    result = run(cfg, f, kind="ooxml")[-1]
    assert result.status == "error" and result.error


def test_missing_file_is_an_error_not_an_exception(cfg, tmp_path):
    result = run(cfg, tmp_path / "gone.txt")[-1]
    assert result.status == "error" and "FileNotFoundError" in result.error


def test_office_document(cfg, tmp_path):
    f = make_docx(tmp_path / "r.docx", ["Quarterly report"])
    *chunks, result = run(cfg, f, kind="ooxml")
    assert result.status == "ok" and "quarterly" in chunks[0].text.lower()


def test_text_beyond_the_chunk_limit_is_dropped_and_reported(cfg, tmp_path):
    cfg.chunk_chars = 100
    f = tmp_path / "a.txt"
    f.write_text("word " * 500, encoding="utf-8")
    *chunks, result = run(cfg, f, max_chunks=3)
    assert len(chunks) == 3
    assert result.status == "truncated"


def test_a_parser_bug_is_an_error_result(cfg, tmp_path, monkeypatch):
    def boom(*args):
        raise RuntimeError("parser bug")
        yield  # makes this a generator

    monkeypatch.setattr(job_module, "extract", boom)
    result = run(cfg, tmp_path / "a.txt")[-1]
    assert result.status == "error" and "RuntimeError: parser bug" in result.error


def test_out_of_memory_asks_for_a_fresh_process(cfg, tmp_path, monkeypatch):
    def boom(*args):
        raise MemoryError
        yield

    monkeypatch.setattr(job_module, "extract", boom)
    result = run(cfg, tmp_path / "a.txt")[-1]
    assert (result.status, result.error, result.recycle) == ("error", "out of memory", True)


def test_stop_is_not_swallowed(cfg, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    stop = threading.Event()
    stop.set()
    with pytest.raises(Stopped):
        list(run_job(Task(1, str(f), "text", 10), cfg, Throttle(cfg, stop)))


def test_inline_runner_takes_one_job_and_remembers_it_wants_a_new_process(cfg, tmp_path, monkeypatch):
    def boom(*args):
        raise MemoryError
        yield

    monkeypatch.setattr(job_module, "extract", boom)
    runner = InlineRunner(cfg, Throttle(cfg, threading.Event()))
    assert runner.has_slot() and not runner.wants_recycle
    runner.submit(Task(1, str(tmp_path / "a.txt"), "text", 10))
    assert not runner.has_slot()
    (result,) = runner.poll(0)
    assert isinstance(result, Result) and runner.has_slot()
    assert runner.wants_recycle
