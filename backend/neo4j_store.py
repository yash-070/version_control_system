"""Neo4j projection for repository branch, commit, and file-tree graphs.

MySQL remains the source of truth. A MySQL outbox makes graph refreshes durable
when Neo4j is temporarily unavailable.
"""

import json
import logging
import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from threading import Lock

from neo4j import GraphDatabase

from db import get_pool


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
_schema_lock = Lock()
_schema_ready = False


@lru_cache(maxsize=1)
def get_driver():
    uri = os.getenv("NEO4J_URI", "").strip()
    if not uri:
        return None
    return GraphDatabase.driver(
        uri,
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
    )


def graph_configured():
    return bool(os.getenv("NEO4J_URI", "").strip())


def graph_available():
    driver = get_driver()
    if driver is None:
        return False
    try:
        driver.verify_connectivity()
        return True
    except Exception:
        logger.info("Neo4j is not reachable; graph reads will use the MySQL fallback.")
        return False


def _database_name():
    return os.getenv("NEO4J_DATABASE", "neo4j")


def _ensure_graph_schema(driver):
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        queries = [
            "CREATE CONSTRAINT vcs_repository_id IF NOT EXISTS FOR (n:Repository) REQUIRE n.repo_id IS UNIQUE",
            "CREATE CONSTRAINT vcs_branch_id IF NOT EXISTS FOR (n:Branch) REQUIRE n.branch_id IS UNIQUE",
            "CREATE CONSTRAINT vcs_commit_id IF NOT EXISTS FOR (n:Commit) REQUIRE n.commit_id IS UNIQUE",
            "CREATE CONSTRAINT vcs_tree_id IF NOT EXISTS FOR (n:Tree) REQUIRE n.tree_id IS UNIQUE",
            "CREATE CONSTRAINT vcs_blob_id IF NOT EXISTS FOR (n:Blob) REQUIRE n.blob_id IS UNIQUE",
        ]
        with driver.session(database=_database_name()) as session:
            for query in queries:
                session.run(query).consume()
        _schema_ready = True


@contextmanager
def _mysql_cursor(write=False):
    connection = get_pool().get_connection()
    cursor = connection.cursor(dictionary=True, buffered=True)
    try:
        yield cursor
        if write:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _iso(value):
    return value.isoformat(sep=" ") if value is not None else None


