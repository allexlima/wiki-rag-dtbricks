"""
Shared Lakebase connection helper.

Works in both:
  - Notebook context (reads secrets via dbutils)
  - Serving/App context (reads environment variables)

Prefers password auth (static `mediawiki` role) when available.
Falls back to OAuth token generation via the Databricks SDK.
"""
from __future__ import annotations

import logging
import os
import uuid
from urllib.parse import quote_plus

import psycopg2
from databricks.sdk import WorkspaceClient

log = logging.getLogger(__name__)

SCOPE = "wiki-rag"
CONNECT_TIMEOUT_SECONDS = 30
DEFAULT_PORT = "5432"


def _get_dbutils():
    """Resolve the notebook dbutils global, or None outside notebooks."""
    # Prefer the notebook-injected global (works on serverless without cluster_id).
    try:
        return get_ipython().user_ns.get("dbutils")  # type: ignore[name-defined]
    except NameError:
        pass

    # Fallback: construct via PySpark (classic compute only).
    try:
        from pyspark.dbutils import DBUtils
        from pyspark.sql import SparkSession

        return DBUtils(SparkSession.getActiveSession())
    except Exception:
        return None


def _get_secrets() -> dict[str, str]:
    """Read Lakebase config from env vars, falling back to dbutils secrets."""
    config = {
        "instance_name": os.environ.get("LAKEBASE_INSTANCE", ""),
        "db_user": os.environ.get("LAKEBASE_USER", ""),
        "db_name": os.environ.get("LAKEBASE_DB", "wikidb"),
        "endpoint_host": os.environ.get("LAKEBASE_HOST", ""),
        "port": os.environ.get("LAKEBASE_PORT", DEFAULT_PORT),
        "password": os.environ.get("LAKEBASE_PASSWORD", ""),
    }

    # If we have host + user + password from env, that's enough for direct auth
    if config["endpoint_host"] and config["db_user"] and config["password"]:
        return config

    # If we have instance + user from env, that's enough for OAuth
    if config["instance_name"] and config["db_user"]:
        return config

    dbu = _get_dbutils()
    if dbu is None:
        raise ValueError(
            "Lakebase credentials not found. Set LAKEBASE_HOST + LAKEBASE_USER + "
            "LAKEBASE_PASSWORD env vars, or LAKEBASE_INSTANCE + LAKEBASE_USER for "
            "OAuth, or configure the 'wiki-rag' secret scope."
        )

    def secret(key: str) -> str:
        return dbu.secrets.get(SCOPE, key)

    def secret_or(key: str, default: str) -> str:
        try:
            return secret(key) or default
        except Exception:  # noqa: BLE001 — Py4J wraps Java exceptions
            return default

    config["instance_name"] = config["instance_name"] or secret("lakebase_instance_name")
    config["db_user"] = config["db_user"] or secret("lakebase_user")
    config["db_name"] = secret_or("lakebase_db", config["db_name"])
    config["endpoint_host"] = config["endpoint_host"] or secret_or("lakebase_host", "")
    config["port"] = secret_or("lakebase_port", config["port"])
    config["password"] = config["password"] or secret_or("mw_password", "")

    # mw_role is the native PG role that owns the password — use it instead of
    # the workspace user (lakebase_user) when password auth is active.
    if config["password"]:
        config["db_user"] = secret_or("mw_role", config["db_user"])

    return config


def get_lakebase_conn(
    w: WorkspaceClient | None = None,
) -> psycopg2.extensions.connection:
    """Open a psycopg2 connection to Lakebase.

    Uses password auth when LAKEBASE_PASSWORD (or the mw_password secret) is
    available.  Falls back to OAuth token generation via the SDK otherwise.
    """
    config = _get_secrets()
    host = config["endpoint_host"]
    port = config["port"]

    # Password auth — simpler, no token expiration
    if config["password"] and host:
        return psycopg2.connect(
            host=host,
            port=port,
            dbname=config["db_name"],
            user=config["db_user"],
            password=config["password"],
            sslmode="require",
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )

    # OAuth token auth — requires SDK calls
    if w is None:
        w = WorkspaceClient()

    instance_name = config["instance_name"]
    instance = w.database.get_database_instance(name=instance_name)
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[instance_name],
    )

    return psycopg2.connect(
        host=host or instance.read_write_dns,
        port=port,
        dbname=config["db_name"],
        user=config["db_user"],
        password=cred.token,
        sslmode="require",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )


def get_lakebase_conn_string() -> str:
    """Build a PostgreSQL connection URI for libraries that need a string (e.g.
    langgraph-checkpoint-postgres).

    Requires password auth (host + user + password).  OAuth tokens are
    short-lived and unsuitable for connection-string-based libraries.
    """
    config = _get_secrets()
    host = config["endpoint_host"]
    port = config["port"]
    user = config["db_user"]
    password = config["password"]
    dbname = config["db_name"]

    if not (host and user and password):
        raise ValueError(
            "get_lakebase_conn_string() requires password auth. "
            "Set LAKEBASE_HOST, LAKEBASE_USER, and LAKEBASE_PASSWORD."
        )

    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{dbname}?sslmode=require"
    )


# ---------------------------------------------------------------------------
# Bundle configuration reader
# ---------------------------------------------------------------------------

def load_bundle_defaults() -> dict[str, str]:
    """Read variable defaults from ``databricks.yml``.

    Resolves ``${var.X}`` references within default values.
    Searches for ``databricks.yml`` in the current directory and one level up.

    Returns:
        Dict mapping variable names to their default values.
    """
    import yaml

    for candidate in ("databricks.yml", "../databricks.yml"):
        path = os.path.join(os.getcwd(), candidate)
        if os.path.isfile(path):
            break
    else:
        raise FileNotFoundError("databricks.yml not found")

    with open(path) as f:
        cfg = yaml.safe_load(f)

    raw = {k: v.get("default", "") for k, v in cfg.get("variables", {}).items()}

    # Resolve ${var.X} references (single pass — no nested refs)
    import re
    resolved = {}
    for k, v in raw.items():
        resolved[k] = re.sub(
            r"\$\{var\.(\w+)\}",
            lambda m: raw.get(m.group(1), m.group(0)),
            str(v),
        )
    return resolved
