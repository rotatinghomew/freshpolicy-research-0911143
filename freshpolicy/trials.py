"""试算记录的不可变保存：完整规格、报告与内容校验，与研究备注同样不可覆盖。"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cases import canonical


class TrialStore:
    def __init__(self, directory):
        self.directory = Path(directory) / "trials"
        self.directory.mkdir(parents=True, exist_ok=True)

    def create(self, catalog, spec, report):
        payload = {
            "id": uuid.uuid4().hex,
            "label": spec.get("label") or "未命名试算",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": catalog.fingerprint,
            "spec": spec,
            "report": report,
        }
        envelope = {"content": payload, "sha256": hashlib.sha256(canonical(payload)).hexdigest()}
        with (self.directory / (payload["id"] + ".json")).open("xb") as target:
            target.write(canonical(envelope))
        return payload

    def read(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise KeyError(identifier)
        try:
            envelope = json.loads((self.directory / (identifier + ".json")).read_bytes())
        except FileNotFoundError as error:
            raise KeyError(identifier) from error
        content = envelope["content"]
        if hashlib.sha256(canonical(content)).hexdigest() != envelope["sha256"]:
            raise ValueError("试算记录完整性校验失败")
        return content

    def list(self):
        return [{field: record[field] for field in ("id", "label", "created_at", "source_fingerprint")}
                for record in (self.read(path.stem) for path in sorted(self.directory.glob("*.json")))]
