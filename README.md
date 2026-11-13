<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./media/logo_light.svg">
    <source media="(prefers-color-scheme: light)" srcset="./media/logo_dark.svg">
    <!-- The fallback image for older browsers -->
    <img alt="orca_3mf" src="./media/logo_light.svg" width="200">
  </picture>
  <br>
</h1>

# Orca 3MF

An **Orca3mf** tab in the 3D Viewport sidebar that builds OrcaSlicer projects (`.3mf`) from the STLs Combi Export writes. You set up each project once in Blender: its plates, the parts on each plate, how each part stands on the plate, and the seam drawn on it. **Build** then writes the project, so a new export is one click away from a ready Orca project, seams included.

The setup is saved in the .blend file. The tab is laid out like Combi Export: the roots on top, then a list per level with its buttons on the right.

## The tab

| Panel | What it holds |
| --- | --- |
| Orca Projects | **STL root**: where the parts' paths start. Empty: Combi Export's root. **3MF root**: where the projects are saved, `../orca_slicer` by default. Next to it, **Import** (make a project from an Orca file) and **Build** (every ticked project). **Seams**: the width of the painted seam strip and how far a seam line may be from the surface. |
| Info | The log of the last build or import, and a short guide. |
| Projects | One row per 3MF file: its Build button, tick, name and folder. Below the list, the active project's STL folder, **Settings from**, **Update from 3MF**, **Plate names are folders** and **Overwrite the 3MF**. |
| Plates | One row per Orca plate: tick, number, name, **gap** and STL folder. |
| Parts | One row per part: tick, name (as Orca lists it), copies, seam mark and STL path. A red path means the STL is not there. Below the list, the active part's **Up**, **Turn**, **Mirror**, **Seam** and **Copies** (open it for each copy's X, Y and Turn). |

