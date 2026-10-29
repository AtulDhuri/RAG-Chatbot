"""Run ordered SQL migrations: python -m app.migrations."""
from __future__ import annotations
from pathlib import Path
import psycopg
from .config import get_settings

ROOT = Path(__file__).parent / "sql"

def migrate() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
        for path in sorted(ROOT.glob("*.sql")):
            if path.name in applied:
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (path.name,))
    print("Migrations complete")

if __name__ == "__main__":
    migrate()
