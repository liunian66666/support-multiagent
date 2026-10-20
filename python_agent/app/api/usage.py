"""Token 用量报表（从 main.py 拆出）。"""
import logging

from fastapi import APIRouter, Request

from app.infra.context import trace_id_var
from app.infra.db_pool import _pool_conn

logger = logging.getLogger("agent")
router = APIRouter()


@router.get("/agent/token-usage")
async def token_usage_report(request: Request):
    """Token 用量聚合报表
    query 参数：
      days      int    默认 30，统计近 N 天
      group_by  str    默认 day；day=按天聚合, tenant=按租户聚合, model=按模型聚合
      tenant_id str    可选，指定租户（租户管理端注入自身 tenant_id）
      model     str    可选，按模型名筛选
      source    str    可选，platform=平台模型 / custom=租户自定义（平台模型专项统计用）
    """
    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
    try:
        days = int(request.query_params.get("days", "30"))
        if days <= 0 or days > 365:
            days = 30
        group_by = request.query_params.get("group_by", "day")
        if group_by not in ("day", "tenant", "model"):
            group_by = "day"
        tenant_id = request.query_params.get("tenant_id", "") or None
        model_name = request.query_params.get("model", "") or None
        source = request.query_params.get("source", "") or None

        where = "created_at >= now() - make_interval(days => %s)"
        params: list = [days]
        if tenant_id:
            where += " AND tenant_id = %s"
            params.append(tenant_id)
        if model_name:
            where += " AND model = %s"
            params.append(model_name)
        if source:
            where += " AND source = %s"
            params.append(source)

        if group_by == "tenant":
            cols = "tenant_id AS dim, date_trunc('day', created_at) AS d"
        elif group_by == "model":
            cols = "model AS dim, NULL::timestamptz AS d"
        else:
            cols = "'-' AS dim, date_trunc('day', created_at) AS d"

        sql = f"""
            SELECT {cols},
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                   COUNT(*)                         AS calls,
                   COALESCE(SUM(cost), 0)           AS cost
            FROM token_usage
            WHERE {where}
            GROUP BY dim, d ORDER BY d DESC NULLS LAST, dim
        """
        async with _pool_conn(request.app) as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()

        data = []
        for r in rows:
            data.append({
                "dim": r[0],
                "date": r[1].isoformat() if r[1] else None,
                "input_tokens": int(r[2]),
                "output_tokens": int(r[3]),
                "total_tokens": int(r[4]),
                "calls": int(r[5]),
                "cost": float(r[6]) if r[6] else 0.0,
            })

        # 汇总行
        totals_sql = f"""
            SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(total_tokens),0), COUNT(*), COALESCE(SUM(cost),0)
            FROM token_usage WHERE {where}
        """
        async with _pool_conn(request.app) as conn:
            async with conn.cursor() as cur:
                await cur.execute(totals_sql, params)
                t = await cur.fetchone()

        return {"code": 0, "data": {
            "days": days,
            "group_by": group_by,
            "rows": data,
            "summary": {
                "input_tokens": int(t[0]),
                "output_tokens": int(t[1]),
                "total_tokens": int(t[2]),
                "calls": int(t[3]),
                "total_cost": float(t[4]) if t[4] else 0.0,
            },
        }}
    except Exception as e:
        logger.exception("/agent/token-usage error")
        return {"code": 500, "msg": str(e)}
