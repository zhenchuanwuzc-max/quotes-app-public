"""
py2app 打包脚本
用法：
    cd ~/quotes-app
    ~/daily-todo/venv/bin/python setup.py py2app -A   # alias 模式（开发，快）
    ~/daily-todo/venv/bin/python setup.py py2app      # 完整打包（产 dist/quotes.app）
产物：dist/quotes.app
"""
import os
from setuptools import setup

VERSION = (
    open(os.path.join(os.path.dirname(__file__), "VERSION")).read().strip()
    if os.path.exists(os.path.join(os.path.dirname(__file__), "VERSION"))
    else "0.0.1"
)

APP = ["desktop_app.py"]


# 递归收集 vendor/（Vditor 编辑器库），保持目录结构打进 .app 的 Resources/vendor/
# 否则打包出的 .app 里没有 vendor，server serve /vendor/ 全 404、编辑器加载不出来
def _collect_vendor(base="vendor"):
    out = []
    for root, _dirs, files in os.walk(base):
        keep = [os.path.join(root, f) for f in files if not f.startswith(".")]
        if keep:
            out.append((root, keep))  # py2app: (目标相对目录, [文件]) → Resources/<root>/
    return out


DATA_FILES = ["index.html", "server.py", "VERSION"] + _collect_vendor()

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "icon.icns",
    "plist": {
        "CFBundleName": "quotes",
        "CFBundleDisplayName": "quotes",
        "CFBundleIdentifier": "com.ocean.quotes-app",
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
        "LSMinimumSystemVersion": "11.0",
    },
    "packages": ["webview"],
    "includes": [
        "server",
        "json",
        "sqlite3",
        "http.server",
        "urllib.request",
        "urllib.parse",
        "Foundation",
        "WebKit",
        "AppKit",
        "zoneinfo",
    ],
    "excludes": ["tkinter", "test", "unittest"],
}

setup(
    app=APP,
    name="quotes",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
