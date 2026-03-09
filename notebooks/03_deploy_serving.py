# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 03 — Register Model + Deploy Serving Endpoint
# MAGIC 
# MAGIC Logs the WikiRAG **ResponsesAgent** to MLflow, registers it in Unity Catalog,
# MAGIC and deploys a Model Serving endpoint using the **`databricks-agents` SDK** —
# MAGIC the Databricks-recommended approach for production agent deployment.
# MAGIC 
# MAGIC **Architecture — Models from Code pattern:**
# MAGIC ```
# MAGIC src/rag.py (ResponsesAgent + LangGraph)
# MAGIC   │  mlflow.pyfunc.log_model(python_model="src/rag.py")
# MAGIC   ▼
# MAGIC Unity Catalog (main.wiki_rag.wiki_rag_agent)
# MAGIC   │  agents.deploy()
# MAGIC   ▼
# MAGIC Model Serving Endpoint (wiki-rag-endpoint)
# MAGIC   ├── env vars injected from Databricks secret scope
# MAGIC   ├── DatabricksServingEndpoint resources (LLM + embeddings)
# MAGIC   ├── DatabricksLakebase resource (vector store + memory)
# MAGIC   └── Review App (auto-generated for testing)
# MAGIC ```
# MAGIC 
# MAGIC **Why `agents.deploy()`?**
# MAGIC - Single call replaces manual endpoint create/update + polling
# MAGIC - Automatically creates a **Review App** for interactive testing
# MAGIC - Handles secret injection, resource provisioning, and version management
# MAGIC - Recommended by Databricks for all GenAI agent deployments
# MAGIC 
# MAGIC **Prerequisites:**
# MAGIC - Run `00_setup_lakebase.py` first (Lakebase + secrets must exist)
# MAGIC - Run `make setup-secrets` if not already done
# MAGIC 
# MAGIC | Step | What it does |
# MAGIC |------|-------------|
# MAGIC | 1 | Log model to MLflow using Models from Code (`src/rag.py`) |
# MAGIC | 2 | Register model version in Unity Catalog |
# MAGIC | 3 | Validate all required secrets exist before deployment |
# MAGIC | 4 | Deploy via `agents.deploy()` (endpoint + Review App) |
# MAGIC | 5 | Smoke test via chat completions query |
# MAGIC 
# MAGIC > **Idempotent** — safe to re-run. `register_model` creates a new version,
# MAGIC > and `agents.deploy()` updates the endpoint atomically.

# COMMAND ----------

# MAGIC %pip install databricks-langchain langgraph psycopg2-binary pgvector mwparserfromhell tenacity databricks-agents
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC 
# MAGIC Parameters are auto-populated by the DAB job (`resources/jobs.yml`), or you can
# MAGIC set them manually via the widget bar when running interactively.

# COMMAND ----------

import os
import sys

# ─── Bundle root resolution ──────────────────────────────────────────
# DAB deploys notebooks/ and src/ as siblings under .bundle/.../files/.
# Notebook CWD may point to notebooks/, so we go up one level to reach
# the bundle root where src/ lives alongside notebooks/.
_cwd = os.getcwd()
if os.path.basename(_cwd) == "notebooks":
    BUNDLE_ROOT = os.path.dirname(_cwd)
else:
    BUNDLE_ROOT = _cwd
sys.path.insert(0, BUNDLE_ROOT)
os.chdir(BUNDLE_ROOT)

# COMMAND ----------

import mlflow
from databricks.sdk import WorkspaceClient
from mlflow.models.resources import DatabricksLakebase, DatabricksServingEndpoint
from src.config import load_bundle_defaults

# ─── Widget parameters (defaults from databricks.yml) ────────────────

_defaults = load_bundle_defaults()

dbutils.widgets.text("model_name", _defaults["model_name"], "UC Model Name")
dbutils.widgets.text(
    "endpoint_name", _defaults["endpoint_name"], "Serving Endpoint Name"
)
dbutils.widgets.text("embedding_model", _defaults["embedding_model"], "Embedding Model")
dbutils.widgets.text("llm_model", _defaults["llm_model"], "LLM Model")
dbutils.widgets.text("secret_scope", _defaults["secret_scope"], "Secret Scope")
dbutils.widgets.text(
    "lakebase_instance_name", _defaults["lakebase_instance_name"], "Lakebase Instance"
)

# COMMAND ----------

MODEL_NAME = dbutils.widgets.get("model_name")
ENDPOINT_NAME = dbutils.widgets.get("endpoint_name")
EMBEDDING_ENDPOINT = dbutils.widgets.get("embedding_model")
LLM_ENDPOINT = dbutils.widgets.get("llm_model")
SECRET_SCOPE = dbutils.widgets.get("secret_scope")
LAKEBASE_INSTANCE = dbutils.widgets.get("lakebase_instance_name")

