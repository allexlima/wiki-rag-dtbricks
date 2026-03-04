# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 02 — RAG Agent Testing
# MAGIC
# MAGIC Interactive notebook to test the LangGraph RAG agent against Lakebase.
# MAGIC
# MAGIC **Pipeline:** embed query → pgvector retrieval → grade docs → (rewrite?) → generate answer.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary pgvector databricks-openai databricks-sdk langchain-text-splitters langgraph langchain-core --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from pgvector.psycopg2 import register_vector

from src.config import get_lakebase_conn
from src.rag.agent import run_agent
from src.rag.retriever import retrieve

conn = get_lakebase_conn()
register_vector(conn)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test retriever only

# COMMAND ----------

docs = retrieve(conn, "What is the main topic of the wiki?", top_k=3)

for doc in docs:
    print(f"[{doc.page_title}] (similarity: {doc.similarity:.4f})")
    print(f"  {doc.chunk_text[:200]}...\n")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test full RAG agent

# COMMAND ----------

result = run_agent(conn, "What is the main topic of the wiki?")

print(f"Answer:\n{result['answer']}\n")
print("Sources:")
for src in result["sources"]:
    print(f"  - {src['title']} (similarity: {src['similarity']})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Try your own questions
# MAGIC
# MAGIC Change `QUESTION` below and re-run the cell.

# COMMAND ----------

QUESTION = "Tell me about the latest changes in the wiki"

result = run_agent(conn, QUESTION)

print(f"Q: {QUESTION}\n")
print(f"A: {result['answer']}\n")
print("Sources:")
for src in result["sources"]:
    print(f"  - {src['title']} ({src['similarity']})")

# COMMAND ----------

conn.close()
print("✓ Done")
