"""Run PostgreSQL schema migrations for deployment tooling."""
from .db import init_db


if __name__ == "__main__":
    init_db()
    print("Database schema is ready.")
