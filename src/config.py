"""
Shared Lakebase connection helper.

Works in both:
  - Notebook context (uses dbutils for secrets)
  - Serving/App context (uses environment variables)
"""
from __future__ import annotations

import os
import uuid

import psycopg2
from databricks.sdk import WorkspaceClient

SCOPE = "wiki-rag"


def _get_secrets() -> dict[str, str]:
    """Read Lakebase config from env vars, falling back to dbutils secrets."""
    config = {
        "instance_name": os.environ.get("LAKEBASE_INSTANCE", ""),
        "db_user": os.environ.get("LAKEBASE_USER", ""),
        "db_name": os.environ.get("LAKEBASE_DB", "databricks_postgres"),
        "endpoint_host": os.environ.get("LAKEBASE_HOST", ""),
    }

    if config["instance_name"] and config["db_user"]:
        return config

    # In notebooks, dbutils is a pre-existing global — avoids the cluster_id
    # requirement that DBUtils(spark) has on serverless compute.
    try:
        _dbutils = get_ipython().user_ns.get("dbutils")  # type: ignore[name-defined]
    except NameError:
        _dbutils = None

    if _dbutils is None:
        try:
            from pyspark.dbutils import DBUtils
            from pyspark.sql import SparkSession

            _dbutils = DBUtils(SparkSession.getActiveSession())
        except Exception:
            raise ValueError(
                "Lakebase credentials not found. Set LAKEBASE_INSTANCE, LAKEBASE_USER "
                "env vars or configure the 'wiki-rag' secret scope."
            )

    config["instance_name"] = config["instance_name"] or _dbutils.secrets.get(SCOPE, "lakebase_instance_name")
    config["db_user"] = config["db_user"] or _dbutils.secrets.get(SCOPE, "lakebase_user")
    config["db_name"] = _dbutils.secrets.get(SCOPE, "lakebase_db") or config["db_name"]
    try:
        config["endpoint_host"] = config["endpoint_host"] or _dbutils.secrets.get(SCOPE, "lakebase_host")
    except Exception:
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
    db_user = config["db_user"]
    db_name = config["db_name"]
    host = config["endpoint_host"]

    instance = w.database.get_database_instance(name=instance_name)
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[instance_name],
    )
    host = host or instance.read_write_dns

    return psycopg2.connect(
        host=host,
        dbname=db_name,
        user=db_user,
        password=cred.token,
        sslmode="require",
    )
