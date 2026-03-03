# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — RAG Agent Testing
# MAGIC
# MAGIC Interactive notebook to test the LangGraph RAG agent.
# MAGIC Retrieves from pgvector, grades documents, optionally rewrites the query, and generates an answer.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary pgvector databricks-openai databricks-sdk langchain-text-splitters langgraph langchain-core --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), ".."))

# COMMAND ----------

import uuid
import psycopg2
from pgvector.psycopg2 import register_vector
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
INSTANCE = dbutils.secrets.get("wiki-rag", "lakebase_instance_name")
DB_USER = dbutils.secrets.get("wiki-rag", "lakebase_user")
DB_NAME = dbutils.secrets.get("wiki-rag", "lakebase_db")

def get_conn():
    instance = w.database.get_database_instance(name=INSTANCE)
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[INSTANCE],
    )
    conn = psycopg2.connect(
        host=instance.read_write_dns,
        dbname=DB_NAME,
        user=DB_USER,
        password=cred.token,
        sslmode="require",
    )
    register_vector(conn)
    return conn

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test retriever only

# COMMAND ----------

from src.rag.retriever import retrieve

conn = get_conn()
docs = retrieve(conn, "What is the main topic of the wiki?", top_k=3)

for doc in docs:
    print(f"[{doc.page_title}] (similarity: {doc.similarity:.4f})")
    print(f"  {doc.chunk_text[:200]}...")
    print()

conn.close()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test full RAG agent

# COMMAND ----------

from src.rag.agent import run_agent

conn = get_conn()
result = run_agent(conn, "What is the main topic of the wiki?")
conn.close()

print("Answer:")
print(result["answer"])
print("\nSources:")
for src in result["sources"]:
    print(f"  - {src['title']} (similarity: {src['similarity']})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Try your own questions
# MAGIC
# MAGIC Modify the question below and run again.

# COMMAND ----------

QUESTION = "Tell me about the latest changes in the wiki"

conn = get_conn()
result = run_agent(conn, QUESTION)
conn.close()

print(f"Q: {QUESTION}\n")
print(f"A: {result['answer']}\n")
print("Sources:")
for src in result["sources"]:
    print(f"  - {src['title']} ({src['similarity']})")
