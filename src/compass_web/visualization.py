"""Visualization helpers: camera, bounds, scene rendering, and PyVista viewers."""

from __future__ import annotations

import colorsys
import tempfile
from pathlib import Path
from uuid import uuid4

import numpy as np
import pyvista as pv


def distinct_colors(count: int) -> list[str]:
    if count <= 0:
        return []
    return [
        "#%02x%02x%02x"
        % tuple(
            int(ch * 255) for ch in colorsys.hsv_to_rgb(i / count, 0.7, 1.0)
        )
        for i in range(count)
    ]


def camera_position_from_bounds(
    bounds: tuple[float, float, float, float, float, float],
    target: np.ndarray | list[float] | tuple[float, float, float],
) -> list[list[float]]:
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    max_span = max(xmax - xmin, ymax - ymin, zmax - zmin, 1.0)
    target_array = np.asarray(target, dtype=float)
    camera_position = target_array + np.array(
        [1.05 * max_span, -1.45 * max_span, 0.78 * max_span], dtype=float
    )
    return [camera_position.tolist(), target_array.tolist(), [0.0, 0.0, 1.0]]


def merge_bounds(
    bounds_list: list[tuple[float, float, float, float, float, float]],
) -> tuple[float, float, float, float, float, float]:
    if not bounds_list:
        raise ValueError("bounds_list must not be empty.")
    mins = np.array([[b[0], b[2], b[4]] for b in bounds_list], dtype=float)
    maxs = np.array([[b[1], b[3], b[5]] for b in bounds_list], dtype=float)
    lo = mins.min(axis=0)
    hi = maxs.max(axis=0)
    return (float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]), float(lo[2]), float(hi[2]))


def bounds_from_points(
    points: np.ndarray,
) -> tuple[float, float, float, float, float, float] | None:
    if len(points) == 0:
        return None
    pts = np.asarray(points, dtype=float)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    return (float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]), float(lo[2]), float(hi[2]))


def padded_scene_bounds(
    bounds: tuple[float, float, float, float, float, float],
    padding_fraction: float = 0.22,
    min_padding: float = 1.0,
) -> tuple[float, float, float, float, float, float]:
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    spans = np.array([xmax - xmin, ymax - ymin, zmax - zmin], dtype=float)
    pad = np.maximum(spans * padding_fraction, min_padding)
    return (
        float(xmin - pad[0]), float(xmax + pad[0]),
        float(ymin - pad[1]), float(ymax + pad[1]),
        float(zmin - pad[2]), float(zmax + pad[2]),
    )


def center_from_bounds(
    bounds: tuple[float, float, float, float, float, float],
) -> np.ndarray:
    return np.array(
        [0.5 * (bounds[0] + bounds[1]), 0.5 * (bounds[2] + bounds[3]), 0.5 * (bounds[4] + bounds[5])],
        dtype=float,
    )


def add_scene_content(
    plotter: pv.Plotter,
    *,
    meshes: list[tuple[pv.DataSet, dict[str, object]]] | None = None,
    line_meshes: list[tuple[pv.PolyData, dict[str, object]]] | None = None,
    point_sets: list[tuple[np.ndarray, dict[str, object]]] | None = None,
    label_sets: list[tuple[np.ndarray, list[str], dict[str, object]]] | None = None,
) -> None:
    for mesh, kwargs in meshes or []:
        if mesh.n_points == 0:
            continue
        plotter.add_mesh(mesh, **kwargs)
    for lm, kwargs in line_meshes or []:
        if lm.n_points == 0:
            continue
        plotter.add_mesh(lm, render_lines_as_tubes=True, **kwargs)
    for pts, kwargs in point_sets or []:
        if len(pts) == 0:
            continue
        plotter.add_points(np.asarray(pts, dtype=float), **kwargs)
    for pts, labels, kwargs in label_sets or []:
        if len(pts) == 0 or not labels:
            continue
        plotter.add_point_labels(np.asarray(pts, dtype=float), labels, **kwargs)


