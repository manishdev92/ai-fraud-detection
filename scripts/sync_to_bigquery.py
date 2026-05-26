#!/usr/bin/env python3
"""Sync local transactions to BigQuery."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.database import Transaction, get_session_factory, init_db
from app.services import sync_transactions_to_bigquery


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync local transactions to BigQuery")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not get_settings().gcp_project_id:
        print("ERROR: Set GCP_PROJECT_ID in .env", file=sys.stderr)
        return 1

    init_db()
    db = get_session_factory()()
    try:
        if args.dry_run:
            pending = list(db.scalars(select(Transaction).where(Transaction.synced_to_bq.is_(False))))
            print(f"DRY RUN: would sync {len(pending)} transactions")
            return 0
        result = sync_transactions_to_bigquery(db, get_settings(), batch_size=args.batch_size)
        print(result["message"])
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
