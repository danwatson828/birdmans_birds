"""Permanent verification suite for the NBA analytics database.

Run standalone:
    python -m tests.run                  # against nba.duckdb
    python -m tests.run --db other.duckdb
    python -m tests.run --quiet          # exit code only

Also invoked automatically by ingest.py as half of the snapshot-publish gate:
a snapshot is published only when ingestion completed cleanly AND this suite
passes.
"""
