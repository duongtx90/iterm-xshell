"""Tự đồng bộ kênh từ ~/.ssh/config khi khởi động và khi file (hoặc file Include) thay đổi."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from . import ssh_config_import as sci
from .sessions_store import SessionStore

log = logging.getLogger(__name__)


class SshConfigSync:
    def __init__(self, store: SessionStore, path: str | None = None, interval: float = 2.0):
        self.store = store
        self.path = os.path.expanduser(path or store.settings.get("ssh_config") or "~/.ssh/config")
        self.interval = interval
        self._sig = None
        self._scan: sci.ConfigScan | None = None
        self._last = {"added": [], "missing": [], "restored": []}
        self._last_time = 0.0
        self._task: asyncio.Task | None = None

    def info(self) -> dict:
        return {"path": self.path, "files": len(self._scan.files) if self._scan else 0,
                "hosts": len(self._scan.hosts) if self._scan else 0,
                "errors": self._scan.errors if self._scan else [],
                "last_sync": self._last_time, "last_result": self._last}

    def sync_now(self) -> dict:
        self._scan = sci.scan(self.path)
        self._sig = sci.signature(self.path, last=self._scan)
        res = self.store.sync_ssh_config(self._scan.aliases)
        self._last, self._last_time = res, time.time()
        if any(res.values()):
            log.info("ssh config sync: %s", res)
        return res

    async def sync(self, force: bool = False) -> dict:
        if force or sci.signature(self.path, last=self._scan) != self._sig:
            return self.sync_now()
        return {"added": [], "missing": [], "restored": []}

    async def _loop(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.sync()
            except Exception:
                log.exception("ssh config sync failed")

    def start(self):
        self.sync_now()
        self._task = asyncio.ensure_future(self._loop())
        return self._task
