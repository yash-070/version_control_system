# Forge Version Control System

Forge is a small browser based VCS built with HTML/CSS/JavaScript, a Python Flask API, MySQL, and Neo4j. It covers the everyday basics without requiring GitHub workflows.

## Database responsibility split

The planned split is **70% MySQL and 30% Neo4j by system responsibility**. This describes which database owns which features; it is not a measurement of bytes stored or lines of code.

| Database | Responsibility |
| --- | --- |
| MySQL — about 70% | Accounts, repository settings, members and roles, pull request status and approval, file/object metadata, commits, and transaction-safe writes. MySQL is the source of truth. |
| Neo4j — about 30% | A graph projection of repositories, branches, commit ancestry, snapshots, folders, and files. Branch file browsing reads this graph when it is available. |

Uploaded file contents remain under `file-storage/`; Neo4j stores graph links and metadata, not a second copy of file bytes. MySQL's `VCS_GRAPH_OUTBOX` records graph updates in the same transaction as each source change. A worker applies the projection after commit and leaves failed events available for retry. The API falls back to MySQL while Neo4j is unavailable.

## 1. Set up MySQL

Use MySQL 8. The complete schema and sample data are in `database/version_control_system_FINAL.sql`. It begins with `DROP DATABASE IF EXISTS version_control_system`, so importing it deletes the existing database with that name. Back up your data before importing it.

For an existing database based on the earlier schema, run each missing migration once in MySQL Workbench, in this order:

1. `database/merge_support_migration.sql`
2. `database/branch_management_migration.sql`
3. `database/merge_approval_migration.sql`
4. `database/neo4j_projection_migration.sql`

The last migration gives any older empty repository a `main` branch, creates the graph outbox, queues existing repositories for projection, and installs a trigger that validates pull request branches, commits, and author write access.

Create or update the app account as a MySQL administrator:

```sql
CREATE USER IF NOT EXISTS 'vcs_app'@'localhost' IDENTIFIED BY 'choose-a-unique-password';
ALTER USER 'vcs_app'@'localhost' IDENTIFIED BY 'choose-a-unique-password';
GRANT SELECT, INSERT, UPDATE, DELETE ON version_control_system.* TO 'vcs_app'@'localhost';
FLUSH PRIVILEGES;
```

Use your chosen password in `.env` as `DB_PASSWORD`. If it contains `#` or spaces, put it in double quotes. The app account does not need permission to create or drop database objects.

## 2. Set up Neo4j

Use Neo4j 5 or later. Start a local database in Neo4j Desktop or install a Neo4j server. The app creates the required uniqueness constraints when it connects. Enable Bolt and note the Bolt URI, username, and password. Local defaults are usually `bolt://127.0.0.1:7687` and database `neo4j`.

Neo4j is optional for starting the site. Without it, the application still works using MySQL; configure Neo4j to enable graph projection and graph backed branch browsing.

## 3. Install and run

Install Python 3.10 or later. From this project folder in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with both database connections and a long random Flask `SECRET_KEY`:

```dotenv
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=version_control_system
DB_USER=vcs_app
DB_PASSWORD="your-mysql-password"

NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD="your-neo4j-password"
NEO4J_DATABASE=neo4j
```

Restart the server after changing `.env`, then start Flask:

```powershell
.\.venv\Scripts\python.exe backend\app.py
```

Open <http://localhost:3000>, create an account, or sign in with a seeded account. Older sample users have SHA-256 password hashes; a successful login upgrades the used hash.

To retry queued graph updates manually after Neo4j comes back, run:

```powershell
.\.venv\Scripts\python.exe backend\graph_sync.py
```

The app also retries graph updates during writes and health checks. The connection indicator reports whether MySQL and Neo4j are connected and whether MySQL fallback is active.

## Using the VCS

### Branches, files, and commits

New repositories start with a `main` branch. The **Working branch** selector and clickable branch names switch the active branch and show that branch's own file tree. The selected branch is remembered in the browser for each repository. Create a new branch from the current working branch, then add, edit, or delete files while it is selected. Each file change creates a commit on that branch. The **Record commit** action also records the selected branch's current snapshot.

Sample SQL file records may have metadata but no actual bytes. Such entries show as metadata only; files uploaded through Forge can be downloaded or edited. Uploaded content lives in `file-storage/`, so back it up along with MySQL.

### Pull requests

Every merge is submitted as a pull request, including one created by the repository owner. The repository owner approves and merges it or rejects it in **Pull requests**. The request records both branch head commits; if a branch changes before approval, the request becomes stale and must be submitted again. The merge uses shared commit ancestry to combine independent file additions, edits, and deletions in nested folders. Conflicting paths use the requester's selected source or target preference. The resulting commit records both parent commits. This is a compact three-way merge for the project; it does not implement Git's full conflict-resolution algorithm.

### Roles

Maintainers manage members and roles and can manage repository settings. Contributors can create branches and make commits, pushes, file changes, and pull requests. Readers can browse. The owner remains a Maintainer and is the only person who can approve/reject pull requests or delete the repository.

### Roll back a branch

The repository owner can select the working branch and use **Rollback branch** to restore it to an earlier commit in that branch's history. Forge creates a new commit whose file snapshot matches the selected commit and whose parent is the branch's current head. This preserves the full commit history; it does not erase later commits. To reverse a rollback, roll forward to the commit that was the head before the rollback.

Rollback is an owner-only operation. The new commit, parent link, and graph outbox event are written in one MySQL transaction, so a database error leaves the branch unchanged. Files are immutable snapshots, so rollback reuses the selected commit's tree and does not rewrite or delete stored file contents. No schema migration is needed.

## SQL transactions, triggers, and functions

Flask wraps MySQL writes in transactions using `READ COMMITTED`. Source records and their Neo4j outbox event commit or roll back together. The graph refresh happens after the MySQL commit because MySQL and Neo4j do not share one atomic transaction. If graph projection fails, MySQL retains the event to retry later.

MySQL triggers continue enforcing repository membership, branch creators, valid objects, commit authors, and push users. A pull request trigger checks that its branches and head commits match the repository and that its requester has write access. The included SQL functions `fn_repo_commit_count` and `fn_branch_commit_count` remain available; existing functions are used where SQL reporting needs them. Branch/file graph traversal uses Cypher in Neo4j.

## Main API routes

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/auth/register` | Create an account and sign in |
| `POST` | `/api/auth/login` | Sign in |
| `GET` | `/api/health` | MySQL/Neo4j connection state and queued graph updates |
| `GET` | `/api/repositories/:repoId` | Repository, branches, people, commits, and pull requests |
| `POST` / `DELETE` | `/api/repositories` and `/api/repositories/:repoId` | Create or delete a repository |
| `POST` | `/api/repositories/:repoId/branches` | Create a branch from the selected base |
| `POST` | `/api/branches/:branchId/commits` | Record a commit on that branch |
| `GET` | `/api/branches/:branchId/history` | List earlier rollback targets (repository owner only) |
| `POST` | `/api/branches/:branchId/rollback` | Restore a branch snapshot as a new owner-authored commit |
| `POST` | `/api/repositories/:repoId/merges` | Submit a pull request |
| `POST` | `/api/merge-requests/:id/approve` or `/reject` | Owner review and merge/rejection |
| `GET` | `/api/repositories/:repoId/files/tree` | Browse the selected branch's files |
| `GET` / `PUT` | `/api/repositories/:repoId/files/content` | Read or edit a text file |
| `POST` / `DELETE` | `/api/repositories/:repoId/files` | Upload or delete a file |

The browser talks only to Flask. Flask talks to MySQL and Neo4j; the frontend never connects directly to either database.
