"""GUI-facing state for the Semantic Pattern Editor."""

from __future__ import annotations

import copy
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from pygarment.pattern.wrappers import VisPattern

from .core import (
    SemanticEditError,
    SemanticPatternEditor,
    SimulationResult,
    build_semantic_index_summary,
    normalize_semantic_predictions,
    parse_semantic_command,
    run_pattern_simulation,
    _read_json_bytes,
)


SpecDict = Dict[str, Any]


@dataclass
class SemanticSimRecord:
    success: bool
    message: str
    out_dir: Optional[Path] = None
    sim_mesh_path: Optional[Path] = None
    sim_mesh_url: Optional[str] = None
    glb_path: Optional[Path] = None
    glb_url: Optional[str] = None
    render_front_path: Optional[Path] = None
    render_front_url: Optional[str] = None
    render_back_path: Optional[Path] = None
    render_back_url: Optional[str] = None
    traceback_text: Optional[str] = None

    @classmethod
    def from_result(
        cls,
        result: SimulationResult,
        sim_mesh_url: Optional[str] = None,
        glb_url: Optional[str] = None,
        render_front_url: Optional[str] = None,
        render_back_url: Optional[str] = None,
    ) -> "SemanticSimRecord":
        return cls(
            success=result.success,
            message=result.message,
            out_dir=result.out_dir,
            sim_mesh_path=result.sim_mesh_path,
            sim_mesh_url=sim_mesh_url,
            glb_path=result.glb_path,
            glb_url=glb_url,
            render_front_path=result.render_front_path,
            render_front_url=render_front_url,
            render_back_path=result.render_back_path,
            render_back_url=render_back_url,
            traceback_text=result.traceback_text,
        )


