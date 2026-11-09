-- 聊天会话索引：让对话脱离 2 小时登录令牌存活（隔天重新登录仍能续聊）。
-- 可安全应用到已有 MySQL 卷：只建表，不改动 customer_sessions / messages / chat_turns。
CREATE TABLE IF NOT EXISTS `chat_conversations` (
  `thread_id` varchar(255) NOT NULL COMMENT '会话线程ID（与 messages.thread_id 同值，不带 support_v2: 前缀）',
  `tenant_id` varchar(64) NOT NULL DEFAULT '' COMMENT '租户ID',
  `customer_id` varchar(255) NOT NULL DEFAULT '' COMMENT '稳定客户标识（服务端判定，不取前端入参）',
  `is_guest` tinyint(1) NOT NULL DEFAULT 0 COMMENT '免登录访客会话（不提供历史列表）',
  `is_preview` tinyint(1) NOT NULL DEFAULT 0 COMMENT '商家预览会话（不提供历史列表）',
  `status` varchar(16) NOT NULL DEFAULT 'draft' COMMENT 'draft/active/expired',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `last_active_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '最后一次成功受理客户消息',
  PRIMARY KEY (`thread_id`),
  KEY `idx_conv_owner` (`tenant_id`, `customer_id`, `last_active_at`),
  KEY `idx_conv_status_active` (`status`, `last_active_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='聊天会话索引（跨登录令牌存活）';

-- 回填：把已经产生过消息的历史会话补进来，避免升级后老用户看不到已有对话。
-- 只回填能确定归属的（thread_id 形如 "{customer_id}:{后缀}"，customer_id 含租户前缀）。
INSERT IGNORE INTO `chat_conversations`
  (`thread_id`, `tenant_id`, `customer_id`, `is_guest`, `is_preview`,
   `status`, `created_at`, `last_active_at`)
SELECT m.thread_id,
       SUBSTRING_INDEX(m.thread_id, ':', 1),
       SUBSTRING_INDEX(SUBSTRING_INDEX(m.thread_id, ':', 2), ':', -2),
       0, 0, 'active',
       MIN(m.created_at), MAX(m.created_at)
FROM `messages` m
WHERE m.thread_id LIKE '%:%:%'
  AND m.role = 'user'
GROUP BY m.thread_id;

-- 存量库里 idx_csess_thread 是 UNIQUE（init.sql 早已改成普通索引，但没有迁移过）。
-- 恢复历史会话要把当前令牌绑到一条旧 thread_id 上，同一会话因此会有多行令牌记录
-- （多设备登录本来就是正常场景），唯一约束会让这一步直接失败。
-- 幂等：已经是普通索引时什么也不做。
SET @csess_thread_unique := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'customer_sessions'
    AND index_name = 'idx_csess_thread' AND non_unique = 0);
SET @csess_sql := IF(@csess_thread_unique > 0,
  'ALTER TABLE `customer_sessions` DROP INDEX `idx_csess_thread`, ADD INDEX `idx_csess_thread` (`thread_id`)',
  'DO 0');
PREPARE csess_stmt FROM @csess_sql;
EXECUTE csess_stmt;
DEALLOCATE PREPARE csess_stmt;

-- 会话自定义名称与软删除（2026-10-29）。幂等：字段已存在时什么也不做。
SET @conv_has_title := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'chat_conversations'
    AND column_name = 'title');
SET @conv_title_sql := IF(@conv_has_title = 0,
  'ALTER TABLE `chat_conversations` ADD COLUMN `title` varchar(120) NOT NULL DEFAULT '''' AFTER `customer_id`',
  'DO 0');
PREPARE conv_title_stmt FROM @conv_title_sql;
EXECUTE conv_title_stmt;
DEALLOCATE PREPARE conv_title_stmt;

SET @conv_has_deleted := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'chat_conversations'
    AND column_name = 'deleted_at');
SET @conv_deleted_sql := IF(@conv_has_deleted = 0,
  'ALTER TABLE `chat_conversations` ADD COLUMN `deleted_at` datetime(3) DEFAULT NULL COMMENT ''用户主动删除时间；非空即不再出现在列表'' AFTER `last_active_at`',
  'DO 0');
PREPARE conv_deleted_stmt FROM @conv_deleted_sql;
EXECUTE conv_deleted_stmt;
DEALLOCATE PREPARE conv_deleted_stmt;

SET @conv_has_deleted_idx := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'chat_conversations'
    AND index_name = 'idx_conv_deleted');
SET @conv_idx_sql := IF(@conv_has_deleted_idx = 0,
  'ALTER TABLE `chat_conversations` ADD INDEX `idx_conv_deleted` (`tenant_id`, `customer_id`, `deleted_at`)',
  'DO 0');
PREPARE conv_idx_stmt FROM @conv_idx_sql;
EXECUTE conv_idx_stmt;
DEALLOCATE PREPARE conv_idx_stmt;
