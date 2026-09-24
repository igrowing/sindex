"""PDF text layer via pypdf, one page at a time."""

from __future__ import annotations

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
    for i in range(count):
        ctx.throttle.checkpoint()
        try:
            text = reader.pages[i].extract_text()
        except DependencyError as e:
            raise ExtractError(f"pdf: missing dependency: {e}") from e
        except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, ZeroDivisionError) as e:
            failed += 1  # skip the broken page, keep the rest of the document
            last_error = e
            continue
        if text:
            yield text + "\n"
    if count and failed == count:
        raise ExtractError(f"pdf: no readable pages ({type(last_error).__name__}: {last_error})")
