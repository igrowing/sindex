import re
from dataclasses import fields

import pytest

from sindex import config


def test_template_has_a_placeholder_for_every_setting_and_no_other():
    placeholders = set(re.findall(r"^\w+ = \{(\w+)\}$", config.CONFIG_TEMPLATE, flags=re.MULTILINE))
    assert placeholders == {f.name for f in fields(config.Config)} - {"data_dir"}


def test_every_editable_setting_is_a_config_field():
    assert config.SPECS.keys() <= {f.name for f in fields(config.Config)}
    assert not config.SPECS.keys() & {"token", "web_host", "web_port"}


def test_presets_and_fresh_install_values_pass_validation():
    for values in [*config.THROTTLE_PRESETS.values(), config.FRESH_INSTALL]:
        assert config.validate_changes(values) == values


def test_fresh_install_starts_from_the_fresh_values(tmp_path):
    cfg = config.load(tmp_path)

    assert cfg.duty_factor == 0.1 and cfg.memory_limit_mb == 512 and cfg.sqlite_cache_mb == 64
    assert cfg.max_text_mb == config.Config().max_text_mb  # what is not listed keeps its default
    assert len(cfg.token) >= 24
    assert config.load(tmp_path).token == cfg.token  # a second load does not make a new token


def test_render_and_load_round_trip(tmp_path):
    cfg = config.Config(data_dir=tmp_path, token="tok")
    cfg.roots = ["D:\\Docs", 'E:\\with "quotes"', "F:\\Ünï\\東京", "G:\\a"]
    cfg.exclude_paths = ["C:\\one"]
    cfg.text_extensions = ["txt", "md", "weird", "ünï"]
    cfg.duty_factor = 0.25
    cfg.index_hidden = True
    (tmp_path / "config.toml").write_text(config.render(cfg), encoding="utf-8")

    loaded = config.load(tmp_path)

    for f in fields(config.Config):
        assert getattr(loaded, f.name) == getattr(cfg, f.name), f.name


@pytest.mark.parametrize("changes,key", [
    ({"nope": 1}, "nope"),
    ({"token": "x"}, "token"),
    ({"web_port": 80}, "web_port"),
    ({"duty_factor": "fast"}, "duty_factor"),
    ({"duty_factor": -1}, "duty_factor"),
    ({"duty_factor": float("nan")}, "duty_factor"),
    ({"duty_factor": float("inf")}, "duty_factor"),
    ({"extract_workers": 2.5}, "extract_workers"),
    ({"extract_workers": True}, "extract_workers"),  # a bool is not a number
    ({"extract_workers": 99}, "extract_workers"),
    ({"cpu_rate_percent": 101}, "cpu_rate_percent"),
    ({"memory_limit_mb": 10}, "memory_limit_mb"),  # 0 = off is fine, a limit of 10 MB would stop the indexer
    ({"pause_on_battery": 1}, "pause_on_battery"),
    ({"roots": "D:\\"}, "roots"),
    ({"roots": ["D:\\", ""]}, "roots"),
    ({"roots": [1]}, "roots"),
])
def test_bad_changes_are_rejected_with_the_key_named(changes, key):
    with pytest.raises(config.ConfigError) as e:
        config.validate_changes(changes)
    assert key in e.value.errors


@pytest.mark.parametrize("changes", [None, [], {}, "duty_factor"])
def test_changes_must_be_a_non_empty_object(changes):
    with pytest.raises(config.ConfigError):
        config.validate_changes(changes)


def test_every_bad_key_is_reported_at_once():
    with pytest.raises(config.ConfigError) as e:
        config.validate_changes({"duty_factor": -1, "extract_workers": 99, "cpu_rate_percent": 5})
    assert e.value.errors.keys() == {"duty_factor", "extract_workers"}


def test_values_are_converted_to_their_type():
    checked = config.validate_changes({"duty_factor": 1, "extract_workers": 2.0, "memory_limit_mb": 0,
                                       "roots": [" D:\\Docs "]})
    assert checked == {"duty_factor": 1.0, "extract_workers": 2, "memory_limit_mb": 0, "roots": ["D:\\Docs"]}
    assert isinstance(checked["duty_factor"], float) and isinstance(checked["extract_workers"], int)


def test_apply_writes_the_changes_and_keeps_the_rest(tmp_path):
    before = config.load(tmp_path)

    after = config.apply(tmp_path, {"duty_factor": 0.5, "roots": ["D:\\Docs"]})

    loaded = config.load(tmp_path)
    assert (loaded.duty_factor, loaded.roots) == (0.5, ["D:\\Docs"]) == (after.duty_factor, after.roots)
    assert loaded.token == before.token and loaded.sqlite_cache_mb == before.sqlite_cache_mb
    assert config.load(tmp_path / "x").duty_factor == 0.1  # unrelated data directories are unaffected


def test_apply_with_a_bad_value_leaves_the_file_untouched(tmp_path):
    config.load(tmp_path)
    path = tmp_path / "config.toml"
    original = path.read_bytes()

    with pytest.raises(config.ConfigError):
        config.apply(tmp_path, {"duty_factor": 0.5, "extract_workers": 99})

    assert path.read_bytes() == original
    assert not (tmp_path / "config.toml.bak").exists()


