"""Local artifact storage for platform review runs."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any


class ArtifactStore:
    """Persist per-run artifacts below a configurable root directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save_review_artifacts(self, run_id: str, result: Any) -> dict[str, str]:
        """Write available review artifacts and return DB-safe relative paths."""
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}

        review_response = getattr(result, "review_response", None)
        if review_response is not None:
            paths["review_response_path"] = self._write_json(
                run_id,
                "review_response.json",
                review_response,
            )

        run_summary = getattr(result, "run_summary", None)
        if run_summary is not None:
            paths["run_summary_path"] = self._write_json(
                run_id,
                "run_summary.json",
                run_summary,
            )

        publish_result = getattr(result, "publish_result", None)
        if publish_result is not None:
            paths["publish_result_path"] = self._write_json(
                run_id,
                "publish_result.json",
                publish_result,
            )

        diff_text = getattr(result, "diff_text", None)
        if diff_text:
            self._write_text(run_id, "pr.diff", str(diff_text))

        changed_lines = getattr(result, "changed_lines", None)
        if changed_lines is not None:
            self._write_json(run_id, "changed_lines.json", changed_lines)

        for event_log_path in getattr(result, "event_log_paths", []) or []:
            self.copy_event_log(run_id, event_log_path)

        return paths

    def save_pipeline_result(self, run_id: str, result: Any) -> str:
        return self._write_json(run_id, "pipeline_result.json", result)

    def load_pipeline_result(self, relative_path: str) -> dict[str, Any]:
        path = self.root / relative_path
        digest = path.with_name(path.name + ".sha256")
        expected = digest.read_text(encoding="ascii").strip()
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("pipeline result artifact digest mismatch")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("pipeline result artifact must be an object")
        return payload

    def metadata_for_run(self, run_id: str) -> dict[str, str]:
        """Return known artifact paths without reading large artifact bodies."""
        run_dir = self.root / run_id
        names = [
            "review_response.json",
            "run_summary.json",
            "publish_result.json",
            "pr.diff",
            "changed_lines.json",
            "pipeline_result.json",
        ]
        metadata = {
            name: self._relative(run_id, name)
            for name in names
            if (run_dir / name).exists()
        }
        event_dir = run_dir / "event_logs"
        if event_dir.exists():
            for path in event_dir.glob("*.jsonl"):
                metadata[f"event_logs/{path.name}"] = self._relative(
                    run_id,
                    f"event_logs/{path.name}",
                )
        return metadata

    def copy_event_log(self, run_id: str, source: str | Path) -> str:
        src = Path(source)
        if not src.exists():
            return ""
        dest_name = src.name
        return self._write_text(
            run_id, f"event_logs/{dest_name}", src.read_text(encoding="utf-8")
        )

    def _write_json(self, run_id: str, name: str, payload: Any) -> str:
        text = json.dumps(_jsonable(payload), ensure_ascii=True, indent=2, sort_keys=True)
        return self._write_text(run_id, name, text + "\n")

    def _write_text(self, run_id: str, name: str, text: str) -> str:
        path = self.root / run_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = text.encode("utf-8")
        temp = path.with_name(f".{path.name}.{hashlib.sha256(encoded).hexdigest()[:12]}.tmp")
        temp.write_bytes(encoded)
        temp.replace(path)
        digest_path = path.with_name(path.name + ".sha256")
        digest_text = hashlib.sha256(encoded).hexdigest() + "\n"
        digest_temp = digest_path.with_name(f".{digest_path.name}.tmp")
        digest_temp.write_text(digest_text, encoding="ascii")
        digest_temp.replace(digest_path)
        return self._relative(run_id, name)

    @staticmethod
    def _relative(run_id: str, name: str) -> str:
        return f"{run_id}/{name}".replace("\\", "/")


def _jsonable(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, dict):
        return {str(key): _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_jsonable(value) for value in payload]
    if isinstance(payload, tuple):
        return [_jsonable(value) for value in payload]
    return payload
