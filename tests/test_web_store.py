"""Tests for web.store: config.yaml and .env read/write."""

import pytest
import yaml

from web.store import SECRET_MASK, ConfigStore


def test_write_then_read_creators_roundtrip(store):
    store.write_config(
        [{"name": "主播 A", "id": "aaa111"}, {"name": "主播 B", "id": "bbb222"}],
        "https://api.rplay.live",
    )
    data, warnings = store.read_config()
    assert warnings == []
    assert data["creators"] == [
        {"name": "主播 A", "id": "aaa111"},
        {"name": "主播 B", "id": "bbb222"},
    ]
    assert data["apiBaseUrl"] == "https://api.rplay.live"


def test_write_config_creates_parent_directory(tmp_path):
    target = tmp_path / "nested" / "deep" / "config.yaml"
    ConfigStore(config_path=target, env_path=tmp_path / ".env").write_config(
        [{"name": "A", "id": "x"}], "https://api.rplay.live"
    )
    assert target.exists()


def test_write_config_rejects_empty_creator_list(store):
    with pytest.raises(ValueError, match="至少需要保留一个主播"):
        store.write_config([], "https://api.rplay.live")


@pytest.mark.parametrize(
    "creator", [{"name": "", "id": "x"}, {"name": "A", "id": ""}, {"id": "x"}]
)
def test_write_config_rejects_invalid_creator(store, creator):
    with pytest.raises(ValueError, match="数据无效"):
        store.write_config([creator], "https://api.rplay.live")


def test_write_config_rejects_bad_api_base_url(store):
    with pytest.raises(ValueError, match="API 地址无效"):
        store.write_config([{"name": "A", "id": "x"}], "not-a-url")


def test_write_config_strips_trailing_slash(store):
    store.write_config([{"name": "A", "id": "x"}], "https://api.rplay.live/")
    data, _ = store.read_config()
    assert data["apiBaseUrl"] == "https://api.rplay.live"


def test_read_config_missing_file_reports_warning(store):
    data, warnings = store.read_config()
    assert data == {}
    assert warnings


def test_read_config_bad_yaml_keeps_previous_file_intact(store):
    store.config_path.write_text("creators: [\n", encoding="utf-8")
    data, warnings = store.read_config()
    assert data == {}
    assert any("YAML" in w for w in warnings)


