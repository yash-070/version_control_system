USE version_control_system;

-- Durable MySQL outbox: source writes commit before the Neo4j projection runs.
CREATE TABLE IF NOT EXISTS `VCS_GRAPH_OUTBOX` (
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

-- Give any older empty repository a usable default branch.
INSERT INTO `BRANCH` (branch_name, repo_id, created_by_user_id)
SELECT 'main', r.repo_id, r.owner_user_id
FROM `REPOSITORY` r
WHERE NOT EXISTS (
    SELECT 1 FROM `BRANCH` b WHERE b.repo_id = r.repo_id
);

-- Queue a first projection for every repository already in the relational DB.
INSERT INTO `VCS_GRAPH_OUTBOX` (event_type, payload)
SELECT 'sync_repository', JSON_OBJECT('repo_id', r.repo_id)
FROM `REPOSITORY` r
WHERE NOT EXISTS (
    SELECT 1 FROM `VCS_GRAPH_OUTBOX` e
    WHERE e.event_type = 'sync_repository'
      AND JSON_UNQUOTE(JSON_EXTRACT(e.payload, '$.repo_id')) = CAST(r.repo_id AS CHAR)
);

DROP TRIGGER IF EXISTS trg_merge_request_validate;
DELIMITER $$
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
