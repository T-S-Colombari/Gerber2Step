import sys
import threading
import warnings
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from gerbonara import GerberFile, ExcellonFile
from gerbonara import graphic_primitives as primitives
from gerbonara.utils import MM
from shapely.affinity import scale
from shapely.geometry import Point, LineString, Polygon, MultiPolygon
from shapely.ops import unary_union
from build123d import Face, Wire, Compound, extrude, export_step

# To create the standalone executable with PyInstaller, run in the cmd with the virtual environment active:
# pyinstaller --noconsole --onefile --clean --name Gerber2Step --collect-all OCP --collect-all build123d --collect-all lib3mf --collect-all gerbonara gerber2step.py

# Silence harmless KiCad G90 Excellon header warnings
warnings.filterwarnings("ignore", category=SyntaxWarning)

# Ensure config.txt stays next to the .exe when packaged with PyInstaller
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_FILE = APP_DIR / "config.txt"

DEFAULTS = {
    "gerber_dir": "",
    "output_dir": "",
    "output_name": "pcb_output.step",
    "split_pcb": "1",
    "board_thickness": "2.0",
    "top_trench": "0.3",
    "bottom_trench": "0.2",
    "hole_tolerance": "0.0",
    "arc_segments": "8",
    "generate_stamps": "1",
    "stamp_exclude_holes": "1",
    "stamp_base_thickness": "0.8",
    "stamp_height": "1.0",
    "stamp_xy_clearance": "0.15",
}


# ==========================================
# 1. GERBER / EXCELLON -> 2D SHAPELY POLYGONS
# ==========================================
def find_file(folder: Path, pattern: str) -> Path | None:
    matches = list(folder.glob(pattern))
    return matches[0] if matches else None


def layer_to_shapely(layer, extra_radius: float = 0.0, quad_segs: int = 8):
    """Convert all dark-polarity objects in a Gerber/Excellon layer into a merged 2D Shapely geometry."""
    polys = []
    arc_tol = max(0.005, 0.16 / max(quad_segs, 1))

    for obj in layer.objects:
        if not obj.polarity_dark:
            continue
        for prim in obj.to_primitives(unit=MM):
            poly = None

            if isinstance(prim, primitives.Circle):
                poly = Point(prim.x, prim.y).buffer(prim.r + extra_radius, quad_segs=quad_segs)

            elif isinstance(prim, primitives.Line):
                poly = LineString([(prim.x1, prim.y1), (prim.x2, prim.y2)]).buffer(
                    (prim.width / 2) + extra_radius, cap_style="round", quad_segs=quad_segs
                )

            else:
                arc_poly = prim.to_arc_poly()
                pts = arc_poly.approximate(arc_tol) if hasattr(arc_poly, "approximate") else arc_poly.outline
                if hasattr(pts, "outline"):
                    pts = pts.outline
                if len(pts) >= 3:
                    poly = Polygon(pts).buffer(0)
                    if extra_radius > 0:
                        poly = poly.buffer(extra_radius, quad_segs=quad_segs)

            if poly is not None and not poly.is_empty:
                polys.append(poly)

    return unary_union(polys) if polys else None


def extract_board_outline(edge_layer, quad_segs: int = 8) -> Polygon:
    """Convert Edge.Cuts segments into a filled 2D board Polygon (supporting internal cutouts)."""
    edge_geom = layer_to_shapely(edge_layer, quad_segs=quad_segs)
    if edge_geom is None or edge_geom.is_empty:
        raise RuntimeError("No geometry found in Edge.Cuts!")

    polys = list(edge_geom.geoms) if isinstance(edge_geom, MultiPolygon) else [edge_geom]
    filled = sorted([Polygon(p.exterior).buffer(0) for p in polys], key=lambda p: p.area, reverse=True)

    board_2d = filled[0]
    for cutout in filled[1:]:
        board_2d = board_2d.difference(cutout)
    return board_2d


def mirror_x(geom, origin):
    """Mirror a 2D Shapely geometry horizontally around an origin point."""
    if geom is None or geom.is_empty:
        return None
    return scale(geom, xfact=-1.0, yfact=1.0, origin=origin)


# ==========================================
# 2. 2D SHAPELY POLYGONS -> 3D BUILD123D SOLIDS
# ==========================================
def shapely_to_faces(geom, z_height: float = 0.0) -> list[Face]:
    """Convert a Shapely Polygon or MultiPolygon (including interior rings) into build123d Faces at Z."""
    if geom is None or geom.is_empty:
        return []

    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    faces = []

    for poly in polys:
        if not isinstance(poly, Polygon) or poly.is_empty:
            continue

        outer_pts = [(x, y, z_height) for x, y in poly.exterior.coords]
        outer_wire = Wire.make_polygon(outer_pts)

        inner_wires = [
            Wire.make_polygon([(x, y, z_height) for x, y in interior.coords])
            for interior in poly.interiors
        ]

        faces.append(Face(outer_wire, inner_wires))

    return faces


