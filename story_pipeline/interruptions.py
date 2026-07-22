"""プロセス割り込みを run 境界で識別可能にする。"""

from __future__ import annotations

from contextlib import contextmanager
import signal
import threading
from collections.abc import Iterator


class TerminationSignal(BaseException):
    """SIGTERM を安全な終了記録へ変換するための内部割り込み。"""


@contextmanager
def capture_sigterm() -> Iterator[None]:
    """メインスレッドでの実行中だけ SIGTERM を捕捉する。"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signum: int, _frame: object) -> None:
        raise TerminationSignal()

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
