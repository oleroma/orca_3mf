# Orca tab handoff (2026-10-16)

## What it does
- Projects (3MF files) > plates > parts, saved in the .blend, laid out like Combi Export.
- A part is an STL path (STL root, empty = Combi Export's root, / project STL folder / plate STL folder / path), plus Up, Turn, Copies and an optional Seam curve or mesh.
- A plate has a name and a gap. Build nests each plate (bounding boxes, largest first, centred) and lays plates out on Orca's grid.
- Build uses the STLs Combi Export already wrote, not Blender meshes. Only the seam lines come from the scene.
- A new project takes its printer settings from "Settings from" (any Orca project for the printer). An existing file keeps its settings, part settings and plate settings, and keeps painting when the shape did not change.
- Import makes a project from an existing Orca 3MF (STLs found below the STL root, matched by shape when names repeat).

## Not done (from the review, most important first)
1. Mirroring: Import turns parts mirrored in Orca into unmirrored ones (dongle project: left trigger and left bumper). Fix: detect det < 0, add a Mirror tick on parts, write the mirror into the item transform.
2. Build drops modifiers, negative parts and support blockers, plus layer_heights_profile.txt, layer_config_ranges.xml and brim_ear_points.txt (keyed by 1-based object index). Fix: carry the extra volumes over with component transform Cm^-1 * Cn, and remap the index files.
3. Same-named parts (tight/loose) take Orca part settings in file order. Fix: match by shape, then source file, then plate, before falling back to name.
4. Plate settings and custom_gcode_per_layer.xml go by position, so unticking or moving a plate shifts them. Fix: match plates by name.
5. Not checked: bed_exclude_area, non-rectangular beds, printable_height.
6. Minor: written file gets mode 0600; UNC paths; two projects can resolve to the same 3MF; seam cache keyed by object name; an existing file without settings ignores Settings from; Import of a .3MF upper-case extension; NURBS seams follow control points.
- Detailed findings with repro scripts were in this session's scratchpad (not kept).
