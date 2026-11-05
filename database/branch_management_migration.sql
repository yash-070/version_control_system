USE version_control_system;

ALTER TABLE `REPOSITORY`
    ADD COLUMN default_branch_id INT NULL AFTER owner_user_id;

UPDATE `REPOSITORY` r
SET default_branch_id = (
    SELECT b.branch_id
    FROM `BRANCH` b
    WHERE b.repo_id = r.repo_id
    ORDER BY (b.branch_name = 'main') DESC, b.created_at, b.branch_id
    LIMIT 1
)
WHERE r.repo_id > 0;

DROP TRIGGER IF EXISTS trg_branch_set_default;
DELIMITER $$
CREATE TRIGGER trg_branch_set_default
AFTER INSERT ON `BRANCH`
FOR EACH ROW
BEGIN
    UPDATE `REPOSITORY`
       SET default_branch_id = NEW.branch_id
     WHERE repo_id = NEW.repo_id
       AND default_branch_id IS NULL;
END$$
DELIMITER ;
