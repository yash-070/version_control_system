USE version_control_system;

CREATE TABLE IF NOT EXISTS `MERGE_REQUEST` (
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
