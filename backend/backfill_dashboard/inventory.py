from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class MachineInventoryProvider(Protocol):
    def list_machine_ids(self) -> list[str]:
        ...


@dataclass(frozen=True)
class FileMachineInventoryProvider:
    path: Path

    def list_machine_ids(self) -> list[str]:
        if not self.path.exists():
            raise FileNotFoundError(f"Machine inventory file not found: {self.path}")
        machine_ids = []
        seen = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            machine_id = line.strip()
            if not machine_id or machine_id.startswith("#") or machine_id in seen:
                continue
            machine_ids.append(machine_id)
            seen.add(machine_id)
        if not machine_ids:
            raise ValueError(f"Machine inventory file is empty: {self.path}")
        return machine_ids

