"""NiceGUI panel for the semantic pattern editor."""

from __future__ import annotations

import asyncio
import html
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from nicegui import app, events, ui

from .core import SemanticPatternEditor


class SemanticEditorPanel:
    """Small, isolated GUI surface for semantic JSON edits."""

    def __init__(self, work_dir: Optional[Path] = None) -> None:
        self.editor = SemanticPatternEditor(work_dir=work_dir)
        self.executor = ThreadPoolExecutor(1)
        self.local_3d_path = self.editor.work_dir / "semantic_3d"
        self.local_3d_path.mkdir(parents=True, exist_ok=True)
        self.static_3d_route = f"/semantic_geo_{self.editor.work_dir.name}"
        app.add_static_files(self.static_3d_route, self.local_3d_path)
        self.original_svg = None
        self.edited_svg = None
        self.command_input = None
        self.report_area = None
        self.error_label = None
        self.sim_label = None
        self.scene = None
        self.garment_3d = None

    def release(self) -> None:
        self.editor.release()
        self.executor.shutdown(wait=False)

    def build(self) -> None:
        with ui.column(wrap=False).classes("w-full h-full p-2 m-0 gap-2"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.upload(
                    label="Upload semantic pattern JSON",
                    on_upload=self._handle_upload,
                    auto_upload=True,
                ).props('accept=".json"').classes("max-w-sm")
                ui.button("Reset", on_click=self._reset).props("outline")
                ui.button("Download edited JSON", on_click=self._download_json).props("outline")
                ui.button("Run Simulation on Edited Pattern", on_click=self._run_simulation).props("outline")

            with ui.row().classes("w-full items-end gap-2"):
                self.command_input = ui.textarea(
                    label="Semantic edit command",
                    placeholder="Make the shoulder edge and armhole curve meet at 90 degrees",
                ).props("outlined autogrow").classes("grow")
                ui.button("Apply", on_click=self._apply_command).classes("mb-1")

            with ui.row().classes("w-full gap-2"):
                ui.button(
                    "SH-AH 90",
                    on_click=lambda: self.command_input.set_value("让前片肩线和袖窿连接端保持90度"),
                ).props("flat dense")
                ui.button(
                    "SS-AH 90",
                    on_click=lambda: self.command_input.set_value("让袖窿和侧缝连接处成90度"),
                ).props("flat dense")
                ui.button(
                    "Side Dart",
                    on_click=lambda: self.command_input.set_value("在前片侧缝距下摆12cm处插入宽2cm深10cm的省道"),
                ).props("flat dense")
                ui.button(
                    "Remove Sleeves",
                    on_click=lambda: self.command_input.set_value("Remove all sleeves"),
                ).props("flat dense")

            self.error_label = ui.label("").classes("text-red-600 font-semibold")
            self.sim_label = ui.label("").classes("text-stone-600")
            self.report_area = ui.html("").classes("w-full text-sm")

            with ui.row(wrap=False).classes("w-full grow gap-3 overflow-hidden"):
                with ui.column().classes("w-1/2 h-full min-h-[320px]"):
                    ui.label("Original Pattern").classes("font-semibold")
                    with ui.scroll_area().classes("w-full h-full border rounded bg-white"):
                        self.original_svg = ui.html(self._empty_svg_message()).classes("w-full p-2")
                with ui.column().classes("w-1/2 h-full min-h-[320px]"):
                    ui.label("Edited Pattern").classes("font-semibold")
                    with ui.scroll_area().classes("w-full h-full border rounded bg-white"):
                        self.edited_svg = ui.html(self._empty_svg_message()).classes("w-full p-2")

            with ui.expansion("Edited 3D Drape", value=False).classes("w-full"):
                with ui.scene(width=900, height=360, grid=False, background_color="#ffffff").classes("w-full h-[360px]") as self.scene:
                    self._create_lights()
                    self.scene.stl("/body/mean_all.stl").rotate(3.14159 / 2, 0.0, 0.0).material(color="#000000")

    @staticmethod
    def _empty_svg_message() -> str:
        return '<div style="color:#777;padding:16px;">Upload a semantic pattern JSON to preview it.</div>'

    async def _handle_upload(self, e: events.UploadEventArguments) -> None:
        try:
            self.editor.load_bytes(e.content.read(), e.name)
            self._set_error("")
            self.sim_label.set_text("")
            self._refresh_svgs()
            self._set_report([{"message": f"Loaded {e.name}"}], [])
            ui.notify(f"Loaded {e.name}", type="positive")
        except BaseException as exc:
            traceback.print_exc()
            self._set_error(str(exc))
            ui.notify(str(exc), type="negative", close_button=True)

    def _reset(self) -> None:
        try:
            self.editor.reset_to_original()
            self._set_error("")
            self.sim_label.set_text("")
            self._refresh_svgs()
            self._set_report([], [])
        except BaseException as exc:
            self._set_error(str(exc))

    def _download_json(self) -> None:
        try:
            path = self.editor.save_current_json()
            ui.download(path, path.name)
        except BaseException as exc:
            self._set_error(str(exc))
            ui.notify(str(exc), type="negative", close_button=True)

    async def _apply_command(self) -> None:
        try:
            result = self.editor.apply_text_command(self.command_input.value or "")
            self._set_error("")
            self.sim_label.set_text("")
            self._refresh_svgs()
            self._set_report(result.report, result.warnings)
            ui.notify("Semantic edit applied", type="positive")
        except BaseException as exc:
            traceback.print_exc()
            self._set_error(str(exc))
            ui.notify(str(exc), type="negative", close_button=True)

    async def _run_simulation(self) -> None:
        if not self.editor.has_pattern:
            self._set_error("Upload a semantic pattern JSON before simulation")
            return
        self.sim_label.set_text("Simulation running...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(self.executor, self.editor.run_simulation)
        if result.success:
            self.sim_label.set_text(f"Simulation completed: {result.glb_path or result.out_dir}")
            if result.glb_path:
                self._show_simulation_result(result.glb_path)
            ui.notify("Simulation completed", type="positive")
        else:
            self.sim_label.set_text(f"Simulation failed: {result.message}")
            ui.notify("Simulation failed; see panel message", type="negative", close_button=True)

    def _refresh_svgs(self) -> None:
        original = self.editor.render_svg("original") if self.editor.original_spec else self._empty_svg_message()
        edited = self.editor.render_svg("current") if self.editor.current_spec else self._empty_svg_message()
        self.original_svg.set_content(self._fit_svg(original))
        self.edited_svg.set_content(self._fit_svg(edited))

    def _set_error(self, message: str) -> None:
        self.error_label.set_text(message)

    def _create_lights(self) -> None:
        positions = [
            [1.6, 1.2, 1.5],
            [1.3, -2.5, 1.9],
            [-2.8, 2.3, 1.3],
            [0.2, 3.5, 1.8],
        ]
        for pos in positions:
            self.scene.spot_light(color="#ffffff", intensity=45.0, angle=3.14159).move(pos[0], pos[1], pos[2])

    def _show_simulation_result(self, glb_path: Path) -> None:
        if self.scene is None:
            return
        if self.garment_3d is not None:
            self.garment_3d.delete()
            self.garment_3d = None
        target = self.local_3d_path / glb_path.name
        shutil.copy2(glb_path, target)
        route = f"{self.static_3d_route.lstrip('/')}/{target.name}"
        with self.scene:
            self.garment_3d = self.scene.gltf(route).scale(0.01).rotate(3.14159 / 2, 0.0, 0.0)

    @staticmethod
    def _fit_svg(content: str) -> str:
        if "<svg" not in content:
            return content
        return (
            '<div style="width:100%;">'
            '<style>.semantic-pattern-preview svg{max-width:100%;height:auto;}</style>'
            f'<div class="semantic-pattern-preview">{content}</div>'
            "</div>"
        )

    def _set_report(self, report, warnings) -> None:
        chunks = []
        for item in report:
            if "before_angle_deg" in item:
                chunks.append(
                    "<div>"
                    f"{html.escape(str(item.get('panel_label', item.get('panel'))))}: "
                    f"{html.escape(str(item['line_edge']))}-{html.escape(str(item['curve_edge']))} angle "
                    f"{item['before_angle_deg']:.1f}&deg; -&gt; {item['after_angle_deg']:.1f}&deg;"
                    "</div>"
                )
            elif item.get("operation") == "insert_edge_dart":
                chunks.append(
                    "<div>"
                    f"{html.escape(str(item.get('panel_label', item.get('panel'))))}: inserted "
                    f"{html.escape(str(item['dart_id']))} on {html.escape(str(item['base_edge']))}, "
                    f"width={item['dart_width_cm']}cm, depth={item['dart_depth_cm']}cm"
                    "</div>"
                )
            elif item.get("operation") == "remove_edge_dart":
                selector = (
                    f"index={int(item['dart_index'])}"
                    if item.get("dart_index") is not None
                    else f"distance={item.get('distance_from_reference_cm', 0):.2f}cm"
                )
                chunks.append(
                    "<div>"
                    f"{html.escape(str(item.get('panel_label', item.get('panel'))))}: removed "
                    f"{html.escape(str(item['dart_id']))} on {html.escape(str(item['base_edge']))}, "
                    f"{html.escape(selector)}"
                    "</div>"
                )
            elif item.get("operation") == "remove_component":
                removed = ", ".join(item.get("removed_panels", [])) or "none"
                chunks.append(
                    "<div>"
                    f"removed {html.escape(str(item.get('side', 'all')))} "
                    f"{html.escape(str(item.get('component', 'component')))}: "
                    f"{html.escape(removed)}"
                    "</div>"
                )
            elif "message" in item:
                chunks.append(f"<div>{html.escape(str(item['message']))}</div>")
        for warning in warnings:
            chunks.append(f'<div style="color:#8a5a00;">Warning: {html.escape(warning)}</div>')
        self.report_area.set_content("".join(chunks))
