"""
WikiRAG Streamlit Chat UI — Databricks App.
"""
import json
import logging
import os

import streamlit as st

log = logging.getLogger(__name__)
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import DataframeSplitInput

st.set_page_config(page_title="WikiRAG", page_icon="📖", layout="wide")

ENDPOINT_NAME = os.environ.get("SERVING_ENDPOINT_NAME", "wiki-rag-endpoint")


@st.cache_resource
def get_client():
    return WorkspaceClient()


def query_rag(question: str) -> dict:
    """Call the WikiRAG serving endpoint."""
    w = get_client()
    response = w.serving_endpoints.query(
        name=ENDPOINT_NAME,
        dataframe_split=DataframeSplitInput(
            columns=["question"],
            data=[[question]],
        ),
    )
    prediction = response.predictions
    if isinstance(prediction, list):
        prediction = prediction[0]
    return prediction


# ---------- UI ----------
st.title("📖 WikiRAG")
st.caption("Ask questions about the wiki — powered by Databricks")

# Chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("sources"):
            with st.expander("Sources"):
                for src in msg["sources"]:
                    st.write(f"- **{src['title']}** (similarity: {src['similarity']})")

# Chat input
if prompt := st.chat_input("Ask a question about the wiki..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Searching the wiki..."):
            try:
                result = query_rag(prompt)
                answer = result.get("answer", "I couldn't find an answer.")
                sources = result.get("sources", [])
                # Handle sources as string (JSON) from serving
                if isinstance(sources, str):
                    sources = json.loads(sources)
            except Exception:
                log.exception("RAG query failed")
                answer = "Sorry, something went wrong. Please try again later."
                sources = []

        st.markdown(answer)
        if sources:
            with st.expander("Sources"):
                for src in sources:
                    st.write(f"- **{src['title']}** (similarity: {src['similarity']})")

    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "sources": sources,
    })
