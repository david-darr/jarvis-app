"""Integer accounting only; account quota percentages are separate signals.

There is deliberately no tokens-to-subscription-percentage conversion here.
"""
from dataclasses import dataclass


def units(value: int, name: str = "units", *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < int(positive):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'} integer")
    return value


@dataclass(frozen=True)
class BudgetLimit:
    ceiling: int | None
    pause_percent: int = 80
    checkpoint_reserve: int = 0

    def __post_init__(self):
        units(self.pause_percent, "pause_percent", positive=True)
        units(self.checkpoint_reserve, "checkpoint_reserve")
        if self.ceiling is None:
            if self.checkpoint_reserve:
                raise ValueError("an uncapped allocation cannot reserve ceiling units")
            if self.pause_percent > 100:
                raise ValueError("invalid pause threshold")
            return
        units(self.ceiling, "ceiling", positive=True)
        if self.pause_percent > 100 or self.checkpoint_reserve >= self.stop_at:
            raise ValueError("invalid pause threshold or checkpoint reserve")

    @property
    def stop_at(self) -> int | None:
        if self.ceiling is None:
            return None
        return self.ceiling * self.pause_percent // 100

    @property
    def work_ceiling(self) -> int | None:
        if self.stop_at is None:
            return None
        return self.stop_at - self.checkpoint_reserve

    def admits(self, consumed: int, reserved: int, requested: int) -> bool:
        if self.work_ceiling is None:
            return True
        return consumed + reserved + requested <= self.work_ceiling

    def exhausted(self, consumed: int) -> bool:
        if self.work_ceiling is None:
            return False
        return consumed >= self.work_ceiling
