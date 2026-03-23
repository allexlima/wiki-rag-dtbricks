# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 02 — RAG Agent Testing
# MAGIC
# MAGIC Interactive notebook to test the LangGraph RAG agent (retrieval, grading,
# MAGIC generation, and multi-turn memory) without deploying to a serving endpoint.
# MAGIC
# MAGIC **Prerequisites:** Run `00_setup_lakebase`, `02_ingest_mediawiki`, and `make setup-secrets`.

# COMMAND ----------

# MAGIC %pip install databricks-langchain langgraph psycopg2-binary pgvector mwparserfromhell tenacity -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

from src.config import load_bundle_defaults
_defaults = load_bundle_defaults()

dbutils.widgets.text("secret_scope", _defaults["secret_scope"], "Secret Scope")
dbutils.widgets.text("question", "Quais são os sistemas principais do veículo Databricks Galáctica?", "Question")
dbutils.widgets.text("top_k", "3", "Top K Results")

# COMMAND ----------

import os
import sys
from contextlib import closing

try:
    dbutils  # noqa: F821
except NameError:
    dbutils = None  # type: ignore[assignment]

_cwd = os.getcwd()
if os.path.basename(_cwd) == "notebooks":
    BUNDLE_ROOT = os.path.dirname(_cwd)
else:
    BUNDLE_ROOT = _cwd
sys.path.insert(0, BUNDLE_ROOT)

from pgvector.psycopg2 import register_vector

from src.config import get_lakebase_conn
from src.rag import WikiRAGAgent, run_agent

# ─── Parameters ─────────────────────────────────────────────────────────
SCOPE = dbutils.widgets.get("secret_scope")
QUESTION = dbutils.widgets.get("question")
TOP_K = int(dbutils.widgets.get("top_k"))

