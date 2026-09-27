"""Searching and browsing by image/video metadata filters."""

from pathlib import Path

from sindex import db
from sindex.search import search

from .conftest import FIXTURES, make_jpeg
from .test_index_search import names, rescan, run_index


def _index_photo_and_video(cfg, tmp_path: Path) -> Path:
    cfg.index_media_metadata = True
    root = tmp_path / "docs"
    root.mkdir()
    # camera/model are structured fields (media.camera), not free text: keyword tests use ImageDescription
    make_jpeg(root / "photo.jpg", exif={270: "a holiday snapshot", 271: "Canon", 272: "EOS R5",
                                        36867: "2023:06:01 10:00:00"}, gps=(40.6892, -74.0445))
    (root / "clip.mp4").write_bytes((FIXTURES / "sample.mp4").read_bytes())  # duration 0.04s, no GPS
    run_index(cfg, root)
    return root


def test_kind_filter_narrows_a_keyword_search(cfg, tmp_path):
    _index_photo_and_video(cfg, tmp_path)
    assert names(cfg, "holiday", kind="image") == {"photo.jpg"}
    assert names(cfg, "holiday", kind="video") == set()


def test_has_gps_filter(cfg, tmp_path):
    _index_photo_and_video(cfg, tmp_path)
    assert names(cfg, "", kind="image", has_gps=True) == {"photo.jpg"}
    assert names(cfg, "", kind="video", has_gps=False) == {"clip.mp4"}
    assert names(cfg, "", has_gps=True) == {"photo.jpg"}


def test_taken_at_range_filter(cfg, tmp_path):
    _index_photo_and_video(cfg, tmp_path)  # both fixtures carry the same 2023-06-01 taken/recorded date
    assert names(cfg, "", kind="image", taken_after="2023-05-01", taken_before="2023-06-30") == {"photo.jpg"}
    assert names(cfg, "", taken_after="2023-05-01", taken_before="2023-06-30") == {"photo.jpg", "clip.mp4"}
    assert names(cfg, "", taken_after="2023-07-01") == set()
    assert names(cfg, "", taken_before="2023-05-31") == set()


def test_duration_filter_finds_the_video_with_no_keyword(cfg, tmp_path):
    _index_photo_and_video(cfg, tmp_path)
    assert names(cfg, "", kind="video", min_duration_s=0.01, max_duration_s=1.0) == {"clip.mp4"}
    assert names(cfg, "", kind="video", min_duration_s=10.0) == set()


def test_camera_and_resolution_filters(cfg, tmp_path):
    _index_photo_and_video(cfg, tmp_path)
    assert names(cfg, "", camera="canon") == {"photo.jpg"}
    assert names(cfg, "", min_width=1, min_height=1) == {"photo.jpg", "clip.mp4"}
    assert names(cfg, "", min_width=1000) == set()


def test_plain_keyword_search_is_unaffected_by_media_filters(cfg, tmp_path):
    root = _index_photo_and_video(cfg, tmp_path)
    (root / "notes.txt").write_text("just some plain text", encoding="utf-8")
    rescan(cfg, root)
    assert names(cfg, "plain") == {"notes.txt"}


def test_media_dict_is_attached_to_results(cfg, tmp_path):
    _index_photo_and_video(cfg, tmp_path)
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        res = search(conn, "holiday")
    finally:
        conn.close()
    photo = next(r for r in res["results"] if r["name"] == "photo.jpg")
    assert photo["media"]["kind"] == "image"
    assert photo["media"]["camera"] == "Canon EOS R5"


def test_empty_query_with_no_filters_still_prompts_for_a_keyword(cfg, tmp_path):
    _index_photo_and_video(cfg, tmp_path)
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        res = search(conn, "")
    finally:
        conn.close()
    assert res["error"] and res["results"] == []