def _stored_file_exists(storage_path):
    if not storage_path:
        return False
    root = (ROOT / "file-storage").resolve()
    path = (ROOT / storage_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path.is_file()


def _repository_snapshot(repo_id):
    with _mysql_cursor() as cursor:
        cursor.execute("""
            SELECT repo_id, repo_name, default_branch_id
            FROM `REPOSITORY` WHERE repo_id = %s
        """, (repo_id,))
        repository = cursor.fetchone()
        if not repository:
            return None

        cursor.execute("""
            SELECT b.branch_id, b.repo_id, b.branch_name, b.created_by_user_id,
                   (r.default_branch_id = b.branch_id) AS is_default,
                   (SELECT c.commit_id FROM `COMMIT` c WHERE c.branch_id = b.branch_id
                    ORDER BY c.committed_at DESC, c.commit_id DESC LIMIT 1) AS head_commit_id,
                   (SELECT c.tree_id FROM `COMMIT` c WHERE c.branch_id = b.branch_id
                    ORDER BY c.committed_at DESC, c.commit_id DESC LIMIT 1) AS head_tree_id
            FROM `BRANCH` b JOIN `REPOSITORY` r ON r.repo_id = b.repo_id
            WHERE b.repo_id = %s
        """, (repo_id,))
        branches = []
        for row in cursor.fetchall():
            item = dict(row)
            item["is_default"] = bool(item["is_default"])
            if item.get("head_commit_id") is None:
                item.pop("head_commit_id", None)
            if item.get("head_tree_id") is None:
                item.pop("head_tree_id", None)
            branches.append(item)

        cursor.execute("""
            SELECT c.commit_id, c.branch_id, b.repo_id, c.author_user_id, c.tree_id,
                   c.message, c.committed_at
            FROM `COMMIT` c JOIN `BRANCH` b ON b.branch_id = c.branch_id
            WHERE b.repo_id = %s
        """, (repo_id,))
        commits = [dict(row, committed_at=_iso(row["committed_at"])) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT cp.commit_id, cp.parent_commit_id, cp.parent_order
            FROM `COMMIT_PARENT` cp
            JOIN `COMMIT` child ON child.commit_id = cp.commit_id
            JOIN `BRANCH` b ON b.branch_id = child.branch_id
            WHERE b.repo_id = %s
        """, (repo_id,))
        parents = cursor.fetchall()

        cursor.execute("""
            SELECT t.tree_id, o.repo_id
            FROM `TREE` t JOIN `OBJECT` o ON o.object_id = t.tree_id
            WHERE o.repo_id = %s AND o.object_type = 'tree'
        """, (repo_id,))
        trees = cursor.fetchall()

        cursor.execute("""
            SELECT b.blob_id, o.repo_id, b.mime_type, b.storage_path
            FROM `BLOBS` b JOIN `OBJECT` o ON o.object_id = b.blob_id
            WHERE o.repo_id = %s AND o.object_type = 'blob'
        """, (repo_id,))
        blobs = []
        for row in cursor.fetchall():
            blobs.append({"blob_id": row["blob_id"], "repo_id": row["repo_id"],
                          "mime_type": row["mime_type"],
                          "has_content": _stored_file_exists(row["storage_path"])})

        cursor.execute("""
            SELECT te.tree_id, te.file_name, te.file_mode, te.subtree_id, te.blob_id
            FROM `TREE_ENTRY` te
            JOIN `TREE` t ON t.tree_id = te.tree_id
            JOIN `OBJECT` o ON o.object_id = t.tree_id
            WHERE o.repo_id = %s AND o.object_type = 'tree'
        """, (repo_id,))
        entries = cursor.fetchall()

    return {"repository": repository, "branches": branches, "commits": commits,
            "parents": parents, "trees": trees, "blobs": blobs, "entries": entries}


def _write_graph_projection(tx, repo_id, snapshot):
    tx.run("MATCH (n) WHERE n.repo_id = $repo_id DETACH DELETE n", repo_id=repo_id).consume()
    if snapshot is None:
        return

    repository = snapshot["repository"]
    tx.run("""
        CREATE (:Repository {repo_id: $repo_id, repo_name: $repo_name})
    """, repo_id=repository["repo_id"], repo_name=repository["repo_name"]).consume()

    tx.run("""
        UNWIND $rows AS row
        CREATE (b:Branch)
        SET b += row
    """, rows=snapshot["branches"]).consume()
    tx.run("""
        UNWIND $rows AS row
        CREATE (c:Commit)
        SET c += row
    """, rows=snapshot["commits"]).consume()
    tx.run("""
        UNWIND $rows AS row
        CREATE (t:Tree)
        SET t += row
    """, rows=snapshot["trees"]).consume()
    tx.run("""
        UNWIND $rows AS row
        CREATE (b:Blob)
        SET b += row
    """, rows=snapshot["blobs"]).consume()

    tx.run("""
        MATCH (r:Repository {repo_id: $repo_id})
        MATCH (b:Branch {repo_id: $repo_id})
        CREATE (r)-[:HAS_BRANCH]->(b)
    """, repo_id=repo_id).consume()
    tx.run("""
        MATCH (b:Branch {repo_id: $repo_id})
        MATCH (c:Commit {commit_id: b.head_commit_id, repo_id: $repo_id})
        CREATE (b)-[:HEAD]->(c)
    """, repo_id=repo_id).consume()
    tx.run("""
        MATCH (b:Branch {repo_id: $repo_id})
        MATCH (c:Commit {branch_id: b.branch_id, repo_id: $repo_id})
        CREATE (b)-[:HAS_COMMIT]->(c)
    """, repo_id=repo_id).consume()
    tx.run("""
        MATCH (c:Commit {repo_id: $repo_id})
        MATCH (t:Tree {tree_id: c.tree_id, repo_id: $repo_id})
        CREATE (c)-[:SNAPSHOT]->(t)
    """, repo_id=repo_id).consume()
    tx.run("""
        UNWIND $rows AS row
        MATCH (c:Commit {commit_id: row.commit_id, repo_id: $repo_id})
        MATCH (p:Commit {commit_id: row.parent_commit_id, repo_id: $repo_id})
        CREATE (c)-[:PARENT {parent_order: row.parent_order}]->(p)
    """, repo_id=repo_id, rows=snapshot["parents"]).consume()
    tx.run("""
        UNWIND $rows AS row
        MATCH (t:Tree {tree_id: row.tree_id, repo_id: $repo_id})
        MATCH (child:Tree {tree_id: row.subtree_id, repo_id: $repo_id})
        CREATE (t)-[:CONTAINS {name: row.file_name, mode: row.file_mode}]->(child)
    """, repo_id=repo_id,
           rows=[row for row in snapshot["entries"] if row["subtree_id"] is not None]).consume()
    tx.run("""
        UNWIND $rows AS row
        MATCH (t:Tree {tree_id: row.tree_id, repo_id: $repo_id})
        MATCH (blob:Blob {blob_id: row.blob_id, repo_id: $repo_id})
        CREATE (t)-[:CONTAINS {name: row.file_name, mode: row.file_mode}]->(blob)
    """, repo_id=repo_id,
           rows=[row for row in snapshot["entries"] if row["blob_id"] is not None]).consume()


def _replace_repository_graph(repo_id):
    driver = get_driver()
    if driver is None:
        return False
    _ensure_graph_schema(driver)
    snapshot = _repository_snapshot(repo_id)
    with driver.session(database=_database_name()) as session:
        session.execute_write(_write_graph_projection, repo_id, snapshot)
    return True


def process_graph_outbox(limit=25):
    """Apply pending MySQL projection events to Neo4j; safe to retry."""
    driver = get_driver()
    if driver is None:
        return {"configured": False, "processed": 0, "failed": 0}
    try:
        driver.verify_connectivity()
        _ensure_graph_schema(driver)
    except Exception as error:
        logger.info("Neo4j graph sync is unavailable: %s", error)
        return {"configured": True, "processed": 0, "failed": 0}

    with _mysql_cursor(write=True) as cursor:
        cursor.execute("""
            SELECT event_id, event_type, payload
            FROM `VCS_GRAPH_OUTBOX`
            WHERE processed_at IS NULL
              AND (claimed_at IS NULL OR claimed_at < CURRENT_TIMESTAMP - INTERVAL 5 MINUTE)
            ORDER BY event_id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        """, (limit,))
        events = cursor.fetchall()
        for event in events:
            cursor.execute("""
                UPDATE `VCS_GRAPH_OUTBOX` SET claimed_at = CURRENT_TIMESTAMP
                WHERE event_id = %s AND processed_at IS NULL
            """, (event["event_id"],))

    processed = failed = 0
    for event in events:
        try:
            payload = event["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if event["event_type"] != "sync_repository":
                raise ValueError(f"Unsupported graph event type: {event['event_type']}")
            _replace_repository_graph(int(payload["repo_id"]))
            with _mysql_cursor(write=True) as cursor:
                cursor.execute("""
                    UPDATE `VCS_GRAPH_OUTBOX`
                    SET processed_at = CURRENT_TIMESTAMP, attempts = attempts + 1,
                        last_error = NULL, claimed_at = NULL
                    WHERE event_id = %s AND processed_at IS NULL
                """, (event["event_id"],))
            processed += 1
        except Exception as error:
            logger.exception("Could not sync graph event %s", event["event_id"])
            with _mysql_cursor(write=True) as cursor:
                cursor.execute("""
                    UPDATE `VCS_GRAPH_OUTBOX`
                    SET attempts = attempts + 1, last_error = %s, claimed_at = NULL
                    WHERE event_id = %s AND processed_at IS NULL
                """, (str(error)[:1000], event["event_id"]))
            failed += 1
            if not graph_available():
                break
    return {"configured": True, "processed": processed, "failed": failed}


def read_branch_tree(repo_id, branch_id, tree_id=None):
    """Return direct children of the selected branch head or a nested tree.

    Returns None when Neo4j is unavailable or the graph projection has not yet
    been built, allowing the API to fall back to the relational tree tables.
    """
    driver = get_driver()
    if driver is None:
        return None
    try:
        driver.verify_connectivity()
        _ensure_graph_schema(driver)
        with driver.session(database=_database_name()) as session:
            record = session.execute_read(
                lambda tx: tx.run("""
                    MATCH (b:Branch {repo_id: $repo_id, branch_id: $branch_id})
                    MATCH (b)-[:HEAD]->(:Commit)-[:SNAPSHOT]->(head:Tree)
                    MATCH path=(head)-[:CONTAINS*0..50]->(folder:Tree)
                    WHERE ($tree_id IS NULL AND folder = head)
                       OR ($tree_id IS NOT NULL AND folder.tree_id = $tree_id)
                    WITH folder LIMIT 1
                    OPTIONAL MATCH (folder)-[rel:CONTAINS]->(child)
                    RETURN folder.tree_id AS tree_id,
                           collect(CASE WHEN child IS NULL THEN null ELSE {
                               name: rel.name,
                               tree_id: child.tree_id,
                               blob_id: child.blob_id,
                               mime_type: child.mime_type,
                               is_folder: child:Tree,
                               can_download: CASE WHEN child:Blob THEN child.has_content ELSE false END
                           } END) AS entries
                """, repo_id=repo_id, branch_id=branch_id, tree_id=tree_id).single()
            )
        if record is None:
            return None
        return {"tree_id": record["tree_id"], "entries": record["entries"] or []}
    except Exception:
        logger.exception("Neo4j branch tree read failed; using MySQL fallback.")
        return None


def pending_graph_events():
    with _mysql_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS count FROM `VCS_GRAPH_OUTBOX` WHERE processed_at IS NULL")
        return cursor.fetchone()["count"]
