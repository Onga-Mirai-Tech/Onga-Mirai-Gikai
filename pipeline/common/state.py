"""取得済みの記録（state.json）。

`state.json` は **public リポジトリ側** に置く。取得した原文そのものは
private の data リポジトリにあるが、「何をどこから取ったか」は公開して、
第三者が自分で取得すれば同じ結果に到達できる状態を保つため。

そのため URL・保存先・SHA-256・取得日時だけを持ち、本文は持たない。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))


def now_jst_iso() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class Entry:
    url: str
    path: str
    sha256: str
    bytes: int
    fetched_at: str


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_checked_at: str | None = None
        self._by_url: dict[str, Entry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.last_checked_at = raw.get("last_checked_at")
        for item in raw.get("fetched", []):
            entry = Entry(**item)
            self._by_url[entry.url] = entry

    def has(self, url: str) -> bool:
        """取得済みか。同じURLは二度取りに行かない。"""
        return url in self._by_url

    def get(self, url: str) -> Entry | None:
        return self._by_url.get(url)

    def record(self, url: str, path: Path, body: bytes, root: Path) -> Entry:
        entry = Entry(
            url=url,
            path=path.relative_to(root).as_posix(),
            sha256=sha256_hex(body),
            bytes=len(body),
            fetched_at=now_jst_iso(),
        )
        self._by_url[url] = entry
        return entry

    def save(self, touch_last_checked: bool = True) -> None:
        if touch_last_checked:
            self.last_checked_at = now_jst_iso()
        # path 順に並べる。差分が読みやすくなる。
        entries = sorted(self._by_url.values(), key=lambda e: (e.path, e.url))
        payload = {
            "last_checked_at": self.last_checked_at,
            "fetched": [asdict(e) for e in entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def __len__(self) -> int:
        return len(self._by_url)
