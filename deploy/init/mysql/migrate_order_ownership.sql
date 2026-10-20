-- ============================================================
-- 订单归属迁移脚本（幂等，可重复执行）
--
-- 背景：orders 表新增归属列 tenant_id/customer_id（Go AutoMigrate 自动补列）。
-- 本脚本只把【明确指定的演示订单】认领给演示客户，不动其余任何旧订单：
-- 没有可信归属信息的订单保持不可查询（fail-closed）。
--
-- 安全条件：仅当 tenant_id 与 customer_id 【两列都为空】时才认领。
-- 任一列已有值（含人工改过的）一律不碰，避免半归属数据被覆盖。
--
-- 用法（在 MySQL 容器内执行，按需改库名）：
--   docker exec -i cs_mysql mysql -uroot -p123456 cs_biz_db < migrate_order_ownership.sql
--
-- 注意：不执行 down -v / 不重建数据卷；执行前请先备份：
--   docker exec cs_mysql mysqldump -uroot -p123456 cs_biz_db orders > orders_backup.sql
--
-- 执行顺序：先跑第 1 段 SELECT 预检（列出将被修改的行，与预期核对），
-- 确认无误后再执行第 2 段 UPDATE。
-- ============================================================

-- ---------- 第 1 段：预检（只读，列出将被修改的行） ----------
-- 预期：仅 ORD-2024-001 / ORD-2024-002 两行，且 tenant_id/customer_id 均为空。
-- 若出现其他行或已有归属值的行，请人工核对后再决定是否执行第 2 段。
SELECT `id`, `order_id`, `tenant_id`, `customer_id`, `status`
FROM `orders`
WHERE `order_id` IN ('ORD-2024-001', 'ORD-2024-002')
  AND (`tenant_id` = '' OR `tenant_id` IS NULL)
  AND (`customer_id` = '' OR `customer_id` IS NULL);

-- ---------- 第 2 段：认领（仅两列都为空的指定演示订单） ----------
UPDATE `orders`
SET `tenant_id` = 't_demo', `customer_id` = 't_demo:buyer1'
WHERE `order_id` IN ('ORD-2024-001', 'ORD-2024-002')
  AND (`tenant_id` = '' OR `tenant_id` IS NULL)
  AND (`customer_id` = '' OR `customer_id` IS NULL);

-- ---------- 验收查询（应只有 2 行归属 t_demo:buyer1） ----------
-- SELECT `id`,`order_id`,`tenant_id`,`customer_id`,`status` FROM `orders`;
