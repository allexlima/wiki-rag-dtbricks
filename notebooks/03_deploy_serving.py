# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 03 — Register Model + Deploy Serving Endpoint
# MAGIC
# MAGIC Logs the WikiRAG PyFunc model to MLflow, registers it in Unity Catalog,
# MAGIC and creates or updates a Model Serving endpoint.
# MAGIC
# MAGIC Uses the **"Models from Code"** pattern — the model source is
# MAGIC `src/serving/pyfunc_model.py`, not a serialized pickle.

# COMMAND ----------

# MAGIC %pip install mlflow databricks-sdk databricks-openai psycopg2-binary pgvector langgraph langchain-core langchain-text-splitters --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

import mlflow
from mlflow.models.signature import ModelSignature
from mlflow.types.schema import ColSpec, Schema

CATALOG = "main"
SCHEMA = "wiki_rag"
MODEL_NAME = f"{CATALOG}.{SCHEMA}.wiki_rag_agent"
ENDPOINT_NAME = "wiki-rag-endpoint"

mlflow.set_registry_uri("databricks-uc")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Log model

# COMMAND ----------

input_schema = Schema([ColSpec("string", "question")])
output_schema = Schema([
    ColSpec("string", "answer"),
    ColSpec("string", "sources"),
])
signature = ModelSignature(inputs=input_schema, outputs=output_schema)

with mlflow.start_run(run_name="wiki-rag-agent") as run:
    model_info = mlflow.pyfunc.log_model(
        artifact_path="wiki_rag_agent",
        python_model="src/serving/pyfunc_model.py",
        code_paths=["src/"],
        signature=signature,
        pip_requirements=[
            "mlflow>=2.17.0",
            "databricks-sdk>=0.40.0",
            "databricks-openai>=0.2.0",
            "psycopg2-binary>=2.9.0",
            "pgvector>=0.3.0",
            "langchain-text-splitters>=0.3.0",
        ],
    )
    run_id = run.info.run_id

print(f"✓ Model logged (run_id={run_id})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Register in Unity Catalog

# COMMAND ----------

registered = mlflow.register_model(
    model_uri=f"runs:/{run_id}/wiki_rag_agent",
    name=MODEL_NAME,
)
print(f"✓ Registered {MODEL_NAME} v{registered.version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Deploy serving endpoint

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.serving import (
    EndpointCoreConfigInput,
    ServedEntityInput,
)

w = WorkspaceClient()

served_entity = ServedEntityInput(
    entity_name=MODEL_NAME,
    entity_version=str(registered.version),
    workload_size="Small",
    scale_to_zero_enabled=True,
    environment_vars={
        "LAKEBASE_INSTANCE": "{{secrets/wiki-rag/lakebase_instance_name}}",
        "LAKEBASE_HOST": "{{secrets/wiki-rag/lakebase_host}}",
        "LAKEBASE_PORT": "{{secrets/wiki-rag/lakebase_port}}",
        "LAKEBASE_DB": "{{secrets/wiki-rag/lakebase_db}}",
        "LAKEBASE_USER": "{{secrets/wiki-rag/mw_role}}",
        "LAKEBASE_PASSWORD": "{{secrets/wiki-rag/mw_password}}",
        "EMBEDDING_MODEL": "databricks-gte-large-en",
        "LLM_MODEL": "databricks-meta-llama-3-3-70b-instruct",
    },
)

try:
    w.serving_endpoints.get(ENDPOINT_NAME)
    print(f"⏳ Updating endpoint '{ENDPOINT_NAME}' ...")
    w.serving_endpoints.update_config(
        name=ENDPOINT_NAME,
        served_entities=[served_entity],
    )
except NotFound:
    print(f"⏳ Creating endpoint '{ENDPOINT_NAME}' ...")
    w.serving_endpoints.create(
        name=ENDPOINT_NAME,
        config=EndpointCoreConfigInput(
            served_entities=[served_entity],
        ),
    )

print(f"✓ Endpoint '{ENDPOINT_NAME}' deployed (v{registered.version})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Wait for endpoint readiness

# COMMAND ----------

import time

MAX_WAIT_SECONDS = 900  # 15 min
POLL_INTERVAL = 30
elapsed = 0

print(f"Waiting for '{ENDPOINT_NAME}' to become ready ...")
while elapsed < MAX_WAIT_SECONDS:
    ep = w.serving_endpoints.get(ENDPOINT_NAME)
    if ep.state.ready == "READY":
        break
    print(f"  state={ep.state.ready} — polling in {POLL_INTERVAL}s ...")
    time.sleep(POLL_INTERVAL)
    elapsed += POLL_INTERVAL
else:
    raise TimeoutError(
        f"Endpoint not ready after {MAX_WAIT_SECONDS}s"
    )

print("✓ Endpoint is ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test the endpoint

# COMMAND ----------

from databricks.sdk.service.serving import DataframeSplitInput

response = w.serving_endpoints.query(
    name=ENDPOINT_NAME,
    dataframe_split=DataframeSplitInput(
        columns=["question"],
        data=[["What is the main topic of the wiki?"]],
    ),
)

print(response.predictions)
