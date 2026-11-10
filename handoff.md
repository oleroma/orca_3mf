# Orca 3mf

## What it does
- Projects (3MF files) > plates > parts, saved in the .blend, laid out like Combi Export.
- A part is an STL path (STL root, empty = Combi Export's root, / project STL folder / plate STL folder / path), plus Up, Turn, Mirror, Copies and an optional Seam curve or mesh.
- A plate has a name and a gap. Build nests each plate (bounding boxes, largest first, centred) and lays plates out on Orca's grid.
- Build uses the STLs Combi Export already wrote, not Blender meshes. Only the seam lines come from the scene.
- A new project takes its printer settings from "Settings from" (any Orca project for the printer). An existing file keeps its settings, part settings and plate settings, and keeps painting when the shape did not change.
- Import makes a project from an existing Orca 3MF (STLs found below the STL root, matched by shape when names repeat). Mirrored parts come back with their Mirror tick set.
- An Orca object of several parts (an assembly, made in Orca with Assemble) imports as one tab part per STL, each with the object's name in **Assembly** and its **Place** in it. Build writes the parts of a plate with the same Assembly as one object again.

## Changes done in this pass

### Fixes from the review

1. **Mirroring** — Import now detects a mirrored Orca transform (`det < 0`), records it as a part **Mirror** tick and factors the reflection out as a local-X mirror so Up/Turn stay a clean rotation; Build writes the mirror back into the item transform. Files: `layout.py` (`det`), `props.py` (`mirror`), `build3mf.py` (PartSpec/ImportedPart/`_prepare`/`read_layout`), `operators.py` (import/build), `ui.py` (checkbox).

2. **Extra volumes + index files** — Build no longer drops modifiers, negative parts and support blockers; it copies each extra volume's sub-model and `<part>` settings and re-anchors its component transform with `Cm^-1 · Cn`. It also re-keys `layer_heights_profile.txt`, `layer_config_ranges.xml`, `brim_ear_points.txt` (1-based object index) and `custom_gcode_per_layer.xml` (1-based plate index) to the new object/plate order. Format taken from OrcaSlicer `Format/bbs_3mf.cpp` / `Model.cpp`. Files: `reload3mf.py` (`invert`), `build3mf.py` (Base/`_OldObject`/`_ExtraVolume`/`_carry_extras`/`write`/`_model_settings`/remap helpers).

3. **Same-named parts** — parts are matched to their old Orca settings by shape, then source file, then plate, then name (was file order). Files: `build3mf.py` (`_OldObject.plates`, `Base.take_part`).

4. **Plate settings by name** — plate metadata in `model_settings.config` is matched by `plater_name` rather than position, and per-layer custom G-code follows the same plate-name mapping. Files: `build3mf.py` (`Base.take_plate`, `_remap_positioned_files`).

5. **Print-volume checks** — a part taller than the printer's `printable_height` stops the build; `bed_exclude_area` and non-rectangular printable areas are reported as warnings. Files: `build3mf.py` (`area_points`/`bed_warnings`/`_axis_aligned_rectangle`, `build`).

6. **Minor** — written 3MF files get mode `0644` (was `0600`); two projects resolving to the same 3MF are reported and skipped; an existing file without printer settings now takes them from **Settings from**; Import accepts `.3MF` and targets the same file (preserves the extension); the seam paint cache is keyed by the seam object rather than its name. Files: `build3mf.py`, `reload3mf.py` (chmod), `operators.py`.

### Test layout / runner fixes

- Test paths and docstrings updated from `extensions/orca_reload` to this checkout's `orca_3mf`; `test_blender.py` now runs under Blender and stages the add-on at the folder name Blender expects. Files: `tests/test_build.py`, `tests/test_seams.py`, `tests/test_blender.py`, `README.md`. The `tools` folder is gone: `orca_3mf/reload3mf.py` runs from the command line itself, and its tests are `tests/test_reload.py`.
- Added tests: mirror round-trip, plate-settings-follow-name on reorder, same-name-settings-follow-shape, printable-height, non-rect/exclude warnings, and extra-volumes + index-file remap across a plate reorder.

## Verification

```
python -I tests/test_build.py        # 25 OK
python -I tests/test_seams.py        # 8 OK
python -I tests/test_reload.py       # 17 OK
python -I tests/test_paths.py        # 17 OK
blender -b -P tests/test_blender.py  # 17 OK (Blender 5.2; the pip bpy 5.2.2 module also works)
```

Also smoke-tested against `GripV_parts.3mf` (import -> rebuild round-trips 31 parts / 9 plates and their settings) and the dongle project (R2/R4 mirror detection). The extra-volume and index-remap paths are validated with a synthetic `modifier_part` fixture built to Orca's format; none of the local `.3mf` files contain a non-`normal_part` volume.

### Later

- **NURBS seams** — a NURBS seam now follows the smooth curve: each NURBS spline is copied alone into a temporary curve and evaluated by Blender (`blender_seams.py: nurbs_points`). Test: `test_nurbs_seam_follows_the_smooth_curve`.

- **Relative roots** — STL root, 3MF root and Settings from may be relative (`../stl`), starting at the .blend file's folder like `//../stl`; `/` and `\` both work on Windows and Linux. The folder buttons next to these fields are the add-on's own (`browse_root`, `browse_template`; the properties are plain strings, so Blender's own button and its `//..\` format are gone) and save the pick as `../stl` from the .blend folder (absolute while unsaved or on another drive, `paths.relative_form`). Picked part STLs are saved from the plate folder, `../` when outside it. Default 3MF root is `../orca_slicer`. Files: `paths.py` (new, no Blender), `props.py`, `operators.py`. Tests: `tests/test_paths.py` (runs with Linux and Windows path rules), `test_relative_roots_start_at_the_blend_file`.

- **Relative part STLs** — every part's STL is saved from its plate's folder (`../elsewhere/box.stl` when outside it), the same way the roots are saved from the .blend folder: picked, added and now also imported parts (an STL found outside the STL root, or Orca's stored path when the STL is not found; a relative stored path starts at the 3MF's folder, a path from the other system is kept). A path stays absolute only when there is no way there: another drive, or the .blend is not saved yet while the STL root is relative (`paths.relative_form` needs an absolute base). An imported 3MF outside the 3MF root gets its folder saved from the root too (`../other`). The 3MF's `source_file` is written from the 3MF's folder (`../../stl/GripV/parts/box.stl`) when the STL is below the STL root, full otherwise (`operators._source_file`; Orca and our Import read a relative one from the 3MF's folder; Orca's own Reload from disk is not tried with it yet). The tab, the build report and the import log show paths from the .blend file's folder, root included (`../stl/GripV/parts/box.stl`), full only outside both roots (`props.shown`); the STL root hint shows the root from the .blend folder. Test: `test_paths_in_the_3mf_and_the_tab_start_at_the_roots`. Files: `paths.py`, `props.py` (`part_relative`), `operators.py` (`_source_path`). Tests: `test_import_saves_outside_and_missing_stls_from_the_plate_folder`, `test_relative_form`.

- **Build progress bar** — looks like Combi Export's export bar: the row's Build button turns into a Cancel button, the row's fields are greyed out, and a slider at the row's end shows `Part 3/12 | 4.2s` (then `3MF 1/2 | ...` while writing). Build all does the same in the 3MF root row. How it runs fits this add-on, not Combi's (which polls a worker process): Build pressed in the tab is a modal operator with a timer that does one part per tick (read the STL, paint its seam) and writes each 3MF in a tick of its own; everything is taken from the scene before the first tick, so editing the tab mid-build does not change the build. Events pass through; Cancel stops it and the log says how many projects were written. Which build runs is kept in the module (`props.building()`), so a restart always starts idle. From a script or with no window, Build still runs at once (`execute`). Files: `operators.py` (`_build_steps`, `ORCA_OT_build`, `ORCA_OT_cancel_build`), `props.py`, `ui.py` (`draw_build_button`, `draw_progress`). Tests: `test_build_steps_report_progress_part_by_part` (drives the modal with plain events), `test_panels_draw`.

- **Assemblies** (2026-11-10) — Import used to skip every object with more than one part ("has 13 parts, only objects of one STL are taken"). Now each model part of such an object becomes a tab part with `assembly` (the object's name; a second object of the same name gets "name 2", so each stays its own) and `place` (middle of the part's bounding box in the object, turned like the object's first copy; its Up/Turn/Mirror come from that copy's turn times the part's own). Build groups a plate's parts by Assembly into one object (`AssemblySpec`, `_grouped`, `_prepare_assembly`): each part is a volume with its own component transform and `matrix` setting, the copies only move (no turn), the whole is nested as one footprint, copies = the most copies among its parts. Old settings: the old assembly is matched by shared source files, then plate, then name (`Base.take_assembly`); each part's settings and painting by shape/source within it; its modifiers re-anchored with the old copy's turn. Files: `build3mf.py` (`_Volume`, `_volume`, `_matrix_text`, `read_layout`, `_unique`), `props.py` (`assembly`, `place`), `operators.py`, `ui.py` (Assembly/Place rows, group icon in the list). Tests: `test_assembly_is_one_object_with_its_parts_in_place`, `test_assembly_survives_import_and_rebuild`, `test_assemblies_with_the_same_name_stay_apart_on_import`, Blender `test_assembly_imports_and_rebuilds`. Not yet tried on a real Orca assembly file.

## Still to do

- **UNC paths** — parked by Olex (2026-10-18). `\\server\share\...` written with backslashes is now kept as a network path on Windows (tested with Windows path rules only); `//server/share` with forward slashes is Blender's blend-relative prefix and wins. Not tried on a real share.