# ─── Validate parameters ─────────────────────────────────────────────

_params = {
    "model_name": MODEL_NAME,
    "endpoint_name": ENDPOINT_NAME,
    "embedding_model": EMBEDDING_ENDPOINT,
    "llm_model": LLM_ENDPOINT,
    "secret_scope": SECRET_SCOPE,
    "lakebase_instance_name": LAKEBASE_INSTANCE,
}
for _name, _val in _params.items():
    assert _val and _val.strip(), f"Widget '{_name}' must be non-empty"

mlflow.set_registry_uri("databricks-uc")
w = WorkspaceClient()
CURRENT_USER = w.current_user.me().user_name

# Ensure the UC catalog + schema exist for model registration.
# MODEL_NAME format: "catalog.schema.model_name"
_catalog, _schema, _ = MODEL_NAME.split(".")
w.catalogs.get(_catalog)  # raises if catalog doesn't exist
try:
    w.schemas.get(f"{_catalog}.{_schema}")
except Exception:
    w.schemas.create(name=_schema, catalog_name=_catalog)
    print(f"Created UC schema: {_catalog}.{_schema}")

# Ensure DATABRICKS_HOST is set so ChatDatabricks can resolve the API URL
# during log_model validation. On serving, this is auto-injected.
if "DATABRICKS_HOST" not in os.environ:
    os.environ["DATABRICKS_HOST"] = w.config.host