class SemanticEditState:
    """Session-scoped semantic-pattern state and generated file paths."""

    preview_route = "/semantic_preview"
    sim_original_route = "/semantic_sim_original"
    sim_edited_route = "/semantic_sim_edited"

    def __init__(self, session_id: str, root: Optional[Path] = None) -> None:
        self.session_id = session_id
        self.root = Path(root or Path.cwd() / "tmp_gui" / "semantic")
        self.upload_dir = self.root / "uploads"
        self.original_dir = self.root / "original"
        self.edited_dir = self.root / "edited"
        self.preview_dir = self.root / "preview"
        self.sim_original_dir = self.root / "sim_original"
        self.sim_edited_dir = self.root / "sim_edited"
        for directory in [
            self.upload_dir,
            self.original_dir,
            self.edited_dir,
            self.preview_dir,
            self.sim_original_dir,
            self.sim_edited_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

        self.original_spec: Optional[SpecDict] = None
        self.edited_spec: Optional[SpecDict] = None
        self.original_spec_path: Optional[Path] = None
        self.edited_spec_path: Optional[Path] = None
        self.original_svg_path: Optional[Path] = None
        self.edited_svg_path: Optional[Path] = None
        self.original_sim_result: Optional[Dict[str, Any]] = None
        self.edited_sim_result: Optional[Dict[str, Any]] = None
        self.semantic_index_summary: Dict[str, Any] = {}
        self.last_command: Optional[Dict[str, Any]] = None
        self.last_report: Optional[Dict[str, Any]] = None
        self.validation_report: Optional[Dict[str, Any]] = None
        self.active_filename: str = ""

    @property
    def has_semantic_pattern(self) -> bool:
        return self.original_spec is not None and self.edited_spec is not None

    @property
    def original_svg_url(self) -> str:
        return self._url_for(self.preview_route, self.original_svg_path)

    @property
    def edited_svg_url(self) -> str:
        return self._url_for(self.preview_route, self.edited_svg_path)

    def load_bytes(self, data: bytes, filename: str) -> None:
        spec = _read_json_bytes(data)
        normalize_semantic_predictions(spec)

        timestamp = self._timestamp()
        upload_path = self.upload_dir / f"{self.session_id}_{timestamp}_{Path(filename).name}"
        upload_path.write_bytes(data)

        self.original_spec = copy.deepcopy(spec)
        self.edited_spec = copy.deepcopy(spec)
        self.active_filename = filename
        self.semantic_index_summary = build_semantic_index_summary(self.original_spec)
        self.validation_report = self._validate_semantic_index()
        self.last_command = None
        self.last_report = None
        self.original_sim_result = None
        self.edited_sim_result = None

        self.original_spec_path = self._write_spec(self.original_spec, self.original_dir, "original")
        self.edited_spec_path = self._write_spec(self.edited_spec, self.edited_dir, "edited")
        self.original_svg_path = self._render_svg(self.original_spec, "original")
        self.edited_svg_path = self._render_svg(self.edited_spec, "edited")

    def reset_edited(self) -> None:
        if self.original_spec is None:
            raise SemanticEditError("No semantic pattern has been uploaded yet")
        self.edited_spec = copy.deepcopy(self.original_spec)
        self.last_command = None
        self.last_report = None
        self.validation_report = self._validate_semantic_index()
        self.edited_sim_result = None
        self.edited_spec_path = self._write_spec(self.edited_spec, self.edited_dir, "edited")
        self.edited_svg_path = self._render_svg(self.edited_spec, "edited")

    def apply_text_command(self, text: str) -> Dict[str, Any]:
        return self.apply_command(parse_semantic_command(text))

    def apply_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        return self.apply_commands([command])

    def apply_commands(self, commands: list[Dict[str, Any]]) -> Dict[str, Any]:
        if self.edited_spec is None:
            raise SemanticEditError("Upload a semantic pattern JSON before applying edits")

        spec = copy.deepcopy(self.edited_spec)
        combined_report = []
        combined_warnings = []
        for command in commands:
            editor = SemanticPatternEditor(work_dir=self.root / "engine")
            editor.current_spec = spec
            result = editor.apply_command(command)
            spec = result.spec
            combined_report.extend(result.report)
            combined_warnings.extend(result.warnings)

        self.edited_spec = spec
        self.last_command = copy.deepcopy(commands[0] if len(commands) == 1 else commands)
        self.last_report = {
            "report": combined_report,
            "warnings": combined_warnings,
        }
        self.validation_report = self._validate_semantic_index()
        self.edited_sim_result = None
        self.edited_spec_path = self._write_spec(self.edited_spec, self.edited_dir, "edited")
        self.edited_svg_path = self._render_svg(self.edited_spec, "edited")
        return self.last_report

    def run_simulation(self, which: str) -> Dict[str, Any]:
        if which not in {"original", "edited"}:
            raise SemanticEditError(f"Unknown semantic simulation target: {which}")
        spec = self.original_spec if which == "original" else self.edited_spec
        if spec is None:
            raise SemanticEditError("Upload a semantic pattern JSON before simulation")

        sim_dir = self.sim_original_dir if which == "original" else self.sim_edited_dir
        route = self.sim_original_route if which == "original" else self.sim_edited_route
        result = run_pattern_simulation(
            spec,
            sim_dir,
            f"{self.session_id}_{which}_{self._timestamp()}",
        )
        sim_mesh_url = None
        if result.success and result.sim_mesh_path:
            sim_mesh_url = self._url_for(route, result.sim_mesh_path)
        glb_url = None
        if result.success and result.glb_path:
            public_glb = sim_dir / f"{self.session_id}_{which}_{self._timestamp()}.glb"
            shutil.copy2(result.glb_path, public_glb)
            result.glb_path = public_glb
            glb_url = self._url_for(route, public_glb)
        render_front_url = None
        if result.success and result.render_front_path:
            render_front_url = self._url_for(route, result.render_front_path)
        render_back_url = None
        if result.success and result.render_back_path:
            render_back_url = self._url_for(route, result.render_back_path)
        record = SemanticSimRecord.from_result(
            result,
            sim_mesh_url=sim_mesh_url,
            glb_url=glb_url,
            render_front_url=render_front_url,
            render_back_url=render_back_url,
        ).__dict__
        record["ok"] = record["success"]
        record["glb"] = str(record["glb_path"]) if record["glb_path"] else None
        record["sim_mesh"] = str(record["sim_mesh_path"]) if record["sim_mesh_path"] else None
        if which == "original":
            self.original_sim_result = record
        else:
            self.edited_sim_result = record
        return record

    def release(self) -> None:
        # Keep generated files around while the server lives; browser static paths may
        # still be resolving after callbacks complete. Session ids avoid collisions.
        pass

    def _validate_semantic_index(self) -> Dict[str, Any]:
        panels = self.semantic_index_summary.get("panels", {})
        missing = [label for label in ["LFP", "RFP", "LBP", "RBP"] if label not in panels]
        return {
            "ok": not missing,
            "missing_panel_labels": missing,
            "detected_panel_labels": self.semantic_index_summary.get("detected_panel_labels", []),
        }

    def _write_spec(self, spec: SpecDict, directory: Path, role: str) -> Path:
        path = directory / f"{self.session_id}_{role}_{self._timestamp()}_specification.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
        return path

    def _render_svg(self, spec: SpecDict, role: str) -> Path:
        spec_path = self.preview_dir / f"{self.session_id}_{role}_{self._timestamp()}_specification.json"
        svg_path = self.preview_dir / f"{self.session_id}_{role}_{self._timestamp()}.svg"
        with spec_path.open("w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
        pattern = VisPattern(str(spec_path))
        dwg = pattern.get_svg(
            str(svg_path),
            with_text=False,
            view_ids=False,
            flat=True,
            fill_panels=True,
            margin=2,
        )
        dwg.save()
        return svg_path

    def _url_for(self, route: str, path: Optional[Path]) -> str:
        if path is None:
            return ""
        try:
            if route == self.preview_route:
                rel = path.relative_to(self.preview_dir)
            elif route == self.sim_original_route:
                rel = path.relative_to(self.sim_original_dir)
            else:
                rel = path.relative_to(self.sim_edited_dir)
            return f"{route}/{rel.as_posix()}?v={self._timestamp()}"
        except ValueError:
            return ""

    @staticmethod
    def _timestamp() -> str:
        return str(time.time()).replace(".", "_")
