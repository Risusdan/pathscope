# core/adapter/pyocd_swd.py
"""SWD transport via pyOCD + ST-Link. Attach-only: never resets the
target on connect, we are an observer."""
from typing import List, Optional

from pyocd.core.helpers import ConnectHelper

from .base import TargetAdapter, TargetInfo, TargetLostError

DBGMCU_IDCODE = 0xE0042000


class PyOCDAdapter(TargetAdapter):
    def __init__(self, target: str = "stm32f411ce"):
        self.target_name = target
        self._session = None

    def connect(self) -> TargetInfo:
        try:
            if self._session is not None:
                self.disconnect()
            self._session = ConnectHelper.session_with_chosen_probe(
                target_override=self.target_name,
                connect_mode="attach",
                blocking=False)
            if self._session is None:
                raise TargetLostError("no debug probe found")
            self._session.open()
            # DBGMCU_IDCODE is an STM32-specific debug register; a
            # non-STM32 target faults or transfer-errors reading it.
            # idcode is display-only (cli.py, ui/app.py), so a failed
            # read must not fail connect() - fall back to 0.
            try:
                idcode = self._session.target.read32(DBGMCU_IDCODE)
            except Exception:
                idcode = 0
            return TargetInfo(name=self.target_name, idcode=idcode)
        except TargetLostError:
            raise
        except Exception as e:
            self._session = None
            raise TargetLostError(str(e))

    def disconnect(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _target(self):
        if self._session is None:
            raise TargetLostError("not connected")
        return self._session.target

    def read_block32(self, addr: int, count: int) -> List[int]:
        try:
            return self._target().read_memory_block32(addr, count)
        except TargetLostError:
            raise
        except Exception as e:
            raise TargetLostError(str(e))

    def write32(self, addr: int, value: int) -> None:
        try:
            self._target().write32(addr, value)
        except TargetLostError:
            raise
        except Exception as e:
            raise TargetLostError(str(e))

    def halt(self) -> None:
        try:
            self._target().halt()
        except Exception as e:
            raise TargetLostError(str(e))

    def resume(self) -> None:
        try:
            self._target().resume()
        except Exception as e:
            raise TargetLostError(str(e))

    def is_running(self) -> bool:
        try:
            from pyocd.core.target import Target
            return self._target().get_state() == Target.State.RUNNING
        except Exception as e:
            raise TargetLostError(str(e))
