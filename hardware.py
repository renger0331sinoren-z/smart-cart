"""実機アダプタ(現在は常に無効)。

REAL_HARDWARE_ENABLED を True にする前に、非常停止・通信断時停止・障害物検知・速度制限・
操作権限・電子ロックのフェイルセキュア設計を専門家が検証すること。
環境変数や画面操作では有効化できないようにしてあります。
"""

REAL_HARDWARE_ENABLED = False


class HardwareDisabled(RuntimeError):
    pass


def _guard():
    if not REAL_HARDWARE_ENABLED:
        raise HardwareDisabled("実機制御は無効です(安全検証前)。シミュレーションのみ利用できます。")


def drive(speed_kmh: float):
    _guard()


def emergency_stop():
    _guard()


def set_lock(locked: bool):
    _guard()
