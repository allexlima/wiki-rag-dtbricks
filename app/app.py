"""
WikiRAG Streamlit Chat UI — Databricks App.

Talks to the WikiRAG ResponsesAgent serving endpoint using chat completions format.
Supports multi-turn conversation via conversation_id.
"""
import logging
import os
import uuid

import streamlit as st
from databricks.sdk import WorkspaceClient

log = logging.getLogger(__name__)

st.set_page_config(page_title="WikiRAG", page_icon="📖", layout="wide")

ENDPOINT_NAME = os.environ.get("SERVING_ENDPOINT_NAME", "wiki-rag-endpoint")


@st.cache_resource
def get_client() -> WorkspaceClient:
    return WorkspaceClient()


def query_rag(messages: list[dict]) -> str:
    """Call the WikiRAG serving endpoint with chat completions format."""
    w = get_client()
    response = w.serving_endpoints.query(
        name=ENDPOINT_NAME,
        messages=messages,
        max_tokens=1024,
    )
    return response.choices[0].message.content


# ---------- Session state ----------
if "messages" not in st.session_state:
    st.session_state.messages = []

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = str(uuid.uuid4())

# ---------- UI ----------
st.title("📖 WikiRAG")
st.caption("Ask questions about the wiki — powered by Databricks")

# Render chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Chat input
if prompt := st.chat_input("Ask a question about the wiki..."):
    # Add user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Build messages for the endpoint (include history for context)
    api_messages = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages
    ]

    # Query the agent
    with st.chat_message("assistant"):
        with st.spinner("Searching the wiki..."):
            try:
                answer = query_rag(api_messages)
            except Exception:
                log.exception("RAG query failed for endpoint '%s'", ENDPOINT_NAME)
                answer = "Sorry, something went wrong. Please try again later."

        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
