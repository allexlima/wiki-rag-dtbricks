# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 03 — Register Model + Deploy Serving Endpoint
# MAGIC
# MAGIC Logs the WikiRAG **ResponsesAgent** to MLflow, registers it in Unity Catalog,
# MAGIC and deploys a Model Serving endpoint.
# MAGIC
# MAGIC Uses the **"Models from Code"** pattern — the model source is
# MAGIC `src/rag.py` (a `ResponsesAgent` wrapping LangGraph).

# COMMAND ----------

# MAGIC %pip install mlflow>=3.0.0 databricks-sdk databricks-openai psycopg2-binary pgvector langgraph langchain-core langchain-text-splitters tenacity --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), ".."))

import mlflow
from mlflow.models.resources import DatabricksLakebase, DatabricksServingEndpoint

# Parameters — auto-populated by DAB job base_parameters, or set manually via widgets
dbutils.widgets.text("model_name", "main.wiki_rag.wiki_rag_agent", "UC Model Name")
dbutils.widgets.text("endpoint_name", "wiki-rag-endpoint", "Serving Endpoint Name")
dbutils.widgets.text("embedding_model", "databricks-gte-large-en", "Embedding Model")
dbutils.widgets.text("llm_model", "databricks-meta-llama-3-3-70b-instruct", "LLM Model")
dbutils.widgets.text("secret_scope", "wiki-rag", "Secret Scope")
dbutils.widgets.text("lakebase_instance_name", "wiki-rag-lakebase", "Lakebase Instance")

MODEL_NAME = dbutils.widgets.get("model_name")
ENDPOINT_NAME = dbutils.widgets.get("endpoint_name")
EMBEDDING_ENDPOINT = dbutils.widgets.get("embedding_model")
LLM_ENDPOINT = dbutils.widgets.get("llm_model")
SECRET_SCOPE = dbutils.widgets.get("secret_scope")
LAKEBASE_INSTANCE = dbutils.widgets.get("lakebase_instance_name")

mlflow.set_registry_uri("databricks-uc")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Log model (ResponsesAgent)

# COMMAND ----------

resources = [
    DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
    DatabricksServingEndpoint(endpoint_name=EMBEDDING_ENDPOINT),
    DatabricksLakebase(database_instance_name=LAKEBASE_INSTANCE),
]

input_example = {
    "input": [{"role": "user", "content": "What is the main topic of the wiki?"}]
}

with mlflow.start_run(run_name="wiki-rag-agent") as run:
    model_info = mlflow.pyfunc.log_model(
        artifact_path="wiki_rag_agent",
        python_model="src/rag.py",
        code_paths=["src/"],
        resources=resources,
        input_example=input_example,
        pip_requirements=[
            "mlflow>=3.0.0,<4.0.0",
            "langgraph>=0.3.0,<0.4.0",
            "databricks-langchain>=0.5.0,<0.6.0",
            "langchain-core>=0.3.0,<0.4.0",
            "databricks-sdk>=0.40.0,<0.50.0",
            "databricks-openai>=0.2.0,<0.3.0",
            "psycopg2-binary>=2.9.0,<3.0.0",
            "pgvector>=0.3.0,<0.4.0",
            "tenacity>=8.0.0,<10.0.0",
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
# MAGIC ## Validate secrets before deployment

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

REQUIRED_SECRETS = [
    "lakebase_instance_name",
    "lakebase_host",
    "lakebase_port",
    "lakebase_db",
    "mw_role",
    "mw_password",
]

print(f"Validating secrets in scope '{SECRET_SCOPE}':")
for key in REQUIRED_SECRETS:
    try:
        val = w.secrets.get_secret(SECRET_SCOPE, key)
        print(f"  ✓ {key}")
    except Exception as e:
        raise ValueError(f"Missing required secret '{SECRET_SCOPE}/{key}': {e}") from e

print("✓ All secrets validated")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Deploy serving endpoint

# COMMAND ----------

from databricks.sdk.errors import NotFound
from databricks.sdk.service.serving import (
    EndpointCoreConfigInput,
    ServedEntityInput,
)

served_entity = ServedEntityInput(
    entity_name=MODEL_NAME,
    entity_version=str(registered.version),
    workload_size="Small",
    scale_to_zero_enabled=True,
    environment_vars={
        "LAKEBASE_INSTANCE": f"{{{{secrets/{SECRET_SCOPE}/lakebase_instance_name}}}}",
        "LAKEBASE_HOST": f"{{{{secrets/{SECRET_SCOPE}/lakebase_host}}}}",
        "LAKEBASE_PORT": f"{{{{secrets/{SECRET_SCOPE}/lakebase_port}}}}",
        "LAKEBASE_DB": f"{{{{secrets/{SECRET_SCOPE}/lakebase_db}}}}",
        "LAKEBASE_USER": f"{{{{secrets/{SECRET_SCOPE}/mw_role}}}}",
        "LAKEBASE_PASSWORD": f"{{{{secrets/{SECRET_SCOPE}/mw_password}}}}",
        "EMBEDDING_MODEL": EMBEDDING_ENDPOINT,
        "LLM_MODEL": LLM_ENDPOINT,
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
    raise TimeoutError(f"Endpoint not ready after {MAX_WAIT_SECONDS}s")

print("✓ Endpoint is ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test the endpoint (chat completions format)

# COMMAND ----------

response = w.serving_endpoints.query(
    name=ENDPOINT_NAME,
    messages=[{"role": "user", "content": "What is the main topic of the wiki?"}],
    max_tokens=500,
)

print(response.choices[0].message.content)
