"""Per-turn record of untrusted content returned to a model."""
from dataclasses import dataclass, field


@dataclass
class TurnTaint:
    tainted: bool = False
    sources: list[str] = field(default_factory=list)

    def mark(self, source: str) -> None:
        self.tainted = True
        if source and source not in self.sources:
            self.sources.append(source)

    def reset(self) -> None:
        self.tainted = False
        self.sources.clear()

    @property
    def reason(self) -> str:
        return ", ".join(self.sources) or "untrusted content"