The roots can be absolute (`D:\Grip\stl`, `/home/olex/Grip/stl`) or relative to the .blend file's folder: `../stl` and Blender's `//../stl` are the same folder. Folders can be written with `/` or `\` on Windows and Linux alike, so a .blend set up on one works on the other. The folder button next to STL root, 3MF root and **Settings from** saves the picked folder or file in that relative form (`../stl`); it stays absolute while the .blend is not saved, or when it is on another drive.

A part's STL is found at STL root / project's STL folder / plate's STL folder / the part's path, with `/` between folders. So a project whose STL folder is `GripV/parts` and a plate whose STL folder is `buttons/default` take `cap_default/circle.stl` from `GripV/parts/buttons/default/cap_default/circle.stl`.

The **+** of the Parts list opens a file browser: pick one or more STLs, each becomes a part (Shift + **+** adds an empty one). The folder button next to a part's path picks another file. Picked STLs are saved from the plate's STL folder, with `../` when they are outside it.

## Placing the parts

- **Up**: the part's direction, in Blender as exported, that points up on the plate. `0, 0, 1` prints it as it stands in Blender, `0, 0, -1` upside down, `0, 1, 0` on its front face (Blender's front is −Y).
- **Turn**: then turns it around that axis.
- **Copies**: each copy has its own spot, X and Y (the centre of its footprint, from the plate's front left corner) and its own Turn on top of the part's. **+** adds a copy, **−** removes the selected one, and the round arrow lets the next Build nest all of them again.
- **Mirror**: mirror the part across its own X axis (a part mirrored in Orca imports with this ticked, and builds back mirrored).

A copy with a spot stands there. A copy without one (a new part, a new copy, or one sent back to nesting) is nested in the free space around them: stood up, set on the bed and placed by its footprint, largest first, **gap** apart and half a gap from the edges (a compact block in the middle of an empty plate). Build then records where it went, so every copy keeps its spot from then on. A part that does not fit stops the build and the log says which. The plates are laid out the way Orca lays them out, so each part lands on its plate.

## Seams

Pick a curve or a mesh in a part's **Seam** field, drawn on that part's surface where the seam should go.

- **Curve**: its splines. Snapping to Face helps to place the points. Bevel and modifiers are ignored.
- **Mesh made of edges only**: all its edges.
- **Mesh with faces**: the edges marked as seam (Edge › Mark Seam). Geometry Nodes can output them too: store a boolean edge attribute named `orca_seam` (or `uv_seam`, Blender's own seam mark).

The line is snapped onto the part's surface within **Snap**, so one line serves the tight, default and loose versions of a part: give all of them the same Seam. Orca puts the seam where each layer's perimeter crosses the painted strip.

## Build

1. Export the STLs with Combi Export.
2. Press the Build button of a project row, or the one next to the 3MF root for every ticked project. While it builds, a progress bar like Combi Export's ends that row (or the 3MF root's row), showing `Part 3/12 | 4.2s`, then `3MF 1/2` while it writes; the rest of the tab's fields are greyed out. The row's Build button turns into **Cancel**: the projects written so far are kept.

A **new** project takes its printer, process and filament settings from **Settings from**: any Orca project made for your printer. A project added to the list starts with the Settings from of the selected project, or that project's own file. Orca opens a project without settings as bare parts, without its plates, so Build refuses to write one.

An **existing** project is never changed unless **Overwrite the 3MF** is ticked in the project box (it asks first). Without it, each Build writes into a `3mf_builds` folder next to the project: first under the project's own name (`GripV_parts.3mf`), then numbered (`GripV_parts_001.3mf`, `_002`, ...); old ones are left for you to delete. A new project (no file yet) is written as the project itself.

Build never reads the old 3MF: everything comes from the .blend. Import saved there:

- the printer, process and filament settings (used instead of **Settings from**)
- each plate's own settings (for example its bed type)
- each part's own settings (for example wall loops or infill set on a part in Orca)
- where each copy stands, and its turn

The meshes come fresh from the STLs and the seams from the Seam lines, so what belongs to Orca's old meshes is not kept: painting, modifiers, negative parts and support blockers, variable layer heights, height ranges, brim ears and per-layer custom G-code. Import lists what it left out. Objects made of several STLs are not built.

To take changes made in Orca back (moved copies, new settings), use **Update from 3MF** in the project box: it opens at the newest build, and reads that file into the project like a re-import (links, seams and Build ticks are kept; the project's own 3MF stays the same).

## Import

**Import** makes a project from an existing Orca project: its plates, and each part with its STL, Up, Turn, each copy's spot and the Orca settings listed under Build. Importing a 3MF that is already a project in the tab updates that project instead, and keeps its links, Build ticks and seams. Import never takes a file by its name alone, since every tolerance version of a part has the same file name. For each part, in order:

1. the path Orca stored, when the file is there;
2. on a re-import, the tab's own link for that file name on that plate, when it is the only one;
3. with **Plate names are folders** ticked (in the Import window, and per project in the tab): the plate's name read as its folder, a path below the project's STL folder like `tight/cap_loose` (set the STL folder in the Import window; empty, it is the STL root). The file must be in exactly one of that folder and the folders above it, up to the project's STL folder. Never a sibling folder;
4. the stored path's folders below the STL root.

With **Plate names are folders** on, a plate's name *is* its folder: the plate's own STL folder field goes away, its parts' paths start at that folder (`dpad.stl`, `../button.stl`), and renaming the plate points it at another folder (the name turns red when there is no such folder). Turning it on names each plate after the deepest folder its parts share, when they sit in one line of folders; turning it off gives each plate that folder as its STL folder. Either way every part keeps its file. For a 3MF whose plates aren't named after folders yet: import it with the switch off, then turn it on in the project box.

Anything else comes in red with the reason. **Relink missing parts** (under the parts list) fixes the whole plate at once: pick a folder, each missing part is looked for in that folder only; with Plate names are folders ticked also in the folders above it, and the plate is named after the folder so the next import finds its parts by itself. Each part also has its own folder button. Import's **STL folder** field sets the project's STL folder where looking up stops; empty, it is the folder all found parts share.

The parts list marks a part red when its STL is missing or nothing sure was found; the part's box says why. Click its warning icon to pick the file (any name). The warning icon of a plate opens Relink for that plate. A file you pick or add by hand is never marked: Update from 3MF keeps the tab's links.

## How it works

| File | What it holds |
| --- | --- |
| `build3mf.py` | Writes projects from projects › plates › parts, and reads an existing project's layout for Import. No Blender needed. |
| `layout.py` | Up axis and turn to a rotation, footprints, nesting, and Orca's plate grid. No Blender needed. |
| `reload3mf.py` | Reads Orca projects and reloads STLs into them. Also runs on its own from the command line, see below. No Blender needed. |
| `seams.py` | Seam lines to Orca's seam painting: each triangle near the line is split until the painted strip follows it. No Blender needed. |
| `blender_seams.py` | Reads a seam line from a curve or mesh. |
| `paths.py` | Roots and folders to absolute paths, with Windows or Linux separators. No Blender needed. |
| `props.py` | What the .blend saves. |
| `operators.py`, `ui.py` | The tab's buttons and panels. |

Orca stores each part's mesh centred on itself, and the part's place and rotation on the build item; plates sit side by side, 1.2 bed widths apart, √(number of plates), rounded up, to a row, rows going towards −Y, and each part belongs to the plate it lies on.

## Not done yet

- Seam blockers (places the seam must avoid).
- Nesting by outline: parts are nested by their bounding box, so L-shaped parts leave space unused.
- Curves made by Geometry Nodes: a curve object's modifiers are ignored. A mesh object with Geometry Nodes works when its nodes output mesh edges (add Curve to Mesh).
- Scale set in Orca is dropped on Import.
- Bed exclude areas are only warned about, not honoured when nesting; round and non-rectangular beds nest on their bounding box.

## Command line reload

`reload3mf.py` also reloads every STL part of Orca projects from disk without Blender or Orca, like Orca's **Reload from disk** on every part. It needs only Python 3.8 or newer (Blender's own Python works). The original is never overwritten; the result is written as `NAME_reloaded.3mf`.

```
python orca_3mf\reload3mf.py D:\Grip\orca_slicer\GripV\GripV_parts.3mf
python orca_3mf\reload3mf.py D:\Grip\orca_slicer
```

The second line does every `.3mf` under that folder. Options: `-o FILE`, `--out-dir DIR`, `--suffix TEXT`, `--search DIR` (where to look for moved STLs), `--anchor center|origin`, `--no-drop`, `--dry-run`; `--help` explains each. Exit code 0 means all parts were found, 2 some were missing, 1 a project failed.

## Tests

```
python -I tests/test_build.py
python -I tests/test_seams.py
blender -b -P tests/test_blender.py
python -I tests/test_reload.py
python -I tests/test_paths.py
```

`test_blender.py` needs a Python with the `bpy` module (Blender 5.2 or newer); the others need only Python. None of them open Grip.blend.

The tests run on the dev PC; GitHub CI doesn't run them on push. When the PC is offline, the manual `fallback.yml` workflow runs all of them, `test_blender.py` with the `bpy` module.