print(f"User: {CURRENT_USER}")
print(f"Model: {MODEL_NAME}")
print(f"Endpoint: {ENDPOINT_NAME}")
print(f"LLM: {LLM_ENDPOINT}  |  Embeddings: {EMBEDDING_ENDPOINT}")
print(f"Lakebase: {LAKEBASE_INSTANCE}  |  Scope: {SECRET_SCOPE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Log Model to MLflow
# MAGIC 
# MAGIC Uses the **Models from Code** pattern: `python_model` points to `src/rag.py`
# MAGIC (a `ResponsesAgent` wrapping a LangGraph graph). MLflow captures the source
# MAGIC code as-is rather than pickling a Python object — this makes the model fully
# MAGIC reproducible, auditable, and diff-friendly in version control.
# MAGIC 
# MAGIC **Resources declared:**
# MAGIC - `DatabricksServingEndpoint` for the LLM and embedding model — tells Databricks
# MAGIC   which Foundation Model API endpoints the model needs at inference time.
# MAGIC - `DatabricksLakebase` — grants the serving endpoint network access to the
# MAGIC   Lakebase PostgreSQL instance (vector store + conversation memory).

# COMMAND ----------

# Declare external resources the model depends on at inference time.
# Databricks uses these declarations to:
#   1. Auto-provision network access (e.g., Lakebase firewall rules)
#   2. Validate that referenced endpoints exist before deployment
#   3. Surface dependencies in the Unity Catalog lineage graph
resources = [
    DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
    DatabricksServingEndpoint(endpoint_name=EMBEDDING_ENDPOINT),
    # Note: DatabricksLakebase is not used here because our agent connects to
    # Lakebase Autoscaling via direct PostgreSQL (host/port/password from secrets),
    # not through Databricks-managed network passthrough.
]

input_example = {
    "input": [{"role": "user", "content": "What is the main topic of the wiki?"}]
}

with mlflow.start_run(run_name="wiki-rag-agent") as run:
    model_info = mlflow.pyfunc.log_model(
        name="wiki_rag_agent",
        # Models from Code: point at the source file, not a Python object
        python_model=os.path.join("src", "rag.py"),
        # Include the entire src/ directory so all imports resolve at serving time
        code_paths=["src"],
        resources=resources,
        input_example=input_example,
        # Pin to minor-version ranges: reproducible yet picks up patch fixes.
        # Load pip requirements from the single source of truth: src/requirements.txt
        # This avoids maintaining a duplicate list — edit the file once, both the
        # notebook %pip install and the model packaging pick it up.
        pip_requirements=os.path.join("src", "requirements.txt"),
    )
    run_id = run.info.run_id

print(f"Model logged  run_id={run_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Register in Unity Catalog
# MAGIC 
# MAGIC Registers a new model version in Unity Catalog under the three-level namespace
# MAGIC (e.g., `main.wiki_rag.wiki_rag_agent`). Each re-run creates a new version —
# MAGIC `agents.deploy()` in step 4 automatically points to this latest version.

# COMMAND ----------

registered = mlflow.register_model(
    model_uri=f"runs:/{run_id}/wiki_rag_agent",
    name=MODEL_NAME,
)
print(f"Registered {MODEL_NAME} v{registered.version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Validate Secrets
# MAGIC 
# MAGIC Confirms that all required secrets exist in the Databricks secret scope before
# MAGIC deployment. The serving endpoint injects these as environment variables at
# MAGIC runtime — a missing secret would cause a silent startup failure.

# COMMAND ----------

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
        w.secrets.get_secret(SECRET_SCOPE, key)
        print(f"  {key}")
    except Exception as e:
        raise ValueError(
            f"Missing required secret '{SECRET_SCOPE}/{key}'. "
            f"Run 'make setup-secrets' and '00_setup_lakebase' first.\n{e}"
        ) from e

print(f"All {len(REQUIRED_SECRETS)} secrets validated")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Deploy with `agents.deploy()`
# MAGIC 
# MAGIC Uses the **`databricks-agents` SDK** — the Databricks-recommended approach for
# MAGIC production agent deployment. A single `agents.deploy()` call:
# MAGIC 
# MAGIC 1. Creates or updates the Model Serving endpoint
# MAGIC 2. Injects secrets as environment variables via `{{secrets/scope/key}}` syntax
# MAGIC 3. Generates a **Review App** — a web UI for interactive testing and feedback
# MAGIC 4. Waits until the endpoint is ready (handles polling internally)
# MAGIC 
# MAGIC This replaces the manual `w.serving_endpoints.create()` + polling loop pattern.

# COMMAND ----------

from databricks import agents

# Environment variables injected at container startup.
# Secrets use {{secrets/scope/key}} syntax — Databricks resolves these at runtime,
# so plain-text credentials never appear in the endpoint config.
environment_vars = {
    "LAKEBASE_INSTANCE": f"{{{{secrets/{SECRET_SCOPE}/lakebase_instance_name}}}}",
    "LAKEBASE_HOST": f"{{{{secrets/{SECRET_SCOPE}/lakebase_host}}}}",
    "LAKEBASE_PORT": f"{{{{secrets/{SECRET_SCOPE}/lakebase_port}}}}",
    "LAKEBASE_DB": f"{{{{secrets/{SECRET_SCOPE}/lakebase_db}}}}",
    "LAKEBASE_USER": f"{{{{secrets/{SECRET_SCOPE}/mw_role}}}}",
    "LAKEBASE_PASSWORD": f"{{{{secrets/{SECRET_SCOPE}/mw_password}}}}",
    "EMBEDDING_MODEL": EMBEDDING_ENDPOINT,
    "LLM_MODEL": LLM_ENDPOINT,
}

# agents.deploy() is the recommended production deployment method.
# It wraps endpoint creation, version management, secret injection,
# and Review App provisioning in a single idempotent call.
deployment = agents.deploy(
    model_name=MODEL_NAME,
    model_version=int(registered.version),
    endpoint_name=ENDPOINT_NAME,
    scale_to_zero_enabled=True,
    environment_vars=environment_vars,
)

print(f"Endpoint:   {deployment.endpoint_name}")
print(f"Version:    {registered.version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Wait for Endpoint Ready + Smoke Test
# MAGIC 
# MAGIC `agents.deploy()` returns quickly — the endpoint may still be provisioning.
# MAGIC This cell polls until the endpoint is `READY`, then sends a test query
# MAGIC using the **Responses API** format (`input`, not `messages`).

# COMMAND ----------

import json
import time

from databricks.sdk.service.serving import EndpointStateReady

MAX_WAIT = 900  # 15 min
POLL_INTERVAL = 30

elapsed = 0
print(f"Waiting for '{ENDPOINT_NAME}' to become ready ...")

while elapsed < MAX_WAIT:
    ep = w.serving_endpoints.get(ENDPOINT_NAME)
    state = ep.state.ready if ep.state else None

    if state == EndpointStateReady.READY:
        break

    mins, secs = divmod(elapsed, 60)
    state_str = state.value if state else "UNKNOWN"
    print(f"  {state_str}  ({mins}m{secs:02d}s elapsed)")
    time.sleep(POLL_INTERVAL)
    elapsed += POLL_INTERVAL
else:
    raise TimeoutError(
        f"Endpoint '{ENDPOINT_NAME}' not ready after {MAX_WAIT}s. "
        f"Check the Serving UI for details."
    )

mins, secs = divmod(elapsed, 60)
print(f"Endpoint READY ({mins}m{secs:02d}s)")

# COMMAND ----------

# ResponsesAgent uses the Responses API format: `input` (not `messages`)
response = w.serving_endpoints.query(
    name=ENDPOINT_NAME,
    input=[{"role": "user", "content": "What is the main topic of the wiki?"}],
)

# Parse response — structure depends on ResponsesAgent output format
resp_dict = response.as_dict()
try:
    answer = resp_dict["output"][0]["content"][0]["text"]
except (KeyError, IndexError, TypeError):
    answer = json.dumps(resp_dict, indent=2, ensure_ascii=False)[:2000]

print(f"Smoke test passed\n\n{answer}")

# COMMAND ----------


