"""PDF text layer via pypdf, one page at a time."""

from __future__ import annotations

import gc
from collections.abc import Iterator

from . import Context, ExtractError


def extract(f, ctx: Context) -> Iterator[str]:
    from pypdf import PdfReader
    from pypdf.errors import DependencyError, PyPdfError

    # DependencyError (an optional package such as `cryptography` for AES-encrypted PDFs is
    # missing) is a plain Exception, not a PyPdfError, so it has to be listed on its own. It is
    # never skipped page by page: every page would fail the same way.
    try:
        reader = PdfReader(f, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("encrypted PDF")
        count = len(reader.pages)
    except ExtractError:
        raise
    except DependencyError as e:
        raise ExtractError(f"pdf: missing dependency: {e}") from e
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        raise ExtractError(f"pdf: {type(e).__name__}: {e}") from e

    failed = 0
    last_error = None
    try:
        for i in range(count):
            ctx.throttle.checkpoint()
            text = ""
            try:
                text = reader.pages[i].extract_text()
            except DependencyError as e:
                raise ExtractError(f"pdf: missing dependency: {e}") from e
            except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, ZeroDivisionError,
                    MemoryError) as e:
                # skip the broken page, keep the rest of the document. MemoryError belongs here: one
                # page can need over 100 MB to decode, and under the memory cap that fails just this page.
                failed += 1
                last_error = e
            _forget_decoded_objects(reader)
            if text:
                yield text + "\n"
    finally:
        # pypdf objects reference their parents, so reference counting alone does not free them
        del reader
        gc.collect()
    if count and failed == count:
        raise ExtractError(f"pdf: no readable pages ({type(last_error).__name__}: {last_error})")


def _forget_decoded_objects(reader) -> None:
    """Drop what pypdf decoded for the page just read (fonts, content streams).

    pypdf keeps all of it for the life of the reader, so memory grew with the page count.
    Measured on a 124-page PDF: peak +47 MB, +22 MB with this. Collecting garbage on every page
    as well only saved 4 MB more but made a busy process 2.6x slower, so that happens once per PDF.
    """
    cache = getattr(reader, "resolved_objects", None)
    if isinstance(cache, dict):
        cache.clear()