def test_read_config_drops_invalid_creator_entries(store):
    store.config_path.write_text(
        yaml.safe_dump(
            {
                "creators": [
                    {"name": "ok", "id": "x1"},
                    {"name": "missing id"},
                    "not a dict",
                    {"name": "also ok", "id": "x2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    data, warnings = store.read_config()
    assert data["creators"] == [
        {"name": "ok", "id": "x1"},
        {"name": "also ok", "id": "x2"},
    ]
    assert len(warnings) == 2


def test_delete_creator_removes_only_that_entry(store):
    store.write_config(
        [{"name": "A", "id": "x1"}, {"name": "B", "id": "x2"}],
        "https://api.rplay.live",
    )
    store.delete_creator("x1")
    data, _ = store.read_config()
    assert data["creators"] == [{"name": "B", "id": "x2"}]


def test_delete_creator_unknown_id_raises(store):
    store.write_config([{"name": "A", "id": "x1"}], "https://api.rplay.live")
    with pytest.raises(ValueError, match="找不到该主播"):
        store.delete_creator("nope")


def test_delete_last_creator_is_refused(store):
    store.write_config([{"name": "A", "id": "x1"}], "https://api.rplay.live")
    with pytest.raises(ValueError, match="至少需要保留一个主播"):
        store.delete_creator("x1")


# ---------- .env ----------


def test_read_env_parses_quotes_and_comments(store):
    store.env_path.write_text(
        "# comment\nUSER_OID=123\n\nDISCORD_WEBHOOK_URL=\"https://x/y\"\nBAD LINE\n",
        encoding="utf-8",
    )
    values = store.read_env()
    assert values["USER_OID"] == "123"
    assert values["DISCORD_WEBHOOK_URL"] == "https://x/y"
    assert "BAD LINE" not in values


def test_update_env_creates_file_when_missing(store):
    changed = store.update_env({"USER_OID": "42"})
    assert changed == ["USER_OID"]
    assert store.read_env()["USER_OID"] == "42"


def test_update_env_preserves_comments_and_unknown_keys(store):
    store.env_path.write_text(
        "# my notes\nUSER_OID=old\nCUSTOM_KEY=keepme\n", encoding="utf-8"
    )
    store.update_env({"USER_OID": "new"})
    text = store.env_path.read_text(encoding="utf-8")
    assert "# my notes" in text
    assert "CUSTOM_KEY=keepme" in text
    assert "USER_OID=new" in text
    assert "USER_OID=old" not in text


def test_update_env_appends_missing_key(store):
    store.env_path.write_text("# only comments\n", encoding="utf-8")
    store.update_env({"LOG_LEVEL": "DEBUG"})
    assert store.read_env()["LOG_LEVEL"] == "DEBUG"


def test_update_env_secret_mask_keeps_real_value(store):
    store.env_path.write_text("REFRESH_TOKEN=real-token\n", encoding="utf-8")
    changed = store.update_env({"REFRESH_TOKEN": SECRET_MASK})
    assert changed == []
    assert store.read_env()["REFRESH_TOKEN"] == "real-token"


def test_update_env_ignores_unknown_keys(store):
    assert store.update_env({"NOT_A_FIELD": "x"}) == []
    assert store.read_env() == {}


def test_update_env_reports_no_change_when_identical(store):
    store.env_path.write_text("INTERVAL=60\n", encoding="utf-8")
    assert store.update_env({"INTERVAL": "60"}) == []


def test_blank_value_clears_optional_key(store):
    store.env_path.write_text("DISCORD_WEBHOOK_URL=https://x\n", encoding="utf-8")
    store.update_env({"DISCORD_WEBHOOK_URL": ""})
    assert store.read_env()["DISCORD_WEBHOOK_URL"] == ""


@pytest.mark.parametrize("value", ["10", "120", "3600"])
def test_update_env_accepts_valid_interval(store, value):
    assert store.update_env({"INTERVAL": value}) == ["INTERVAL"]


@pytest.mark.parametrize("value", ["9", "3601", "abc", "-1"])
def test_update_env_rejects_invalid_interval(store, value):
    with pytest.raises(ValueError, match="取值不合法"):
        store.update_env({"INTERVAL": value})


@pytest.mark.parametrize("value", ["DEBUG", "info", "CRITICAL"])
def test_update_env_accepts_log_level(store, value):
    assert store.update_env({"LOG_LEVEL": value}) == ["LOG_LEVEL"]


def test_update_env_rejects_invalid_log_level(store):
    with pytest.raises(ValueError, match="取值不合法"):
        store.update_env({"LOG_LEVEL": "LOUD"})


def test_rejected_value_leaves_file_untouched(store):
    """Validation runs for every key before anything is written."""
    store.env_path.write_text("USER_OID=keep\nINTERVAL=60\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.update_env({"USER_OID": "changed", "INTERVAL": "99999"})
    assert store.read_env()["USER_OID"] == "keep"
    assert store.read_env()["INTERVAL"] == "60"


def test_env_status_flags_placeholder_values(store):
    store.env_path.write_text(
        "USER_OID=your_user_oid\nREFRESH_TOKEN=real\n", encoding="utf-8"
    )
    status = store.env_status()
    assert status["USER_OID"]["placeholder"] is True
    assert status["USER_OID"]["set"] is True
    assert status["REFRESH_TOKEN"]["placeholder"] is False
    assert status["LOG_LEVEL"]["set"] is False


def test_env_template_falls_back_to_generated(store):
    template = store.env_template()
    assert "USER_OID=" in template
    assert "REFRESH_TOKEN=" in template
