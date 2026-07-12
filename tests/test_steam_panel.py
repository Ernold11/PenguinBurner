from ui.components.steam_panel import SteamPanel


class _RunningThread:
    def is_alive(self) -> bool:
        return True


class _Manager:
    def refresh(self, **kwargs):
        raise AssertionError("auto-sync must not race the full rescan")


def test_auto_sync_skips_while_full_rescan_is_running() -> None:
    panel = object.__new__(SteamPanel)
    panel._rescan_thread = _RunningThread()
    panel.manager = _Manager()

    panel._auto_sync()
