import hashlib
import hmac
import json
import mimetypes
import os
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_file, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_pool


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
from neo4j_store import graph_available, graph_configured, pending_graph_events, process_graph_outbox, read_branch_tree

app = Flask(__name__, static_folder=str(ROOT / "public"), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.secret_key = os.getenv("SECRET_KEY", "local-demo-secret-change-this")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
app.json.sort_keys = False

VISIBILITY = {"Public", "Private", "Internal"}
MEMBER_ROLES = {"Maintainer", "Contributor", "Reader"}


@app.before_request
def require_login_for_api():
    open_paths = {"/api/auth/me", "/api/auth/login", "/api/auth/register", "/api/auth/logout"}
    if request.path.startswith("/api/") and request.path not in open_paths and not session.get("user_id"):
        return jsonify({"error": "Please sign in to continue."}), 401


@contextmanager
def db_cursor(write=False):
    connection = get_pool().get_connection()
    cursor = None
    try:
        if write:
            connection.start_transaction(isolation_level="READ COMMITTED")
        cursor = connection.cursor(dictionary=True, buffered=True)
        yield cursor
        if write:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if cursor:
            cursor.close()
        connection.close()


def queue_graph_sync(cursor, repo_id):
    """Queue a graph refresh inside the same MySQL transaction as its source change."""
    cursor.execute(
        "INSERT INTO `VCS_GRAPH_OUTBOX` (event_type, payload) VALUES (%s, %s)",
        ("sync_repository", json.dumps({"repo_id": int(repo_id)})),
    )


def sync_graph_after_write():
    """Apply queued projections when Neo4j is available; failed events remain retryable."""
    try:
        process_graph_outbox(limit=10)
    except Exception:
        app.logger.exception("Neo4j graph projection will be retried later.")


def positive_id(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def clean_text(value, limit):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def body_json():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def safe_name(value):
    if not isinstance(value, str):
        return None
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."} or "\x00" in name or len(name) > 255:
        return None
    return name


def current_user_id():
    return positive_id(session.get("user_id"))


def repository_role(cursor, repo_id, user_id=None):
    user_id = user_id or current_user_id()
    cursor.execute("""
        SELECT r.owner_user_id, rm.role
        FROM `REPOSITORY` r
        LEFT JOIN `REPOSITORY_MEMBER` rm
          ON rm.repo_id = r.repo_id AND rm.user_id = %s
        WHERE r.repo_id = %s
    """, (user_id, repo_id))
    row = cursor.fetchone()
    if not row:
        return None, False
    role = row["role"] or ("Maintainer" if row["owner_user_id"] == user_id else None)
    can_read = row["owner_user_id"] == user_id or role is not None
    return role, can_read


def require_repo_role(cursor, repo_id, allowed):
    role, _ = repository_role(cursor, repo_id)
    if role is None:
        abort(404, description="Repository not found or you do not have access.")
    if role not in allowed:
        abort(403, description="Your repository role does not allow this action.")
    return role


def user_by_id(cursor, user_id):
    cursor.execute("SELECT user_id, username, email FROM `USER` WHERE user_id = %s", (user_id,))
    return cursor.fetchone()


def create_tree_snapshot(cursor, repo_id, entries):
    canonical = [
        {"name": item["file_name"], "mode": item["file_mode"], "hash": item["target_hash"]}
        for item in sorted(entries, key=lambda value: value["file_name"])
    ]
    encoded = json.dumps({"repo": repo_id, "entries": canonical}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    object_hash = hashlib.sha256(encoded).hexdigest()
    cursor.execute("SELECT object_id, object_type, repo_id FROM `OBJECT` WHERE object_hash = %s", (object_hash,))
    existing = cursor.fetchone()
    if existing:
        if existing["object_type"] != "tree" or existing["repo_id"] != repo_id:
            abort(409, description="Object hash conflict; operation was rejected.")
        cursor.execute("SELECT tree_id FROM `TREE` WHERE tree_id = %s", (existing["object_id"],))
        if cursor.fetchone():
            return existing["object_id"]
        tree_id = existing["object_id"]
    else:
        cursor.execute("INSERT INTO `OBJECT` (object_hash, object_type, repo_id) VALUES (%s, 'tree', %s)",
                       (object_hash, repo_id))
        tree_id = cursor.lastrowid
    cursor.execute("INSERT INTO `TREE` (tree_id) VALUES (%s)", (tree_id,))
    if entries:
        cursor.executemany("""
            INSERT INTO `TREE_ENTRY` (file_mode, file_name, tree_id, subtree_id, blob_id)
            VALUES (%s, %s, %s, %s, %s)
        """, [(item["file_mode"], item["file_name"], tree_id,
               item["subtree_id"], item["blob_id"]) for item in entries])
    return tree_id


def require_repo_read(cursor, repo_id):
    cursor.execute("""
        SELECT r.visibility, r.owner_user_id, rm.user_id AS member_user_id
        FROM `REPOSITORY` r
        LEFT JOIN `REPOSITORY_MEMBER` rm ON rm.repo_id = r.repo_id AND rm.user_id = %s
        WHERE r.repo_id = %s
    """, (current_user_id(), repo_id))
    row = cursor.fetchone()
    if not row or (row["visibility"] != "Public" and row["owner_user_id"] != current_user_id()
                   and row["member_user_id"] is None):
        abort(404, description="Repository not found or you do not have access.")


def branch_head(cursor, repo_id, branch_id, for_update=False):
    if for_update:
        cursor.execute("""
            SELECT branch_id, branch_name, repo_id FROM `BRANCH`
            WHERE repo_id = %s AND branch_id = %s FOR UPDATE
        """, (repo_id, branch_id))
        branch = cursor.fetchone()
        if not branch:
            return None
        cursor.execute("""
            SELECT commit_id AS head_commit_id, tree_id AS head_tree_id
            FROM `COMMIT` WHERE branch_id = %s
            ORDER BY committed_at DESC, commit_id DESC LIMIT 1 FOR UPDATE
        """, (branch_id,))
        head = cursor.fetchone() or {}
        return {**branch, "head_commit_id": head.get("head_commit_id"),
                "head_tree_id": head.get("head_tree_id")}
    cursor.execute("""
        SELECT b.branch_id, b.branch_name, b.repo_id,
               c.commit_id AS head_commit_id, c.tree_id AS head_tree_id
        FROM `BRANCH` b
        LEFT JOIN `COMMIT` c ON c.commit_id = (
            SELECT c2.commit_id FROM `COMMIT` c2 WHERE c2.branch_id = b.branch_id
            ORDER BY c2.committed_at DESC, c2.commit_id DESC LIMIT 1
        )
        WHERE b.repo_id = %s AND b.branch_id = %s
    """, (repo_id, branch_id))
    return cursor.fetchone()


def tree_entries(cursor, tree_id):
    if not tree_id:
        return []
    cursor.execute("""
        SELECT te.entry_id, te.file_name, te.file_mode, te.subtree_id, te.blob_id,
               COALESCE(subobj.object_hash, blobobj.object_hash) AS target_hash,
               b.mime_type, b.storage_path
        FROM `TREE_ENTRY` te
        LEFT JOIN `OBJECT` subobj ON subobj.object_id = te.subtree_id
        LEFT JOIN `OBJECT` blobobj ON blobobj.object_id = te.blob_id
        LEFT JOIN `BLOBS` b ON b.blob_id = te.blob_id
        WHERE te.tree_id = %s ORDER BY te.file_name
    """, (tree_id,))
    return cursor.fetchall()


def parse_path(value, allow_empty=False):
    if value == "" and allow_empty:
        return []
    if not isinstance(value, str) or not value:
        abort(400, description="Provide a file path.")
    parts = value.replace("\\", "/").split("/")
    if any(not part or part in {".", ".."} or "\x00" in part or len(part) > 255 for part in parts):
        abort(400, description="That file path is not valid.")
    return parts


def walk_file_path(cursor, root_tree_id, parts):
    if not root_tree_id or not parts:
        abort(404, description="File not found in this branch.")
    current_tree_id = root_tree_id
    for index, part in enumerate(parts):
        entries = tree_entries(cursor, current_tree_id)
        entry = next((item for item in entries if item["file_name"] == part), None)
        if not entry:
            abort(404, description="File not found in this branch.")
        if index < len(parts) - 1:
            if not entry["subtree_id"]:
                abort(404, description="A folder in that path does not exist.")
            current_tree_id = entry["subtree_id"]
        else:
            return current_tree_id, entry
    abort(404, description="File not found in this branch.")


def mutate_tree_path(cursor, repo_id, tree_id, parts, action, blob_id=None, blob_hash=None):
    if not parts:
        abort(400, description="Choose a file path.")
    entries = tree_entries(cursor, tree_id)
    by_name = {item["file_name"]: item for item in entries}
    name = parts[0]
    existing = by_name.get(name)
    if len(parts) > 1:
        if not existing or not existing["subtree_id"]:
            abort(404, description="A folder in that path does not exist.")
        child_tree = mutate_tree_path(cursor, repo_id, existing["subtree_id"], parts[1:],
                                      action, blob_id, blob_hash)
        cursor.execute("SELECT object_hash FROM `OBJECT` WHERE object_id = %s", (child_tree,))
        child_hash = cursor.fetchone()["object_hash"]
        replacement = dict(existing, subtree_id=child_tree, blob_id=None, target_hash=child_hash)
        by_name[name] = replacement
    elif action == "add":
        if existing:
            abort(409, description="A file or folder with that name already exists.")
        by_name[name] = {"file_name": name, "file_mode": "100644", "subtree_id": None,
                         "blob_id": blob_id, "target_hash": blob_hash}
    elif action == "edit":
        if not existing or existing["subtree_id"] or not existing["blob_id"]:
            abort(404, description="Editable file not found in this branch.")
        by_name[name] = dict(existing, subtree_id=None, blob_id=blob_id, target_hash=blob_hash)
    elif action == "delete":
        if not existing or existing["subtree_id"] or not existing["blob_id"]:
            abort(404, description="File not found in this branch.")
        del by_name[name]
    else:
        abort(400, description="Unsupported file operation.")
    return create_tree_snapshot(cursor, repo_id, list(by_name.values()))


def write_blob_bytes(repo_id, content):
    blob_hash = hashlib.sha256(str(repo_id).encode("ascii") + b":" + content).hexdigest()
    relative_path = Path("file-storage") / "objects" / blob_hash[:2] / blob_hash
    disk_path = ROOT / relative_path
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    created = not disk_path.exists()
    if created:
        temp_path = disk_path.with_name(f".{blob_hash}.{uuid.uuid4().hex}.tmp")
        temp_path.write_bytes(content)
        os.replace(temp_path, disk_path)
    return blob_hash, relative_path, disk_path, created


def managed_blob_path(storage_path):
    if not storage_path:
        return None
    root = (ROOT / "file-storage").resolve()
    path = (ROOT / storage_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def text_mime_type(mime_type):
    return bool(mime_type) and (mime_type.startswith("text/") or mime_type in {
        "application/json", "application/javascript", "application/sql", "application/xml",
        "application/x-yaml", "application/toml"
    })


def get_or_create_blob(cursor, repo_id, blob_hash, relative_path, mime_type):
    cursor.execute("SELECT object_id, object_type, repo_id FROM `OBJECT` WHERE object_hash = %s", (blob_hash,))
    blob = cursor.fetchone()
    if blob:
        if blob["object_type"] != "blob" or blob["repo_id"] != repo_id:
            abort(409, description="Object hash conflict; operation was rejected.")
        blob_id = blob["object_id"]
        cursor.execute("SELECT blob_id FROM `BLOBS` WHERE blob_id = %s", (blob_id,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO `BLOBS` (blob_id, storage_path, mime_type) VALUES (%s, %s, %s)",
                           (blob_id, relative_path.as_posix(), mime_type))
    else:
        cursor.execute("INSERT INTO `OBJECT` (object_hash, object_type, repo_id) VALUES (%s, 'blob', %s)",
                       (blob_hash, repo_id))
        blob_id = cursor.lastrowid
        cursor.execute("INSERT INTO `BLOBS` (blob_id, storage_path, mime_type) VALUES (%s, %s, %s)",
                       (blob_id, relative_path.as_posix(), mime_type))
    return blob_id


def commit_tree(cursor, branch_id, author_id, tree_id, message):
    cursor.execute("""
        SELECT commit_id FROM `COMMIT` WHERE branch_id = %s
        ORDER BY committed_at DESC, commit_id DESC LIMIT 1 FOR UPDATE
    """, (branch_id,))
    previous = cursor.fetchone()
    cursor.execute("""
        INSERT INTO `COMMIT` (message, branch_id, author_user_id, tree_id)
        VALUES (%s, %s, %s, %s)
    """, (message, branch_id, author_id, tree_id))
    commit_id = cursor.lastrowid
    if previous:
        cursor.execute("""
            INSERT INTO `COMMIT_PARENT` (commit_id, parent_commit_id, parent_order)
            VALUES (%s, %s, 1)
        """, (commit_id, previous["commit_id"]))
    return commit_id


def merge_branch_state(cursor, repo_id, source_id, target_id, for_update=False):
    source = branch_head(cursor, repo_id, source_id, for_update=for_update)
    target = branch_head(cursor, repo_id, target_id, for_update=for_update)
    if not source or not target:
        abort(400, description="Both branches must belong to this repository.")
    if not source["head_commit_id"] or not target["head_commit_id"] or not source["head_tree_id"] or not target["head_tree_id"]:
        abort(400, description="Both branches need at least one commit before merging.")
    if source["head_commit_id"] == target["head_commit_id"]:
        abort(409, description="These branches already point to the same commit.")
    return source, target


def merge_base_commit(cursor, source_commit_id, target_commit_id):
    cursor.execute("""
        WITH RECURSIVE
        source_ancestors(commit_id, depth) AS (
            SELECT %s, 0
            UNION ALL
            SELECT cp.parent_commit_id, source_ancestors.depth + 1
            FROM source_ancestors
            JOIN `COMMIT_PARENT` cp ON cp.commit_id = source_ancestors.commit_id
            WHERE source_ancestors.depth < 900
        ),
        target_ancestors(commit_id, depth) AS (
            SELECT %s, 0
            UNION ALL
            SELECT cp.parent_commit_id, target_ancestors.depth + 1
            FROM target_ancestors
            JOIN `COMMIT_PARENT` cp ON cp.commit_id = target_ancestors.commit_id
            WHERE target_ancestors.depth < 900
        )
        SELECT source_ancestors.commit_id
        FROM source_ancestors
        JOIN target_ancestors ON target_ancestors.commit_id = source_ancestors.commit_id
        ORDER BY GREATEST(source_ancestors.depth, target_ancestors.depth),
                 source_ancestors.depth + target_ancestors.depth,
                 source_ancestors.commit_id DESC
        LIMIT 1
    """, (source_commit_id, target_commit_id))
    return cursor.fetchone()


def _tree_entry_signature(entry):
    if entry is None:
        return None
    object_type = "tree" if entry["subtree_id"] is not None else "blob"
    return object_type, entry["target_hash"], entry["file_mode"]


def merge_tree_entries(cursor, repo_id, base_tree_id, source_tree_id, target_tree_id,
                       strategy, path="", depth=0):
    """Three-way merge immutable tree snapshots, including nested folders and deletes."""
    base = {entry["file_name"]: entry for entry in tree_entries(cursor, base_tree_id)}
    source = {entry["file_name"]: entry for entry in tree_entries(cursor, source_tree_id)}
    target = {entry["file_name"]: entry for entry in tree_entries(cursor, target_tree_id)}
    conflicts = []
    merged = []

    for name in sorted(set(base) | set(source) | set(target)):
        base_entry, source_entry, target_entry = base.get(name), source.get(name), target.get(name)
        base_signature = _tree_entry_signature(base_entry)
        source_signature = _tree_entry_signature(source_entry)
        target_signature = _tree_entry_signature(target_entry)

        if source_signature == base_signature:
            chosen = target_entry
        elif target_signature == base_signature or source_signature == target_signature:
            chosen = source_entry
        elif (depth < 50 and source_entry and target_entry
              and source_entry["subtree_id"] is not None
              and target_entry["subtree_id"] is not None
              and (base_entry is None or base_entry["subtree_id"] is not None)):
            nested, nested_conflicts = merge_tree_entries(
                cursor, repo_id,
                base_entry["subtree_id"] if base_entry else None,
                source_entry["subtree_id"], target_entry["subtree_id"],
                strategy, f"{path}/{name}".strip("/"), depth + 1,
            )
            nested_tree_id = create_tree_snapshot(cursor, repo_id, nested)
            cursor.execute("SELECT object_hash FROM `OBJECT` WHERE object_id = %s", (nested_tree_id,))
            nested_hash = cursor.fetchone()["object_hash"]
            chosen = dict(target_entry, subtree_id=nested_tree_id, blob_id=None,
                          target_hash=nested_hash)
            conflicts.extend(nested_conflicts)
        else:
            conflicts.append(f"{path}/{name}".strip("/"))
            chosen = source_entry if strategy == "source" else target_entry

        if chosen is not None:
            merged.append(chosen)
    return merged, conflicts


def create_merge_commit(cursor, repo_id, source_id, target_id, author_id, strategy,
                        expected_source_commit, expected_target_commit):
    cursor.execute("""
        SELECT branch_id FROM `BRANCH`
        WHERE repo_id = %s AND branch_id IN (%s, %s)
        FOR UPDATE
    """, (repo_id, source_id, target_id))
    if len(cursor.fetchall()) != 2:
        abort(404, description="One of the merge request branches no longer exists.")
    source, target = merge_branch_state(cursor, repo_id, source_id, target_id, for_update=True)
    if (source["head_commit_id"] != expected_source_commit
            or target["head_commit_id"] != expected_target_commit):
        abort(409, description="A branch changed after this request was submitted. Ask for a new merge approval.")

    base = merge_base_commit(cursor, source["head_commit_id"], target["head_commit_id"])
    base_tree_id = None
    if base:
        cursor.execute("SELECT tree_id FROM `COMMIT` WHERE commit_id = %s", (base["commit_id"],))
        base_tree_id = cursor.fetchone()["tree_id"]
    merged_entries, conflicts = merge_tree_entries(
        cursor, repo_id, base_tree_id, source["head_tree_id"], target["head_tree_id"], strategy
    )
    resulting_tree_id = create_tree_snapshot(cursor, repo_id, merged_entries)
    message = f"Merge branch '{source['branch_name']}' into '{target['branch_name']}'"
    cursor.execute("""
        INSERT INTO `COMMIT` (message, branch_id, author_user_id, tree_id)
        VALUES (%s, %s, %s, %s)
    """, (message, target_id, author_id, resulting_tree_id))
    commit_id = cursor.lastrowid
    cursor.executemany("""
        INSERT INTO `COMMIT_PARENT` (commit_id, parent_commit_id, parent_order)
        VALUES (%s, %s, %s)
    """, [(commit_id, target["head_commit_id"], 1),
           (commit_id, source["head_commit_id"], 2)])
    return {"commit_id": commit_id, "tree_id": resulting_tree_id,
            "source_branch": source["branch_name"], "target_branch": target["branch_name"],
            "conflicts": conflicts, "conflict_strategy": strategy}


@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    with db_cursor() as cursor:
        cursor.execute("SELECT 1")
    sync_graph_after_write()
    neo4j_connected = graph_available()
    try:
        pending = pending_graph_events()
    except mysql.connector.Error:
        pending = None
    return jsonify({"ok": True, "database": os.getenv("DB_NAME", "version_control_system"),
                    "neo4j_configured": graph_configured(), "neo4j": neo4j_connected,
                    "graph_pending": pending})


@app.get("/api/auth/me")
def auth_me():
    user_id = current_user_id()
    if not user_id:
        return jsonify({"user": None})
    with db_cursor() as cursor:
        user = user_by_id(cursor, user_id)
    if not user:
        session.clear()
    return jsonify({"user": user})


@app.post("/api/auth/register")
def register():
    body = body_json()
    username = clean_text(body.get("username"), 50)
    email = clean_text(body.get("email"), 100)
    password = body.get("password")
    if not username or not email or "@" not in email or not isinstance(password, str) or len(password) < 8:
        abort(400, description="Enter a username, valid email, and password of at least 8 characters.")
    with db_cursor(write=True) as cursor:
        cursor.execute("""
            INSERT INTO `USER` (username, email, password_hash)
            VALUES (%s, %s, %s)
        """, (username, email, generate_password_hash(password)))
        user = {"user_id": cursor.lastrowid, "username": username, "email": email}
    session.clear()
    session["user_id"] = user["user_id"]
    session.permanent = True
    return jsonify({"user": user}), 201


@app.post("/api/auth/login")
def login():
    body = body_json()
    identifier = clean_text(body.get("identifier"), 100)
    password = body.get("password")
    if not identifier or not isinstance(password, str):
        abort(400, description="Enter your username or email and password.")
    with db_cursor(write=True) as cursor:
        cursor.execute("""
            SELECT user_id, username, email, password_hash
            FROM `USER` WHERE username = %s OR email = %s LIMIT 1
        """, (identifier, identifier))
        user = cursor.fetchone()
        if not user:
            abort(401, description="Username/email or password is incorrect.")
        stored_hash = user["password_hash"] or ""
        valid = False
        try:
            valid = check_password_hash(stored_hash, password)
        except (ValueError, TypeError):
            pass
        if not valid:
            legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
            if hmac.compare_digest(stored_hash.lower(), legacy_hash):
                cursor.execute("UPDATE `USER` SET password_hash = %s WHERE user_id = %s",
                               (generate_password_hash(password), user["user_id"]))
                valid = True
        if not valid:
            abort(401, description="Username/email or password is incorrect.")
        result = {"user_id": user["user_id"], "username": user["username"], "email": user["email"]}
    session.clear()
    session["user_id"] = result["user_id"]
    session.permanent = True
    return jsonify({"user": result})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/users")
def users():
    with db_cursor() as cursor:
        cursor.execute("SELECT user_id, username FROM `USER` ORDER BY username")
        return jsonify(cursor.fetchall())


@app.get("/api/overview")
def overview():
    with db_cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(DISTINCT r.repo_id) AS repository_count,
                   COUNT(DISTINCT b.branch_id) AS branch_count,
                   COUNT(DISTINCT c.commit_id) AS commit_count,
                   COUNT(DISTINCT p.push_id) AS push_count
            FROM `REPOSITORY` r
            LEFT JOIN `BRANCH` b ON b.repo_id = r.repo_id
            LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
            LEFT JOIN `PUSH_EVENT` p ON p.branch_id = b.branch_id
            WHERE r.visibility = 'Public' OR r.owner_user_id = %s OR EXISTS (
              SELECT 1 FROM `REPOSITORY_MEMBER` rm
              WHERE rm.repo_id = r.repo_id AND rm.user_id = %s
            )
        """, (current_user_id(), current_user_id()))
        stats = cursor.fetchone()
        cursor.execute("""
            SELECT c.commit_id, c.message, c.committed_at,
                   u.username AS author, b.branch_name, r.repo_name, r.repo_id
            FROM `COMMIT` c
            JOIN `USER` u ON u.user_id = c.author_user_id
            JOIN `BRANCH` b ON b.branch_id = c.branch_id
            JOIN `REPOSITORY` r ON r.repo_id = b.repo_id
            WHERE r.visibility = 'Public' OR r.owner_user_id = %s OR EXISTS (
              SELECT 1 FROM `REPOSITORY_MEMBER` rm
              WHERE rm.repo_id = r.repo_id AND rm.user_id = %s
            )
            ORDER BY c.committed_at DESC, c.commit_id DESC LIMIT 8
        """, (current_user_id(), current_user_id()))
        activity = cursor.fetchall()
    return jsonify({"stats": stats, "activity": activity})


@app.get("/api/repositories")
def repositories():
    filters, params = [], []
    search = request.args.get("q", "", type=str).strip()
    visibility = request.args.get("visibility", "", type=str)
    if search:
        filters.append("(a.repo_name LIKE %s OR a.owner LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    if visibility in VISIBILITY:
        filters.append("a.visibility = %s")
        params.append(visibility)
    filters.append("""(r.visibility = 'Public' OR r.owner_user_id = %s OR EXISTS (
        SELECT 1 FROM `REPOSITORY_MEMBER` access_row
        WHERE access_row.repo_id = r.repo_id AND access_row.user_id = %s
    ))""")
    params.extend([current_user_id(), current_user_id()])
    where = "WHERE " + " AND ".join(filters) if filters else ""
    query = f"""
        SELECT a.repo_id, a.repo_name, r.description, a.owner, a.visibility,
               a.member_count, a.branch_count, a.commit_count, a.latest_commit
        FROM `vw_repository_activity` a
        JOIN `REPOSITORY` r ON r.repo_id = a.repo_id
        {where}
        ORDER BY a.commit_count DESC, a.repo_name
    """
    with db_cursor() as cursor:
        cursor.execute(query, tuple(params))
        return jsonify(cursor.fetchall())


@app.get("/api/repositories/<repo_id>")
def repository_detail(repo_id):
    repo_id = positive_id(repo_id)
    if not repo_id:
        abort(400, description="Repository ID must be a positive integer.")
    with db_cursor() as cursor:
        cursor.execute("""
            SELECT r.repo_id, r.repo_name, r.description, r.visibility, r.created_at,
                   r.owner_user_id, r.default_branch_id, u.username AS owner
            FROM `REPOSITORY` r JOIN `USER` u ON u.user_id = r.owner_user_id
            WHERE r.repo_id = %s AND (
              r.visibility = 'Public' OR r.owner_user_id = %s OR EXISTS (
                SELECT 1 FROM `REPOSITORY_MEMBER` access_row
                WHERE access_row.repo_id = r.repo_id AND access_row.user_id = %s
              )
            )
        """, (repo_id, current_user_id(), current_user_id()))
        repo = cursor.fetchone()
        if not repo:
            abort(404, description="Repository not found or you do not have access.")

        my_role, _ = repository_role(cursor, repo_id)

        cursor.execute("""
            SELECT b.branch_id, b.branch_name, b.created_at, u.username AS created_by,
                   COUNT(c.commit_id) AS commit_count, MAX(c.committed_at) AS latest_commit,
                   (SELECT c2.commit_id FROM `COMMIT` c2 WHERE c2.branch_id = b.branch_id
                    ORDER BY c2.committed_at DESC, c2.commit_id DESC LIMIT 1) AS head_commit_id,
                   (SELECT c2.tree_id FROM `COMMIT` c2 WHERE c2.branch_id = b.branch_id
                    ORDER BY c2.committed_at DESC, c2.commit_id DESC LIMIT 1) AS head_tree_id
            FROM `BRANCH` b JOIN `USER` u ON u.user_id = b.created_by_user_id
            LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
            WHERE b.repo_id = %s
            GROUP BY b.branch_id, b.branch_name, b.created_at, u.username
            ORDER BY b.branch_name
        """, (repo_id,))
        branches = cursor.fetchall()

        cursor.execute("""
            SELECT u.user_id, u.username, rm.role, rm.joined_at
            FROM `REPOSITORY_MEMBER` rm JOIN `USER` u ON u.user_id = rm.user_id
            WHERE rm.repo_id = %s
            ORDER BY FIELD(rm.role, 'Maintainer', 'Contributor', 'Reader'), u.username
        """, (repo_id,))
        members = cursor.fetchall()

        cursor.execute("""
            SELECT t.tree_id, o.object_hash FROM `TREE` t
            JOIN `OBJECT` o ON o.object_id = t.tree_id
            WHERE o.repo_id = %s AND o.object_type = 'tree'
            ORDER BY o.created_at DESC, t.tree_id DESC
        """, (repo_id,))
        trees = cursor.fetchall()

        cursor.execute("""
            SELECT c.commit_id, c.message, c.committed_at, c.tree_id,
                   u.username AS author, b.branch_name, b.branch_id
            FROM `COMMIT` c JOIN `USER` u ON u.user_id = c.author_user_id
            JOIN `BRANCH` b ON b.branch_id = c.branch_id
            WHERE b.repo_id = %s
            ORDER BY c.committed_at DESC, c.commit_id DESC LIMIT 12
        """, (repo_id,))
        commits = cursor.fetchall()

        merge_requests = []
        if my_role is not None:
            cursor.execute("""
                SELECT mr.merge_request_id, mr.status, mr.conflict_strategy,
                       mr.requested_at, mr.reviewed_at, mr.merge_commit_id,
                       mr.requested_by_user_id, requester.username AS requested_by,
                       reviewer.username AS reviewed_by,
                       mr.source_head_commit_id, mr.target_head_commit_id,
                       source_branch.branch_name AS source_branch_name,
                       target_branch.branch_name AS target_branch_name
                FROM `MERGE_REQUEST` mr
                JOIN `USER` requester ON requester.user_id = mr.requested_by_user_id
                LEFT JOIN `USER` reviewer ON reviewer.user_id = mr.reviewed_by_user_id
                JOIN `BRANCH` source_branch ON source_branch.branch_id = mr.source_branch_id
                JOIN `BRANCH` target_branch ON target_branch.branch_id = mr.target_branch_id
                WHERE mr.repo_id = %s
                ORDER BY (mr.status = 'Pending') DESC, mr.requested_at DESC
                LIMIT 20
            """, (repo_id,))
            merge_requests = cursor.fetchall()

        head_tree_id = commits[0]["tree_id"] if commits else None
        files = []
        if head_tree_id:
            cursor.execute("""
                SELECT te.entry_id, te.file_name, te.file_mode, te.blob_id,
                       te.subtree_id, b.mime_type
                FROM `TREE_ENTRY` te LEFT JOIN `BLOBS` b ON b.blob_id = te.blob_id
                WHERE te.tree_id = %s ORDER BY te.file_name
            """, (head_tree_id,))
            files = cursor.fetchall()

    is_owner = repo["owner_user_id"] == current_user_id()
    return jsonify({"repository": repo, "branches": branches, "members": members,
                    "trees": trees, "commits": commits, "head_tree_id": head_tree_id,
                    "files": files, "my_role": my_role, "merge_requests": merge_requests,
                    "is_owner": is_owner,
                    "can_write": my_role in {"Maintainer", "Contributor"},
                    "can_admin": my_role == "Maintainer"})


@app.post("/api/repositories")
def create_repository():
    body = body_json()
    name = clean_text(body.get("repo_name"), 100)
    description = body.get("description", "")
    description = description.strip()[:5000] if isinstance(description, str) else ""
    owner_id = current_user_id()
    visibility = body.get("visibility")
    if not name or not owner_id or visibility not in VISIBILITY:
        abort(400, description="Provide a repository name, valid owner, and visibility.")
    with db_cursor(write=True) as cursor:
        cursor.execute("""
            INSERT INTO `REPOSITORY` (repo_name, description, visibility, owner_user_id)
            VALUES (%s, %s, %s, %s)
        """, (name, description or None, visibility, owner_id))
        repo_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO `BRANCH` (branch_name, repo_id, created_by_user_id)
            VALUES ('main', %s, %s)
        """, (repo_id, owner_id))
        main_branch_id = cursor.lastrowid
        cursor.execute("SELECT repo_id, repo_name FROM `REPOSITORY` WHERE repo_id = %s", (repo_id,))
        result = cursor.fetchone()
        result["default_branch_id"] = main_branch_id
        queue_graph_sync(cursor, repo_id)
    sync_graph_after_write()
    return jsonify(result), 201


@app.post("/api/repositories/<repo_id>/members")
def add_member(repo_id):
    repo_id = positive_id(repo_id)
    body = body_json()
    user_id = positive_id(body.get("user_id"))
    role = body.get("role")
    if not repo_id or not user_id or role not in MEMBER_ROLES:
        abort(400, description="Choose a user and valid role.")
    with db_cursor(write=True) as cursor:
        require_repo_role(cursor, repo_id, {"Maintainer"})
        cursor.execute("""
            INSERT INTO `REPOSITORY_MEMBER` (user_id, repo_id, role)
            VALUES (%s, %s, %s)
        """, (user_id, repo_id, role))
        membership_id = cursor.lastrowid
    return jsonify({"membership_id": membership_id, "user_id": user_id, "role": role}), 201


@app.patch("/api/repositories/<repo_id>/members/<user_id>")
def change_member_role(repo_id, user_id):
    repo_id, user_id = positive_id(repo_id), positive_id(user_id)
    role = body_json().get("role")
    if not repo_id or not user_id or role not in MEMBER_ROLES:
        abort(400, description="Choose a repository member and valid role.")
    with db_cursor(write=True) as cursor:
        require_repo_role(cursor, repo_id, {"Maintainer"})
        cursor.execute("SELECT owner_user_id FROM `REPOSITORY` WHERE repo_id = %s", (repo_id,))
        repo = cursor.fetchone()
        if not repo:
            abort(404, description="Repository not found.")
        if repo["owner_user_id"] == user_id and role != "Maintainer":
            abort(400, description="The repository owner must remain a Maintainer.")
        cursor.execute("SELECT membership_id FROM `REPOSITORY_MEMBER` WHERE repo_id = %s AND user_id = %s",
                       (repo_id, user_id))
        membership = cursor.fetchone()
        if not membership:
            abort(404, description="That user is not a member of this repository.")
        cursor.execute("UPDATE `REPOSITORY_MEMBER` SET role = %s WHERE repo_id = %s AND user_id = %s",
                       (role, repo_id, user_id))
    return jsonify({"user_id": user_id, "role": role})


@app.delete("/api/repositories/<repo_id>/members/<user_id>")
def remove_member(repo_id, user_id):
    repo_id, user_id = positive_id(repo_id), positive_id(user_id)
    if not repo_id or not user_id:
        abort(400, description="Choose a repository and member.")
    with db_cursor(write=True) as cursor:
        require_repo_role(cursor, repo_id, {"Maintainer"})
        cursor.execute("SELECT owner_user_id FROM `REPOSITORY` WHERE repo_id = %s", (repo_id,))
        repo = cursor.fetchone()
        if repo["owner_user_id"] == user_id:
            abort(400, description="The repository owner cannot be removed.")
        cursor.execute("DELETE FROM `REPOSITORY_MEMBER` WHERE repo_id = %s AND user_id = %s",
                       (repo_id, user_id))
        if cursor.rowcount == 0:
            abort(404, description="That user is not a member of this repository.")
    return jsonify({"removed": True, "user_id": user_id})


@app.delete("/api/repositories/<repo_id>")
def delete_repository(repo_id):
    repo_id = positive_id(repo_id)
    if not repo_id:
        abort(400, description="Repository ID must be a positive integer.")
    stored_paths = []
    with db_cursor(write=True) as cursor:
        cursor.execute("SELECT owner_user_id FROM `REPOSITORY` WHERE repo_id = %s", (repo_id,))
        repo = cursor.fetchone()
        if not repo:
            abort(404, description="Repository not found.")
        if repo["owner_user_id"] != current_user_id():
            abort(403, description="Only the repository owner can delete it.")
        cursor.execute("""
            SELECT b.storage_path FROM `BLOBS` b
            JOIN `OBJECT` o ON o.object_id = b.blob_id
            WHERE o.repo_id = %s AND b.storage_path IS NOT NULL
        """, (repo_id,))
        stored_paths = [row["storage_path"] for row in cursor.fetchall()]
        queue_graph_sync(cursor, repo_id)
        cursor.execute("DELETE FROM `REPOSITORY` WHERE repo_id = %s", (repo_id,))
        if cursor.rowcount == 0:
            abort(404, description="Repository not found.")
    sync_graph_after_write()
    storage_root = (ROOT / "file-storage").resolve()
    for relative in stored_paths:
        try:
            with db_cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS uses_count FROM `BLOBS` WHERE storage_path = %s", (relative,))
                if cursor.fetchone()["uses_count"]:
                    continue
            path = (ROOT / relative).resolve()
            path.relative_to(storage_root)
            path.unlink(missing_ok=True)
        except (ValueError, OSError, mysql.connector.Error):
            app.logger.warning("Could not clean up stored file %s after repository deletion", relative)
            continue
    return jsonify({"deleted": True, "repo_id": repo_id})


@app.post("/api/repositories/<repo_id>/branches")
def create_branch(repo_id):
    repo_id = positive_id(repo_id)
    body = body_json()
    name = clean_text(body.get("branch_name"), 100)
    from_branch_id = positive_id(body.get("from_branch_id"))
    creator_id = current_user_id()
    if not repo_id or not name:
        abort(400, description="Provide a repository and branch name.")
    with db_cursor(write=True) as cursor:
        require_repo_role(cursor, repo_id, {"Maintainer", "Contributor"})
        base = branch_head(cursor, repo_id, from_branch_id, for_update=True) if from_branch_id else None
        if from_branch_id and not base:
            abort(400, description="The base branch must belong to this repository.")
        cursor.execute("""
            INSERT INTO `BRANCH` (branch_name, repo_id, created_by_user_id)
            VALUES (%s, %s, %s)
        """, (name, repo_id, creator_id))
        branch_id = cursor.lastrowid
        cursor.execute("UPDATE `REPOSITORY` SET default_branch_id = %s WHERE repo_id = %s AND default_branch_id IS NULL",
                       (branch_id, repo_id))
        if base and base["head_commit_id"] and base["head_tree_id"]:
            cursor.execute("""
                INSERT INTO `COMMIT` (message, branch_id, author_user_id, tree_id)
                VALUES (%s, %s, %s, %s)
            """, (f"Branch from '{base['branch_name']}'", branch_id, creator_id, base["head_tree_id"]))
            initial_commit_id = cursor.lastrowid
            cursor.execute("""
                INSERT INTO `COMMIT_PARENT` (commit_id, parent_commit_id, parent_order)
                VALUES (%s, %s, 1)
            """, (initial_commit_id, base["head_commit_id"]))
        queue_graph_sync(cursor, repo_id)
    sync_graph_after_write()
    return jsonify({"branch_id": branch_id, "branch_name": name,
                    "from_branch_id": from_branch_id}), 201


@app.patch("/api/branches/<branch_id>")
def rename_branch(branch_id):
    branch_id = positive_id(branch_id)
    branch_name = clean_text(body_json().get("branch_name"), 100)
    if not branch_id or not branch_name:
        abort(400, description="Enter a branch name.")
    with db_cursor(write=True) as cursor:
        cursor.execute("SELECT repo_id FROM `BRANCH` WHERE branch_id = %s FOR UPDATE", (branch_id,))
        branch = cursor.fetchone()
        if not branch:
            abort(404, description="Branch not found.")
        require_repo_role(cursor, branch["repo_id"], {"Maintainer", "Contributor"})
        cursor.execute("UPDATE `BRANCH` SET branch_name = %s WHERE branch_id = %s",
                       (branch_name, branch_id))
        queue_graph_sync(cursor, branch["repo_id"])
    sync_graph_after_write()
    return jsonify({"branch_id": branch_id, "branch_name": branch_name})


@app.delete("/api/branches/<branch_id>")
def delete_branch(branch_id):
    branch_id = positive_id(branch_id)
    if not branch_id:
        abort(400, description="Branch ID must be a positive integer.")
    with db_cursor(write=True) as cursor:
        cursor.execute("""
            SELECT b.repo_id, r.default_branch_id FROM `BRANCH` b
            JOIN `REPOSITORY` r ON r.repo_id = b.repo_id WHERE b.branch_id = %s
        """, (branch_id,))
        branch = cursor.fetchone()
        if not branch:
            abort(404, description="Branch not found.")
        require_repo_role(cursor, branch["repo_id"], {"Maintainer"})
        if branch["default_branch_id"] == branch_id:
            abort(400, description="Choose another default branch before deleting this one.")
        cursor.execute("DELETE FROM `BRANCH` WHERE branch_id = %s", (branch_id,))
        queue_graph_sync(cursor, branch["repo_id"])
    sync_graph_after_write()
    return jsonify({"deleted": True, "branch_id": branch_id})


@app.put("/api/repositories/<repo_id>/default-branch")
def set_default_branch(repo_id):
    repo_id = positive_id(repo_id)
    branch_id = positive_id(body_json().get("branch_id"))
    if not repo_id or not branch_id:
        abort(400, description="Select a default branch.")
    with db_cursor(write=True) as cursor:
        require_repo_role(cursor, repo_id, {"Maintainer"})
        cursor.execute("SELECT branch_id FROM `BRANCH` WHERE branch_id = %s AND repo_id = %s",
                       (branch_id, repo_id))
        if not cursor.fetchone():
            abort(400, description="The selected branch is not in this repository.")
        cursor.execute("UPDATE `REPOSITORY` SET default_branch_id = %s WHERE repo_id = %s",
                       (branch_id, repo_id))
        queue_graph_sync(cursor, repo_id)
    sync_graph_after_write()
    return jsonify({"default_branch_id": branch_id})


@app.post("/api/branches/<branch_id>/commits")
def create_commit(branch_id):
    branch_id = positive_id(branch_id)
    body = body_json()
    author_id = current_user_id()
    tree_id = positive_id(body.get("tree_id"))
    message = clean_text(body.get("message"), 5000)
    if not branch_id or not tree_id or not message:
        abort(400, description="Provide a branch, tree, and commit message.")
    with db_cursor(write=True) as cursor:
        cursor.execute("SELECT repo_id FROM `BRANCH` WHERE branch_id = %s", (branch_id,))
        branch_row = cursor.fetchone()
        if not branch_row:
            abort(404, description="Branch not found.")
        require_repo_role(cursor, branch_row["repo_id"], {"Maintainer", "Contributor"})
        branch = branch_head(cursor, branch_row["repo_id"], branch_id, for_update=True)
        if not branch or not branch["head_tree_id"] or tree_id != branch["head_tree_id"]:
            abort(400, description="A commit must use the current snapshot of its selected branch.")
        cursor.execute("""
            SELECT commit_id FROM `COMMIT` WHERE branch_id = %s
            ORDER BY committed_at DESC, commit_id DESC LIMIT 1 FOR UPDATE
        """, (branch_id,))
        previous = cursor.fetchone()
        cursor.execute("""
            INSERT INTO `COMMIT` (message, branch_id, author_user_id, tree_id)
            VALUES (%s, %s, %s, %s)
        """, (message, branch_id, author_id, tree_id))
        commit_id = cursor.lastrowid
        if previous:
            cursor.execute("""
                INSERT INTO `COMMIT_PARENT` (commit_id, parent_commit_id, parent_order)
                VALUES (%s, %s, 1)
            """, (commit_id, previous["commit_id"]))
        queue_graph_sync(cursor, branch["repo_id"])
    sync_graph_after_write()
    return jsonify({"commit_id": commit_id, "message": message}), 201


@app.get("/api/branches/<branch_id>/history")
def branch_history(branch_id):
    branch_id = positive_id(branch_id)
    if not branch_id:
        abort(400, description="Branch ID must be a positive integer.")
    with db_cursor() as cursor:
        cursor.execute("""
            SELECT b.repo_id, r.owner_user_id
            FROM `BRANCH` b JOIN `REPOSITORY` r ON r.repo_id = b.repo_id
            WHERE b.branch_id = %s
        """, (branch_id,))
        branch_row = cursor.fetchone()
        if not branch_row:
            abort(404, description="Branch not found.")
        if branch_row["owner_user_id"] != current_user_id():
            abort(403, description="Only the repository owner can view rollback targets.")
        branch = branch_head(cursor, branch_row["repo_id"], branch_id)
        if not branch or not branch["head_commit_id"]:
            return jsonify([])
        cursor.execute("""
            WITH RECURSIVE history(commit_id) AS (
                SELECT %s
                UNION
                SELECT cp.parent_commit_id
                FROM `COMMIT_PARENT` cp JOIN history h ON cp.commit_id = h.commit_id
            )
            SELECT c.commit_id, c.message, c.committed_at, c.tree_id,
                   b.branch_name, u.username AS author
            FROM history h JOIN `COMMIT` c ON c.commit_id = h.commit_id
            JOIN `BRANCH` b ON b.branch_id = c.branch_id
            JOIN `USER` u ON u.user_id = c.author_user_id
            WHERE c.commit_id <> %s AND b.repo_id = %s
            ORDER BY c.committed_at DESC, c.commit_id DESC
        """, (branch["head_commit_id"], branch["head_commit_id"], branch_row["repo_id"]))
        commits = cursor.fetchall()
    return jsonify(commits)


@app.post("/api/branches/<branch_id>/rollback")
def rollback_branch(branch_id):
    branch_id = positive_id(branch_id)
    body = body_json()
    target_commit_id = positive_id(body.get("target_commit_id"))
    message = clean_text(body.get("message"), 5000)
    if not branch_id or not target_commit_id:
        abort(400, description="Choose a branch and an earlier commit to restore.")

    with db_cursor(write=True) as cursor:
        cursor.execute("""
            SELECT b.repo_id, r.owner_user_id
            FROM `BRANCH` b JOIN `REPOSITORY` r ON r.repo_id = b.repo_id
            WHERE b.branch_id = %s FOR UPDATE
        """, (branch_id,))
        branch_row = cursor.fetchone()
        if not branch_row:
            abort(404, description="Branch not found.")
        if branch_row["owner_user_id"] != current_user_id():
            abort(403, description="Only the repository owner can roll back a branch.")

        branch = branch_head(cursor, branch_row["repo_id"], branch_id, for_update=True)
        if not branch or not branch["head_commit_id"] or not branch["head_tree_id"]:
            abort(400, description="This branch does not have a committed snapshot to roll back.")
        cursor.execute("""
            WITH RECURSIVE history(commit_id) AS (
                SELECT %s
                UNION
                SELECT cp.parent_commit_id
                FROM `COMMIT_PARENT` cp JOIN history h ON cp.commit_id = h.commit_id
            )
            SELECT c.commit_id, c.tree_id
            FROM history h JOIN `COMMIT` c ON c.commit_id = h.commit_id
            JOIN `BRANCH` b ON b.branch_id = c.branch_id
            WHERE c.commit_id = %s AND c.commit_id <> %s AND b.repo_id = %s
        """, (branch["head_commit_id"], target_commit_id,
              branch["head_commit_id"], branch_row["repo_id"]))
        target = cursor.fetchone()
        if not target:
            abort(400, description="Choose an earlier commit from this branch's history.")

        rollback_message = message or f"Rollback {branch['branch_name']} to commit #{target_commit_id}"
        cursor.execute("""
            INSERT INTO `COMMIT` (message, branch_id, author_user_id, tree_id)
            VALUES (%s, %s, %s, %s)
        """, (rollback_message, branch_id, current_user_id(), target["tree_id"]))
        rollback_commit_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO `COMMIT_PARENT` (commit_id, parent_commit_id, parent_order)
            VALUES (%s, %s, 1)
        """, (rollback_commit_id, branch["head_commit_id"]))
        queue_graph_sync(cursor, branch_row["repo_id"])

    sync_graph_after_write()
    return jsonify({"commit_id": rollback_commit_id,
                    "target_commit_id": target_commit_id,
                    "branch_id": branch_id,
                    "message": rollback_message}), 201


@app.post("/api/repositories/<repo_id>/merges")
def merge_branches(repo_id):
    repo_id = positive_id(repo_id)
    body = body_json()
    source_id = positive_id(body.get("source_branch_id"))
    target_id = positive_id(body.get("target_branch_id"))
    author_id = current_user_id()
    strategy = body.get("conflict_strategy", "target")
    if not repo_id or not source_id or not target_id or source_id == target_id:
        abort(400, description="Choose different source and target branches.")
    if strategy not in {"source", "target"}:
        abort(400, description="Conflict strategy must be source or target.")

    with db_cursor(write=True) as cursor:
        require_repo_role(cursor, repo_id, {"Maintainer", "Contributor"})
        source, target = merge_branch_state(cursor, repo_id, source_id, target_id, for_update=True)
        cursor.execute("""
            SELECT merge_request_id, source_head_commit_id, target_head_commit_id
            FROM `MERGE_REQUEST`
            WHERE repo_id = %s AND source_branch_id = %s AND target_branch_id = %s
              AND requested_by_user_id = %s AND status = 'Pending'
            ORDER BY requested_at DESC LIMIT 1
        """, (repo_id, source_id, target_id, author_id))
        existing_request = cursor.fetchone()
        if existing_request:
            if (existing_request["source_head_commit_id"] == source["head_commit_id"]
                    and existing_request["target_head_commit_id"] == target["head_commit_id"]):
                return jsonify({"merge_request_id": existing_request["merge_request_id"],
                                "status": "Pending", "approval_required": True}), 202
            cursor.execute("""
                UPDATE `MERGE_REQUEST` SET status = 'Stale', reviewed_at = CURRENT_TIMESTAMP
                WHERE merge_request_id = %s AND status = 'Pending'
            """, (existing_request["merge_request_id"],))

        cursor.execute("""
            INSERT INTO `MERGE_REQUEST`
                (repo_id, source_branch_id, target_branch_id,
                 source_head_commit_id, target_head_commit_id,
                 requested_by_user_id, conflict_strategy)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (repo_id, source_id, target_id, source["head_commit_id"],
              target["head_commit_id"], author_id, strategy))
        request_id = cursor.lastrowid
    return jsonify({"merge_request_id": request_id, "status": "Pending",
                    "approval_required": True}), 202


def review_merge_request(merge_request_id, approved):
    merge_request_id = positive_id(merge_request_id)
    if not merge_request_id:
        abort(400, description="Merge request ID must be a positive integer.")
    response = None
    merge_completed = False
    with db_cursor(write=True) as cursor:
        cursor.execute("""
            SELECT mr.*, r.owner_user_id
            FROM `MERGE_REQUEST` mr
            JOIN `REPOSITORY` r ON r.repo_id = mr.repo_id
            WHERE mr.merge_request_id = %s
            FOR UPDATE
        """, (merge_request_id,))
        merge_request = cursor.fetchone()
        if not merge_request:
            abort(404, description="Merge request not found.")
        if merge_request["owner_user_id"] != current_user_id():
            abort(403, description="Only the repository owner can review merge requests.")
        if merge_request["status"] != "Pending":
            abort(409, description="This merge request has already been reviewed.")

        if approved:
            try:
                result = create_merge_commit(
                    cursor, merge_request["repo_id"], merge_request["source_branch_id"],
                    merge_request["target_branch_id"], current_user_id(),
                    merge_request["conflict_strategy"], merge_request["source_head_commit_id"],
                    merge_request["target_head_commit_id"])
            except HTTPException as error:
                if error.code == 409 and ("changed after" in error.description
                                          or "already point to the same commit" in error.description):
                    cursor.execute("""
                        UPDATE `MERGE_REQUEST`
                        SET status = 'Stale', reviewed_by_user_id = %s,
                            reviewed_at = CURRENT_TIMESTAMP
                        WHERE merge_request_id = %s AND status = 'Pending'
                    """, (current_user_id(), merge_request_id))
                    response = (jsonify({"error": "The branches changed. This request is now stale; the member can submit a new request.",
                                         "status": "Stale"}), 409)
                else:
                    raise
            else:
                cursor.execute("""
                    UPDATE `MERGE_REQUEST`
                    SET status = 'Approved', reviewed_by_user_id = %s,
                        reviewed_at = CURRENT_TIMESTAMP, merge_commit_id = %s
                    WHERE merge_request_id = %s AND status = 'Pending'
                """, (current_user_id(), result["commit_id"], merge_request_id))
                queue_graph_sync(cursor, merge_request["repo_id"])
                response = jsonify({**result, "status": "Approved"})
                merge_completed = True
        else:
            cursor.execute("""
                UPDATE `MERGE_REQUEST`
                SET status = 'Rejected', reviewed_by_user_id = %s,
                    reviewed_at = CURRENT_TIMESTAMP
                WHERE merge_request_id = %s AND status = 'Pending'
            """, (current_user_id(), merge_request_id))
            response = jsonify({"merge_request_id": merge_request_id, "status": "Rejected"})
    if merge_completed:
        sync_graph_after_write()
    return response


@app.post("/api/merge-requests/<merge_request_id>/approve")
def approve_merge_request(merge_request_id):
    return review_merge_request(merge_request_id, True)


@app.post("/api/merge-requests/<merge_request_id>/reject")
def reject_merge_request(merge_request_id):
    return review_merge_request(merge_request_id, False)


@app.post("/api/branches/<branch_id>/pushes")
def create_push(branch_id):
    branch_id = positive_id(branch_id)
    user_id = current_user_id()
    if not branch_id:
        abort(400, description="Provide a branch.")
    with db_cursor(write=True) as cursor:
        cursor.execute("SELECT repo_id FROM `BRANCH` WHERE branch_id = %s", (branch_id,))
        branch = cursor.fetchone()
        if not branch:
            abort(404, description="Branch not found.")
        require_repo_role(cursor, branch["repo_id"], {"Maintainer", "Contributor"})
        cursor.execute("INSERT INTO `PUSH_EVENT` (user_id, branch_id) VALUES (%s, %s)",
                       (user_id, branch_id))
        push_id = cursor.lastrowid
    return jsonify({"push_id": push_id}), 201


@app.get("/api/repositories/<repo_id>/files/tree")
def browse_tree(repo_id):
    repo_id = positive_id(repo_id)
    branch_id = positive_id(request.args.get("branch_id"))
    requested_tree_id = positive_id(request.args.get("tree_id"))
    if not repo_id or not branch_id:
        abort(400, description="Choose a repository and branch.")
    with db_cursor() as cursor:
        require_repo_read(cursor, repo_id)
        branch = branch_head(cursor, repo_id, branch_id)
        if not branch:
            abort(404, description="Branch not found.")
        tree_id = requested_tree_id or branch["head_tree_id"]
        if requested_tree_id:
            cursor.execute("""
                WITH RECURSIVE reachable_trees(tree_id, depth) AS (
                    SELECT tree_id, 0 FROM `COMMIT` WHERE commit_id = %s
                    UNION ALL
                    SELECT te.subtree_id, reachable_trees.depth + 1
                    FROM reachable_trees
                    JOIN `TREE_ENTRY` te ON te.tree_id = reachable_trees.tree_id
                    WHERE te.subtree_id IS NOT NULL AND reachable_trees.depth < 50
                )
                SELECT tree_id FROM reachable_trees WHERE tree_id = %s LIMIT 1
            """, (branch["head_commit_id"], requested_tree_id))
            if not cursor.fetchone():
                abort(404, description="Folder not found in the selected branch.")
        entries = tree_entries(cursor, tree_id) if tree_id else []

    graph_tree = read_branch_tree(repo_id, branch_id, tree_id)
    if graph_tree is not None:
        graph_entries = []
        for entry in graph_tree["entries"]:
            if not entry:
                continue
            mime_type = entry.get("mime_type")
            can_download = bool(entry.get("can_download"))
            graph_entries.append({
                "name": entry["name"],
                "tree_id": entry.get("tree_id"),
                "blob_id": entry.get("blob_id"),
                "mime_type": mime_type,
                "is_folder": bool(entry.get("is_folder")),
                "can_download": can_download,
                "can_edit": can_download and text_mime_type(mime_type),
            })
        return jsonify({"branch_id": branch_id, "tree_id": graph_tree["tree_id"],
                        "entries": graph_entries, "storage": "neo4j"})

    result = []
    for entry in entries:
        local_path = managed_blob_path(entry["storage_path"])
        result.append({
            "name": entry["file_name"],
            "tree_id": entry["subtree_id"],
            "blob_id": entry["blob_id"],
            "mime_type": entry["mime_type"],
            "is_folder": bool(entry["subtree_id"]),
            "can_download": local_path is not None,
            "can_edit": local_path is not None and text_mime_type(entry["mime_type"]),
        })
    return jsonify({"branch_id": branch_id, "tree_id": tree_id, "entries": result,
                    "storage": "mysql"})


@app.get("/api/repositories/<repo_id>/files/content")
def read_file_content(repo_id):
    repo_id = positive_id(repo_id)
    branch_id = positive_id(request.args.get("branch_id"))
    path_parts = parse_path(request.args.get("path", ""))
    if not repo_id or not branch_id:
        abort(400, description="Choose a repository and branch.")
    with db_cursor() as cursor:
        require_repo_read(cursor, repo_id)
        branch = branch_head(cursor, repo_id, branch_id)
        if not branch:
            abort(404, description="Branch not found.")
        _, entry = walk_file_path(cursor, branch["head_tree_id"], path_parts)
        if entry["subtree_id"] or not entry["blob_id"]:
            abort(400, description="Select a file, not a folder.")
        local_path = managed_blob_path(entry["storage_path"])
        if not local_path:
            abort(404, description="This sample file has no uploaded contents to edit.")
        try:
            content = local_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            abort(415, description="This file is not UTF-8 text and cannot be edited in the browser.")
    return jsonify({"path": "/".join(path_parts), "content": content,
                    "message": f"Edit {entry['file_name']}"})


@app.put("/api/repositories/<repo_id>/files/content")
def edit_file_content(repo_id):
    repo_id = positive_id(repo_id)
    body = body_json()
    branch_id = positive_id(body.get("branch_id"))
    path_parts = parse_path(body.get("path", ""))
    content = body.get("content")
    message = clean_text(body.get("message"), 5000) or f"Update {path_parts[-1]}"
    if not repo_id or not branch_id or not isinstance(content, str):
        abort(400, description="Provide a branch and text file contents.")
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > app.config["MAX_CONTENT_LENGTH"]:
        abort(413, description="Maximum file size is 10 MB.")
    blob_hash, relative_path, disk_path, created_file = write_blob_bytes(repo_id, content_bytes)
    try:
        with db_cursor(write=True) as cursor:
            require_repo_role(cursor, repo_id, {"Maintainer", "Contributor"})
            branch = branch_head(cursor, repo_id, branch_id, for_update=True)
            if not branch or not branch["head_tree_id"]:
                abort(404, description="Branch has no committed tree yet.")
            _, old_entry = walk_file_path(cursor, branch["head_tree_id"], path_parts)
            if old_entry["subtree_id"] or not old_entry["blob_id"]:
                abort(400, description="Select a file, not a folder.")
            mime_type = old_entry["mime_type"] or "text/plain"
            if not text_mime_type(mime_type):
                abort(415, description="Only text files can be edited in the browser.")
            blob_id = get_or_create_blob(cursor, repo_id, blob_hash, relative_path, mime_type)
            new_tree = mutate_tree_path(cursor, repo_id, branch["head_tree_id"], path_parts,
                                        "edit", blob_id, blob_hash)
            commit_id = commit_tree(cursor, branch_id, current_user_id(), new_tree, message)
            queue_graph_sync(cursor, repo_id)
    except Exception:
        if created_file:
            disk_path.unlink(missing_ok=True)
        raise
    sync_graph_after_write()
    return jsonify({"commit_id": commit_id, "tree_id": new_tree, "path": "/".join(path_parts)})


@app.delete("/api/repositories/<repo_id>/files")
def delete_file(repo_id):
    repo_id = positive_id(repo_id)
    body = body_json()
    branch_id = positive_id(body.get("branch_id"))
    path_parts = parse_path(body.get("path", ""))
    message = clean_text(body.get("message"), 5000) or f"Delete {path_parts[-1]}"
    if not repo_id or not branch_id:
        abort(400, description="Choose a repository and branch.")
    with db_cursor(write=True) as cursor:
        require_repo_role(cursor, repo_id, {"Maintainer", "Contributor"})
        branch = branch_head(cursor, repo_id, branch_id, for_update=True)
        if not branch or not branch["head_tree_id"]:
            abort(404, description="Branch has no committed tree yet.")
        _, entry = walk_file_path(cursor, branch["head_tree_id"], path_parts)
        if entry["subtree_id"] or not entry["blob_id"]:
            abort(400, description="Select a file, not a folder.")
        new_tree = mutate_tree_path(cursor, repo_id, branch["head_tree_id"], path_parts, "delete")
        commit_id = commit_tree(cursor, branch_id, current_user_id(), new_tree, message)
        queue_graph_sync(cursor, repo_id)
    sync_graph_after_write()
    return jsonify({"deleted": True, "commit_id": commit_id, "tree_id": new_tree})


@app.post("/api/repositories/<repo_id>/files")
def upload_file(repo_id):
    repo_id = positive_id(repo_id)
    uploaded = request.files.get("file")
    file_name = safe_name(uploaded.filename if uploaded else None)
    branch_id = positive_id(request.form.get("branch_id"))
    parent_path = parse_path(request.form.get("parent_path", ""), allow_empty=True)
    message = clean_text(request.form.get("message"), 5000)
    if not repo_id or not uploaded or not file_name or not branch_id:
        abort(400, description="Choose a file and branch.")
    if len("/".join(parent_path + [file_name])) > 1024:
        abort(400, description="File path is too long.")
    content = uploaded.stream.read(app.config["MAX_CONTENT_LENGTH"] + 1)
    if len(content) > app.config["MAX_CONTENT_LENGTH"]:
        abort(413, description="Maximum upload size is 10 MB.")
    mime_type = (mimetypes.guess_type(file_name)[0] or uploaded.mimetype or "application/octet-stream")[:50]
    blob_hash, relative_path, disk_path, created_file = write_blob_bytes(repo_id, content)

    try:
        with db_cursor(write=True) as cursor:
            require_repo_role(cursor, repo_id, {"Maintainer", "Contributor"})
            branch = branch_head(cursor, repo_id, branch_id, for_update=True)
            if not branch:
                abort(404, description="Branch not found.")
            blob_id = get_or_create_blob(cursor, repo_id, blob_hash, relative_path, mime_type)
            tree_id = mutate_tree_path(cursor, repo_id, branch["head_tree_id"],
                                       parent_path + [file_name], "add", blob_id, blob_hash)
            commit_id = commit_tree(cursor, branch_id, current_user_id(), tree_id,
                                    message or f"Add {file_name}")
            queue_graph_sync(cursor, repo_id)
    except Exception:
        if created_file:
            disk_path.unlink(missing_ok=True)
        raise

    sync_graph_after_write()
    return jsonify({"commit_id": commit_id, "tree_id": tree_id, "blob_id": blob_id,
                    "path": "/".join(parent_path + [file_name])}), 201


@app.get("/api/blobs/<blob_id>/download")
def download_blob(blob_id):
    blob_id = positive_id(blob_id)
    if not blob_id:
        abort(400, description="Blob ID must be a positive integer.")
    with db_cursor() as cursor:
        cursor.execute("""
            SELECT b.storage_path, b.mime_type, o.repo_id
            FROM `BLOBS` b JOIN `OBJECT` o ON o.object_id = b.blob_id
            WHERE b.blob_id = %s
        """, (blob_id,))
        blob = cursor.fetchone()
        if blob:
            require_repo_read(cursor, blob["repo_id"])
    if not blob or not blob["storage_path"]:
        abort(404, description="File content was not found.")
    path = managed_blob_path(blob["storage_path"])
    if not path:
        abort(404, description="File content was not found.")
    name = safe_name(request.args.get("name", "")) or f"blob-{blob_id}"
    return send_file(path, mimetype=blob["mime_type"], as_attachment=True, download_name=name)


@app.errorhandler(HTTPException)
def handle_http_error(error):
    return jsonify({"error": error.description}), error.code


@app.errorhandler(mysql.connector.Error)
def handle_database_error(error):
    if error.errno == 1062:
        return jsonify({"error": "That username or email is already registered, or that name is already in use."}), 409
    if error.errno == 1045:
        return jsonify({"error": "MySQL rejected the app login. Check DB_USER and DB_PASSWORD in .env."}), 503
    if error.errno in {1044, 1142}:
        return jsonify({"error": "The MySQL app account lacks permission for this operation. Reapply the database GRANT statements."}), 503
    if error.errno == 1049:
        return jsonify({"error": "The configured MySQL database does not exist. Check DB_NAME in .env."}), 503
    if error.errno in {2002, 2003, 2005}:
        return jsonify({"error": "Cannot connect to MySQL. Check that MySQL is running and DB_HOST/DB_PORT are correct."}), 503
    if error.errno in {1054, 1146}:
        return jsonify({"error": "The database schema is missing a table or column required by this app. Import the matching SQL schema."}), 503
    if error.errno == 1406:
        return jsonify({"error": "A database column is too short for this value. Check that USER.password_hash is VARCHAR(255)."}), 400
    if error.errno in {1451, 1452}:
        return jsonify({"error": "A selected user, repository, branch, or tree does not exist."}), 400
    if error.errno == 1644:
        return jsonify({"error": error.msg or "The database rejected the operation."}), 400
    app.logger.exception("Database request failed")
    return jsonify({"error": "The server could not complete the request."}), 500


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.exception("Unexpected request failure")
    return jsonify({"error": "The server could not complete the request."}), 500


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "3000")),
            debug=os.getenv("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"})
