"""
Shared Lakebase connection helper.

Works in both:
  - Notebook context (reads secrets via dbutils)
  - Serving/App context (reads environment variables)
"""
from __future__ import annotations

import os
import uuid

import psycopg2
from databricks.sdk import WorkspaceClient

SCOPE = "wiki-rag"
CONNECT_TIMEOUT_SECONDS = 30


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
    }

    if config["instance_name"] and config["db_user"]:
        return config

    dbu = _get_dbutils()
    if dbu is None:
        raise ValueError(
            "Lakebase credentials not found. Set LAKEBASE_INSTANCE, LAKEBASE_USER "
            "env vars or configure the 'wiki-rag' secret scope."
        )

    def secret(key: str) -> str:
        return dbu.secrets.get(SCOPE, key)

    config["instance_name"] = config["instance_name"] or secret("lakebase_instance_name")
    config["db_user"] = config["db_user"] or secret("lakebase_user")
    config["db_name"] = secret("lakebase_db") or config["db_name"]

    # endpoint_host may not exist yet if this runs before 00_setup_lakebase stores it.
    # dbutils raises py4j.protocol.Py4JJavaError when a secret key doesn't exist.
    try:
        config["endpoint_host"] = config["endpoint_host"] or secret("lakebase_host")
    except Exception:  # noqa: BLE001 — Py4J wraps Java exceptions; can't narrow further
        pass

    return config


def get_lakebase_conn(
    w: WorkspaceClient | None = None,
) -> psycopg2.extensions.connection:
    """Open a psycopg2 connection to Lakebase Provisioned using OAuth credentials."""
    if w is None:
        w = WorkspaceClient()

    config = _get_secrets()
    instance_name = config["instance_name"]

    instance = w.database.get_database_instance(name=instance_name)
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[instance_name],
    )

    return psycopg2.connect(
        host=config["endpoint_host"] or instance.read_write_dns,
        dbname=config["db_name"],
        user=config["db_user"],
        password=cred.token,
        sslmode="require",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
