"""Lưu/đọc danh sách kênh SSH ở ~/.config/iterm-xshell/sessions.json (ghi atomic, quyền 600)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import uuid

DEFAULT_PATH = os.path.expanduser("~/.config/iterm-xshell/sessions.json")

FIELDS = ("id", "name", "group", "host", "port", "user", "identity_file", "proxy_jump",
          "extra_args", "remote_dir", "color", "tags", "source", "missing",
          "keychain", "autotype_password")
EDITABLE = ("name", "group", "host", "port", "user", "identity_file", "proxy_jump",
            "extra_args", "remote_dir", "color", "tags", "keychain", "autotype_password")
COLORS = ("", "red", "orange", "yellow", "green", "blue", "purple", "gray")

DEFAULT_SETTINGS = {
    "download_dir": "~/Downloads",
    "strict_host_key_checking": True,
    "ssh_config": "~/.ssh/config",
    "sessions_window": False,       # cửa sổ riêng dock bên trái (mặc định tắt: dùng tool Xshell trong Toolbelt)
    "sessions_window_make_room": True,
    "sessions_in_toolbelt": False,  # thêm panel kênh SSH vào Toolbelt (bên phải)
    "keychain_enabled": True,       # cho phép lưu mật khẩu trong macOS Keychain
    "collapsed_groups": [],
}


class ValidationError(ValueError):
    pass


def _clean(d: dict, base: dict | None = None) -> dict:
    ch = dict(base or {})
    for k in EDITABLE:
        if k in d:
            ch[k] = d[k]
    for k in ("name", "group", "host", "user", "identity_file", "proxy_jump",
              "extra_args", "remote_dir", "color"):
        v = ch.get(k)
        ch[k] = str(v).strip() if v not in (None,) else ""
    if not ch["name"]:
        ch["name"] = ch.get("host", "")
    if not ch["host"]:
        raise ValidationError("Thiếu host")
    if any(c.isspace() for c in ch["host"]) or ch["host"].startswith("-"):
        raise ValidationError("Host không hợp lệ")
    if ch["user"] and (any(c.isspace() for c in ch["user"]) or ch["user"].startswith("-")):
        raise ValidationError("User không hợp lệ")
    port = ch.get("port")
    if port in (None, "", 0, "0"):
        ch["port"] = None
    else:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValidationError("Port không hợp lệ")
        if not 1 <= port <= 65535:
            raise ValidationError("Port phải trong 1–65535")
        ch["port"] = port
    if ch["extra_args"]:
        import shlex
        try:
            shlex.split(ch["extra_args"])
        except ValueError as e:
            raise ValidationError(f"Tham số thêm không hợp lệ: {e}")
    if ch["color"] not in COLORS:
        ch["color"] = ""
    tags = ch.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in (x.strip() for x in tags.replace(";", ",").split(",")) if t]
    ch["tags"] = [str(t)[:40] for t in tags][:20]
    ch["keychain"] = bool(ch.get("keychain"))
    ch["autotype_password"] = bool(ch.get("autotype_password"))
    return ch


class SessionStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = os.path.expanduser(path)
        self._lock = threading.RLock()
        self._data = {"version": 1, "sessions": [], "settings": {}}
        self.load()

    # ---------------------------------------------------------------- I/O
    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                    data.setdefault("settings", {})
                    self._data = data
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                # File hỏng: giữ bản sao để không mất dữ liệu, bắt đầu rỗng.
                try:
                    os.replace(self.path, self.path + f".broken-{int(time.time())}")
                except OSError:
                    pass

    def save(self):
        with self._lock:
            d = os.path.dirname(self.path)
            os.makedirs(d, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".sessions-", suffix=".tmp", dir=d)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ----------------------------------------------------------- settings
    @property
    def settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        s.update(self._data.get("settings") or {})
        return s

    def update_settings(self, **kw):
        with self._lock:
            st = self._data.setdefault("settings", {})
            for k, v in kw.items():
                if k in DEFAULT_SETTINGS:
                    st[k] = v
            self.save()

    # ----------------------------------------------------------- sessions
    def list(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._data["sessions"])

    def get(self, sid: str) -> dict | None:
        with self._lock:
            for ch in self._data["sessions"]:
                if ch["id"] == sid:
                    return copy.deepcopy(ch)
        return None

    def add(self, d: dict, source: str = "manual") -> dict:
        with self._lock:
            ch = _clean(d)
            ch["id"] = str(uuid.uuid4())
            ch["source"] = source
            ch["missing"] = False
            self._data["sessions"].append(ch)
            self.save()
            return copy.deepcopy(ch)

    def update(self, sid: str, d: dict) -> dict:
        with self._lock:
            for i, ch in enumerate(self._data["sessions"]):
                if ch["id"] == sid:
                    new = _clean(d, ch)
                    if ch.get("source") == "ssh_config":
                        new["host"] = ch["host"]      # alias do config quản lý
                    self._data["sessions"][i] = new
                    self.save()
                    return copy.deepcopy(new)
        raise KeyError(sid)

    def delete(self, sid: str) -> bool:
        with self._lock:
            before = len(self._data["sessions"])
            self._data["sessions"] = [c for c in self._data["sessions"] if c["id"] != sid]
            if len(self._data["sessions"]) != before:
                self.save()
                return True
            return False

    def duplicate(self, sid: str) -> dict:
        ch = self.get(sid)
        if not ch:
            raise KeyError(sid)
        ch["name"] = ch["name"] + " (bản sao)"
        return self.add(ch, source="manual")

    def sync_ssh_config(self, aliases: list[str], group: str = "~/.ssh/config") -> dict:
        """Thêm alias mới, đánh dấu alias biến mất (không xoá), không đụng kênh manual."""
        with self._lock:
            want = list(dict.fromkeys(aliases))
            existing = {c["host"]: c for c in self._data["sessions"] if c.get("source") == "ssh_config"}
            added, missing, restored = [], [], []
            for a in want:
                c = existing.get(a)
                if c is None:
                    ch = _clean({"name": a, "host": a, "group": group})
                    ch.update(id=str(uuid.uuid4()), source="ssh_config", missing=False)
                    self._data["sessions"].append(ch)
                    added.append(a)
                elif c.get("missing"):
                    c["missing"] = False
                    restored.append(a)
            wanted = set(want)
            for a, c in existing.items():
                if a not in wanted and not c.get("missing"):
                    c["missing"] = True
                    missing.append(a)
            if added or missing or restored:
                self.save()
            return {"added": added, "missing": missing, "restored": restored}
