-- ============================================================
-- S1 轮次归因锚点 answer_id 迁移脚本（幂等，可重复执行）
--
-- 背景：agent 侧每轮回答生成 answer_id（与 trace 同轮锚点），业务侧
-- messages / messages_archive / satisfaction_ratings 需要落该锚点，
-- 才能把「评价 → 应答 → trace → 经验」串成归因链（S1）。
--
-- 变更内容（全部 DEFAULT ''，对旧行为零影响）：
--   1. messages             加 answer_id + 索引 idx_messages_answer_id
--   2. messages_archive     加 answer_id（随消息归档保留锚点）
--   3. satisfaction_ratings 加 answer_id + 索引 idx_rating_answer
--   4. 新表 feedback_outbox（评价事件 → 异步同步 PG，Outbox 模式）
--
-- 用法（MySQL 5.7/8.0 均可，在业务库执行，按需改库名）：
--   docker exec -i cs_mysql mysql -uroot -p123456 cs_biz_db < migrate_answer_id.sql
--
-- 注意：不执行 down -v / 不重建数据卷；纯加列/加表，不改动既有数据。
-- ============================================================

-- ---------- 1. messages.answer_id ----------
SET @stmt = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'messages'
     AND COLUMN_NAME = 'answer_id') = 0,
  "ALTER TABLE `messages` ADD COLUMN `answer_id` varchar(64) NOT NULL DEFAULT '' COMMENT '轮次归因锚点（agent 生成，评价-应答-溯源关联）'",
  'SELECT "messages.answer_id exists" AS msg');
PREPARE s FROM @stmt; EXECUTE s; DEALLOCATE PREPARE s;

SET @stmt = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'messages'
     AND INDEX_NAME = 'idx_messages_answer_id') = 0,
  'ALTER TABLE `messages` ADD INDEX `idx_messages_answer_id` (`answer_id`)',
  'SELECT "idx_messages_answer_id exists" AS msg');
PREPARE s FROM @stmt; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------- 2. messages_archive.answer_id ----------
SET @stmt = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'messages_archive'
     AND COLUMN_NAME = 'answer_id') = 0,
  "ALTER TABLE `messages_archive` ADD COLUMN `answer_id` varchar(64) NOT NULL DEFAULT '' COMMENT '轮次归因锚点（随消息归档）'",
  'SELECT "messages_archive.answer_id exists" AS msg');
PREPARE s FROM @stmt; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------- 3. satisfaction_ratings.answer_id ----------
SET @stmt = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'satisfaction_ratings'
     AND COLUMN_NAME = 'answer_id') = 0,
  "ALTER TABLE `satisfaction_ratings` ADD COLUMN `answer_id` varchar(64) NOT NULL DEFAULT '' COMMENT '评价所指向的轮次锚点（空=会话级评价）'",
  'SELECT "satisfaction_ratings.answer_id exists" AS msg');
PREPARE s FROM @stmt; EXECUTE s; DEALLOCATE PREPARE s;

SET @stmt = IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'satisfaction_ratings'
     AND INDEX_NAME = 'idx_rating_answer') = 0,
  'ALTER TABLE `satisfaction_ratings` ADD INDEX `idx_rating_answer` (`tenant_id`,`answer_id`)',
  'SELECT "idx_rating_answer exists" AS msg');
PREPARE s FROM @stmt; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------- 4. feedback_outbox（新表，CREATE IF NOT EXISTS 天然幂等） ----------
CREATE TABLE IF NOT EXISTS `feedback_outbox` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `event_type` varchar(32) NOT NULL DEFAULT '' COMMENT '事件类型：rating_submitted 等',
  `tenant_id` varchar(64) NOT NULL DEFAULT '' COMMENT '租户ID',
  `thread_id` varchar(255) NOT NULL DEFAULT '' COMMENT '会话ID',
  `answer_id` varchar(64) NOT NULL DEFAULT '' COMMENT '轮次归因锚点',
  `payload` text COMMENT '事件载荷JSON',
  `status` tinyint(1) NOT NULL DEFAULT 0 COMMENT '0=待同步 1=已同步',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `published_at` datetime(3) DEFAULT NULL COMMENT '同步完成时间',
  PRIMARY KEY (`id`),
  KEY `idx_outbox_status_id` (`status`,`id`),
  KEY `idx_outbox_answer` (`tenant_id`,`answer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='反馈Outbox表（MySQL→PG异步同步）';