def render_static_scene(
    *,
    title: str,
    bounds: tuple[float, float, float, float, float, float],
    target: np.ndarray | list[float] | tuple[float, float, float],
    meshes: list[tuple[pv.DataSet, dict[str, object]]] | None = None,
    line_meshes: list[tuple[pv.PolyData, dict[str, object]]] | None = None,
    point_sets: list[tuple[np.ndarray, dict[str, object]]] | None = None,
    label_sets: list[tuple[np.ndarray, list[str], dict[str, object]]] | None = None,
    fit_bounds: tuple[float, float, float, float, float, float] | None = None,
    fit_target: np.ndarray | list[float] | tuple[float, float, float] | None = None,
    zoom_factor: float = 1.1,
    window_size: tuple[int, int] = (1100, 820),
) -> bytes:
    plotter = pv.Plotter(off_screen=True, window_size=window_size)
    plotter.set_background("#1a1a2e")
    add_scene_content(plotter, meshes=meshes, line_meshes=line_meshes, point_sets=point_sets, label_sets=label_sets)
    plotter.add_axes()
    cam_bounds = fit_bounds if fit_bounds is not None else bounds
    cam_target = np.asarray(fit_target if fit_target is not None else target, dtype=float)
    plotter.camera_position = camera_position_from_bounds(cam_bounds, cam_target)
    plotter.camera.zoom(zoom_factor)
    plotter.add_text(title, position="upper_left", font_size=12, color="white")
    image_path = Path(tempfile.gettempdir()) / f"{uuid4().hex}.png"
    try:
        plotter.screenshot(str(image_path))
        return image_path.read_bytes()
    finally:
        plotter.close()
        image_path.unlink(missing_ok=True)


def display_static_scene(**kwargs: object) -> None:
    from IPython.display import Image, display
    display(Image(data=render_static_scene(**kwargs)))


def display_interactive_scene(
    *,
    title: str,
    bounds: tuple[float, float, float, float, float, float],
    target: np.ndarray | list[float] | tuple[float, float, float],
    meshes: list[tuple[pv.DataSet, dict[str, object]]] | None = None,
    line_meshes: list[tuple[pv.PolyData, dict[str, object]]] | None = None,
    point_sets: list[tuple[np.ndarray, dict[str, object]]] | None = None,
    label_sets: list[tuple[np.ndarray, list[str], dict[str, object]]] | None = None,
    fit_bounds: tuple[float, float, float, float, float, float] | None = None,
    fit_target: np.ndarray | list[float] | tuple[float, float, float] | None = None,
    zoom_factor: float = 1.0,
    window_size: tuple[int, int] = (1200, 900),
) -> None:
    """Open a native VTK interactive viewer window."""
    plotter = pv.Plotter(notebook=False, window_size=window_size)
    plotter.set_background("#1a1a2e")
    add_scene_content(plotter, meshes=meshes, line_meshes=line_meshes, point_sets=point_sets, label_sets=label_sets)
    plotter.add_axes()
    cam_bounds = fit_bounds if fit_bounds is not None else bounds
    cam_target = np.asarray(fit_target if fit_target is not None else target, dtype=float)
    plotter.camera_position = camera_position_from_bounds(cam_bounds, cam_target)
    plotter.camera.zoom(zoom_factor)
    plotter.add_text(title, position="upper_left", font_size=12, color="white")
    plotter.show()


def show_mesh_interactive(
    mesh: pv.PolyData,
    *,
    title: str = "Voronoi Shell Preview",
    color: str = "#9b8cff",
    zoom: float = 1.8,
    window_size: tuple[int, int] = (1300, 950),
) -> None:
    """Quick interactive viewer for a single assembled mesh."""
    display_interactive_scene(
        title=title,
        bounds=mesh.bounds,
        target=mesh.center,
        meshes=[(mesh, {"color": color, "opacity": 1.0, "smooth_shading": True})],
        zoom_factor=zoom,
        window_size=window_size,
    )


def save_screenshot(
    mesh: pv.PolyData,
    path: str | Path,
    *,
    title: str = "Voronoi Shell",
    color: str = "#9b8cff",
    zoom: float = 1.8,
    window_size: tuple[int, int] = (1300, 950),
) -> Path:
    """Render a mesh to a PNG screenshot file."""
    img_bytes = render_static_scene(
        title=title,
        bounds=mesh.bounds,
        target=mesh.center,
        meshes=[(mesh, {"color": color, "opacity": 1.0, "smooth_shading": True})],
        zoom_factor=zoom,
        window_size=window_size,
    )
    out = Path(path)
    out.write_bytes(img_bytes)
    return out
