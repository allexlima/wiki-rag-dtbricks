"""Tests for src.config — connection string building and secret resolution."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# get_lakebase_conn_string tests (mock _get_secrets)
# ---------------------------------------------------------------------------

def _make_secrets(
    host="lakebase.example.com",
    user="wiki_user",
    password="s3cret",
    port="5432",
    db_name="wikidb",
    instance_name="my-instance",
):
    return {
        "instance_name": instance_name,
        "db_user": user,
        "db_name": db_name,
        "endpoint_host": host,
        "port": port,
        "password": password,
    }


@patch("src.config._get_secrets")
def test_get_lakebase_conn_string_formats_uri(mock_secrets):
    """Connection string follows postgresql://user:pass@host:port/db?sslmode=require."""
    from src.config import get_lakebase_conn_string

    mock_secrets.return_value = _make_secrets()
    uri = get_lakebase_conn_string()

    assert uri.startswith("postgresql://")
    assert "wiki_user" in uri
    assert "s3cret" in uri
    assert "@lakebase.example.com:5432/wikidb" in uri
    assert uri.endswith("?sslmode=require")


@patch("src.config._get_secrets")
def test_get_lakebase_conn_string_encodes_special_chars(mock_secrets):
    """Special characters in user/password are percent-encoded."""
    from src.config import get_lakebase_conn_string

    mock_secrets.return_value = _make_secrets(user="u@er", password="p@ss#w$rd")
    uri = get_lakebase_conn_string()

    # '@' in user → %40, '#' → %23, '$' → %24
    assert "u%40er" in uri
    assert "p%40ss%23w%24rd" in uri
    # The literal '@' separating credentials from host must still be present
    assert "p%40ss%23w%24rd@lakebase.example.com" in uri


@patch("src.config._get_secrets")
def test_get_lakebase_conn_string_missing_password_raises(mock_secrets):
    """ValueError is raised when password is empty."""
    from src.config import get_lakebase_conn_string

    mock_secrets.return_value = _make_secrets(password="")

    with pytest.raises(ValueError, match="password auth"):
        get_lakebase_conn_string()


# ---------------------------------------------------------------------------
# _get_secrets tests (env var behaviour)
# ---------------------------------------------------------------------------

def test_get_secrets_reads_env_vars(monkeypatch):
    """Environment variables are returned in the config dict."""
    from src.config import _get_secrets

    monkeypatch.setenv("LAKEBASE_HOST", "env-host.example.com")
    monkeypatch.setenv("LAKEBASE_USER", "env_user")
    monkeypatch.setenv("LAKEBASE_PASSWORD", "env_pass")
    monkeypatch.setenv("LAKEBASE_DB", "env_db")
    monkeypatch.setenv("LAKEBASE_PORT", "9999")
    monkeypatch.setenv("LAKEBASE_INSTANCE", "env-instance")

    config = _get_secrets()

    assert config["endpoint_host"] == "env-host.example.com"
    assert config["db_user"] == "env_user"
    assert config["password"] == "env_pass"
    assert config["db_name"] == "env_db"
    assert config["port"] == "9999"
    assert config["instance_name"] == "env-instance"


@patch("src.config._get_dbutils")
def test_get_secrets_env_vars_take_priority(mock_dbutils, monkeypatch):
    """Env vars are preferred over dbutils secrets — dbutils should not be called."""
    from src.config import _get_secrets

    monkeypatch.setenv("LAKEBASE_HOST", "env-host")
    monkeypatch.setenv("LAKEBASE_USER", "env_user")
    monkeypatch.setenv("LAKEBASE_PASSWORD", "env_pass")

    config = _get_secrets()

    assert config["endpoint_host"] == "env-host"
    assert config["db_user"] == "env_user"
    assert config["password"] == "env_pass"
    # dbutils should never be consulted when env vars satisfy requirements
    mock_dbutils.assert_not_called()


def test_get_secrets_oauth_path(monkeypatch):
    """Instance + user (no password) is enough for OAuth path."""
    from src.config import _get_secrets

    monkeypatch.setenv("LAKEBASE_INSTANCE", "my-instance")
    monkeypatch.setenv("LAKEBASE_USER", "user@databricks.com")
    monkeypatch.delenv("LAKEBASE_PASSWORD", raising=False)
    monkeypatch.delenv("LAKEBASE_HOST", raising=False)

    config = _get_secrets()

    assert config["instance_name"] == "my-instance"
    assert config["db_user"] == "user@databricks.com"
    assert config["password"] == ""


