"""Machine-local shortcut settings; independent of quote data and its sync."""
import copy
import json
import os
import shutil
import tempfile
import threading
import uuid


_BROWSE = ["index", "poster"]
_CHAR_OPTIONS = list("abcdefghijklmnopqrstuvwxyz") + ["/", "?"]
_ACTIONS = [
    {"id": "add", "label": "新增", "scope": ["index"], "default": "a",
     "aliases": [{"binding": "n", "scope": _BROWSE}]},
    {"id": "edit", "label": "编辑", "scope": _BROWSE, "default": "e"},
    {"id": "search", "label": "搜索", "scope": _BROWSE, "default": "/"},
    {"id": "previous", "label": "上一条", "scope": _BROWSE, "default": "k",
     "aliases": [{"binding": "ArrowUp", "scope": _BROWSE},
                 {"binding": "ArrowLeft", "scope": ["poster"]}]},
    {"id": "next", "label": "下一条", "scope": _BROWSE, "default": "j",
     "aliases": [{"binding": "ArrowDown", "scope": _BROWSE},
                 {"binding": "ArrowRight", "scope": ["poster"]},
                 {"binding": "Space", "scope": ["poster"]}]},
    {"id": "pin", "label": "置顶", "scope": _BROWSE, "default": "p"},
    {"id": "copy", "label": "复制", "scope": _BROWSE, "default": "c"},
    {"id": "wallpaper", "label": "设为壁纸", "scope": ["poster"], "default": "w"},
    {"id": "index", "label": "返回列表", "scope": ["poster"], "default": "i"},
    {"id": "help", "label": "快捷键面板", "scope": _BROWSE, "default": "?"},
    {"id": "save", "label": "保存", "scope": ["compose"], "default": "mod+s",
     "aliases": [{"binding": "mod+enter", "scope": ["compose"]}]},
]
for _action in _ACTIONS:
    _action.setdefault("aliases", [])
    _action["options"] = (["mod+s", "mod+shift+s"] if _action["id"] == "save" else
                          [key for key in _CHAR_OPTIONS if key != "n" or _action["id"] == "add"])

_LOCK = threading.RLock()


def settings_path():
    return os.path.expanduser("~/Library/Application Support/quotes-app/settings.json")


def defaults():
    return {action["id"]: action["default"] for action in _ACTIONS}


def normalize_bindings(bindings):
    """Validate the complete supported mapping before any file is touched."""
    if not isinstance(bindings, dict) or set(bindings) != set(defaults()):
        raise ValueError("快捷键设置必须包含全部操作，且不能包含未知操作")
    normalized = {}
    used = {}
    for action in _ACTIONS:
        value = bindings[action["id"]]
        if not isinstance(value, str):
            raise ValueError(action["label"] + "快捷键必须是文字")
        value = value.strip().lower()
        if value not in action["options"]:
            raise ValueError(action["label"] + "快捷键不在支持范围内")
        if value in used:
            raise ValueError(action["label"] + "与" + used[value] + "使用了相同快捷键")
        normalized[action["id"]] = value
        used[value] = action["label"]
    return normalized


def _read_bindings():
    try:
        with open(settings_path(), encoding="utf-8") as stream:
            saved = json.load(stream)
    except FileNotFoundError:
        return defaults(), False
    except (ValueError, UnicodeDecodeError):
        return defaults(), True
    try:
        if not isinstance(saved, dict) or saved.get("version") != 1:
            raise ValueError("unsupported settings version")
        return normalize_bindings(saved.get("bindings")), False
    except ValueError:
        return defaults(), True


def _response(bindings, warning=None):
    response = {"ok": True, "bindings": bindings, "defaults": defaults(),
                "actions": copy.deepcopy(_ACTIONS)}
    if warning:
        response["warning"] = warning
    return response


def read_shortcuts():
    with _LOCK:
        bindings, corrupt = _read_bindings()
        return _response(bindings, "快捷键设置文件损坏，已使用默认值；原文件未修改。保存时会保留原文件副本。"
                         if corrupt else None)


def write_shortcuts(bindings):
    normalized = normalize_bindings(bindings)
    with _LOCK:
        _, corrupt = _read_bindings()
        path = settings_path()
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        temporary = None
        backup = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "bindings": normalized}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if corrupt:
                backup = path + ".corrupt-" + uuid.uuid4().hex
                with open(path, "rb") as source, open(backup, "xb") as target:
                    os.chmod(backup, 0o600)
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)
        return _response(normalized, "已保存快捷键；损坏的原设置已保留为 " + os.path.basename(backup)
                         if backup else None)
