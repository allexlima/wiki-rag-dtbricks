"""
MLflow PyFunc wrapper for the WikiRAG LangGraph agent.

Used with "Models from Code" pattern:
  mlflow.pyfunc.log_model(python_model="src/serving/pyfunc_model.py", ...)
"""
from __future__ import annotations

import os
import uuid

import mlflow
import pandas as pd
import psycopg2
from databricks.sdk import WorkspaceClient
from databricks_openai import DatabricksOpenAI
from pgvector.psycopg2 import register_vector


class WikiRAGModel(mlflow.pyfunc.PythonModel):
    """LangGraph RAG agent wrapped as MLflow PyFunc for model serving."""

    def load_context(self, context: mlflow.pyfunc.PythonModelContext):
        """Initialize connections and clients once at endpoint startup."""
        self.w = WorkspaceClient()
        self.instance_name = os.environ.get("LAKEBASE_INSTANCE", "")
        self.db_name = os.environ.get("LAKEBASE_DB", "wikidb")
        self.db_user = os.environ["LAKEBASE_USER"]
        self.db_host = os.environ.get("LAKEBASE_HOST", "")
        self.db_port = os.environ.get("LAKEBASE_PORT", "5432")
        self.db_password = os.environ.get("LAKEBASE_PASSWORD", "")
        self.client = DatabricksOpenAI()
        self.embedding_model = os.environ.get(
            "EMBEDDING_MODEL", "databricks-gte-large-en",
        )
        self.llm_model = os.environ.get(
            "LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct",
        )

    def _get_conn(self) -> psycopg2.extensions.connection:
        """Get a Lakebase connection. Prefers password auth; falls back to OAuth."""
        if self.db_password and self.db_host:
            conn = psycopg2.connect(
                host=self.db_host,
                port=self.db_port,
                dbname=self.db_name,
                user=self.db_user,
                password=self.db_password,
                sslmode="require",
                connect_timeout=30,
            )
        else:
            instance = self.w.database.get_database_instance(name=self.instance_name)
            cred = self.w.database.generate_database_credential(
                request_id=str(uuid.uuid4()),
                instance_names=[self.instance_name],
            )
            conn = psycopg2.connect(
                host=self.db_host or instance.read_write_dns,
                port=self.db_port,
                dbname=self.db_name,
                user=self.db_user,
                password=cred.token,
                sslmode="require",
                connect_timeout=30,
            )
        register_vector(conn)
        return conn

    def _embed(self, text: str) -> list[float]:
        response = self.client.embeddings.create(model=self.embedding_model, input=text)
        return response.data[0].embedding

    def _retrieve(self, conn, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.chunk_id, c.page_title, c.chunk_text,
                       1 - (e.embedding <=> %s::vector) AS similarity
                FROM wiki_rag.wiki_embeddings e
                JOIN wiki_rag.wiki_chunks c ON c.chunk_id = e.chunk_id
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
                """,
                (query_embedding, query_embedding, top_k),
            )
            return [
                {"chunk_id": r[0], "title": r[1], "text": r[2], "similarity": r[3]}
                for r in cur.fetchall()
            ]

    def _grade_doc(self, question: str, doc_text: str) -> bool:
        response = self.client.chat.completions.create(
            model=self.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a relevance grader. Respond with ONLY 'yes' if the document "
                        "is relevant to the question, or 'no' if not."
                    ),
                },
                {"role": "user", "content": f"Question: {question}\n\nDocument: {doc_text}"},
            ],
            max_tokens=3,
            temperature=0,
        )
        return response.choices[0].message.content.strip().lower().startswith("yes")

    def _rewrite_query(self, question: str) -> str:
        response = self.client.chat.completions.create(
            model=self.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Rewrite this question to be more specific for wiki search. "
                        "Return ONLY the rewritten question."
                    ),
                },
                {"role": "user", "content": question},
            ],
            max_tokens=128,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()

    def _generate(self, question: str, docs: list[dict]) -> str:
        context = "\n\n".join(f"[{d['title']}]\n{d['text']}" for d in docs)
        response = self.client.chat.completions.create(
            model=self.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful wiki assistant. Answer using ONLY the provided context. "
                        "Cite source page titles. If the context is insufficient, say so."
                    ),
                },
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
            ],
            max_tokens=1024,
            temperature=0.1,
        )
        return response.choices[0].message.content

    def predict(
        self,
        context: mlflow.pyfunc.PythonModelContext,
        model_input: pd.DataFrame | dict,
        params: dict | None = None,
    ) -> dict:
        """Run the full RAG pipeline: retrieve → grade → rewrite? → generate."""
        if isinstance(model_input, pd.DataFrame):
            question = str(model_input["question"].iloc[0])
        else:
            question = str(model_input.get("question", ""))

        conn = self._get_conn()
        try:
            max_rewrites = 2
            rewrite_count = 0

            while True:
                query_emb = self._embed(question)
                docs = self._retrieve(conn, query_emb)

                # Grade documents
                relevant = [d for d in docs if self._grade_doc(question, d["text"])]

                if relevant or rewrite_count >= max_rewrites:
                    break

                # Rewrite query and retry
                question = self._rewrite_query(question)
                rewrite_count += 1

            # Generate answer (use relevant docs if any, otherwise all retrieved docs)
            final_docs = relevant if relevant else docs
            answer = self._generate(question, final_docs)

            return {
                "answer": answer,
                "sources": [
                    {"title": d["title"], "similarity": round(d["similarity"], 4)}
                    for d in final_docs
                ],
            }
        finally:
            conn.close()
