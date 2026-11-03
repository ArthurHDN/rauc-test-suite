"""Robot Framework listener (API v3) that makes every test auditable.

For each finished test it writes ``artifacts/<TC>/test.json`` (name, tags,
status, message, timing) and a ``SHA256SUMS`` over that test's evidence files,
and appends a line to ``audit/results.jsonl``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import audit_common as ac


class AuditListener:
    ROBOT_LISTENER_API_VERSION = 3

    def end_test(self, data, result):
        out = ac.output_dir()
        tdir = out / "artifacts" / ac.test_dir_name(result.name)
        tdir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": ac.test_dir_name(result.name).split("_")[0],
            "name": result.name,
            "suite": result.parent.full_name if result.parent else "",
            "source": str(data.source) if data.source else "",
            "tags": list(result.tags),
            "status": result.status,
            "message": result.message,
            "start": _iso(getattr(result, "start_time", None)),
            "end": _iso(getattr(result, "end_time", None)),
            "elapsed_s": round(result.elapsed_time.total_seconds(), 3),
            "artifact_dir": tdir.relative_to(out).as_posix(),
            "documentation": result.doc,
        }
        (tdir / "test.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        ac.write_sha256sums(tdir)
        audit = out / "audit"
        audit.mkdir(exist_ok=True)
        with open(audit / "results.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.astimezone()
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return str(value)
