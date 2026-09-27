"""Media metadata surfaced through the web API: /api/status counts and /api/search filters."""

import json

from sindex.indexer import Indexer

from .conftest import FIXTURES, call, make_jpeg


def _index_photo_and_video(cfg, tmp_path):
    cfg.index_media_metadata = True
    root = tmp_path / "docs"
    root.mkdir()
    make_jpeg(root / "photo.jpg", exif={270: "a holiday snapshot"}, gps=(40.6892, -74.0445))
    (root / "clip.mp4").write_bytes((FIXTURES / "sample.mp4").read_bytes())
    assert Indexer(cfg, [str(root)], until_idle=True, limits=False).run() == 0


def test_status_reports_media_counts(web, tmp_path):
    _index_photo_and_video(web, tmp_path)
    status, body = call(web, "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data["index"]["media"] == {"image": 1, "video": 1}


def test_search_api_accepts_media_filters(web, tmp_path):
    _index_photo_and_video(web, tmp_path)
    status, body = call(web, "/api/search?q=holiday&kind=image&has_gps=1")
    assert status == 200
    names = {r["name"] for r in json.loads(body)["results"]}
    assert names == {"photo.jpg"}

    status, body = call(web, "/api/search?kind=video")  # filter-only browse, no keyword
    assert status == 200
    names = {r["name"] for r in json.loads(body)["results"]}
    assert names == {"clip.mp4"}
