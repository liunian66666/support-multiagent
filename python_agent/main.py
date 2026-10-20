"""服务入口：设置进程编码 → 导出 ASGI app。

保留根目录 main.py 作为唯一入口，`python main.py` 与 `uvicorn main:app` 均可用
（Dockerfile 的 CMD、compose 的 /docs 健康检查、scripts/check_py_syntax.sh 依赖此入口）。
"""
import os

# 强制utf-8，解决httpx2 header ascii编码报错（必须在任何网络库导入前设置）
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["LANG"] = "en_US.UTF-8"
os.environ["LC_ALL"] = "en_US.UTF-8"

from app.server import app  # noqa: E402  环境变量必须先于网络库导入生效

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