def extrude_shapely(geom, z_start: float, height: float) -> Compound | None:
    """Extrude a 2D Shapely geometry starting at z_start by height (can be negative)."""
    faces = shapely_to_faces(geom, z_height=z_start)
    if not faces:
        return None
    direction = (0, 0, 1) if height >= 0 else (0, 0, -1)
    solids = [extrude(face, amount=abs(height), dir=direction) for face in faces]
    return Compound(solids)


def build_pcb_half(
    board_2d: Polygon,
    copper_2d,
    holes_2d,
    half_thickness: float,
    trench_depth: float,
    mirror: bool = False,
) -> Compound:
    """Build one half of a split PCB with flat glue side at Z=0 and trenches facing up."""
    origin = board_2d.centroid

    half_board = mirror_x(board_2d, origin) if mirror else board_2d
    half_copper = mirror_x(copper_2d, origin) if mirror else copper_2d
    half_holes = mirror_x(holes_2d, origin) if mirror else holes_2d

    solid = extrude_shapely(half_board, z_start=0.0, height=half_thickness)

    if half_copper is not None and not half_copper.is_empty and trench_depth > 0:
        trench_3d = extrude_shapely(half_copper, z_start=half_thickness, height=-trench_depth)
        if trench_3d:
            solid = solid - trench_3d

    if half_holes is not None and not half_holes.is_empty:
        holes_3d = extrude_shapely(half_holes, z_start=-0.1, height=half_thickness + 0.2)
        if holes_3d:
            solid = solid - holes_3d

    return solid


def build_press_stamp(
    board_2d: Polygon,
    copper_2d,
    holes_2d,
    base_thickness: float,
    feature_height: float,
    xy_clearance: float,
    quad_segs: int,
    mirror: bool = False,
) -> Compound:
    """Build a positive press stamp with raised traces (and optional hole pins) on a solid base."""
    origin = board_2d.centroid

    stamp_board = mirror_x(board_2d, origin) if mirror else board_2d
    stamp_copper = mirror_x(copper_2d, origin) if mirror else copper_2d
    stamp_holes = mirror_x(holes_2d, origin) if mirror else holes_2d

    # Combine trenches and holes (if included) into one 2D positive pattern
    parts = [g for g in (stamp_copper, stamp_holes) if g is not None and not g.is_empty]
    positive_2d = unary_union(parts) if parts else None

    # Shrink slightly inward to leave room for copper tape thickness and FDM tolerances
    if positive_2d is not None and xy_clearance > 0:
        positive_2d = positive_2d.buffer(-xy_clearance, quad_segs=quad_segs)

    # Clip to board outline so nothing overhangs the stamp base
    if positive_2d is not None and not positive_2d.is_empty:
        positive_2d = positive_2d.intersection(stamp_board)

    base_3d = extrude_shapely(stamp_board, z_start=0.0, height=base_thickness)
    if positive_2d is not None and not positive_2d.is_empty and feature_height > 0:
        features_3d = extrude_shapely(positive_2d, z_start=base_thickness, height=feature_height)
        if features_3d:
            return base_3d + features_3d

    return base_3d


