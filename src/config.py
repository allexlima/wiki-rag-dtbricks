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


def get_lakebase_conn(
    w: WorkspaceClient | None = None,
    instance_name: str | None = None,
    db_user: str | None = None,
) -> psycopg2.extensions.connection:
    """
    Open a psycopg2 connection to Lakebase using OAuth credentials.

    Parameters are resolved in order:
      1. Explicit arguments
      2. Environment variables (LAKEBASE_INSTANCE, LAKEBASE_USER, LAKEBASE_DB)
      3. Databricks secrets via dbutils (fallback for notebooks)
    """
    if w is None:
        w = WorkspaceClient()

    instance_name = instance_name or os.environ.get("LAKEBASE_INSTANCE")
    db_user = db_user or os.environ.get("LAKEBASE_USER")
    db_name = os.environ.get("LAKEBASE_DB", "databricks_postgres")

    # Fallback: read from dbutils secrets if env vars not set
    if not instance_name or not db_user:
        try:
            from pyspark.dbutils import DBUtils  # noqa: F811
            from pyspark.sql import SparkSession

            spark = SparkSession.getActiveSession()
            dbutils = DBUtils(spark)
            instance_name = instance_name or dbutils.secrets.get("wiki-rag", "lakebase_instance_name")
            db_user = db_user or dbutils.secrets.get("wiki-rag", "lakebase_user")
            db_name = dbutils.secrets.get("wiki-rag", "lakebase_db") or db_name
        except Exception:
            raise ValueError(
                "Lakebase credentials not found. Set LAKEBASE_INSTANCE, LAKEBASE_USER "
                "env vars or configure the 'wiki-rag' secret scope."
            )

    instance = w.database.get_database_instance(name=instance_name)
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[instance_name],
    )

    return psycopg2.connect(
        host=instance.read_write_dns,
        dbname=db_name,
        user=db_user,
        password=cred.token,
        sslmode="require",
    )
