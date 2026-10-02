from __future__ import annotations

import json
from pathlib import Path

from .cli_runner import run_cli


def create_test_store(path: Path | str, agent_type: str = "coding") -> str:
    store_dir = str(path)
    res = run_cli("init", "--dest", store_dir, "--agent-type", agent_type)
    res.assert_success(f"Failed to initialize store at {store_dir}")
    return store_dir


def create_sample_code_repo(base_dir: Path) -> Path:
    repo = base_dir / "sample_repo"
    repo.mkdir(parents=True, exist_ok=True)

    py_file = repo / "billing_service.py"
    py_file.write_text(
        '''"""Billing service responsible for payment dispatch and receipt logging."""

class PaymentGateway:
    """Gateway adapter connecting to upstream payment rails."""

    def __init__(self, timeout: int = 5):
        self.timeout = timeout

    def process_charge(self, account_id: str, amount_cents: int) -> bool:
        """Process an account charge. Always enforce idempotency keys."""
        if amount_cents <= 0:
            raise ValueError("Charge amount must be positive")
        return True

def refund_transaction(txn_id: str) -> bool:
    """Refund an executed transaction. Must check balance before issuing refund."""
    return True
''',
        encoding="utf-8",
    )

    db_file = repo / "db_pool.py"
    db_file.write_text(
        '''"""Database connection pool configuration."""

def acquire_connection():
    """Acquire connection from pool. Max connection pool size is 20."""
    pass
''',
        encoding="utf-8",
    )
    return repo


def create_sample_markdown_docs(base_dir: Path) -> Path:
    docs_dir = base_dir / "sample_docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    adr_file = docs_dir / "ADR-004-database-concurrency.md"
    adr_file.write_text(
        """# Architecture Decision Record: Database Concurrency

## Context
Under high concurrent loads, connection saturation causes query queuing.

## Decision
We must enforce a maximum connection pool limit of 25 connections per pod.
Services should never perform raw SQL queries without parameterized statements.
All transaction rollbacks must be logged with context error codes.

## Consequences
Mitigates connection exhaustion incidents in the primary database cluster.
""",
        encoding="utf-8",
    )
    return docs_dir


def create_sample_logs(base_dir: Path) -> Path:
    logs_dir = base_dir / "sample_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_file = logs_dir / "service_errors.jsonl"
    records = [
        {
            "timestamp": "2026-10-02T08:00:00Z",
            "level": "INFO",
            "service": "billing",
            "message": "Payment processing started for account acc_123",
        },
        {
            "timestamp": "2026-10-02T08:00:05Z",
            "level": "ERROR",
            "service": "billing",
            "message": "ConnectionTimeout: upstream stripe gateway unresponsive after 5000ms",
            "error_type": "ConnectionTimeout",
            "trace_id": "tr_98765",
        },
        {
            "timestamp": "2026-10-02T08:00:10Z",
            "level": "ERROR",
            "service": "billing",
            "message": "ConnectionTimeout: upstream stripe gateway unresponsive after 5000ms",
            "error_type": "ConnectionTimeout",
            "trace_id": "tr_98766",
        },
    ]
    with open(log_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return log_file


def create_sample_transcripts(base_dir: Path) -> Path:
    transcripts_dir = base_dir / "sample_transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    t_file = transcripts_dir / "incident_transcript.json"
    transcript_data = {
        "agent_id": "agent-worker-01",
        "task": "Migrate customer billing accounts",
        "turns": [
            {
                "turn": 1,
                "role": "user",
                "content": "Run database migration script on cluster",
            },
            {
                "turn": 2,
                "role": "assistant",
                "tool_call": "run_migration",
                "arguments": {"dry_run": False},
            },
            {
                "turn": 3,
                "role": "tool_output",
                "content": "FATAL: LockNotAvailable - Lock timeout occurred while waiting for table lock on 'accounts'",
            },
            {
                "turn": 4,
                "role": "assistant",
                "content": "The migration failed due to lock contention on table 'accounts'.",
            },
        ],
        "outcome": {"success": False, "failure_reason": "LockNotAvailable on table accounts"},
    }
    t_file.write_text(json.dumps(transcript_data, indent=2), encoding="utf-8")
    return t_file
