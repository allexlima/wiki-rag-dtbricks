# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 03 — RAG Evaluation with MLflow GenAI
# MAGIC
# MAGIC Evaluates the deployed wiki-rag serving endpoint against a ground truth dataset
# MAGIC using MLflow 3 GenAI scorers (Correctness, RelevanceToQuery, Guidelines, Safety).
# MAGIC Results are logged to an MLflow experiment for tracking and comparison.
# MAGIC
# MAGIC **Prerequisites:** Endpoint deployed (`01`) and data ingested (`02`).
# MAGIC
# MAGIC > **Idempotent** — safe to re-run. Each run creates a new MLflow run in the experiment.

# COMMAND ----------

# MAGIC %pip install mlflow[genai] -q
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

import json

import mlflow
import pandas as pd
from databricks.sdk import WorkspaceClient
from src.config import load_bundle_defaults

# ─── Widget parameters (defaults from databricks.yml) ────────────────

_defaults = load_bundle_defaults()

dbutils.widgets.text(
    "endpoint_name", _defaults["endpoint_name"], "Serving Endpoint"
)
dbutils.widgets.text("dataset", _defaults.get("dataset", "astromotores"), "Dataset Name")
dbutils.widgets.text("llm_judge", _defaults["llm_model"], "Judge LLM Model")
dbutils.widgets.text(
    "experiment_name", _defaults["experiment_name"], "MLflow Experiment"
)

# COMMAND ----------

ENDPOINT_NAME = dbutils.widgets.get("endpoint_name")
DATASET_NAME = dbutils.widgets.get("dataset")
LLM_JUDGE = dbutils.widgets.get("llm_judge")
EXPERIMENT_NAME = dbutils.widgets.get("experiment_name")

# ─── Resolve ground truth path ───────────────────────────────────────

GT_PATH = os.path.join(
    BUNDLE_ROOT, "mediawiki", "dataset", DATASET_NAME,
    "questions", "ground_truth_test.jsonl",
)
assert os.path.isfile(GT_PATH), (
    f"Ground truth not found: {GT_PATH}\n"
    f"Available datasets: {os.listdir(os.path.join(BUNDLE_ROOT, 'mediawiki', 'dataset'))}"
)

print(f"Endpoint:    {ENDPOINT_NAME}")
print(f"Dataset:     {DATASET_NAME}")
print(f"Judge LLM:   {LLM_JUDGE}")
print(f"Ground truth: {GT_PATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Pre-flight — verify endpoint is ready

# COMMAND ----------

w = WorkspaceClient()

ep = w.serving_endpoints.get(ENDPOINT_NAME)
ep_state = ep.state.ready.value if ep.state and ep.state.ready else None
assert ep_state == "READY", (
    f"Endpoint '{ENDPOINT_NAME}' is not ready (state={ep_state}). "
    f"Deploy it first with: make deploy-agent"
)
print(f"Endpoint '{ENDPOINT_NAME}' is READY")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Load ground truth dataset

# COMMAND ----------

eval_data = []
with open(GT_PATH) as f:
    for line in f:
        row = json.loads(line)
        eval_data.append({
            "inputs": {"query": row["inputs"]["query"]},
            "expectations": {
                "expected_facts": row["expectations"]["expected_facts"],
                "source": row["expectations"].get("source", ""),
            },
        })

print(f"Loaded {len(eval_data)} evaluation examples from '{DATASET_NAME}'")

# Show sample
sample_df = pd.DataFrame([
    {"query": r["inputs"]["query"], "num_facts": len(r["expectations"]["expected_facts"])}
    for r in eval_data[:5]
])
display(sample_df)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Define predict function
# MAGIC
# MAGIC Calls the deployed serving endpoint via `mlflow.deployments` (Responses API format).
# MAGIC
# MAGIC > `predict_fn` receives **unpacked** `inputs` kwargs — since our data has
# MAGIC > `inputs: {"query": "..."}`, the signature is `predict_fn(query)`.

# COMMAND ----------

from mlflow.deployments import get_deploy_client

deploy_client = get_deploy_client("databricks")


def predict_fn(query: str) -> str:
    """Query the serving endpoint and return the response text."""
    response = deploy_client.predict(
        endpoint=ENDPOINT_NAME,
        inputs={
            "input": [{"role": "user", "content": query}],
        },
    )

    # Extract text from Responses API output format
    for item in response.get("output", []):
        # ResponsesAgent wraps text in output items
        if isinstance(item, dict):
            # Format: {"type": "message", "content": [{"type": "output_text", "text": "..."}]}
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") == "output_text":
                    return block["text"]
            # Fallback: direct text field on the item
            if "text" in item:
                return item["text"]

    return str(response.get("output", ""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Configure scorers

# COMMAND ----------

from mlflow.genai.scorers import (
    Correctness,
    Guidelines,
    RelevanceToQuery,
    Safety,
)

JUDGE_MODEL = f"databricks:/{LLM_JUDGE}"

scorers = [
    # Primary factual accuracy — uses expected_facts from ground truth
    Correctness(model=JUDGE_MODEL),

    # Checks if the response addresses the user's question
    RelevanceToQuery(model=JUDGE_MODEL),

    # Custom guidelines for Portuguese RAG quality
    Guidelines(
        name="portuguese_rag_quality",
        guidelines=[
            "The response must be written in Portuguese (PT-BR), matching the language of the request.",
            "The response must directly address the request with specific facts, not vague generalities.",
            "Technical terms and proper nouns (model numbers, chemical formulas, units) must be used accurately.",
            "The response must cite source pages when providing specific facts.",
            "If the context is insufficient, the response must clearly state this rather than fabricating information.",
        ],
        model=JUDGE_MODEL,
    ),

    # Basic safety check
    Safety(model=JUDGE_MODEL),
]

print(f"Configured {len(scorers)} scorers with judge model: {LLM_JUDGE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Run evaluation

# COMMAND ----------

from datetime import datetime

mlflow.set_experiment(EXPERIMENT_NAME)

run_name = f"eval-{DATASET_NAME}-{datetime.now().strftime('%Y%m%d-%H%M')}"

print(f"Running evaluation against '{ENDPOINT_NAME}'...")
print(f"  Dataset:    {DATASET_NAME} ({len(eval_data)} questions)")
print(f"  Judge:      {LLM_JUDGE}")
print(f"  Experiment: {EXPERIMENT_NAME}")
print(f"  Run name:   {run_name}")
print()

with mlflow.start_run(run_name=run_name):
    results = mlflow.genai.evaluate(
        data=eval_data,
        predict_fn=predict_fn,
        scorers=scorers,
    )

    mlflow.set_tags({
        "task": "evaluation",
        "dataset": DATASET_NAME,
        "endpoint": ENDPOINT_NAME,
    })

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Results summary

# COMMAND ----------

print("=" * 60)
print(f"  Evaluation Results — {DATASET_NAME}")
print("=" * 60)

metrics = results.metrics
for name, value in sorted(metrics.items()):
    if isinstance(value, float):
        print(f"  {name:40s} {value:.4f}")
    else:
        print(f"  {name:40s} {value}")

print()
print(f"Run ID:     {results.run_id}")
print(f"Experiment: {EXPERIMENT_NAME}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Per-question results

# COMMAND ----------

display(results.tables["eval_results"])

# COMMAND ----------

# ─── Exit with summary ───────────────────────────────────────────────

summary = {k: round(v, 4) if isinstance(v, float) else v for k, v in metrics.items()}
summary["dataset"] = DATASET_NAME
summary["num_questions"] = len(eval_data)
summary["run_id"] = results.run_id
dbutils.notebook.exit(json.dumps(summary))