def test_apply_keeps_the_previous_file_as_backup(tmp_path):
    config.load(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# my own note\n", encoding="utf-8")

    config.apply(tmp_path, {"duty_factor": 0.5})

    assert "my own note" in (tmp_path / "config.toml.bak").read_text(encoding="utf-8")
    assert "my own note" not in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_detect_preset(tmp_path):
    cfg = config.load(tmp_path)
    assert config.detect_preset(cfg) == "custom"  # a fresh install is faster than "fast" and uses no CPU cap
    for name, values in config.THROTTLE_PRESETS.items():
        for key, value in values.items():
            setattr(cfg, key, value)
        assert config.detect_preset(cfg) == name
    cfg.duty_factor += 0.01
    assert config.detect_preset(cfg) == "custom"


@pytest.mark.parametrize("key,given,expected", [
    ("exclude_extensions", [".PEM", "*.Key", "env", "env"], ["pem", "key", "env"]),
    ("text_extensions", [" .Foo "], ["foo"]),
    ("text_extensions", ["txt, .md;py", "  LOG\tnfo\n*.ini "], ["txt", "md", "py", "log", "nfo", "ini"]),
    ("exclude_extensions", [".pem .key,env"], ["pem", "key", "env"]),
    ("exclude_globs", ["*/.ssh/*", "*.env"], [r"*\.ssh\*", "*.env"]),
    ("exclude_globs", ["Temp", ".*", "tmp?", "*/Private/*"], ["Temp", ".*", "tmp?", "*\\Private\\*"]),
    ("exclude_extensions", ["msi*", "*.p?m", "  .KE* "], ["msi*", "p?m", "ke*"]),
    ("exclude_paths", ["D:/Private/"], [r"D:\Private"]),
    ("roots", [r"D:\Docs\..\Work"], [r"D:\Work"]),
    ("schedule_windows", ["22:00-06:00", "7:30 - 9:05"], ["22:00-06:00", "07:30-09:05"]),
])
def test_list_entries_are_normalized(key, given, expected):
    assert config.validate_changes({key: given}) == {key: expected}


@pytest.mark.parametrize("key,given", [
    ("exclude_extensions", ["a/b"]),  # a space or a comma separates two extensions, a slash is nothing
    ("exclude_extensions", [r"a\b"]),
    ("exclude_globs", ["*"]),
    ("exclude_globs", [r"*\*"]),
    ("exclude_globs", ["*.*"]),  # looks like "everything" (old DOS wildcard), not just files with a dot
    ("exclude_globs", ["?*"]),
    ("exclude_globs", [r"*\*.*"]),
    ("exclude_extensions", ["*"]),
    ("exclude_extensions", ["?*"]),
    ("exclude_extensions", ["*.*"]),
    ("text_extensions", ["md*"]),  # only the skip list takes patterns: a text type must be a real extension
    ("exclude_paths", ["Private"]),
    ("roots", ["docs"]),
    ("schedule_windows", ["22-06"]),
    ("schedule_windows", ["25:00-06:00"]),
    ("schedule_windows", ["10:00-10:00"]),
    ("schedule_windows", ["10:60-11:00"]),
])
def test_bad_list_entries_are_rejected(key, given):
    with pytest.raises(config.ConfigError) as e:
        config.validate_changes({key: given})
    assert key in e.value.errors


def test_roots_must_exist_only_when_asked(tmp_path):
    missing = str(tmp_path / "nope")

    assert config.validate_changes({"roots": [missing]}) == {"roots": [missing]}
    with pytest.raises(config.ConfigError, match="not a folder"):
        config.validate_changes({"roots": [missing]}, check_paths=True)
    assert config.validate_changes({"roots": [str(tmp_path)]}, check_paths=True) == {"roots": [str(tmp_path)]}


def test_parse_window():
    assert config.parse_window("22:00-06:00") == (1320, 360)
    assert config.parse_window("00:00-23:59") == (0, 1439)


def test_the_index_folder_defaults_to_the_data_folder_and_is_never_indexed(tmp_path):
    cfg = config.Config(data_dir=tmp_path / "data")
    assert cfg.db_path == tmp_path / "data" / "index.db"

    cfg.db_dir = str(tmp_path / "elsewhere")

    assert cfg.db_path == tmp_path / "elsewhere" / "index.db"
    assert str(tmp_path / "elsewhere") in cfg.builtin_exclude_paths()


def test_a_new_config_skips_the_usual_cache_folders_by_name_and_pattern(tmp_path):
    cfg = config.load(tmp_path)

    assert cfg.exclude_globs == config.DEFAULT_EXCLUDE_DIR_NAMES
    assert "exclude_dir_names" not in config.render(cfg)


def test_the_old_exclude_dir_names_key_is_merged_into_the_patterns(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[index]\nexclude_dir_names = ["Cache", "node_modules"]\nexclude_globs = ["*.bak", "node_modules"]\n',
        encoding="utf-8")

    cfg = config.load(tmp_path)

    assert cfg.exclude_globs == ["Cache", "node_modules", "*.bak"]  # names first, no duplicate


def test_the_old_exclude_dir_names_key_alone_replaces_the_defaults_as_it_did_before(tmp_path):
    (tmp_path / "config.toml").write_text('[index]\nexclude_dir_names = ["Cache"]\n', encoding="utf-8")

    assert config.load(tmp_path).exclude_globs == ["Cache"]


def test_excluded_extensions_may_hold_wildcards():
    cfg = config.Config(exclude_extensions=["pem", "MSI*", ".t?p"])

    assert [cfg.ext_excluded(e) for e in ("pem", "msi", "msix", "tmp", "top", "temp", "pe", "txt")] == [
        True, True, True, True, True, False, False, False]
    assert config.Config().ext_excluded("pem")  # a default install already excludes keys and certificates
    assert not config.Config().ext_excluded("txt")