print(f"Secret scope : {SCOPE}")
print(f"Top K        : {TOP_K}")
print(f"Question     : {QUESTION[:80]}{'...' if len(QUESTION) > 80 else ''}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Initialize Agent

# COMMAND ----------

agent = WikiRAGAgent()

try:
    conn = get_lakebase_conn()
    register_vector(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_embeddings")
        embedding_count = cur.fetchone()[0]
    print(f"Agent initialised — {embedding_count:,} embeddings in wiki_rag.wiki_embeddings")
except Exception as e:
    print(f"Failed to connect to Lakebase: {e}")
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Test Retrieval

# COMMAND ----------

try:
    docs = agent.retrieve(conn, QUESTION, top_k=TOP_K)

    if not docs:
        print("No documents retrieved. Check that embeddings have been ingested (02_ingest_mediawiki).")
    else:
        rows_html = ""
        for i, doc in enumerate(docs, 1):
            snippet = doc.chunk_text[:300].replace("<", "&lt;").replace(">", "&gt;")
            rows_html += f"""
            <tr>
                <td style="text-align:center">{i}</td>
                <td><strong>{doc.page_title}</strong></td>
                <td style="text-align:center">{doc.similarity:.4f}</td>
                <td style="text-align:center">{doc.chunk_source}</td>
                <td style="font-size:0.9em">{snippet}...</td>
            </tr>"""

        displayHTML(f"""
        <h4>Retrieved {len(docs)} documents (top_k={TOP_K})</h4>
        <table style="width:100%; border-collapse:collapse; margin-top:8px">
            <thead>
                <tr style="background:#f0f0f0">
                    <th style="padding:6px; border:1px solid #ddd; width:40px">#</th>
                    <th style="padding:6px; border:1px solid #ddd">Page Title</th>
                    <th style="padding:6px; border:1px solid #ddd; width:80px">Similarity</th>
                    <th style="padding:6px; border:1px solid #ddd; width:70px">Source</th>
                    <th style="padding:6px; border:1px solid #ddd">Chunk Preview</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
        """)

except Exception as e:
    print(f"Retrieval failed: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Test Full Agent (single-turn)

# COMMAND ----------

try:
    result = run_agent(QUESTION)

    answer = result["answer"]
    conv_id = result["conversation_id"]

    displayHTML(f"""
    <div style="padding:12px; background:#f8f9fa; border-left:4px solid #1b6ac9; margin-bottom:12px">
        <strong>Question:</strong> {QUESTION}
    </div>
    <div style="padding:12px; background:#fff; border:1px solid #e0e0e0; border-radius:4px">
        <strong>Answer:</strong><br><br>
        {answer.replace(chr(10), '<br>')}
    </div>
    <div style="margin-top:8px; font-size:0.85em; color:#666">
        Conversation ID: <code>{conv_id}</code>
    </div>
    """)

except Exception as e:
    print(f"Agent call failed: {e}")
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Test Multi-Turn Conversation
# MAGIC
# MAGIC Reuses `conversation_id` from step 3 to verify memory persistence.

# COMMAND ----------

try:
    followup_question = "Can you tell me more about that?"
    followup = run_agent(followup_question, thread_id=conv_id)

    displayHTML(f"""
    <div style="padding:12px; background:#f8f9fa; border-left:4px solid #28a745; margin-bottom:12px">
        <strong>Follow-up:</strong> {followup_question}<br>
        <span style="font-size:0.85em; color:#666">(using conversation {conv_id[:8]}...)</span>
    </div>
    <div style="padding:12px; background:#fff; border:1px solid #e0e0e0; border-radius:4px">
        <strong>Answer:</strong><br><br>
        {followup['answer'].replace(chr(10), '<br>')}
    </div>
    """)

    # Show conversation history from Lakebase
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, LEFT(content, 120) AS content_preview, created_at
            FROM wiki_rag.messages
            WHERE conversation_id = %s
            ORDER BY created_at
            """,
            (conv_id,),
        )
        messages = cur.fetchall()

    if messages:
        msg_rows = ""
        for role, preview, ts in messages:
            color = "#1b6ac9" if role == "user" else "#28a745"
            msg_rows += f"""
            <tr>
                <td style="padding:4px 8px; color:{color}; font-weight:bold">{role}</td>
                <td style="padding:4px 8px; font-size:0.9em">{preview}...</td>
                <td style="padding:4px 8px; font-size:0.8em; color:#888">{ts}</td>
            </tr>"""

        displayHTML(f"""
        <h4>Conversation History ({len(messages)} messages)</h4>
        <table style="width:100%; border-collapse:collapse">
            <thead>
                <tr style="background:#f0f0f0">
                    <th style="padding:6px; border:1px solid #ddd; width:80px">Role</th>
                    <th style="padding:6px; border:1px solid #ddd">Content Preview</th>
                    <th style="padding:6px; border:1px solid #ddd; width:160px">Timestamp</th>
                </tr>
            </thead>
            <tbody>{msg_rows}</tbody>
        </table>
        """)

except Exception as e:
    print(f"Multi-turn test failed: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Interactive Query

# COMMAND ----------

interactive_q = dbutils.widgets.get("question")

try:
    interactive_result = run_agent(interactive_q)

    displayHTML(f"""
    <div style="padding:16px; background:#fff; border:1px solid #e0e0e0; border-radius:6px">
        <div style="padding:8px 12px; background:#e8f0fe; border-radius:4px; margin-bottom:12px">
            <strong>Q:</strong> {interactive_q}
        </div>
        <div style="padding:8px 12px">
            <strong>A:</strong><br><br>
            {interactive_result['answer'].replace(chr(10), '<br>')}
        </div>
        <div style="margin-top:8px; font-size:0.85em; color:#666; border-top:1px solid #eee; padding-top:8px">
            Conversation ID: <code>{interactive_result['conversation_id']}</code>
        </div>
    </div>
    """)

except Exception as e:
    print(f"Interactive query failed: {e}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cleanup

# COMMAND ----------

if 'conn' in dir() and not conn.closed:
    conn.close()
    print("Lakebase connection closed.")
