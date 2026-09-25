from pathlib import Path

from sindex.indexer import Indexer


def test_status_write_failing_for_lack_of_memory_does_not_crash(cfg, monkeypatch):
    # at the memory cap even opening a small file fails with MemoryError; the status file is
    # best effort, and losing it at exit used to turn a normal recycle into a crash
    indexer = Indexer(cfg, [str(cfg.data_dir)], limits=False)

    def no_memory(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(Path, "write_text", no_memory)
    indexer._status("stopped", force=True)
