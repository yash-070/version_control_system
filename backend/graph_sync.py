"""One-shot retry worker for the MySQL-to-Neo4j graph projection outbox."""

from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from neo4j_store import process_graph_outbox


if __name__ == "__main__":
    result = process_graph_outbox(limit=1000)
    print(
        "Graph sync: "
        f"configured={result['configured']}, "
        f"processed={result['processed']}, failed={result['failed']}"
    )
    if result["failed"]:
        raise SystemExit(1)
