"""把知识库语料导入指定租户（上传 → 索引 → 发布），可重复执行。

知识库有四级门禁，缺一步检索都拿不到内容：

1. 上传只登记 ``status=0``（待索引），不会自动索引；
2. 必须显式触发 ``reindex``，worker 分片 + embedding 完成后 ``status=1``；
3. ``policy_state`` 默认 ``unreviewed``，证据层只放行 ``published`` / ``demo``；
4. ``product_scope`` 默认 ``unknown``，发布时必须指定适用范围。

本脚本按顺序跑完这四步，并按标题去重（重复执行不会产生同名副本）。

用法（容器内，资产目录已随镜像进 /app/assets）::

    python scripts/import_kb.py --tenant t_demo --dir assets/kb_seed/openkb \
        --category 通用问答 --publish
"""
import argparse
import sys
import time
from pathlib import Path

import httpx

DEFAULT_BASE = "http://127.0.0.1:8000"
POLL_SECONDS = 2.0
#: 语料目录里的说明文件不是知识条目，不能入库
SKIP_FILES = {"README.md", "SOURCES.md"}


def _headers(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _find_doc(cli: httpx.Client, tenant: str, title: str) -> dict | None:
    r = cli.get("/agent/kb/documents",
                params={"keyword": title, "page_size": 100},
                headers=_headers(tenant))
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"查询文档失败: {body}")
    for item in body["data"]["list"]:
        if item.get("title") == title:
            return item
    return None


def _upload(cli: httpx.Client, tenant: str, path: Path, title: str,
            category: str) -> str:
    r = cli.post("/agent/kb/upload",
                 data={"title": title, "category": category},
                 files={"file": (path.name, path.read_bytes(), "text/markdown")},
                 headers=_headers(tenant))
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"上传失败 {path.name}: {body}")
    return body["data"]["doc_id"]


def _wait_indexed(cli: httpx.Client, tenant: str, title: str,
                  timeout: float = 300.0) -> tuple[str, int]:
    """轮询到 status=1（已索引）或 status=2（失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = _find_doc(cli, tenant, title)
        if item and int(item.get("status") or 0) == 1:
            return item["doc_id"], int(item.get("chunk_count") or 0)
        if item and int(item.get("status") or 0) == 2:
            raise RuntimeError(f"索引失败 {title}: {item.get('error_msg')}")
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"索引超时 {title}")


def _title_of(path: Path) -> str:
    """取首个一级标题作文档标题，便于后台列表与 chunk 元数据可读。"""
    for line in path.read_text("utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def import_one(cli: httpx.Client, tenant: str, path: Path, category: str,
               publish: bool) -> dict:
    title = _title_of(path)
    existing = _find_doc(cli, tenant, title)
    if existing:
        doc_id = existing["doc_id"]
        action = "reuse"
    else:
        doc_id = _upload(cli, tenant, path, title, category)
        action = "upload"

    r = cli.post(f"/agent/kb/documents/{doc_id}/reindex", headers=_headers(tenant))
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"入队失败 {title}: {body}")

    doc_id, chunks = _wait_indexed(cli, tenant, title)

    state = "unreviewed"
    if publish:
        r = cli.post(f"/agent/kb/documents/{doc_id}/policy",
                     json={"policy_state": "published", "product_scope": "all"},
                     headers=_headers(tenant))
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"发布失败 {title}: {body}")
        state = body["data"]["policy_state"]

    return {"title": title, "doc_id": doc_id, "chunks": chunks,
            "action": action, "policy_state": state}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--dir", default="assets/kb_seed/openkb")
    ap.add_argument("--category", default="通用问答")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--publish", action="store_true",
                    help="索引完成后发布为商家可用口径（policy_state=published）")
    args = ap.parse_args()

    files = sorted(p for p in Path(args.dir).glob("*.md")
                   if p.name not in SKIP_FILES)
    if not files:
        print(f"没有找到 markdown：{args.dir}", file=sys.stderr)
        return 2

    failed = 0
    with httpx.Client(base_url=args.base, timeout=60.0) as cli:
        for path in files:
            try:
                info = import_one(cli, args.tenant, path, args.category, args.publish)
            except Exception as exc:                      # 单个文档失败不影响其它文档
                failed += 1
                print(f"[FAIL] {path.name}: {exc}", file=sys.stderr)
                continue
            print("[{action}] {title} -> {doc_id} chunks={chunks} state={policy_state}"
                  .format(**info))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
