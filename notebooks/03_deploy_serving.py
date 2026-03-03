# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Register Model + Deploy Serving Endpoint
# MAGIC
# MAGIC Logs the WikiRAG PyFunc model to MLflow, registers it in Unity Catalog,
# MAGIC and creates/updates a model serving endpoint.

# COMMAND ----------

# MAGIC %pip install mlflow databricks-sdk databricks-openai psycopg2-binary pgvector langgraph langchain-core langchain-text-splitters --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

import mlflow
import mlflow.pyfunc
from mlflow.models.signature import ModelSignature
from mlflow.types.schema import ColSpec, Schema

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

CATALOG = "main"
SCHEMA = "wiki_rag"
MODEL_NAME = f"{CATALOG}.{SCHEMA}.wiki_rag_agent"

mlflow.set_registry_uri("databricks-uc")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Log model with "Models from Code" pattern

# COMMAND ----------

input_schema = Schema([ColSpec("string", "question")])
output_schema = Schema([ColSpec("string", "answer"), ColSpec("string", "sources")])
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

print(f"Model logged: run_id={run_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Register in Unity Catalog

# COMMAND ----------

registered = mlflow.register_model(
    model_uri=f"runs:/{run_id}/wiki_rag_agent",
    name=MODEL_NAME,
)
print(f"Registered: {MODEL_NAME} version {registered.version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Deploy serving endpoint

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import (
    EndpointCoreConfigInput,
    ServedEntityInput,
)

w = WorkspaceClient()
ENDPOINT_NAME = "wiki-rag-endpoint"

served_entity = ServedEntityInput(
    entity_name=MODEL_NAME,
    entity_version=str(registered.version),
    workload_size="Small",
    scale_to_zero_enabled=True,
    environment_vars={
        "LAKEBASE_INSTANCE": "{{secrets/wiki-rag/lakebase_instance_name}}",
        "LAKEBASE_DB": "{{secrets/wiki-rag/lakebase_db}}",
        "LAKEBASE_USER": "{{secrets/wiki-rag/lakebase_user}}",
        "EMBEDDING_MODEL": "databricks-gte-large-en",
        "LLM_MODEL": "databricks-meta-llama-3-3-70b-instruct",
    },
)

# Create or update endpoint
try:
    existing = w.serving_endpoints.get(ENDPOINT_NAME)
    print(f"Updating existing endpoint: {ENDPOINT_NAME}")
    w.serving_endpoints.update_config(
        name=ENDPOINT_NAME,
        served_entities=[served_entity],
    )
except Exception:
    print(f"Creating new endpoint: {ENDPOINT_NAME}")
    w.serving_endpoints.create(
        name=ENDPOINT_NAME,
        config=EndpointCoreConfigInput(served_entities=[served_entity]),
    )

print(f"Endpoint '{ENDPOINT_NAME}' deployed with model version {registered.version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test the endpoint

# COMMAND ----------

import time

# Wait for endpoint to be ready
print("Waiting for endpoint to be ready...")
while True:
    ep = w.serving_endpoints.get(ENDPOINT_NAME)
    state = ep.state.ready
    if state == "READY":
        break
    print(f"  State: {state} — waiting 30s...")
    time.sleep(30)

print("Endpoint is ready! Testing...")

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
