USE version_control_system;

CREATE TABLE IF NOT EXISTS `COMMIT_PARENT` (
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

-- Preserve existing same-branch history as first-parent links. Existing schema
-- data cannot tell us which branch a branch originally forked from.
INSERT IGNORE INTO `COMMIT_PARENT` (commit_id, parent_commit_id, parent_order)
SELECT chain.commit_id, chain.parent_commit_id, 1
FROM (
    SELECT commit_id,
           LAG(commit_id) OVER (PARTITION BY branch_id ORDER BY committed_at, commit_id) AS parent_commit_id
    FROM `COMMIT`
) AS chain
WHERE chain.parent_commit_id IS NOT NULL;
