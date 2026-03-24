# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 01 — Register Model + Deploy Serving Endpoint
# MAGIC
# MAGIC Logs the WikiRAG ResponsesAgent to MLflow (Models from Code), registers in Unity Catalog,
# MAGIC and deploys a serving endpoint via `agents.deploy()`.
# MAGIC
# MAGIC **Prerequisites:** Run `00_setup_lakebase.py` and `make setup-secrets` first.
# MAGIC
# MAGIC > **Idempotent** — safe to re-run. Creates a new model version and updates the endpoint atomically.

# COMMAND ----------

# MAGIC %pip install databricks-langchain langgraph psycopg2-binary pgvector mwparserfromhell tenacity databricks-agents
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC
# MAGIC Auto-populated by the DAB job, or set manually via widgets.

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
from mlflow.models.resources import DatabricksServingEndpoint
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
dbutils.widgets.text(
    "experiment_name", _defaults["experiment_name"], "MLflow Experiment"
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
# MAGIC Logs `src/rag.py` using Models from Code pattern with resource declarations for serving endpoints.

# COMMAND ----------

# External resources the model needs at inference time.
# Note: DatabricksLakebase is not used — it only supports Lakebase Provisioned,
# not Autoscaling. Our agent connects via secrets-injected password auth instead.
resources = [
    DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
    DatabricksServingEndpoint(endpoint_name=EMBEDDING_ENDPOINT),
]

input_example = {
    "input": [{"role": "user", "content": "What is the main topic of the wiki?"}]
}

mlflow.set_experiment(dbutils.widgets.get("experiment_name"))

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
# MAGIC ## 2. Register in Unity Catalog (Serverless Optimized)
# MAGIC
# MAGIC `env_pack` pre-packages the serving container at registration time (not deploy time),
# MAGIC cutting endpoint updates from ~15 min to ~2-3 min. Requires serverless compute.

# COMMAND ----------

registered = mlflow.register_model(
    model_uri=f"runs:/{run_id}/wiki_rag_agent",
    name=MODEL_NAME,
    env_pack="databricks_model_serving",
)
print(f"Registered {MODEL_NAME} v{registered.version} (serverless optimized)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Validate Secrets
# MAGIC
# MAGIC Confirms all required secrets exist before deployment (missing secrets cause silent startup failure).

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
# MAGIC Single call: creates/updates endpoint, injects secrets, provisions Review App.

# COMMAND ----------

from databricks import agents

# Secrets use {{secrets/scope/key}} syntax — resolved at runtime, never stored in plaintext.
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

# agents.deploy() wraps endpoint creation, version management, and secret injection.
deployment = agents.deploy(
    model_name=MODEL_NAME,
    model_version=int(registered.version),
    endpoint_name=ENDPOINT_NAME,
    scale_to_zero_enabled=True,
    environment_vars=environment_vars,
)

print(f"Endpoint:   {deployment.endpoint_name}")
print(f"Version:    {registered.version}")
print(f"\n🚀 Deployment initiated. The endpoint is provisioning in the background.")
print(f"   Check status:  databricks serving-endpoints get {ENDPOINT_NAME}")
print(f"   Once READY, proceed with:  make ingest")
