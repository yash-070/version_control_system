-- ================================================================
-- DBMS MINI PROJECT - STAGE 2 / COMPLETE SQL
-- Project: Version Control Metadata & Object Tracking System
-- Students: Pranav B N (PES2UG24CS649), Yashas K C (PES2UG24CS669)
--
-- Target DBMS: MySQL 8.0+
--
-- This file is organised as:
-- 1. Database and table creation (Stage-1 schema, included for portability)
-- 2. INSERT / UPDATE / DELETE operations
-- 3. SELECT, JOIN, GROUP BY, HAVING, ORDER BY, aggregates, subqueries
-- 4. Complex query
-- 5. VIEW
-- 6. Transactions: COMMIT and ROLLBACK
-- 7. Functions
-- 8. Stored procedures
-- 9. Triggers and trigger demonstrations
--    Version-control integrity is enforced by repository, branch, object,
--    tree-entry, commit and push rules.
--
-- The script is intended to be executed as one complete file.
-- ================================================================

DROP DATABASE IF EXISTS version_control_system;
CREATE DATABASE version_control_system
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
USE version_control_system;

SET NAMES utf8mb4;

-- ================================================================
-- 1. DATABASE SCHEMA / DDL
-- ================================================================

CREATE TABLE `USER` (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    email VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE `REPOSITORY` (
    repo_id INT AUTO_INCREMENT PRIMARY KEY,
    repo_name VARCHAR(100) NOT NULL,
    description TEXT,
    visibility VARCHAR(20) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    owner_user_id INT NOT NULL,
    default_branch_id INT NULL,
    CONSTRAINT chk_repository_visibility
        CHECK (visibility IN ('Public', 'Private', 'Internal')),
    CONSTRAINT fk_repository_owner
        FOREIGN KEY (owner_user_id) REFERENCES `USER`(user_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_repository_owner_name
        UNIQUE (owner_user_id, repo_name)
);

CREATE TABLE `REPOSITORY_MEMBER` (
    membership_id INT AUTO_INCREMENT PRIMARY KEY,
    role VARCHAR(20) NOT NULL,
    joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    user_id INT NOT NULL,
    repo_id INT NOT NULL,
    CONSTRAINT chk_repository_member_role
        CHECK (role IN ('Maintainer', 'Contributor', 'Reader')),
    CONSTRAINT fk_repository_member_user
        FOREIGN KEY (user_id) REFERENCES `USER`(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_repository_member_repo
        FOREIGN KEY (repo_id) REFERENCES `REPOSITORY`(repo_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_repository_membership
        UNIQUE (user_id, repo_id)
);

CREATE TABLE `BRANCH` (
    branch_id INT AUTO_INCREMENT PRIMARY KEY,
    branch_name VARCHAR(100) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    repo_id INT NOT NULL,
    created_by_user_id INT NOT NULL,
    CONSTRAINT fk_branch_repo
        FOREIGN KEY (repo_id) REFERENCES `REPOSITORY`(repo_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_branch_creator
        FOREIGN KEY (created_by_user_id) REFERENCES `USER`(user_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_branch_name_per_repository
        UNIQUE (repo_id, branch_name)
);

CREATE TABLE `OBJECT` (
    object_id INT AUTO_INCREMENT PRIMARY KEY,
    object_hash VARCHAR(64) NOT NULL UNIQUE,
    object_type VARCHAR(10) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    repo_id INT NOT NULL,
    CONSTRAINT chk_object_type
        CHECK (object_type IN ('blob', 'tree', 'commit')),
    CONSTRAINT fk_object_repo
        FOREIGN KEY (repo_id) REFERENCES `REPOSITORY`(repo_id)
        ON DELETE CASCADE
);

CREATE TABLE `TREE` (
    tree_id INT PRIMARY KEY,
    CONSTRAINT fk_tree_object
        FOREIGN KEY (tree_id) REFERENCES `OBJECT`(object_id)
        ON DELETE CASCADE
);

CREATE TABLE `BLOBS` (
    blob_id INT PRIMARY KEY,
    storage_path VARCHAR(255) NOT NULL,
    mime_type VARCHAR(50),
    CONSTRAINT fk_blob_object
        FOREIGN KEY (blob_id) REFERENCES `OBJECT`(object_id)
        ON DELETE CASCADE
);

CREATE TABLE `TREE_ENTRY` (
    entry_id INT AUTO_INCREMENT PRIMARY KEY,
    file_mode VARCHAR(10) NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    tree_id INT NOT NULL,
    subtree_id INT,
    blob_id INT,
    CONSTRAINT fk_tree_entry_tree
        FOREIGN KEY (tree_id) REFERENCES `TREE`(tree_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tree_entry_subtree
        FOREIGN KEY (subtree_id) REFERENCES `TREE`(tree_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tree_entry_blob
        FOREIGN KEY (blob_id) REFERENCES `BLOBS`(blob_id)
        ON DELETE CASCADE,
    CONSTRAINT chk_tree_entry_target
        CHECK (
            (subtree_id IS NOT NULL AND blob_id IS NULL)
            OR
            (subtree_id IS NULL AND blob_id IS NOT NULL)
        ),
    CONSTRAINT uq_tree_entry_name
        UNIQUE (tree_id, file_name)
);

CREATE TABLE `COMMIT` (
    commit_id INT AUTO_INCREMENT PRIMARY KEY,
    message TEXT NOT NULL,
    committed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    branch_id INT NOT NULL,
    author_user_id INT NOT NULL,
    tree_id INT NOT NULL,
    CONSTRAINT fk_commit_branch
        FOREIGN KEY (branch_id) REFERENCES `BRANCH`(branch_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_commit_author
        FOREIGN KEY (author_user_id) REFERENCES `USER`(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_commit_tree
        FOREIGN KEY (tree_id) REFERENCES `TREE`(tree_id)
        ON DELETE CASCADE
);

-- Optional parent links make two-parent merge commits representable.
CREATE TABLE `COMMIT_PARENT` (
    commit_id INT NOT NULL,
    parent_commit_id INT NOT NULL,
    parent_order TINYINT UNSIGNED NOT NULL,
    PRIMARY KEY (commit_id, parent_order),
    CONSTRAINT uq_commit_parent UNIQUE (commit_id, parent_commit_id),
    CONSTRAINT chk_commit_parent_order CHECK (parent_order IN (1, 2)),
    CONSTRAINT chk_commit_not_own_parent CHECK (commit_id <> parent_commit_id),
    CONSTRAINT fk_commit_parent_child
        FOREIGN KEY (commit_id) REFERENCES `COMMIT`(commit_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_commit_parent_parent
        FOREIGN KEY (parent_commit_id) REFERENCES `COMMIT`(commit_id)
        ON DELETE CASCADE
);

-- Every branch merge is stored until the repository owner reviews it.
CREATE TABLE `MERGE_REQUEST` (
    merge_request_id INT AUTO_INCREMENT PRIMARY KEY,
    repo_id INT NOT NULL,
    source_branch_id INT NOT NULL,
    target_branch_id INT NOT NULL,
    source_head_commit_id INT NOT NULL,
    target_head_commit_id INT NOT NULL,
    requested_by_user_id INT NOT NULL,
    conflict_strategy VARCHAR(10) NOT NULL DEFAULT 'target',
    status VARCHAR(10) NOT NULL DEFAULT 'Pending',
    requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    reviewed_by_user_id INT NULL,
    reviewed_at DATETIME NULL,
    merge_commit_id INT NULL,
    CONSTRAINT chk_merge_request_status
        CHECK (status IN ('Pending', 'Approved', 'Rejected', 'Stale')),
    CONSTRAINT chk_merge_request_strategy
        CHECK (conflict_strategy IN ('source', 'target')),
    CONSTRAINT chk_merge_request_branches
        CHECK (source_branch_id <> target_branch_id),
    CONSTRAINT fk_merge_request_repo
        FOREIGN KEY (repo_id) REFERENCES `REPOSITORY`(repo_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_merge_request_source_branch
        FOREIGN KEY (source_branch_id) REFERENCES `BRANCH`(branch_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_merge_request_target_branch
        FOREIGN KEY (target_branch_id) REFERENCES `BRANCH`(branch_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_merge_request_source_commit
        FOREIGN KEY (source_head_commit_id) REFERENCES `COMMIT`(commit_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_merge_request_target_commit
        FOREIGN KEY (target_head_commit_id) REFERENCES `COMMIT`(commit_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_merge_request_author
        FOREIGN KEY (requested_by_user_id) REFERENCES `USER`(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_merge_request_reviewer
        FOREIGN KEY (reviewed_by_user_id) REFERENCES `USER`(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_merge_request_commit
        FOREIGN KEY (merge_commit_id) REFERENCES `COMMIT`(commit_id)
        ON DELETE SET NULL,
    INDEX ix_merge_request_repo_status (repo_id, status, requested_at)
);

-- Durable queue for the Neo4j graph projection. MySQL remains the source of truth.
CREATE TABLE `VCS_GRAPH_OUTBOX` (
    event_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at DATETIME NULL,
    processed_at DATETIME NULL,
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    last_error VARCHAR(1000) NULL,
    INDEX ix_vcs_graph_outbox_pending (processed_at, claimed_at, event_id)
);

CREATE TABLE `PUSH_EVENT` (
    push_id INT AUTO_INCREMENT PRIMARY KEY,
    pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    user_id INT NOT NULL,
    branch_id INT NOT NULL,
    CONSTRAINT fk_push_user
        FOREIGN KEY (user_id) REFERENCES `USER`(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_push_branch
        FOREIGN KEY (branch_id) REFERENCES `BRANCH`(branch_id)
        ON DELETE CASCADE
);

-- ================================================================
-- 2. TRIGGERS USED BY THE SYSTEM
--    These enforce version-control rules at the database level.
-- ================================================================

DELIMITER $$

-- Automatically make a newly-created repository owner its Maintainer.
CREATE TRIGGER trg_repository_owner_membership
AFTER INSERT ON `REPOSITORY`
FOR EACH ROW
BEGIN
    INSERT INTO `REPOSITORY_MEMBER` (role, user_id, repo_id)
    VALUES ('Maintainer', NEW.owner_user_id, NEW.repo_id);
END$$

-- Only repository Maintainers/Contributors can create branches.
CREATE TRIGGER trg_branch_validate_creator
BEFORE INSERT ON `BRANCH`
FOR EACH ROW
BEGIN
    DECLARE v_member_count INT DEFAULT 0;

    SELECT COUNT(*)
      INTO v_member_count
      FROM `REPOSITORY_MEMBER`
     WHERE repo_id = NEW.repo_id
       AND user_id = NEW.created_by_user_id
       AND role IN ('Maintainer', 'Contributor');

    IF v_member_count = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Branch creator must be a repository Maintainer or Contributor';
    END IF;
END$$

-- Assign the first branch as the repository default unless one is already set.
CREATE TRIGGER trg_branch_set_default
AFTER INSERT ON `BRANCH`
FOR EACH ROW
BEGIN
    UPDATE `REPOSITORY`
       SET default_branch_id = NEW.branch_id
     WHERE repo_id = NEW.repo_id
       AND default_branch_id IS NULL;
END$$

-- TREE rows may only represent OBJECT rows whose type is tree.
CREATE TRIGGER trg_tree_validate_object
BEFORE INSERT ON `TREE`
FOR EACH ROW
BEGIN
    DECLARE v_tree_objects INT DEFAULT 0;

    SELECT COUNT(*)
      INTO v_tree_objects
      FROM `OBJECT`
     WHERE object_id = NEW.tree_id
       AND object_type = 'tree';

    IF v_tree_objects = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'TREE row must reference an OBJECT of type tree';
    END IF;
END$$

-- BLOBS rows may only represent OBJECT rows whose type is blob.
CREATE TRIGGER trg_blob_validate_object
BEFORE INSERT ON `BLOBS`
FOR EACH ROW
BEGIN
    DECLARE v_blob_objects INT DEFAULT 0;

    SELECT COUNT(*)
      INTO v_blob_objects
      FROM `OBJECT`
     WHERE object_id = NEW.blob_id
       AND object_type = 'blob';

    IF v_blob_objects = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'BLOBS row must reference an OBJECT of type blob';
    END IF;
END$$

-- A tree entry's parent and target must belong to the same repository.
-- A subtree cannot point to itself.
CREATE TRIGGER trg_tree_entry_validate
BEFORE INSERT ON `TREE_ENTRY`
FOR EACH ROW
BEGIN
    DECLARE v_parent_repo INT DEFAULT NULL;
    DECLARE v_target_repo INT DEFAULT NULL;

    SELECT o.repo_id
      INTO v_parent_repo
      FROM `TREE` t
      JOIN `OBJECT` o ON o.object_id = t.tree_id
     WHERE t.tree_id = NEW.tree_id;

    IF v_parent_repo IS NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Parent tree does not exist';
    END IF;

    IF NEW.subtree_id IS NOT NULL THEN
        IF NEW.subtree_id = NEW.tree_id THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'A tree cannot contain itself';
        END IF;

        SELECT o.repo_id
          INTO v_target_repo
          FROM `TREE` t
          JOIN `OBJECT` o ON o.object_id = t.tree_id
         WHERE t.tree_id = NEW.subtree_id;

    ELSE
        SELECT o.repo_id
          INTO v_target_repo
          FROM `BLOBS` b
          JOIN `OBJECT` o ON o.object_id = b.blob_id
         WHERE b.blob_id = NEW.blob_id;
    END IF;

    IF v_target_repo IS NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Tree entry target does not exist';
    END IF;

    IF v_parent_repo <> v_target_repo THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Tree entry target must belong to the same repository as its parent tree';
    END IF;
END$$

-- A commit author must be a Maintainer/Contributor of the branch repository,
-- and the referenced tree must belong to the same repository as the branch.
CREATE TRIGGER trg_commit_validate
BEFORE INSERT ON `COMMIT`
FOR EACH ROW
BEGIN
    DECLARE v_author_ok INT DEFAULT 0;
    DECLARE v_tree_ok INT DEFAULT 0;

    SELECT COUNT(*)
      INTO v_author_ok
      FROM `BRANCH` b
      JOIN `REPOSITORY_MEMBER` rm
        ON rm.repo_id = b.repo_id
       AND rm.user_id = NEW.author_user_id
       AND rm.role IN ('Maintainer', 'Contributor')
     WHERE b.branch_id = NEW.branch_id;

    IF v_author_ok = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Commit author must be a repository Maintainer or Contributor';
    END IF;

    SELECT COUNT(*)
      INTO v_tree_ok
      FROM `TREE` t
      JOIN `OBJECT` o ON o.object_id = t.tree_id
      JOIN `BRANCH` b ON b.repo_id = o.repo_id
     WHERE t.tree_id = NEW.tree_id
       AND o.object_type = 'tree'
       AND b.branch_id = NEW.branch_id;

    IF v_tree_ok = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Commit tree must be a tree object in the same repository as the branch';
    END IF;
END$$

-- Pushes are allowed only for Maintainers/Contributors of the branch repository.
CREATE TRIGGER trg_push_validate_user
BEFORE INSERT ON `PUSH_EVENT`
FOR EACH ROW
BEGIN
    DECLARE v_member_count INT DEFAULT 0;

    SELECT COUNT(*)
      INTO v_member_count
      FROM `BRANCH` b
      JOIN `REPOSITORY_MEMBER` rm
        ON rm.repo_id = b.repo_id
       AND rm.user_id = NEW.user_id
       AND rm.role IN ('Maintainer', 'Contributor')
     WHERE b.branch_id = NEW.branch_id;

    IF v_member_count = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Push user must be a repository Maintainer or Contributor';
    END IF;
END$$

-- A pull request must use branches and head commits from one repository,
-- and its requester must have write access (or own the repository).
CREATE TRIGGER trg_merge_request_validate
BEFORE INSERT ON `MERGE_REQUEST`
FOR EACH ROW
BEGIN
    DECLARE v_branch_count INT DEFAULT 0;
    DECLARE v_source_commit_count INT DEFAULT 0;
    DECLARE v_target_commit_count INT DEFAULT 0;
    DECLARE v_owner_id INT DEFAULT NULL;
    DECLARE v_writer_count INT DEFAULT 0;

    SELECT COUNT(*) INTO v_branch_count
      FROM `BRANCH`
     WHERE repo_id = NEW.repo_id
       AND branch_id IN (NEW.source_branch_id, NEW.target_branch_id);
    IF v_branch_count <> 2 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Pull request branches must belong to its repository';
    END IF;

    SELECT COUNT(*) INTO v_source_commit_count
      FROM `COMMIT`
     WHERE commit_id = NEW.source_head_commit_id
       AND branch_id = NEW.source_branch_id;
    SELECT COUNT(*) INTO v_target_commit_count
      FROM `COMMIT`
     WHERE commit_id = NEW.target_head_commit_id
       AND branch_id = NEW.target_branch_id;
    IF v_source_commit_count = 0 OR v_target_commit_count = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Pull request head commits must belong to their branches';
    END IF;

    SELECT owner_user_id INTO v_owner_id
      FROM `REPOSITORY` WHERE repo_id = NEW.repo_id;
    IF NEW.requested_by_user_id <> v_owner_id THEN
        SELECT COUNT(*) INTO v_writer_count
          FROM `REPOSITORY_MEMBER`
         WHERE repo_id = NEW.repo_id
           AND user_id = NEW.requested_by_user_id
           AND role IN ('Maintainer', 'Contributor');
        IF v_writer_count = 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Pull request author must have write access';
        END IF;
    END IF;
END$$

DELIMITER ;

-- ================================================================
-- 3. INSERT OPERATIONS / SAMPLE DATA
-- ================================================================

-- 3.1 USERS
INSERT INTO `USER`
    (user_id, username, email, password_hash, created_at)
VALUES
    (1, 'pranav', 'pranav@example.com', SHA2('Pranav@123', 256), '2026-09-01 09:00:00'),
    (2, 'yashas', 'yashas@example.com', SHA2('Yashas@123', 256), '2026-09-01 09:05:00'),
    (3, 'rahul', 'rahul@example.com', SHA2('Rahul@123', 256), '2026-09-01 09:10:00'),
    (4, 'ananya', 'ananya@example.com', SHA2('Ananya@123', 256), '2026-09-01 09:15:00'),
    (5, 'karthik', 'karthik@example.com', SHA2('Karthik@123', 256), '2026-09-01 09:20:00'),
    (6, 'sneha', 'sneha@example.com', SHA2('Sneha@123', 256), '2026-09-01 09:25:00');

-- 3.2 REPOSITORIES
-- trg_repository_owner_membership automatically inserts each owner as Maintainer.
INSERT INTO `REPOSITORY`
    (repo_id, repo_name, description, visibility, created_at, owner_user_id)
VALUES
    (1, 'VersionControlSystem', 'Mini GitHub-style metadata and object tracking project.', 'Public', '2026-09-02 10:00:00', 1),
    (2, 'NetworkToolkit', 'Utilities for network diagnostics and configuration.', 'Internal', '2026-09-03 10:00:00', 2),
    (3, 'WebPortal', 'Small web application prototype for the project team.', 'Private', '2026-09-04 10:00:00', 3),
    (4, 'DBMSMiniProject', 'Database management systems project artifacts.', 'Public', '2026-09-05 10:00:00', 4);

-- 3.3 ADDITIONAL REPOSITORY MEMBERS
INSERT INTO `REPOSITORY_MEMBER` (role, joined_at, user_id, repo_id)
VALUES
    ('Contributor', '2026-09-02 10:30:00', 2, 1),
    ('Reader',      '2026-09-02 10:35:00', 3, 1),
    ('Contributor', '2026-09-02 10:40:00', 4, 1),
    ('Contributor', '2026-09-03 10:30:00', 1, 2),
    ('Reader',      '2026-09-03 10:35:00', 5, 2),
    ('Contributor', '2026-09-04 10:30:00', 2, 3),
    ('Reader',      '2026-09-04 10:35:00', 6, 3),
    ('Contributor', '2026-09-05 10:30:00', 1, 4),
    ('Contributor', '2026-09-05 10:35:00', 5, 4);

-- 3.4 BRANCHES
INSERT INTO `BRANCH`
    (branch_id, branch_name, created_at, repo_id, created_by_user_id)
VALUES
    (1, 'main',         '2026-09-02 11:00:00', 1, 1),
    (2, 'develop',      '2026-09-02 11:10:00', 1, 1),
    (3, 'feature-auth', '2026-09-02 11:20:00', 1, 2),
    (4, 'main',         '2026-09-03 11:00:00', 2, 2),
    (5, 'feature-parser','2026-09-03 11:15:00', 2, 1),
    (6, 'main',         '2026-09-04 11:00:00', 3, 3),
    (7, 'main',         '2026-09-05 11:00:00', 4, 4),
    (8, 'analytics',    '2026-09-05 11:15:00', 4, 5),
    (9, 'release',      '2026-09-05 11:25:00', 4, 4);

-- Prefer a branch named main as each repository's default, otherwise use its first branch.
UPDATE `REPOSITORY` r
SET default_branch_id = (
    SELECT b.branch_id
    FROM `BRANCH` b
    WHERE b.repo_id = r.repo_id
    ORDER BY (b.branch_name = 'main') DESC, b.created_at, b.branch_id
    LIMIT 1
)
WHERE r.repo_id > 0;

-- 3.5 OBJECTS
-- Blob objects: 1-9; Tree objects: 10-14; Commit-object metadata: 15-18.
INSERT INTO `OBJECT`
    (object_id, object_hash, object_type, created_at, repo_id)
VALUES
    (1,  '1111111111111111111111111111111111111111111111111111111111111111', 'blob',   '2026-09-02 11:30:00', 1),
    (2,  '2222222222222222222222222222222222222222222222222222222222222222', 'blob',   '2026-09-02 11:31:00', 1),
    (3,  '3333333333333333333333333333333333333333333333333333333333333333', 'blob',   '2026-09-02 11:32:00', 1),
    (4,  '4444444444444444444444444444444444444444444444444444444444444444', 'blob',   '2026-09-03 11:30:00', 2),
    (5,  '5555555555555555555555555555555555555555555555555555555555555555', 'blob',   '2026-09-03 11:31:00', 2),
    (6,  '6666666666666666666666666666666666666666666666666666666666666666', 'blob',   '2026-09-04 11:30:00', 3),
    (7,  '7777777777777777777777777777777777777777777777777777777777777777', 'blob',   '2026-09-04 11:31:00', 3),
    (8,  '8888888888888888888888888888888888888888888888888888888888888888', 'blob',   '2026-09-05 11:30:00', 4),
    (9,  '9999999999999999999999999999999999999999999999999999999999999999', 'blob',   '2026-09-05 11:31:00', 4),
    (10, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'tree',   '2026-09-02 11:35:00', 1),
    (11, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tree',   '2026-09-02 11:36:00', 1),
    (12, 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'tree',   '2026-09-03 11:35:00', 2),
    (13, 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', 'tree',   '2026-09-04 11:35:00', 3),
    (14, 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 'tree',   '2026-09-05 11:35:00', 4),
    (15, 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', 'commit', '2026-09-02 11:40:00', 1),
    (16, '1010101010101010101010101010101010101010101010101010101010101010', 'commit', '2026-09-03 11:40:00', 2),
    (17, '1212121212121212121212121212121212121212121212121212121212121212', 'commit', '2026-09-04 11:40:00', 3),
    (18, '1313131313131313131313131313131313131313131313131313131313131313', 'commit', '2026-09-05 11:40:00', 4);

-- 3.6 TREE AND BLOB SPECIALISATIONS
INSERT INTO `BLOBS` (blob_id, storage_path, mime_type)
VALUES
    (1, '/objects/11/README.md',       'text/markdown'),
    (2, '/objects/22/main.c',           'text/x-c'),
    (3, '/objects/33/auth.c',           'text/x-c'),
    (4, '/objects/44/README.md',        'text/markdown'),
    (5, '/objects/55/router.py',        'text/x-python'),
    (6, '/objects/66/index.html',        'text/html'),
    (7, '/objects/77/app.js',            'application/javascript'),
    (8, '/objects/88/schema.sql',        'application/sql'),
    (9, '/objects/99/report.pdf',        'application/pdf');

INSERT INTO `TREE` (tree_id)
VALUES
    (10),
    (11),
    (12),
    (13),
    (14);

-- 3.7 TREE ENTRIES
INSERT INTO `TREE_ENTRY`
    (entry_id, file_mode, file_name, tree_id, subtree_id, blob_id)
VALUES
    (1,  '100644', 'README.md',  10, NULL, 1),
    (2,  '040000', 'src',        10, 11,   NULL),
    (3,  '100644', 'main.c',     11, NULL, 2),
    (4,  '100644', 'auth.c',     11, NULL, 3),
    (5,  '100644', 'README.md',  12, NULL, 4),
    (6,  '100644', 'router.py',  12, NULL, 5),
    (7,  '100644', 'index.html', 13, NULL, 6),
    (8,  '100644', 'app.js',     13, NULL, 7),
    (9,  '100644', 'schema.sql', 14, NULL, 8),
    (10, '100644', 'report.pdf', 14, NULL, 9);

-- 3.8 COMMITS
INSERT INTO `COMMIT`
    (commit_id, message, committed_at, branch_id, author_user_id, tree_id)
VALUES
    (1,  'Initial repository structure',       '2026-09-02 12:00:00', 1, 1, 10),
    (2,  'Add authentication module',          '2026-09-02 13:00:00', 1, 1, 11),
    (3,  'Refactor authentication flow',        '2026-09-02 14:00:00', 2, 2, 11),
    (4,  'Add login validation',                '2026-09-02 15:00:00', 3, 2, 11),
    (5,  'Initial network toolkit',            '2026-09-03 12:00:00', 4, 2, 12),
    (6,  'Add router configuration parser',     '2026-09-03 13:00:00', 4, 2, 12),
    (7,  'Improve parser error handling',       '2026-09-03 14:00:00', 5, 1, 12),
    (8,  'Initial web portal',                  '2026-09-04 12:00:00', 6, 3, 13),
    (9,  'Add dashboard scripts',               '2026-09-04 13:00:00', 6, 2, 13),
    (10, 'Initial DBMS project',                '2026-09-05 12:00:00', 7, 4, 14),
    (11, 'Add analytics queries',               '2026-09-05 13:00:00', 8, 5, 14),
    (12, 'Prepare final report',                '2026-09-05 14:00:00', 9, 4, 14);

-- Store first-parent links for the sample commits on each branch.
INSERT INTO `COMMIT_PARENT` (commit_id, parent_commit_id, parent_order)
SELECT chain.commit_id, chain.parent_commit_id, 1
FROM (
    SELECT commit_id,
           LAG(commit_id) OVER (PARTITION BY branch_id ORDER BY committed_at, commit_id) AS parent_commit_id
    FROM `COMMIT`
) AS chain
WHERE chain.parent_commit_id IS NOT NULL;

-- 3.9 PUSH EVENTS
INSERT INTO `PUSH_EVENT` (push_id, pushed_at, user_id, branch_id)
VALUES
    (1,  '2026-09-02 12:05:00', 1, 1),
    (2,  '2026-09-02 13:05:00', 1, 1),
    (3,  '2026-09-02 14:05:00', 2, 2),
    (4,  '2026-09-02 15:05:00', 2, 3),
    (5,  '2026-09-03 12:05:00', 2, 4),
    (6,  '2026-09-03 13:05:00', 2, 4),
    (7,  '2026-09-03 14:05:00', 1, 5),
    (8,  '2026-09-04 12:05:00', 3, 6),
    (9,  '2026-09-04 13:05:00', 2, 6),
    (10, '2026-09-05 12:05:00', 4, 7),
    (11, '2026-09-05 13:05:00', 5, 8),
    (12, '2026-09-05 14:05:00', 4, 9);

-- ================================================================
-- 4. UPDATE OPERATIONS
-- ================================================================

-- Update 1: change repository description.
UPDATE `REPOSITORY`
SET description = 'Mini GitHub-style system for repository, branch, commit and object tracking.'
WHERE repo_id = 1;

-- Update 2: rename a feature branch.
UPDATE `BRANCH`
SET branch_name = 'feature-login'
WHERE branch_id = 3;

-- Update 3: change a repository member role.
UPDATE `REPOSITORY_MEMBER`
SET role = 'Reader'
WHERE repo_id = 1
  AND user_id = 4;

-- Update 4: change a user's email.
UPDATE `USER`
SET email = 'karthik.dev@example.com'
WHERE user_id = 5;

-- ================================================================
-- 5. DELETE OPERATION + CASCADE DEMONSTRATION
-- ================================================================

-- Create a temporary repository specifically for demonstrating DELETE.
-- Its owner membership is inserted automatically by the repository trigger.
INSERT INTO `REPOSITORY`
    (repo_name, description, visibility, owner_user_id)
VALUES
    ('DeleteDemoRepository', 'Temporary repository used for DELETE demonstration.', 'Private', 6);

SET @delete_demo_repo_id = LAST_INSERT_ID();

INSERT INTO `BRANCH`
    (branch_name, repo_id, created_by_user_id)
VALUES
    ('demo', @delete_demo_repo_id, 6);

-- Verify that the temporary repository exists.
SELECT repo_id, repo_name, visibility, owner_user_id
FROM `REPOSITORY`
WHERE repo_id = @delete_demo_repo_id;

-- DELETE the parent repository. Related memberships and branches are removed
-- by ON DELETE CASCADE.
DELETE FROM `REPOSITORY`
WHERE repo_id = @delete_demo_repo_id;

-- Confirm that the repository no longer exists.
SELECT COUNT(*) AS remaining_delete_demo_repositories
FROM `REPOSITORY`
WHERE repo_id = @delete_demo_repo_id;

-- ================================================================
-- 6. BASIC SELECT QUERIES
-- ================================================================

-- 6.1 List all public repositories.
SELECT repo_id, repo_name, visibility, owner_user_id
FROM `REPOSITORY`
WHERE visibility = 'Public'
ORDER BY repo_name;

-- 6.2 List branches of the VersionControlSystem repository.
SELECT branch_id, branch_name, created_at
FROM `BRANCH`
WHERE repo_id = 1
ORDER BY branch_name;

-- 6.3 Show the latest commits.
SELECT commit_id, message, committed_at, branch_id, author_user_id, tree_id
FROM `COMMIT`
ORDER BY committed_at DESC;

-- 6.4 Show repository membership details.
SELECT membership_id, repo_id, user_id, role, joined_at
FROM `REPOSITORY_MEMBER`
ORDER BY repo_id, role, user_id;

-- ================================================================
-- 7. JOIN QUERIES
-- ================================================================

-- 7.1 Repository + owner.
SELECT
    r.repo_id,
    r.repo_name,
    u.username AS owner,
    r.visibility
FROM `REPOSITORY` r
JOIN `USER` u ON u.user_id = r.owner_user_id
ORDER BY r.repo_name;

-- 7.2 Commit + author + branch + repository.
SELECT
    c.commit_id,
    c.message,
    u.username AS author,
    b.branch_name,
    r.repo_name,
    c.committed_at
FROM `COMMIT` c
JOIN `USER` u ON u.user_id = c.author_user_id
JOIN `BRANCH` b ON b.branch_id = c.branch_id
JOIN `REPOSITORY` r ON r.repo_id = b.repo_id
ORDER BY c.committed_at DESC;

-- 7.3 Repository members with their usernames and roles.
SELECT
    r.repo_name,
    u.username,
    rm.role,
    rm.joined_at
FROM `REPOSITORY_MEMBER` rm
JOIN `REPOSITORY` r ON r.repo_id = rm.repo_id
JOIN `USER` u ON u.user_id = rm.user_id
ORDER BY r.repo_name, rm.role, u.username;

-- 7.4 Tree entries with either their blob or subtree target.
SELECT
    te.entry_id,
    te.file_name,
    te.file_mode,
    te.tree_id,
    te.subtree_id,
    te.blob_id,
    b.storage_path,
    b.mime_type
FROM `TREE_ENTRY` te
LEFT JOIN `BLOBS` b ON b.blob_id = te.blob_id
ORDER BY te.tree_id, te.entry_id;

-- ================================================================
-- 8. GROUP BY
-- ================================================================

-- Number of repositories owned by each user.
SELECT
    u.user_id,
    u.username,
    COUNT(r.repo_id) AS repository_count
FROM `USER` u
LEFT JOIN `REPOSITORY` r ON r.owner_user_id = u.user_id
GROUP BY u.user_id, u.username
ORDER BY repository_count DESC, u.username;

-- Number of commits in each repository.
SELECT
    r.repo_id,
    r.repo_name,
    COUNT(c.commit_id) AS commit_count
FROM `REPOSITORY` r
LEFT JOIN `BRANCH` b ON b.repo_id = r.repo_id
LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
GROUP BY r.repo_id, r.repo_name
ORDER BY commit_count DESC, r.repo_name;

-- ================================================================
-- 9. GROUP BY + HAVING
-- ================================================================

-- Repositories having at least three commits.
SELECT
    r.repo_id,
    r.repo_name,
    COUNT(c.commit_id) AS commit_count
FROM `REPOSITORY` r
LEFT JOIN `BRANCH` b ON b.repo_id = r.repo_id
LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
GROUP BY r.repo_id, r.repo_name
HAVING COUNT(c.commit_id) >= 3
ORDER BY commit_count DESC;

-- Users who have made at least two push events.
SELECT
    u.user_id,
    u.username,
    COUNT(p.push_id) AS push_count
FROM `USER` u
JOIN `PUSH_EVENT` p ON p.user_id = u.user_id
GROUP BY u.user_id, u.username
HAVING COUNT(p.push_id) >= 2
ORDER BY push_count DESC, u.username;

-- ================================================================
-- 10. AGGREGATE FUNCTIONS
-- ================================================================

-- COUNT, AVG, MAX and MIN over repository commit activity.
SELECT
    SUM(commit_count) AS total_commits,
    AVG(commit_count) AS average_commits_per_repository,
    MAX(commit_count) AS maximum_commits_in_a_repository,
    MIN(commit_count) AS minimum_commits_in_a_repository
FROM (
    SELECT
        r.repo_id,
        COUNT(c.commit_id) AS commit_count
    FROM `REPOSITORY` r
    LEFT JOIN `BRANCH` b ON b.repo_id = r.repo_id
    LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
    GROUP BY r.repo_id
) AS repo_commit_summary;

-- Latest commit timestamp and earliest commit timestamp.
SELECT
    MIN(committed_at) AS first_commit_time,
    MAX(committed_at) AS latest_commit_time
FROM `COMMIT`;

-- ================================================================
-- 11. ORDER BY
-- ================================================================

-- Branches ordered by number of commits.
SELECT
    b.branch_id,
    b.branch_name,
    r.repo_name,
    COUNT(c.commit_id) AS commit_count
FROM `BRANCH` b
JOIN `REPOSITORY` r ON r.repo_id = b.repo_id
LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
GROUP BY b.branch_id, b.branch_name, r.repo_name
ORDER BY commit_count DESC, r.repo_name, b.branch_name;

-- ================================================================
-- 12. SUBQUERIES
-- ================================================================

-- 12.1 Repositories having more commits than the average repository.
SELECT
    r.repo_id,
    r.repo_name
FROM `REPOSITORY` r
WHERE (
    SELECT COUNT(*)
    FROM `COMMIT` c
    JOIN `BRANCH` b2 ON b2.branch_id = c.branch_id
    WHERE b2.repo_id = r.repo_id
) > (
    SELECT AVG(repo_commit_count)
    FROM (
        SELECT
            r2.repo_id,
            COUNT(c2.commit_id) AS repo_commit_count
        FROM `REPOSITORY` r2
        LEFT JOIN `BRANCH` b3 ON b3.repo_id = r2.repo_id
        LEFT JOIN `COMMIT` c2 ON c2.branch_id = b3.branch_id
        GROUP BY r2.repo_id
    ) AS avg_repo_commits
)
ORDER BY r.repo_name;

-- 12.2 Users who have made more pushes than the average push count.
SELECT
    u.user_id,
    u.username
FROM `USER` u
WHERE (
    SELECT COUNT(*)
    FROM `PUSH_EVENT` p
    WHERE p.user_id = u.user_id
) > (
    SELECT AVG(user_push_count)
    FROM (
        SELECT
            u2.user_id,
            COUNT(p2.push_id) AS user_push_count
        FROM `USER` u2
        LEFT JOIN `PUSH_EVENT` p2 ON p2.user_id = u2.user_id
        GROUP BY u2.user_id
    ) AS avg_user_pushes
)
ORDER BY u.username;

-- 12.3 Find the repositories owned by the user(s) with the most repositories.
SELECT
    r.repo_id,
    r.repo_name,
    r.owner_user_id
FROM `REPOSITORY` r
WHERE r.owner_user_id IN (
    SELECT owner_counts.user_id
    FROM (
        SELECT owner_user_id AS user_id, COUNT(*) AS repository_count
        FROM `REPOSITORY`
        GROUP BY owner_user_id
    ) AS owner_counts
    WHERE owner_counts.repository_count = (
        SELECT MAX(x.repository_count)
        FROM (
            SELECT owner_user_id, COUNT(*) AS repository_count
            FROM `REPOSITORY`
            GROUP BY owner_user_id
        ) AS x
    )
)
ORDER BY r.repo_name;

-- ================================================================
-- 13. COMPLEX QUERY
-- ================================================================

-- Repository activity dashboard:
-- owner, members, branches, commits and latest commit time.
SELECT
    r.repo_id,
    r.repo_name,
    u.username AS owner,
    COUNT(DISTINCT rm.membership_id) AS member_count,
    COUNT(DISTINCT b.branch_id) AS branch_count,
    COUNT(DISTINCT c.commit_id) AS commit_count,
    MAX(c.committed_at) AS latest_commit
FROM `REPOSITORY` r
JOIN `USER` u ON u.user_id = r.owner_user_id
LEFT JOIN `REPOSITORY_MEMBER` rm ON rm.repo_id = r.repo_id
LEFT JOIN `BRANCH` b ON b.repo_id = r.repo_id
LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
GROUP BY r.repo_id, r.repo_name, u.username
HAVING COUNT(DISTINCT c.commit_id) >= 2
ORDER BY commit_count DESC, r.repo_name;

-- ================================================================
-- 14. VIEW
-- ================================================================

CREATE VIEW `vw_repository_activity` AS
SELECT
    r.repo_id,
    r.repo_name,
    u.username AS owner,
    r.visibility,
    COUNT(DISTINCT rm.membership_id) AS member_count,
    COUNT(DISTINCT b.branch_id) AS branch_count,
    COUNT(DISTINCT c.commit_id) AS commit_count,
    MAX(c.committed_at) AS latest_commit
FROM `REPOSITORY` r
JOIN `USER` u ON u.user_id = r.owner_user_id
LEFT JOIN `REPOSITORY_MEMBER` rm ON rm.repo_id = r.repo_id
LEFT JOIN `BRANCH` b ON b.repo_id = r.repo_id
LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
GROUP BY r.repo_id, r.repo_name, u.username, r.visibility;

-- View execution.
SELECT *
FROM `vw_repository_activity`
ORDER BY commit_count DESC, repo_name;

-- ================================================================
-- 15. TRANSACTION WITH COMMIT
-- ================================================================

-- Demonstrate a successful branch + commit + push workflow.
START TRANSACTION;

INSERT INTO `BRANCH`
    (branch_name, repo_id, created_by_user_id)
VALUES
    ('transaction-demo', 1, 1);

SET @transaction_branch_id = LAST_INSERT_ID();

INSERT INTO `COMMIT`
    (message, branch_id, author_user_id, tree_id)
VALUES
    ('Transaction demonstration commit', @transaction_branch_id, 1, 10);

SET @transaction_commit_id = LAST_INSERT_ID();

INSERT INTO `PUSH_EVENT`
    (user_id, branch_id)
VALUES
    (1, @transaction_branch_id);

COMMIT;

-- Verify that the committed transaction is persistent.
SELECT
    b.branch_id,
    b.branch_name,
    c.commit_id,
    c.message,
    p.push_id
FROM `BRANCH` b
LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
LEFT JOIN `PUSH_EVENT` p ON p.branch_id = b.branch_id
WHERE b.branch_id = @transaction_branch_id;

-- ================================================================
-- 16. TRANSACTION WITH ROLLBACK
-- ================================================================

-- Demonstrate that a branch/commit/push sequence can be completely undone.
START TRANSACTION;

INSERT INTO `BRANCH`
    (branch_name, repo_id, created_by_user_id)
VALUES
    ('rollback-demo', 1, 1);

SET @rollback_branch_id = LAST_INSERT_ID();

INSERT INTO `COMMIT`
    (message, branch_id, author_user_id, tree_id)
VALUES
    ('Temporary commit that will be rolled back', @rollback_branch_id, 1, 10);

INSERT INTO `PUSH_EVENT`
    (user_id, branch_id)
VALUES
    (1, @rollback_branch_id);

-- Shows the records while the transaction is still active.
SELECT
    b.branch_id,
    b.branch_name,
    c.commit_id,
    p.push_id
FROM `BRANCH` b
LEFT JOIN `COMMIT` c ON c.branch_id = b.branch_id
LEFT JOIN `PUSH_EVENT` p ON p.branch_id = b.branch_id
WHERE b.branch_id = @rollback_branch_id;

ROLLBACK;

-- After ROLLBACK the temporary branch and its dependent rows are gone.
SELECT COUNT(*) AS rollback_branch_count
FROM `BRANCH`
WHERE branch_id = @rollback_branch_id;

-- ================================================================
-- 17. STORED FUNCTIONS
-- ================================================================

DELIMITER $$

CREATE FUNCTION fn_repo_commit_count(p_repo_id INT)
RETURNS INT
DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_commit_count INT DEFAULT 0;

    SELECT COUNT(c.commit_id)
      INTO v_commit_count
      FROM `COMMIT` c
      JOIN `BRANCH` b ON b.branch_id = c.branch_id
     WHERE b.repo_id = p_repo_id;

    RETURN v_commit_count;
END$$

CREATE FUNCTION fn_branch_commit_count(p_branch_id INT)
RETURNS INT
DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_commit_count INT DEFAULT 0;

    SELECT COUNT(*)
      INTO v_commit_count
      FROM `COMMIT`
     WHERE branch_id = p_branch_id;

    RETURN v_commit_count;
END$$

DELIMITER ;

-- Function execution.
SELECT
    repo_id,
    repo_name,
    fn_repo_commit_count(repo_id) AS commit_count
FROM `REPOSITORY`
ORDER BY commit_count DESC, repo_name;

SELECT
    branch_id,
    branch_name,
    fn_branch_commit_count(branch_id) AS commit_count
FROM `BRANCH`
ORDER BY commit_count DESC, branch_id;

-- ================================================================
-- 18. STORED PROCEDURES
-- ================================================================

DELIMITER $$

-- Create a repository and return its generated ID.
CREATE PROCEDURE sp_create_repository(
    IN p_owner_user_id INT,
    IN p_repo_name VARCHAR(100),
    IN p_description TEXT,
    IN p_visibility VARCHAR(20),
    OUT p_repo_id INT
)
BEGIN
    DECLARE v_user_count INT DEFAULT 0;

    SELECT COUNT(*)
      INTO v_user_count
      FROM `USER`
     WHERE user_id = p_owner_user_id;

    IF v_user_count = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Repository owner does not exist';
    END IF;

    INSERT INTO `REPOSITORY`
        (repo_name, description, visibility, owner_user_id)
    VALUES
        (p_repo_name, p_description, p_visibility, p_owner_user_id);

    SET p_repo_id = LAST_INSERT_ID();
END$$

-- Create a branch through the stored procedure.
CREATE PROCEDURE sp_create_branch(
    IN p_repo_id INT,
    IN p_branch_name VARCHAR(100),
    IN p_created_by_user_id INT,
    OUT p_branch_id INT
)
BEGIN
    DECLARE v_repo_count INT DEFAULT 0;
    DECLARE v_user_count INT DEFAULT 0;
    DECLARE v_member_count INT DEFAULT 0;

    SELECT COUNT(*) INTO v_repo_count
    FROM `REPOSITORY`
    WHERE repo_id = p_repo_id;

    SELECT COUNT(*) INTO v_user_count
    FROM `USER`
    WHERE user_id = p_created_by_user_id;

    SELECT COUNT(*) INTO v_member_count
    FROM `REPOSITORY_MEMBER`
    WHERE repo_id = p_repo_id
      AND user_id = p_created_by_user_id
      AND role IN ('Maintainer', 'Contributor');

    IF v_repo_count = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Repository does not exist';
    END IF;

    IF v_user_count = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Branch creator does not exist';
    END IF;

    IF v_member_count = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Branch creator must be a Maintainer or Contributor';
    END IF;

    INSERT INTO `BRANCH`
        (branch_name, repo_id, created_by_user_id)
    VALUES
        (p_branch_name, p_repo_id, p_created_by_user_id);

    SET p_branch_id = LAST_INSERT_ID();
END$$

-- Record a commit. Pushes remain a separate operation in PUSH_EVENT.
CREATE PROCEDURE sp_record_commit(
    IN p_branch_id INT,
    IN p_author_user_id INT,
    IN p_tree_id INT,
    IN p_message TEXT,
    OUT p_commit_id INT
)
BEGIN
    INSERT INTO `COMMIT`
        (message, branch_id, author_user_id, tree_id)
    VALUES
        (p_message, p_branch_id, p_author_user_id, p_tree_id);

    SET p_commit_id = LAST_INSERT_ID();
END$$

-- Record a push event separately from the commit operation.
CREATE PROCEDURE sp_record_push(
    IN p_branch_id INT,
    IN p_user_id INT,
    OUT p_push_id INT
)
BEGIN
    INSERT INTO `PUSH_EVENT`
        (user_id, branch_id)
    VALUES
        (p_user_id, p_branch_id);

    SET p_push_id = LAST_INSERT_ID();
END$$

DELIMITER ;

-- ================================================================
-- 19. PROCEDURE EXECUTION
-- ================================================================

-- Create a new repository using the procedure.
SET @new_repo_id = NULL;
CALL sp_create_repository(
    6,
    'AnalyticsLab',
    'Repository created through the stored procedure demonstration.',
    'Public',
    @new_repo_id
);

-- Add user 5 as a contributor for the new repository.
INSERT INTO `REPOSITORY_MEMBER`
    (role, user_id, repo_id)
VALUES
    ('Contributor', 5, @new_repo_id);

-- Create the main branch through the branch procedure.
SET @procedure_branch_id = NULL;
CALL sp_create_branch(
    @new_repo_id,
    'main',
    5,
    @procedure_branch_id
);

-- Create a blob object and its BLOBS row.
INSERT INTO `OBJECT`
    (object_hash, object_type, repo_id)
VALUES
    ('1414141414141414141414141414141414141414141414141414141414141414', 'blob', @new_repo_id);

SET @procedure_blob_id = LAST_INSERT_ID();

INSERT INTO `BLOBS`
    (blob_id, storage_path, mime_type)
VALUES
    (@procedure_blob_id, '/objects/14/analytics.sql', 'application/sql');

-- Create a tree object and its TREE row.
INSERT INTO `OBJECT`
    (object_hash, object_type, repo_id)
VALUES
    ('1515151515151515151515151515151515151515151515151515151515151515', 'tree', @new_repo_id);

SET @procedure_tree_id = LAST_INSERT_ID();

INSERT INTO `TREE` (tree_id)
VALUES (@procedure_tree_id);

INSERT INTO `TREE_ENTRY`
    (file_mode, file_name, tree_id, subtree_id, blob_id)
VALUES
    ('100644', 'analytics.sql', @procedure_tree_id, NULL, @procedure_blob_id);

-- Record the commit and push as separate version-control operations.
SET @procedure_commit_id = NULL;
CALL sp_record_commit(
    @procedure_branch_id,
    5,
    @procedure_tree_id,
    'Add analytics SQL module through stored procedure',
    @procedure_commit_id
);

SET @procedure_push_id = NULL;
CALL sp_record_push(
    @procedure_branch_id,
    5,
    @procedure_push_id
);

-- Verify procedure-created records.
SELECT
    r.repo_name,
    b.branch_name,
    c.commit_id,
    c.message,
    u.username AS author,
    p.push_id
FROM `REPOSITORY` r
JOIN `BRANCH` b ON b.repo_id = r.repo_id
JOIN `COMMIT` c ON c.branch_id = b.branch_id
JOIN `USER` u ON u.user_id = c.author_user_id
LEFT JOIN `PUSH_EVENT` p
    ON p.branch_id = b.branch_id
   AND p.user_id = c.author_user_id
WHERE r.repo_id = @new_repo_id
ORDER BY c.committed_at;

-- ================================================================
-- 20. TRIGGER EXECUTION / VERIFICATION
-- ================================================================

-- Trigger 1 verification:
-- The owner of AnalyticsLab was automatically inserted as Maintainer.
SELECT
    rm.membership_id,
    u.username,
    r.repo_name,
    rm.role
FROM `REPOSITORY_MEMBER` rm
JOIN `USER` u ON u.user_id = rm.user_id
JOIN `REPOSITORY` r ON r.repo_id = rm.repo_id
WHERE r.repo_id = @new_repo_id
ORDER BY rm.membership_id;

-- Trigger 2 verification:
-- The branch insertion succeeded because user 5 is a Contributor in AnalyticsLab.
SELECT
    branch_id,
    branch_name,
    repo_id,
    created_by_user_id
FROM `BRANCH`
WHERE branch_id = @procedure_branch_id;

-- Trigger 3/4/5 verification:
-- The commit and push succeeded because the author is a Contributor, the
-- tree is a valid tree object, and the tree belongs to the same repository.
SELECT
    c.commit_id,
    c.message,
    c.branch_id,
    c.author_user_id,
    c.tree_id,
    p.push_id,
    p.user_id,
    p.branch_id
FROM `COMMIT` c
LEFT JOIN `PUSH_EVENT` p
    ON p.user_id = c.author_user_id
   AND p.branch_id = c.branch_id
WHERE c.branch_id = @procedure_branch_id
ORDER BY c.commit_id DESC;

-- ================================================================
-- 21. FINAL DATABASE STATE / SUMMARY QUERIES
-- ================================================================

-- Build the initial Neo4j projection after all seeded repository rows exist.
INSERT INTO `VCS_GRAPH_OUTBOX` (event_type, payload)
SELECT 'sync_repository', JSON_OBJECT('repo_id', repo_id)
FROM `REPOSITORY`;

-- Object catalogue summary by repository and object type.
SELECT
    r.repo_name,
    o.object_type,
    COUNT(*) AS object_count
FROM `OBJECT` o
JOIN `REPOSITORY` r ON r.repo_id = o.repo_id
GROUP BY r.repo_id, r.repo_name, o.object_type
ORDER BY r.repo_name, o.object_type;


-- Final repository activity summary.
SELECT *
FROM `vw_repository_activity`
ORDER BY commit_count DESC, repo_name;

-- Final row counts for the major tables.
SELECT 'USER' AS table_name, COUNT(*) AS row_count FROM `USER`
UNION ALL
SELECT 'REPOSITORY', COUNT(*) FROM `REPOSITORY`
UNION ALL
SELECT 'REPOSITORY_MEMBER', COUNT(*) FROM `REPOSITORY_MEMBER`
UNION ALL
SELECT 'BRANCH', COUNT(*) FROM `BRANCH`
UNION ALL
SELECT 'OBJECT', COUNT(*) FROM `OBJECT`
UNION ALL
SELECT 'TREE', COUNT(*) FROM `TREE`
UNION ALL
SELECT 'BLOBS', COUNT(*) FROM `BLOBS`
UNION ALL
SELECT 'TREE_ENTRY', COUNT(*) FROM `TREE_ENTRY`
UNION ALL
SELECT 'COMMIT', COUNT(*) FROM `COMMIT`
UNION ALL
SELECT 'PUSH_EVENT', COUNT(*) FROM `PUSH_EVENT`
UNION ALL
SELECT 'VCS_GRAPH_OUTBOX', COUNT(*) FROM `VCS_GRAPH_OUTBOX`
ORDER BY table_name;

-- ================================================================
-- END OF FILE
-- ================================================================