# ---------------------------------------------------------------------------
# _get_dbutils
# ---------------------------------------------------------------------------

def test_get_dbutils_returns_none_outside_notebook():
    """_get_dbutils returns None when not in a notebook/PySpark context."""
    from src.config import _get_dbutils
    assert _get_dbutils() is None


# ---------------------------------------------------------------------------
# _get_secrets with dbutils fallback
# ---------------------------------------------------------------------------

@patch("src.config._get_dbutils")
def test_get_secrets_falls_back_to_dbutils(mock_dbutils, monkeypatch):
    """When no env vars satisfy auth, _get_secrets falls back to dbutils secrets."""
    from src.config import _get_secrets

    for key in ["LAKEBASE_HOST", "LAKEBASE_USER", "LAKEBASE_PASSWORD",
                "LAKEBASE_INSTANCE", "LAKEBASE_DB", "LAKEBASE_PORT"]:
        monkeypatch.delenv(key, raising=False)

    mock_dbu = MagicMock()
    mock_dbu.secrets.get.side_effect = lambda scope, key: {
        "lakebase_instance_name": "test-instance",
        "lakebase_user": "test@user.com",
        "lakebase_db": "testdb",
        "lakebase_host": "test-host.com",
        "lakebase_port": "5432",
        "mw_password": "testpass",
        "mw_role": "mw_user",
    }.get(key, "")
    mock_dbutils.return_value = mock_dbu

    config = _get_secrets()

    assert config["instance_name"] == "test-instance"
    assert config["db_user"] == "mw_user"
    assert config["password"] == "testpass"


@patch("src.config._get_dbutils", return_value=None)
def test_get_secrets_raises_when_no_credentials(mock_dbutils, monkeypatch):
    """ValueError raised when neither env vars nor dbutils are available."""
    from src.config import _get_secrets

    for key in ["LAKEBASE_HOST", "LAKEBASE_USER", "LAKEBASE_PASSWORD",
                "LAKEBASE_INSTANCE", "LAKEBASE_DB", "LAKEBASE_PORT"]:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError, match="credentials not found"):
        _get_secrets()


@patch("src.config._get_dbutils")
def test_get_secrets_secret_or_catches_exception(mock_dbutils, monkeypatch):
    """secret_or returns default when dbutils.secrets.get raises for optional keys."""
    from src.config import _get_secrets

    for key in ["LAKEBASE_HOST", "LAKEBASE_USER", "LAKEBASE_PASSWORD",
                "LAKEBASE_INSTANCE", "LAKEBASE_DB", "LAKEBASE_PORT"]:
        monkeypatch.delenv(key, raising=False)

    def mock_get(scope, key):
        # Required keys succeed
        if key in ("lakebase_instance_name", "lakebase_user"):
            return {"lakebase_instance_name": "inst", "lakebase_user": "usr"}[key]
        # Optional keys (called via secret_or) raise
        raise RuntimeError("Py4J: secret not found")

    mock_dbu = MagicMock()
    mock_dbu.secrets.get.side_effect = mock_get
    mock_dbutils.return_value = mock_dbu

    config = _get_secrets()

    assert config["instance_name"] == "inst"
    assert config["db_user"] == "usr"
    # secret_or should have caught the exception and returned defaults
    assert config["db_name"] == "wikidb"
    assert config["port"] == "5432"
    assert config["password"] == ""


@patch("src.config._get_secrets")
def test_get_lakebase_conn_string_missing_host_raises(mock_secrets):
    """ValueError raised when host is empty."""
    from src.config import get_lakebase_conn_string

    mock_secrets.return_value = _make_secrets(host="")
    with pytest.raises(ValueError):
        get_lakebase_conn_string()


@patch("src.config._get_secrets")
def test_get_lakebase_conn_string_missing_user_raises(mock_secrets):
    """ValueError raised when user is empty."""
    from src.config import get_lakebase_conn_string

    mock_secrets.return_value = _make_secrets(user="")
    with pytest.raises(ValueError):
        get_lakebase_conn_string()
