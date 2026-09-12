# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：run.py
# IDE     ：PyCharm
# Author  ：刘景涛
# Date    ：2026/9/11 14:13
import os

import uvicorn

if __name__ == "__main__":
    # 监听 0.0.0.0，局域网 / 其它主机才能访问；
    # 本机调试时用 http://127.0.0.1:8000，远程用 http://<服务器IP>:8000。
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