# ==========================================
# 3. CORE CONVERSION PIPELINE
# ==========================================
def run_conversion(
    gerber_dir: Path,
    output_step: Path,
    split_pcb: bool,
    board_thickness: float,
    top_trench: float,
    bottom_trench: float,
    hole_tolerance: float,
    arc_segments: int,
    generate_stamps: bool,
    stamp_exclude_holes: bool,
    stamp_base_thickness: float,
    stamp_height: float,
    stamp_xy_clearance: float,
    log=print,
):
    log(f"Loading KiCad files from: {gerber_dir}")
    log(f"Arc resolution: {arc_segments} segs/90° ({arc_segments * 4}-sided circles)")

    edge_path = find_file(gerber_dir, "*Edge_Cuts.gbr")
    f_cu_path = find_file(gerber_dir, "*F_Cu.gbr")
    b_cu_path = find_file(gerber_dir, "*B_Cu.gbr")
    drl_paths = list(gerber_dir.glob("*.drl"))

    if not edge_path:
        raise RuntimeError("No *Edge_Cuts.gbr file found in the selected folder!")

    # --- Parse 2D Layers ---
    edge_layer = GerberFile.open(edge_path)
    board_2d = extract_board_outline(edge_layer, quad_segs=arc_segments)

    f_cu_2d = None
    b_cu_2d = None
    holes_2d = None

    if f_cu_path and top_trench > 0:
        f_cu_2d = layer_to_shapely(GerberFile.open(f_cu_path), quad_segs=arc_segments)
    elif not f_cu_path:
        log("F.Cu not found — skipping top trenches.")

    if b_cu_path and bottom_trench > 0:
        b_cu_2d = layer_to_shapely(GerberFile.open(b_cu_path), quad_segs=arc_segments)
    elif not b_cu_path:
        log("B.Cu not found — skipping bottom trenches.")

    if drl_paths:
        log(f"Loading drill holes (+{hole_tolerance} mm dia tolerance)...")
        drill_geoms = [
            layer_to_shapely(ExcellonFile.open(p), extra_radius=hole_tolerance / 2, quad_segs=arc_segments)
            for p in drl_paths
        ]
        valid_drills = [g for g in drill_geoms if g is not None and not g.is_empty]
        if valid_drills:
            holes_2d = unary_union(valid_drills)
    else:
        log("No .drl file found — skipping drill holes.")

    output_step.parent.mkdir(parents=True, exist_ok=True)
    stem = output_step.stem
    suffix = output_step.suffix
    exported_files = []

    # --- Build & Export PCB ---
    if split_pcb:
        half_thickness = board_thickness / 2.0
        log(f"Split mode: building two {half_thickness:.2f} mm halves ({board_2d.area:.1f} mm²)...")

        if f_cu_2d is not None:
            top_path = output_step.with_name(f"{stem}_top{suffix}")
            log(f"Building Top Half — F.Cu (trench={top_trench} mm, trenches facing up)...")
            top_half_3d = build_pcb_half(
                board_2d=board_2d,
                copper_2d=f_cu_2d,
                holes_2d=holes_2d,
                half_thickness=half_thickness,
                trench_depth=top_trench,
                mirror=False,
            )
            log(f"Exporting: {top_path.name}")
            export_step(top_half_3d, str(top_path))
            exported_files.append(top_path.name)

        if b_cu_2d is not None:
            bot_path = output_step.with_name(f"{stem}_bottom{suffix}")
            log(f"Building Bottom Half — B.Cu (mirrored, trench={bottom_trench} mm, trenches facing up)...")
            bot_half_3d = build_pcb_half(
                board_2d=board_2d,
                copper_2d=b_cu_2d,
                holes_2d=holes_2d,
                half_thickness=half_thickness,
                trench_depth=bottom_trench,
                mirror=True,
            )
            log(f"Exporting: {bot_path.name}")
            export_step(bot_half_3d, str(bot_path))
            exported_files.append(bot_path.name)

        if not exported_files:
            raise RuntimeError("No F.Cu or B.Cu copper layers found to generate split halves.")
    else:
        log(f"Single-solid mode: extruding board ({board_2d.area:.1f} mm², thickness={board_thickness} mm)...")
        board_3d = extrude_shapely(board_2d, z_start=0.0, height=board_thickness)

        if f_cu_2d is not None:
            log(f"Cutting F.Cu trenches ({top_trench} mm from top)...")
            f_cu_3d = extrude_shapely(f_cu_2d, z_start=board_thickness, height=-top_trench)
            if f_cu_3d:
                board_3d = board_3d - f_cu_3d

        if b_cu_2d is not None:
            log(f"Cutting B.Cu trenches ({bottom_trench} mm from bottom)...")
            b_cu_3d = extrude_shapely(b_cu_2d, z_start=0.0, height=bottom_trench)
            if b_cu_3d:
                board_3d = board_3d - b_cu_3d

        if holes_2d is not None:
            holes_3d = extrude_shapely(holes_2d, z_start=-0.1, height=board_thickness + 0.2)
            if holes_3d:
                board_3d = board_3d - holes_3d

        log(f"Exporting PCB STEP to: {output_step.name}")
        export_step(board_3d, str(output_step))
        exported_files.append(output_step.name)

    # --- Export Positive Press Stamps ---
    if generate_stamps:
        stamp_holes = None if stamp_exclude_holes else holes_2d
        holes_note = ", no holes" if stamp_exclude_holes else ""

        if f_cu_2d is not None and top_trench > 0:
            top_stamp_path = output_step.with_name(f"{stem}_top_stamp{suffix}")
            log(f"Generating Top Press Stamp (mirrored, height={stamp_height} mm, clearance={stamp_xy_clearance} mm{holes_note})...")
            top_stamp_3d = build_press_stamp(
                board_2d=board_2d,
                copper_2d=f_cu_2d,
                holes_2d=stamp_holes,
                base_thickness=stamp_base_thickness,
                feature_height=stamp_height,
                xy_clearance=stamp_xy_clearance,
                quad_segs=arc_segments,
                mirror=True,
            )
            export_step(top_stamp_3d, str(top_stamp_path))
            exported_files.append(top_stamp_path.name)

        if b_cu_2d is not None and bottom_trench > 0:
            bot_stamp_path = output_step.with_name(f"{stem}_bottom_stamp{suffix}")
            log(f"Generating Bottom Press Stamp (height={stamp_height} mm, clearance={stamp_xy_clearance} mm{holes_note})...")
            bot_stamp_3d = build_press_stamp(
                board_2d=board_2d,
                copper_2d=b_cu_2d,
                holes_2d=stamp_holes,
                base_thickness=stamp_base_thickness,
                feature_height=stamp_height,
                xy_clearance=stamp_xy_clearance,
                quad_segs=arc_segments,
                mirror=False,
            )
            export_step(bot_stamp_3d, str(bot_stamp_path))
            exported_files.append(bot_stamp_path.name)

    log("Done!")
    return exported_files


