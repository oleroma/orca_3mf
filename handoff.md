# Orca 3mf

## What it does
- Projects (3MF files) > plates > parts, saved in the .blend, laid out like Combi Export.
- A part is an STL path (STL root, empty = Combi Export's root, / project STL folder / plate STL folder / path), plus Up, Turn, Mirror, Copies and an optional Seam curve or mesh.
- A plate has a name and a gap. Build nests each plate (bounding boxes, largest first, centred) and lays plates out on Orca's grid.
- Build uses the STLs Combi Export already wrote, not Blender meshes. Only the seam lines come from the scene.
- A new project takes its printer settings from "Settings from" (any Orca project for the printer). An existing file keeps its settings, part settings and plate settings, and keeps painting when the shape did not change.
- Import makes a project from an existing Orca 3MF (STLs found below the STL root, matched by shape when names repeat). Mirrored parts come back with their Mirror tick set.

## Changes done in this pass

### Fixes from the review

1. **Mirroring** — Import now detects a mirrored Orca transform (`det < 0`), records it as a part **Mirror** tick and factors the reflection out as a local-X mirror so Up/Turn stay a clean rotation; Build writes the mirror back into the item transform. Files: `layout.py` (`det`), `props.py` (`mirror`), `build3mf.py` (PartSpec/ImportedPart/`_prepare`/`read_layout`), `operators.py` (import/build), `ui.py` (checkbox).

2. **Extra volumes + index files** — Build no longer drops modifiers, negative parts and support blockers; it copies each extra volume's sub-model and `<part>` settings and re-anchors its component transform with `Cm^-1 · Cn`. It also re-keys `layer_heights_profile.txt`, `layer_config_ranges.xml`, `brim_ear_points.txt` (1-based object index) and `custom_gcode_per_layer.xml` (1-based plate index) to the new object/plate order. Format taken from OrcaSlicer `Format/bbs_3mf.cpp` / `Model.cpp`. Files: `reload3mf.py` (`invert`), `build3mf.py` (Base/`_OldObject`/`_ExtraVolume`/`_carry_extras`/`write`/`_model_settings`/remap helpers).

3. **Same-named parts** — parts are matched to their old Orca settings by shape, then source file, then plate, then name (was file order). Files: `build3mf.py` (`_OldObject.plates`, `Base.take_part`).

4. **Plate settings by name** — plate metadata in `model_settings.config` is matched by `plater_name` rather than position, and per-layer custom G-code follows the same plate-name mapping. Files: `build3mf.py` (`Base.take_plate`, `_remap_positioned_files`).

5. **Print-volume checks** — a part taller than the printer's `printable_height` stops the build; `bed_exclude_area` and non-rectangular printable areas are reported as warnings. Files: `build3mf.py` (`area_points`/`bed_warnings`/`_axis_aligned_rectangle`, `build`).

6. **Minor** — written 3MF files get mode `0644` (was `0600`); two projects resolving to the same 3MF are reported and skipped; an existing file without printer settings now takes them from **Settings from**; Import accepts `.3MF` and targets the same file (preserves the extension); the seam paint cache is keyed by the seam object rather than its name. Files: `build3mf.py`, `reload3mf.py` (chmod), `operators.py`.

### Test layout / runner fixes

- Test paths and docstrings updated from `extensions/orca_reload` to this checkout's `orca_3mf`; `test_blender.py` now runs under Blender and stages the add-on at the folder name Blender expects. Files: `tests/test_build.py`, `tests/test_seams.py`, `tests/test_blender.py`, both `README.md`s, `tools/orca_reload/orca_reload.py`.
- Added tests: mirror round-trip, plate-settings-follow-name on reorder, same-name-settings-follow-shape, printable-height, non-rect/exclude warnings, and extra-volumes + index-file remap across a plate reorder.

## Verification

```
python -I orca_3mf/tests/test_build.py        # 22 OK
python -I orca_3mf/tests/test_seams.py        # 8 OK
python -I orca_3mf/tests/test_reload.py  # 17 OK
blender -b -P orca_3mf/tests/test_blender.py  # 10 OK (Blender 5.2)
```

Also smoke-tested against `GripV_parts.3mf` (import -> rebuild round-trips 31 parts / 9 plates and their settings) and the dongle project (R2/R4 mirror detection). The extra-volume and index-remap paths are validated with a synthetic `modifier_part` fixture built to Orca's format; none of the local `.3mf` files contain a non-`normal_part` volume.

## Still to do

- **UNC paths** — `\server\share\...` collides with Blender's blend-relative `//...` prefix in `props.py`; needs a real share to verify, or a documented decision on which wins.
- **NURBS seams** — a NURBS seam currently follows its control points, not the smooth curve (`blender_seams.py: curve_polylines`); needs the spline evaluated (depsgraph/`to_mesh`) and a Blender test.

