"""Model catalogue: editable list of models with multiplier and enabled flag (6.19)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

ROUTING_ALIASES = {"auto", "default", "router", "routing"}

# Appendix A: starting catalogue. (id, multiplier, group, vision)
STARTING_CATALOGUE: list[tuple[str, float, str, bool]] = [
    ("glm-5.1", 1.0, "green", False),
    ("hy3", 1.0, "green", False),
    ("kimi-k2.6", 1.0, "green", False),
    ("kimi-k2.7-code", 1.0, "green", False),
    ("deepseek-v4-pro", 1.15, "green", False),
    ("glm-5.2", 1.25, "green", False),
    ("mimo-v2.5-pro", 1.4, "green", False),
    ("minimax-m3", 1.4, "green", False),
    ("kimi-k2.7-code-highspeed", 1.5, "green", False),
    ("deepseek-v4-pro-0813", 1.8, "green", False),
    ("deepseek-v4-flash", 2.0, "green", False),
    ("deepseek-v4-flash-0731", 2.0, "green", False),
    ("glm-5.3", 2.0, "green", False),
    ("glm-5.3-flash", 2.0, "green", True),
    ("kimi-k3", 2.0, "green", True),
    ("deepseek-v4-flash-vision-exp", 2.5, "green", True),
    ("glm-5.3-flashx", 2.5, "green", True),
    ("deepseek-v4.1-flash", 2.56, "green", True),
    ("deepseek-v4-mod", 3.0, "green", False),
    ("glm-5.2-mod", 3.0, "green", False),
    ("deepseek-v4.1-mod", 3.2, "green", True),
    ("glm-5.3-mod", 3.2, "green", False),
    ("kimi-k3-mod", 3.5, "green", True),
    ("auto", 1.0, "yellow", False),
    ("gpt-5.6", 5.0, "yellow", True),
    ("gpt-5.6-luna", 5.0, "yellow", True),
    ("gpt-5.6-luna-b", 5.0, "yellow", True),
    ("gpt-5.6-terra", 10.0, "yellow", True),
    ("gpt-5.6-terra-b", 10.0, "yellow", True),
    ("gpt-5.6-sol", 15.0, "yellow", True),
    ("gpt-5.6-sol-b", 15.0, "yellow", True),
    ("gpt-5.6-sol-xhigh", 15.0, "yellow", True),
    ("hy4", 1.4, "white", False),
    ("mimo-v2.6-flash", 2.0, "white", False),
    ("mimo-v2.6-pro", 2.0, "white", True),
    ("glm-5.3-flash-mod", 3.5, "white", True),
    ("glm-5.3-flashx-mod", 4.0, "white", True),
]

INITIAL_PANEL = ["deepseek-v4.1-flash", "kimi-k3", "glm-5.3"]
INITIAL_FALLBACKS = ["deepseek-v4-pro", "minimax-m3"]


@dataclass
class ModelEntry:
    id: str
    multiplier: float = 1.0
    enabled: bool = True
    group: str = ""
    vision: bool = False
    notes: str = ""
    available: bool = True
    latency_ms: Optional[float] = None

    def as_dict(self) -> dict:
        return {"id": self.id, "multiplier": self.multiplier, "enabled": self.enabled,
                "group": self.group, "vision": self.vision, "notes": self.notes}


class ModelCatalog:
    """The catalogue file is editable by the user; changes apply without a restart."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, ModelEntry] = {}
        self.load()

    # -- persistence --------------------------------------------------------
    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                items = data.get("models", data) if isinstance(data, dict) else data
                entries: dict[str, ModelEntry] = {}
                for item in items or []:
                    e = ModelEntry(
                        id=str(item.get("id", "")).strip(),
                        multiplier=float(item.get("multiplier", 1.0)),
                        enabled=bool(item.get("enabled", True)),
                        group=str(item.get("group", "")),
                        vision=bool(item.get("vision", False)),
                        notes=str(item.get("notes", "")),
                    )
                    if e.id:
                        entries[e.id] = e
                if entries:
                    self.entries = entries
                    return
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
                log.warning("model catalogue unreadable (%s); using the starting catalogue", exc)
        self.entries = {mid: ModelEntry(id=mid, multiplier=m, group=g, vision=v)
                        for mid, m, g, v in STARTING_CATALOGUE}
        self.save()

    def save(self) -> None:
        payload = {
            "note": ("Model catalogue. multiplier is the gateway's usage-cost factor (1x-20x). "
                     "Set enabled=false to exclude a model. The system never uses a model that "
                     "is not in this file."),
            "models": [e.as_dict() for e in sorted(self.entries.values(), key=lambda x: x.id)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- access -------------------------------------------------------------
    def get(self, model_id: str) -> Optional[ModelEntry]:
        return self.entries.get(model_id)

    def add(self, model_id: str, multiplier: float, enabled: bool = True,
            notes: str = "") -> ModelEntry:
        e = ModelEntry(id=model_id, multiplier=float(multiplier), enabled=enabled, notes=notes)
        self.entries[model_id] = e
        self.save()
        return e

    def remove(self, model_id: str) -> bool:
        existed = self.entries.pop(model_id, None) is not None
        if existed:
            self.save()
        return existed

    def enable(self, model_id: str, enabled: bool = True) -> bool:
        e = self.entries.get(model_id)
        if not e:
            return False
        e.enabled = enabled
        self.save()
        return True

    def set_multiplier(self, model_id: str, multiplier: float) -> bool:
        e = self.entries.get(model_id)
        if not e:
            return False
        e.multiplier = float(multiplier)
        self.save()
        return True

    def multiplier_of(self, model_id: str, default: float = 1.0) -> float:
        e = self.entries.get(model_id)
        return e.multiplier if e else default

    # -- pools --------------------------------------------------------------
    @staticmethod
    def is_alias(model_id: str) -> bool:
        return model_id.strip().lower() in ROUTING_ALIASES

    def pool(self, max_multiplier: float = 4.0,
             premium: Optional[Iterable[str]] = None) -> list[str]:
        """Enabled catalogue models at or below the multiplier limit, no routing aliases."""
        premium = set(premium or [])
        out = []
        for mid, e in sorted(self.entries.items(), key=lambda kv: kv[1].multiplier):
            if not e.enabled or self.is_alias(mid):
                continue
            if e.multiplier <= max_multiplier or mid in premium:
                out.append(mid)
        return out

    def premium_pool(self, max_multiplier: float,
                     premium: Optional[Iterable[str]] = None) -> list[str]:
        premium = set(premium or [])
        return [mid for mid, e in self.entries.items()
                if e.enabled and not self.is_alias(mid) and e.multiplier > max_multiplier
                and mid in premium]

    def all_ids(self) -> list[str]:
        return sorted(self.entries)
