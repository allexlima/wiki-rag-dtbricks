#!/usr/bin/env python3
"""
One-time interactive setup: create Databricks secret scope and store Lakebase password.

This is the ONLY manual step in the deployment process.
All other steps are automated via ``make deploy``.

Usage:
    python src/setup_secrets.py
    make setup-secrets
    make setup-secrets PROFILE=my-workspace
"""
from __future__ import annotations

import getpass
import os
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import ResourceAlreadyExists

SCOPE = "wiki-rag"


def main() -> None:
    """Prompt for the Lakebase password and store it in the Databricks secret scope."""
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    print("=== Wiki RAG — Secret Scope Setup ===")
    if profile:
        print(f"  Profile: {profile}")
    print()

    password = getpass.getpass("Enter password for the 'mediawiki' Lakebase PG role: ")
    if len(password) < 8:
        print("ERROR: Password must be at least 8 characters.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("ERROR: Passwords do not match.")
        sys.exit(1)

    w = WorkspaceClient(profile=profile)

    # Create scope (idempotent)
    try:
        w.secrets.create_scope(scope=SCOPE)
        print(f"  Created secret scope '{SCOPE}'")
    except ResourceAlreadyExists:
        print(f"  Secret scope '{SCOPE}' already exists")

    # Store password
    w.secrets.put_secret(scope=SCOPE, key="mw_password", string_value=password)
    print(f"  Stored 'mw_password' in scope '{SCOPE}'")

    print("\n=== Done. Run 'make setup-lakebase' next. ===")


if __name__ == "__main__":
    main()