# ==========================================
# 4. CONFIG FILE (config.txt)
# ==========================================
def load_config() -> dict:
    cfg = DEFAULTS.copy()
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                if key in cfg:
                    cfg[key] = val.strip()
    return cfg


def save_config(cfg: dict):
    lines = [f"{k}={v}" for k, v in cfg.items()]
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ==========================================
# 5. TKINTER GUI
# ==========================================
class Gerber2StepApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Gerber2Step — 3D Printed PCB Generator")
        self.root.resizable(False, False)

        cfg = load_config()
        bool_keys = {"split_pcb", "generate_stamps", "stamp_exclude_holes"}
        self.vars = {k: tk.StringVar(value=v) for k, v in cfg.items() if k not in bool_keys}
        self.split_pcb_var = tk.BooleanVar(value=cfg.get("split_pcb", "1") == "1")
        self.generate_stamps_var = tk.BooleanVar(value=cfg.get("generate_stamps", "1") == "1")
        self.stamp_exclude_holes_var = tk.BooleanVar(value=cfg.get("stamp_exclude_holes", "1") == "1")

        main_frame = ttk.Frame(root, padding=14)
        main_frame.grid(row=0, column=0, sticky="nsew")

        # --- Folder & File Paths ---
        path_frame = ttk.LabelFrame(main_frame, text="Folders & Output", padding=10)
        path_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self._add_folder_row(path_frame, 0, "Gerber Folder:", self.vars["gerber_dir"])
        self._add_folder_row(path_frame, 1, "Output Folder:", self.vars["output_dir"])

        ttk.Label(path_frame, text="Output Filename:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(path_frame, textvariable=self.vars["output_name"], width=42).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4
        )

        # --- PCB Parameters ---
        param_frame = ttk.LabelFrame(main_frame, text="PCB Parameters", padding=10)
        param_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        ttk.Checkbutton(
            param_frame,
            text="Split PCB into Top & Bottom Halves (No Bridging)",
            variable=self.split_pcb_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        params = [
            ("Total Board Thickness (mm):", "board_thickness"),
            ("Top Trench Depth — F.Cu (mm):", "top_trench"),
            ("Bottom Trench Depth — B.Cu (mm):", "bottom_trench"),
            ("Hole Dia. Tolerance (mm):", "hole_tolerance"),
            ("Arc Segments (per 90°):", "arc_segments"),
        ]
        for idx, (label, key) in enumerate(params, start=1):
            ttk.Label(param_frame, text=label).grid(row=idx, column=0, sticky="w", pady=3)
            ttk.Entry(param_frame, textvariable=self.vars[key], width=12).grid(
                row=idx, column=1, sticky="w", padx=(12, 0), pady=3
            )

        # --- Positive Press Stamp Parameters ---
        stamp_frame = ttk.LabelFrame(main_frame, text="Positive Press Stamps", padding=10)
        stamp_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))

        ttk.Checkbutton(
            stamp_frame,
            text="Generate Top & Bottom Press Stamps",
            variable=self.generate_stamps_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))

        ttk.Checkbutton(
            stamp_frame,
            text="Exclude Through-Holes from Stamps",
            variable=self.stamp_exclude_holes_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        stamp_params = [
            ("Stamp Base Thickness (mm):", "stamp_base_thickness"),
            ("Stamp Extrusion Height (mm):", "stamp_height"),
            ("Stamp XY Clearance (mm):", "stamp_xy_clearance"),
        ]
        for idx, (label, key) in enumerate(stamp_params, start=2):
            ttk.Label(stamp_frame, text=label).grid(row=idx, column=0, sticky="w", pady=3)
            ttk.Entry(stamp_frame, textvariable=self.vars[key], width=12).grid(
                row=idx, column=1, sticky="w", padx=(12, 0), pady=3
            )

        # --- Action Buttons ---
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.save_btn = ttk.Button(btn_frame, text="Save as Default", command=self.on_save_defaults)
        self.save_btn.pack(side="left")

        self.run_btn = ttk.Button(btn_frame, text="Generate STEP", command=self.on_generate)
        self.run_btn.pack(side="right")

        # --- Status Log ---
        self.log_box = tk.Text(main_frame, height=8, width=62, state="disabled", font=("Consolas", 9))
        self.log_box.grid(row=4, column=0, sticky="ew")

    def _add_folder_row(self, parent, row: int, label: str, var: tk.StringVar):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=var, width=34).grid(row=row, column=1, sticky="ew", padx=(8, 6), pady=4)
        ttk.Button(parent, text="Browse...", command=lambda: self._browse_folder(var)).grid(
            row=row, column=2, pady=4
        )

    def _browse_folder(self, var: tk.StringVar):
        initial = var.get() if var.get() and Path(var.get()).exists() else None
        chosen = filedialog.askdirectory(initialdir=initial)
        if chosen:
            var.set(chosen)

    def log(self, message: str):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.root.after(0, _append)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def on_save_defaults(self):
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        cfg["split_pcb"] = "1" if self.split_pcb_var.get() else "0"
        cfg["generate_stamps"] = "1" if self.generate_stamps_var.get() else "0"
        cfg["stamp_exclude_holes"] = "1" if self.stamp_exclude_holes_var.get() else "0"
        save_config(cfg)
        self.log(f"Defaults saved to {CONFIG_FILE.name}")

    def on_generate(self):
        gerber_str = self.vars["gerber_dir"].get().strip()
        output_str = self.vars["output_dir"].get().strip()
        output_name = self.vars["output_name"].get().strip() or "pcb_output.step"

        if not output_name.lower().endswith((".step", ".stp")):
            output_name += ".step"

        if not gerber_str or not Path(gerber_str).is_dir():
            messagebox.showerror("Invalid Folder", "Please select a valid Gerber folder.")
            return
        if not output_str:
            messagebox.showerror("Invalid Folder", "Please select an output folder.")
            return

        try:
            board_thickness = float(self.vars["board_thickness"].get())
            top_trench = float(self.vars["top_trench"].get())
            bottom_trench = float(self.vars["bottom_trench"].get())
            hole_tolerance = float(self.vars["hole_tolerance"].get())
            arc_segments = int(self.vars["arc_segments"].get())
            stamp_base_thickness = float(self.vars["stamp_base_thickness"].get())
            stamp_height = float(self.vars["stamp_height"].get())
            stamp_xy_clearance = float(self.vars["stamp_xy_clearance"].get())
            if arc_segments < 2:
                raise ValueError("Arc segments must be at least 2.")
        except ValueError:
            messagebox.showerror(
                "Invalid Parameter",
                "Check your numeric parameters (Arc Segments must be an integer >= 2).",
            )
            return

        self._clear_log()
        self.run_btn.configure(state="disabled")

        def worker():
            try:
                files = run_conversion(
                    gerber_dir=Path(gerber_str),
                    output_step=Path(output_str) / output_name,
                    split_pcb=self.split_pcb_var.get(),
                    board_thickness=board_thickness,
                    top_trench=top_trench,
                    bottom_trench=bottom_trench,
                    hole_tolerance=hole_tolerance,
                    arc_segments=arc_segments,
                    generate_stamps=self.generate_stamps_var.get(),
                    stamp_exclude_holes=self.stamp_exclude_holes_var.get(),
                    stamp_base_thickness=stamp_base_thickness,
                    stamp_height=stamp_height,
                    stamp_xy_clearance=stamp_xy_clearance,
                    log=self.log,
                )
                file_list = "\n".join(files)
                self.root.after(0, lambda: messagebox.showinfo("Success", f"Saved in {output_str}:\n\n{file_list}"))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: messagebox.showerror("Error", str(exc)))
            finally:
                self.root.after(0, lambda: self.run_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    Gerber2StepApp(root)
    root.mainloop()