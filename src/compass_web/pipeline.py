"""End-to-end voronoi shell generation pipeline.

This module consolidates the full pipeline that was previously split across
notebook cells: loft -> clip -> voronoi -> intersect -> analyze -> build
solids -> export.  It is designed to work both from the notebook and from CLI.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pyvista as pv
import trimesh

from compass_web.config import PipelineConfig, SMALL_CELL_EXTRUSION_FACTOR
from compass_web.lofted_surface_voronoi import (
    _loft_between_polylines,
    _merge_meshes,
    align_loops_and_loft,
    analyze_and_generate_surfaces,
    build_analysis_output_meshes,
    build_bounded_voronoi_cells,
    build_lofted_surface,
    build_mesh_printability_report,
    build_polyline_mesh,
    clean_meshes_without_naked_edges,
    clip_surface_in_half,
    close_mesh_boundaries,
    count_connected_regions,
    extract_naked_edge_loops,
    extract_surface_mesh,
    intersect_cells_with_surface,
    orient_normals_outward,
    pad_bounds,
    random_points_in_bounds,
    rebuild_polylines_from_discontinuities,
    scale_points_in_xy,
    scale_polydata_in_xy,
    split_and_offset_plane_faces,
    unify_mesh_normals,
)


def polyline_point_keys(
    polyline: np.ndarray,
    tolerance: float,
) -> set[tuple[int, int, int]]:
    unique_points = polyline[:-1] if len(polyline) > 1 else polyline
    return {
        tuple(np.round(np.asarray(point, dtype=float) / tolerance).astype(int).tolist())
        for point in unique_points
    }


def filter_isolated_polylines(
    polylines: list[np.ndarray],
    tolerance: float,
) -> tuple[list[np.ndarray], list[int], list[int]]:
    """Remove polylines that share no boundary points with any other polyline."""
    if not polylines:
        return [], [], []
    point_key_sets = [
        polyline_point_keys(pl, tolerance=tolerance) for pl in polylines
    ]
    kept_indices: list[int] = []
    discarded_indices: list[int] = []
    for index, point_keys in enumerate(point_key_sets):
        has_neighbor = any(
            index != other_index and len(point_keys.intersection(other_point_keys)) > 0
            for other_index, other_point_keys in enumerate(point_key_sets)
        )
        if has_neighbor:
            kept_indices.append(index)
        else:
            discarded_indices.append(index)
    return [polylines[i] for i in kept_indices], kept_indices, discarded_indices


@dataclass
class PipelineResult:
    """Holds everything produced by a single pipeline run."""

    trimesh_result: trimesh.Trimesh
    cell_solids: list[pv.PolyData]
    generated_surface: pv.PolyData
    is_valid_volume: bool
    stats: dict


def build_export_trimesh(solids_list: list[pv.PolyData]) -> trimesh.Trimesh:
    """Convert a list of PyVista cell solids into a single trimesh, with
    outward normals and fixed winding, rotated for printing (sliced face
    on XY).
    """
    cell_tms: list[trimesh.Trimesh] = []
    for solid in solids_list:
        solid_o = orient_normals_outward(solid)
        pts = np.asarray(solid_o.points, dtype=float)
        fraw = np.asarray(solid_o.faces, dtype=int)
        face_verts: list[list[int]] = []
        cursor = 0
        while cursor < len(fraw):
            n = int(fraw[cursor])
            if n == 3:
                face_verts.append(
                    [int(fraw[cursor + 1]), int(fraw[cursor + 2]), int(fraw[cursor + 3])]
                )
            cursor += n + 1
        tm = trimesh.Trimesh(
            vertices=pts, faces=np.array(face_verts), process=True
        )
        trimesh.repair.fix_normals(tm)
        trimesh.repair.fix_winding(tm)
        cell_tms.append(tm)

    combined = trimesh.util.concatenate(cell_tms)
    trimesh.repair.fix_normals(combined, multibody=True)

    rot = trimesh.transformations.rotation_matrix(np.radians(-90), [0, 1, 0])
    combined.apply_transform(rot)
    return combined


def _build_cell_solids(
    cell_patches: list[pv.PolyData],
    *,
    loft_bbox_center: np.ndarray,
    scale_x: float,
    scale_y: float,
    slice_normal: tuple[float, float, float],
    slice_origin: tuple[float, float, float],
    tolerance: float,
) -> list[pv.PolyData]:
    """Build watertight cell solids from mesh patches — the Step 5 logic."""
    cell_solids: list[pv.PolyData] = []
    for cell_mesh in cell_patches:
        surf = extract_surface_mesh(cell_mesh)

        scaled_surf = scale_polydata_in_xy(
            surf, center=loft_bbox_center, scale_x=scale_x, scale_y=scale_y
        )
        _, open_edge_loops = extract_naked_edge_loops(surf, tolerance=tolerance)
        scaled_loops = [
            scale_points_in_xy(loop, center=loft_bbox_center, scale_x=scale_x, scale_y=scale_y)
            for loop in open_edge_loops
        ]
        loft_bands = [
            _loft_between_polylines(src, tgt)
            for src, tgt in zip(open_edge_loops, scaled_loops)
            if len(src) >= 2 and len(tgt) >= 2
        ]
        raw_solid = _merge_meshes(
            [p for p in [surf, scaled_surf] + loft_bands if p.n_cells > 0]
        )

        body_patch, moved_patch = split_and_offset_plane_faces(
            raw_solid,
            plane_normal=slice_normal,
            plane_origin=slice_origin,
            offset_amount=-2.0,
            tolerance=tolerance,
        )

        if moved_patch.n_cells > 0:
            _, body_loops = extract_naked_edge_loops(body_patch, tolerance=tolerance)
            _, moved_loops = extract_naked_edge_loops(moved_patch, tolerance=tolerance)
            wall_lofts: list[pv.PolyData] = []
            used: set[int] = set()
            for bl in body_loops:
                best_mi: int | None = None
                best_d = float("inf")
                for mi, ml in enumerate(moved_loops):
                    if mi in used:
                        continue
                    d = float(np.linalg.norm(bl.mean(axis=0) - ml.mean(axis=0)))
                    if d < best_d:
                        best_d = d
                        best_mi = mi
                if best_mi is not None and len(bl) >= 2:
                    used.add(best_mi)
                    lr = align_loops_and_loft(bl, moved_loops[best_mi], tolerance=tolerance)
                    if lr.n_cells > 0:
                        wall_lofts.append(lr)
            solid = _merge_meshes(
                [p for p in [body_patch, moved_patch] + wall_lofts if p.n_cells > 0]
            )
        else:
            solid = raw_solid

        solid = close_mesh_boundaries(solid, tolerance=tolerance)
        solid = unify_mesh_normals(solid)
        for k in list(solid.cell_data.keys()):
            del solid.cell_data[k]
        cell_solids.append(solid)

    return cell_solids


def _filter_disconnected_cells(cell_solids: list[pv.PolyData]) -> list[pv.PolyData]:
    """Keep only cells that belong to the largest connected region."""
    non_empty = [s for s in cell_solids if s.n_cells > 0]
    if not non_empty:
        return cell_solids
    test_assembly = _merge_meshes(non_empty)
    n_regions = count_connected_regions(test_assembly)
    if n_regions <= 1:
        return cell_solids

    conn = test_assembly.connectivity()
    region_ids = np.asarray(conn["RegionId"], dtype=int)
    region_sizes = Counter(region_ids)
    largest_rid = max(region_sizes, key=region_sizes.get)

    face_offset = 0
    keep_mask: list[bool] = []
    for solid in cell_solids:
        if solid.n_cells == 0:
            keep_mask.append(False)
            continue
        mid = face_offset + solid.n_cells // 2
        in_main = int(region_ids[mid]) == largest_rid if mid < len(region_ids) else False
        keep_mask.append(in_main)
        face_offset += solid.n_cells

    return [s for s, k in zip(cell_solids, keep_mask) if k]


def run_pipeline(config: PipelineConfig, *, verbose: bool = True) -> PipelineResult:
    """Execute the full voronoi shell pipeline for a given config.

    Returns a PipelineResult containing the trimesh, cell solids, assembled
    surface, volume validity flag, and summary stats dict.
    """
    import vtk as _vtk
    _vtk.vtkObject.GlobalWarningDisplayOff()

    surface_config = config.to_surface_config()
    point_config = config.to_point_config()
    extrusion_multiplier = config.effective_extrusion
    scale_x = config.scale_x
    scale_y = config.scale_y
    tolerance = config.line_tolerance

    if verbose:
        print(f"Radii: {list(surface_config.radii)}")
        print(f"Z positions: {list(surface_config.z_levels)}")
        print(f"Voronoi seeds: {point_config.seed_count}, random seed: {point_config.random_seed}")
        print(f"Extrusion: {extrusion_multiplier:.2f}, Scale X: {scale_x:.2f}, Scale Y: {scale_y:.2f}")

    full_surface = build_lofted_surface(surface_config)
    full_loft_bounds = full_surface.bounds
    loft_bbox_center = np.array([
        0.5 * (full_loft_bounds[0] + full_loft_bounds[1]),
        0.5 * (full_loft_bounds[2] + full_loft_bounds[3]),
        0.5 * (full_loft_bounds[4] + full_loft_bounds[5]),
    ], dtype=float)

    half_surface = clip_surface_in_half(
        full_surface,
        normal=surface_config.slice_normal,
        origin=surface_config.slice_origin,
    )
    padded_bounds = pad_bounds(half_surface.bounds, surface_config.bbox_padding)

    if verbose:
        print(f"Full loft: {full_surface.n_points} pts / {full_surface.n_cells} cells")
        print(f"Half surface: {half_surface.n_points} pts / {half_surface.n_cells} cells")

    seed_points = random_points_in_bounds(
        bounds=padded_bounds,
        count=point_config.seed_count,
        seed=point_config.random_seed,
    )
    voronoi_cells = build_bounded_voronoi_cells(seed_points, padded_bounds)
    raw_polylines = intersect_cells_with_surface(
        surface=half_surface, cells=voronoi_cells, tolerance=tolerance,
    )
    closed_polylines, _, _ = filter_isolated_polylines(raw_polylines, tolerance=tolerance)

    polyline_snap_tolerance = max(20.0 * tolerance, 0.02)
    closed_polylines = rebuild_polylines_from_discontinuities(
        closed_polylines,
        tolerance=tolerance,
        discontinuity_angle_degrees=176.0,
        neighbor_snap_tolerance=polyline_snap_tolerance,
    )

    if verbose:
        print(f"Voronoi cells: {len(voronoi_cells)}, retained polylines: {len(closed_polylines)}")

    if not closed_polylines:
        return PipelineResult(
            trimesh_result=trimesh.Trimesh(),
            cell_solids=[],
            generated_surface=half_surface,
            is_valid_volume=False,
            stats={"polyline_count": 0},
        )

    curve_result = analyze_and_generate_surfaces(
        closed_polylines,
        loft_bounds=full_surface.bounds,
        tolerance=tolerance,
        extrusion_multiplier=extrusion_multiplier,
        small_cell_extrusion_factor=SMALL_CELL_EXTRUSION_FACTOR,
        extrusion_scale_origin=loft_bbox_center,
        planar_scale_factors=(scale_x, scale_y),
        slice_plane_x=surface_config.slice_origin[0],
    )

    analysis_output = build_analysis_output_meshes(
        curve_result.analyses,
        average_ratio=curve_result.average_ratio,
        loft_bounds=full_surface.bounds,
        tolerance=tolerance,
        extrusion_multiplier=extrusion_multiplier,
        small_cell_extrusion_factor=SMALL_CELL_EXTRUSION_FACTOR,
        slice_plane_x=surface_config.slice_origin[0],
    )

    mesh_cleanup = clean_meshes_without_naked_edges(
        list(analysis_output.output_meshes), tolerance=tolerance,
    )
    cell_patches = list(mesh_cleanup.kept_meshes)

    if verbose:
        print(f"Analyzed curves: {len(curve_result.analyses)}, cell patches: {len(cell_patches)}")

    cell_solids = _build_cell_solids(
        cell_patches,
        loft_bbox_center=loft_bbox_center,
        scale_x=scale_x,
        scale_y=scale_y,
        slice_normal=surface_config.slice_normal,
        slice_origin=surface_config.slice_origin,
        tolerance=tolerance,
    )
    cell_solids = _filter_disconnected_cells(cell_solids)

    if not cell_solids:
        return PipelineResult(
            trimesh_result=trimesh.Trimesh(),
            cell_solids=[],
            generated_surface=half_surface,
            is_valid_volume=False,
            stats={"polyline_count": len(closed_polylines), "cell_solid_count": 0},
        )

    generated_surface = _merge_meshes([s for s in cell_solids if s.n_cells > 0])
    result_mesh = build_export_trimesh(cell_solids)

    stats = {
        "polyline_count": len(closed_polylines),
        "curve_count": len(curve_result.analyses),
        "cell_patch_count": len(cell_patches),
        "cell_solid_count": len(cell_solids),
        "face_count": len(result_mesh.faces),
        "is_watertight": result_mesh.is_watertight,
        "is_volume": result_mesh.is_volume,
    }

    if verbose:
        print(
            f"Cell solids: {len(cell_solids)}, faces: {len(result_mesh.faces)}, "
            f"watertight: {result_mesh.is_watertight}, volume: {result_mesh.is_volume}"
        )

    return PipelineResult(
        trimesh_result=result_mesh,
        cell_solids=cell_solids,
        generated_surface=generated_surface,
        is_valid_volume=result_mesh.is_volume,
        stats=stats,
    )


def run_pipeline_with_retry(
    config: PipelineConfig,
    *,
    max_attempts: int = 10,
    verbose: bool = True,
) -> tuple[PipelineResult, PipelineConfig]:
    """Run the pipeline, retrying with different seeds if the result is not a valid volume.

    Returns a tuple of (result, config_used) so the caller knows which seed succeeded.
    """
    result = run_pipeline(config, verbose=verbose)
    if result.is_valid_volume:
        return result, config

    if verbose:
        print("Mesh is not a valid volume. Starting auto-retry...")

    base_seed = config.random_seed
    for attempt in range(1, max_attempts + 1):
        new_seed = base_seed + attempt * 7
        if verbose:
            print(f"  Attempt {attempt}/{max_attempts} with seed {new_seed}...")
        retry_config = config.with_seed(new_seed)
        result = run_pipeline(retry_config, verbose=False)
        if result.is_valid_volume:
            if verbose:
                print(f"  SUCCESS with seed {new_seed}: {len(result.trimesh_result.faces)} faces")
            return result, retry_config
        elif verbose:
            print(f"  Seed {new_seed}: not a valid volume, trying next...")

    if verbose:
        print("  All attempts failed. Try adjusting other parameters.")
    return result, config.with_seed(base_seed + max_attempts * 7)


def export_stl(
    result: PipelineResult,
    export_dir: str | Path,
    *,
    suffix: str = "",
) -> Path:
    """Write the trimesh result to an STL file with a timestamped name."""
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"voronoi_shell_{ts}{suffix}.stl"
    path = export_dir / name
    result.trimesh_result.export(str(path))
    return path
