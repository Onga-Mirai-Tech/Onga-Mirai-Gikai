"""取得マナーを守るHTTPクライアント。

CLAUDE.md の「データ取得のマナー」をそのままコードにしたもの。

- 1リクエストごとに3秒以上あける
- 並列アクセスはしない（このクラスは逐次でしか使えない）
- User-Agent に運営者の問い合わせURLを含める

依存ライブラリは使わない（標準ライブラリのみ）。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass

CONTACT_URL = "https://github.com/Onga-Mirai-Tech/Onga-Mirai-Gikai"
USER_AGENT = f"onga-mirai-gikai-bot/0.1 (+{CONTACT_URL})"

MIN_INTERVAL_SEC = 3.0
TIMEOUT_SEC = 30
MAX_ATTEMPTS = 3


class FetchError(Exception):
    """取得に失敗した。リトライ後も回復しなかった場合に送出する。"""


@dataclass
class Response:
    url: str
    status: int
    headers: str
    body: bytes


class Fetcher:
    """逐次・間隔つきのフェッチャ。

    1プロセスに1つだけ作って使い回す。複数作ると間隔の保証が崩れる。
    """

    def __init__(self, min_interval: float = MIN_INTERVAL_SEC) -> None:
        self.min_interval = min_interval
        self._last_request_at: float | None = None
        self.request_count = 0

    def _wait(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str) -> Response:
        """1件取得する。呼ぶたびに必ず間隔を空ける。"""
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as res:
                    body = res.read()
                    headers = str(res.headers)
                    status = res.status
                self._last_request_at = time.monotonic()
                self.request_count += 1
                return Response(url=url, status=status, headers=headers, body=body)
            except urllib.error.HTTPError as e:
                self._last_request_at = time.monotonic()
                self.request_count += 1
                # 404 などはリトライしても変わらない。すぐ諦める。
                if e.code < 500:
                    raise FetchError(f"{e.code} {e.reason}: {url}") from e
                last_error = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self._last_request_at = time.monotonic()
                last_error = e

            if attempt < MAX_ATTEMPTS:
                # 相手は町のサーバー。失敗したときはさらに長く待つ。
                time.sleep(self.min_interval * attempt * 2)

        raise FetchError(f"{MAX_ATTEMPTS}回試しても取得できなかった: {url} ({last_error})")


def decode_cp932(body: bytes) -> str:
    """VOICES の Shift_JIS を読む。

    宣言は `shift_jis` / `Shift_JIS` / `x-sjis` と揺れているので読まない。
    会議録には機種依存文字（﨑 = U+FA11 など）が出るため、
    `shift_jis` ではなく **CP932** でデコードしないと落ちる。
    """
    return body.decode("cp932", errors="replace")
