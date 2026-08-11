from __future__ import annotations

import json
from pathlib import Path

from ..models import ExportData


def export_json(data: ExportData, path: Path) -> None:
    path.write_text(json.dumps(data.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

