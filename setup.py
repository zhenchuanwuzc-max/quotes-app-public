"""
py2app 打包脚本
用法：
    cd /path/to/quotes-app
    python3 setup.py py2app -A   # alias 模式（开发，快）
    python3 setup.py py2app      # 完整打包（产 dist/quotes.app）
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
DATA_FILES = ["index.html", "server.py", "VERSION"]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "icon.icns",
    "plist": {
        "CFBundleName": "quotes",
        "CFBundleDisplayName": "quotes",
        "CFBundleIdentifier": "com.example.quotes-app",
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
