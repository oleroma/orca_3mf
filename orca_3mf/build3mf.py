"""Builds OrcaSlicer projects (.3mf) from projects > plates > parts, each part an STL file with an up axis, a turn
around it and a number of copies. Each part is stood up, nested on its plate with the plate's gap and put on the bed.

Building over an existing project keeps what only Orca holds: the printer, process and filament settings, each
part's own settings (matched by name), each plate's settings (by position), and the painting of a part whose shape
did not change (its seams are replaced when it has a seam line). A new project takes its settings from a template
project: Orca opens a project without settings as bare geometry, without its plates, so one is needed.

Only the Python standard library is used, so it runs inside and outside Blender.
"""

import json
import math
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

try:
    from . import layout, reload3mf
except ImportError:  # run outside the add-on
    import layout
    import reload3mf

ReloadError = reload3mf.ReloadError
SEAM_ATTR = reload3mf.SEAM_ATTR

SETTINGS = "Metadata/project_settings.config"
APPLICATION = "BambuStudio-02.06.00.51"  # what OrcaSlicer 2.4 writes; it marks the file as an Orca project
ORCA_VERSION = "2.4.2"
MAX_PLATES = 36  # Orca's MAX_PLATE_COUNT
BUILD_UUID = "2c7c17d8-22b5-4d84-8835-1976022ea369"
# Mirror across a part's own X axis: the reflection factored out of a mirrored Orca transform on Import and
# written back on Build.
MIRROR = ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _axis_aligned_rectangle(points):
    """True when all polygon corners are the four corners of an axis-aligned rectangle."""
    if len(points) < 3:
        return False
    lo_x = min(p[0] for p in points)
    hi_x = max(p[0] for p in points)
    lo_y = min(p[1] for p in points)
    hi_y = max(p[1] for p in points)
    corners = {(lo_x, lo_y), (lo_x, hi_y), (hi_x, lo_y), (hi_x, hi_y)}
    return len(corners) == 4 and all(p in corners for p in points)
# Settings files that travel with the printer, process and filament settings
SETTINGS_FILES = re.compile(r"Metadata/(project_settings\.config|print_profile\.config|"
                            r"(print_setting|process_settings|filament_settings|machine_settings)_.*)$")
# Files of an existing project that are kept as they are
KEPT_FILES = re.compile(r"(Metadata/custom_gcode_per_layer\.xml|Auxiliaries/(?!\.thumbnails/).*)$")
# Part settings Orca writes itself; the others are the part's own settings and are kept
PART_KEYS = {"name", "matrix", "source_file", "source_object_id", "source_volume_id", "source_offset_x",
             "source_offset_y", "source_offset_z", "source_in_inches", "source_in_meters"}
PLATE_KEYS = {"plater_id", "plater_name", "thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file",
              "pattern_file", "pattern_bbox_file"}
# Files keyed by position (not by the <object id> or <plate plater_id> attribute) that Build re-keys on rebuild:
# layer_heights_profile.txt and brim_ear_points.txt use id=<n>/object_id=<n>, layer_config_ranges.xml uses
# <object id="n">, all with the object's 1-based position in the model; custom_gcode_per_layer.xml uses one
# <plate><plate_info id="n"> per plate's 1-based position.
INDEX_OBJECT_FILES = ("Metadata/layer_heights_profile.txt", "Metadata/layer_config_ranges.xml",
                     "Metadata/brim_ear_points.txt")
CUSTOM_GCODE_FILE = "Metadata/custom_gcode_per_layer.xml"


class PartSpec:
    """One part: the STL file (absolute path), the direction of the part that points up on the plate, the turn
    around it (radians), how many copies, a mirror across the part's own X axis, an optional seam function
    (stl_path, Mesh) -> None or (paint_seam strings by triangle index, note for the report), and the STL path written
    into the project as its source_file (default: the absolute path; relative paths start at the 3MF's folder).

    assembly: the name of the Orca object the part is assembled into (parts of a plate with the same assembly are one
    object with several parts, nested together); empty for an object of its own. place: where the middle of the
    part's bounding box sits in its assembly, in mm, once it is stood up (only for a part of an assembly)."""

    def __init__(self, name, stl, up=(0.0, 0.0, 1.0), angle=0.0, copies=1, seam=None, mirror=False, source=None,
                 assembly="", place=(0.0, 0.0, 0.0)):
        self.name = name or os.path.basename(stl)
        self.stl = stl
        self.source = source or stl
        self.up = tuple(up)
        self.angle = angle
        self.copies = max(int(copies), 1)
        self.seam = seam
        self.mirror = bool(mirror)
        self.assembly = assembly or ""
        self.place = tuple(place)


class AssemblySpec:
    """The parts of a plate assembled into one Orca object: its name, its parts (PartSpec) and how many copies."""

    def __init__(self, name, parts):
        self.name = name
        self.parts = list(parts)
        self.copies = max(p.copies for p in self.parts)
        self.stl = ""
        self.seam = None


class PlateSpec:
    def __init__(self, name="", gap=6.0, parts=()):
        self.name = name
        self.gap = gap
        self.parts = list(parts)


class ProjectSpec:
    """A project: the 3MF file to write (absolute path), its plates, a template project for the settings of a new
    project, the bed size used when no settings come with it, and how the report shows a path (default: as it is)."""

    def __init__(self, path, plates=(), template="", bed=(256.0, 256.0), shown=None):
        self.path = path
        self.shown = shown or (lambda p: p)
        self.plates = list(plates)
        self.template = template
        self.bed = tuple(bed)


# --------------------------------------------------------------------------
# What an existing project or a template brings


class _OldObject:
    def __init__(self, oid, name, meta, part, plates=(), extras=(), index=0, models=(), rotation=None):
        self.id = oid
        self.name = name
        self.meta = meta  # object settings, [(key, value)] without the name
        self.part = part  # reload3mf.Part of its one model part, or None
        self.models = list(models)  # reload3mf.Parts of all its model parts (several for an assembly)
        self.rotation = rotation or layout.IDENTITY  # rotation of its first copy on the bed
        self.plates = frozenset(plates)  # plate names it sits on
        self.extras = list(extras)  # reload3mf.Parts of its extra volumes (modifiers, negatives, blockers, ...)
        self.index = index  # 1-based position in the old model


def _same_source(a, b):
    """Whether two source_file paths point at the same STL (absolute, or at least the same file name)."""
    a = os.path.normcase(a.replace("\\", "/"))
    b = os.path.normcase(b.replace("\\", "/"))
    return a == b or os.path.basename(a) == os.path.basename(b)


class Base:
    """The settings a build starts from: an existing project (everything below), else a template (its settings
    files only), else nothing."""

    def __init__(self, path=None, template=None):
        self.settings_from = None
        self.files = {}  # zip entry -> bytes, written as they are
        self.metadata = {"Application": APPLICATION, "OrcaSlicer": ORCA_VERSION, "BambuStudio:3mfVersion": "1"}
        self.objects = []  # [_OldObject], in file order
        self.plates = []  # [(name, [(key, value)])] of each plate, in file order
        self._plate_taken = set()
        self.project = None
        if path and os.path.isfile(path):
            self._read_project(path)
            if SETTINGS not in self.files and template:
                # An existing file without printer settings still needs them: take them from Settings from,
                # keeping the file's own metadata.
                if not os.path.isfile(template):
                    raise ReloadError("settings project not found: %s" % template)
                with zipfile.ZipFile(template) as z:
                    self._take_settings(z, template, update_metadata=False)
        elif template:
            if not os.path.isfile(template):
                raise ReloadError("settings project not found: %s" % template)
            with zipfile.ZipFile(template) as z:
                self._take_settings(z, template)

    def _take_settings(self, z, path, update_metadata=True):
        for name in z.namelist():
            if SETTINGS_FILES.match(name):
                self.files[name] = z.read(name)
        if SETTINGS not in self.files:
            return
        self.settings_from = path
        if not update_metadata:
            return
        # The settings were written by this Orca version: say so, or Orca may warn about a newer file
        try:
            root = ET.fromstring(z.read(reload3mf.ROOT_MODEL))
        except (KeyError, ET.ParseError):
            return
        for m in root.findall(reload3mf.CORE_NS + "metadata"):
            if m.get("name") in ("Application", "OrcaSlicer") and m.text:
                self.metadata[m.get("name")] = m.text

    def _read_project(self, path):
        project = reload3mf.Project(path)
        self.project = project
        z = project.zip
        self._take_settings(z, path)
        for name in z.namelist():
            if KEPT_FILES.match(name) or name in INDEX_OBJECT_FILES:
                self.files[name] = z.read(name)
        root = ET.fromstring(z.read(reload3mf.ROOT_MODEL))
        for m in root.findall(reload3mf.CORE_NS + "metadata"):
            if m.get("name") in ("Title", "Designer", "Description", "Copyright", "License", "Origin",
                                 "CreationDate"):
                self.metadata[m.get("name")] = m.text or ""
        settings = ET.fromstring(z.read(reload3mf.MODEL_SETTINGS))
        plate_names = {}
        for plate in settings.findall("plate"):
            pmeta = {m.get("key"): m.get("value") for m in plate.findall("metadata") if m.get("key")}
            name = pmeta.get("plater_name") or ""
            self.plates.append((name, [(k, v) for k, v in pmeta.items() if k not in PLATE_KEYS]))
            try:
                plate_names[int(pmeta["plater_id"])] = name
            except (KeyError, ValueError):
                pass
        model_order = {oid: i + 1 for i, oid in enumerate(getattr(project, "model_order", ()))}
        for o in settings.findall("object"):
            obj = project.objects.get(o.get("id"))
            meta = [(m.get("key"), m.get("value")) for m in o.findall("metadata")
                    if m.get("key") and m.get("key") != "name"]
            models = [p for p in obj.parts if p.is_model_part] if obj else []
            part = models[0] if len(models) == 1 else None
            extras = [p for p in obj.parts if not p.is_model_part] if obj else []
            name = obj.name if obj else o.get("id")
            on = (plate_names[i] for i in getattr(obj, "plates", ()) if i in plate_names) if obj else ()
            rotation = None
            if obj and obj.items:
                try:
                    rotation = reload3mf.parse_transform(project.items[obj.items[0]][1])[0]
                except ReloadError:
                    rotation = None
            self.objects.append(_OldObject(o.get("id"), name, meta, part, on, extras,
                                           model_order.get(o.get("id"), 0), models, rotation))

    def take_plate(self, name, position):
        """The old plate settings for `name` (once each), else by position (as before), else a fresh plate."""
        for i in range(len(self.plates)):
            if i not in self._plate_taken and self.plates[i][0] == name:
                self._plate_taken.add(i)
                return self.plates[i][1]
        if 0 <= position < len(self.plates) and position not in self._plate_taken:
            self._plate_taken.add(position)
            return self.plates[position][1]
        return [("locked", "false")]

    def _part_score(self, part, spec, new_mesh):
        """How well an old model part matches a new part: 8 for the same shape, 4 for the same source file."""
        score = 0
        if part.source and (_same_source(part.source, spec.source) or _same_source(part.source, spec.stl)):
            score += 4
        try:
            old_mesh = self.project.old_mesh(part)
        except ReloadError:
            old_mesh = None
        if old_mesh is not None:
            shift = tuple(a - b for a, b in zip(old_mesh.center(), new_mesh.center()))
            if reload3mf.same_shape(old_mesh, new_mesh, (0.0, 0.0, 0.0), shift):
                score += 8
        return score

    def take_part(self, spec, plate_name, new_mesh):
        """The old object that best matches the part: by shape, then source file, then plate, then name.
        Assemblies (objects of several parts) are left for take_assembly."""
        best, best_score = None, -1
        for o in self.objects:
            if len(o.models) > 1:
                continue
            score = self._part_score(o.part, spec, new_mesh) if o.part is not None else 0
            if plate_name and o.plates and plate_name in o.plates:
                score += 2
            if o.name == spec.name:
                score += 1
            if score > best_score:
                best, best_score = o, score
        if best is None or best_score <= 0:
            return None
        self.objects.remove(best)
        return best

    def take_assembly(self, name, plate_name, parts):
        """The old assembly that best matches the new one (parts: [(PartSpec, Mesh)]): by the parts it shares
        (source file), then plate, then name. Returns (old object or None, {part index: old model part})."""
        best, best_score = None, -1
        for o in self.objects:
            if len(o.models) < 2:
                continue
            sources = sum(1 for spec, _ in parts for m in o.models
                          if m.source and (_same_source(m.source, spec.source) or _same_source(m.source, spec.stl)))
            score = 4 * min(sources, len(parts))
            if plate_name and o.plates and plate_name in o.plates:
                score += 2
            if o.name == name:
                score += 1
            if score > best_score:
                best, best_score = o, score
        if best is None or best_score <= 0:
            return None, {}
        self.objects.remove(best)
        taken, free = {}, list(best.models)
        for i, (spec, mesh) in enumerate(parts):
            scored = [(self._part_score(m, spec, mesh) + (1 if m.name == spec.name else 0), -k, m)
                      for k, m in enumerate(free)]
            if not scored:
                break
            score, _, m = max(scored, key=lambda s: s[:2])
            if score > 0:
                taken[i] = m
                free.remove(m)
        return best, taken

    def settings(self):
        if SETTINGS not in self.files:
            return None
        try:
            return json.loads(self.files[SETTINGS].decode("utf-8"))
        except ValueError:
            raise ReloadError("the printer settings of %s cannot be read" % self.settings_from)

    def area_points(self):
        """[(x, y)] of the printable area polygon, which may be empty or not a rectangle."""
        settings = self.settings() or {}
        points = []
        for p in settings.get("printable_area") or []:
            try:
                x, y = p.lower().split("x")
                points.append((float(x), float(y)))
            except ValueError:
                pass
        return points

    def bed(self, fallback):
        """(x0, y0, width, depth) of the printable area's bounding box."""
        points = self.area_points()
        if len(points) >= 3:
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        return 0.0, 0.0, float(fallback[0]), float(fallback[1])

    def bed_warnings(self):
        """Warnings about printer settings this build cannot honour."""
        settings = self.settings() or {}
        warnings = []
        if settings.get("bed_exclude_area"):
            warnings.append("bed exclude areas are not checked; parts may be nested onto them")
        points = self.area_points()
        if points and not _axis_aligned_rectangle(points):
            warnings.append("the printable area is not a rectangle; parts are nested on its bounding box")
        return warnings

    def close(self):
        if self.project is not None:
            self.project.close()


# --------------------------------------------------------------------------
# Building


class _Volume:
    """One model part of a written object: its STL's mesh and where it sits in the object."""

    def __init__(self, spec):
        self.spec = spec
        self.mesh = None  # stored mesh, centred on its bounding box like Orca stores it
        self.offset = None  # STL coordinates of the stored mesh's origin
        self.extras = {}  # per-triangle painting
        self.old_part = None  # reload3mf.Part it was matched to, or None
        self.transform = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), (0.0, 0.0, 0.0)  # in the object
        self.sub_id = None  # its sub-model <object id>
        self.path = None  # its sub-model file


class _Object:
    """One object of the written project: a part's mesh (or an assembly's parts) and its copies."""

    def __init__(self, spec, plate_no):
        self.spec = spec  # PartSpec, or AssemblySpec for an assembly
        self.plate_no = plate_no
        self.volumes = []  # _Volume of each model part
        self.old = None
        self.rotation = None
        self.lo = self.hi = None
        self.transforms = []  # (rows, translation) of each copy
        self.notes = []
        self.id = None  # the object's <object id> in the root model
        self.extra_volumes = []  # _ExtraVolume list (modifiers, negatives, blockers carried over)

    @property
    def assembly(self):
        return isinstance(self.spec, AssemblySpec)


class _ExtraVolume:
    """One extra volume of an object, copied over from its old project."""

    def __init__(self, part, transform, mesh_text, raw_part):
        self.part = part
        self.transform = transform  # (rows, translation), re-anchored to the new main volume
        self.mesh_text = mesh_text  # <mesh>...</mesh> from the old sub-model
        self.raw_part = raw_part  # old <part id=... subtype=...>...</part>, with its id not re-numbered yet
        self.sub_id = None  # new sub-model <object id>


def _load(spec, stl_cache):
    key = os.path.normcase(os.path.abspath(spec.stl))
    if key not in stl_cache:
        stl_cache[key] = reload3mf.read_stl(spec.stl)
    return stl_cache[key]


def _carry_extras(base, old_obj, anchor):
    """Copies the old object's extra volumes (modifiers, negatives, blockers, ...) for the rebuilt object.

    anchor is the transform to re-anchor each volume's component transform onto the new main volume
    (Cm^-1 * Cn: the old main volume's component transform inverted, then the new one applied; our new one is
    always identity, so this is the old main's inverse)."""
    project = base.project
    settings_text = project.text(reload3mf.MODEL_SETTINGS)
    try:
        o_start, o_end = reload3mf._block(settings_text, "object", old_obj.id, 0, len(settings_text))
    except ReloadError:
        return []
    out = []
    for part in old_obj.extras:
        if part.model_path is None:
            continue
        try:
            model_text = project.text(part.model_path)
            m_start, m_end = reload3mf.find_mesh(model_text, part.sub_id)
            p_start, p_end = reload3mf._block(settings_text, "part", part.id, o_start, o_end)
        except (ReloadError, KeyError):
            continue
        transform = reload3mf.compose(anchor, reload3mf.parse_transform(part.transform))
        out.append(_ExtraVolume(part, transform, model_text[m_start:m_end], settings_text[p_start:p_end]))
    return out


def _volume(spec, stl, old_part, base, notes):
    """The volume of a part: its stored mesh (the old one with its painting when the shape did not change) and its
    seam painting. notes gets what changed."""
    vol = _Volume(spec)
    vol.old_part = old_part
    seam = spec.seam(spec.stl, stl) if spec.seam else None
    center = stl.center()
    old = None
    if old_part is not None:
        try:
            old = base.project.old_mesh(old_part)
        except ReloadError:
            old = None
    if old is not None and reload3mf.same_shape(old, stl, (0.0, 0.0, 0.0),
                                                tuple(o - s for o, s in zip(old.center(), center))):
        # Same shape, so the same triangles in the same order: keep the stored mesh and its painting.
        vol.mesh = old
        vol.extras = {i: dict(a) for i, a in old_part.extras.items()}
        vol.offset = tuple(c - o for c, o in zip(center, old.center()))
    else:
        vol.mesh = stl.translated(tuple(-c for c in center))
        vol.offset = center
        if old_part is not None and old_part.extras:
            notes.append("painting removed, the shape changed")
    if seam is not None:
        strings, note = seam
        for a in vol.extras.values():
            a.pop(SEAM_ATTR, None)
        for i, value in strings.items():
            vol.extras.setdefault(i, {})[SEAM_ATTR] = value
        notes.append(note)
    vol.extras = {i: a for i, a in vol.extras.items() if a}
    return vol


def _rotation(spec):
    rotation = layout.orientation(spec.up, spec.angle)
    if spec.mirror:
        rotation = layout.matmul(rotation, MIRROR)
    return rotation


def _prepare(obj, base, stl_cache, plate_name):
    if obj.assembly:
        _prepare_assembly(obj, base, stl_cache, plate_name)
        return
    spec = obj.spec
    stl, _ = _load(spec, stl_cache)  # degenerate triangles are dropped, as Orca's own import does
    obj.old = base.take_part(spec, plate_name, stl)
    obj.extra_volumes = []
    if obj.old is not None and obj.old.extras and obj.old.part is not None:
        try:
            anchor = reload3mf.invert(reload3mf.parse_transform(obj.old.part.transform))
            obj.extra_volumes = _carry_extras(base, obj.old, anchor)
        except ReloadError:
            obj.extra_volumes = []
    obj.volumes = [_volume(spec, stl, obj.old.part if obj.old else None, base, obj.notes)]
    obj.rotation = _rotation(spec)
    obj.lo, obj.hi = layout.footprint(obj.volumes[0].mesh.verts, obj.rotation)


def _prepare_assembly(obj, base, stl_cache, plate_name):
    """An assembly: each part stands in the object as its Up, Turn and Mirror say, with the middle of its bounding
    box at its place; the copies are only moved, not turned."""
    meshes = []
    for spec in obj.spec.parts:
        try:
            meshes.append(_load(spec, stl_cache)[0])
        except (ReloadError, OSError, ValueError) as e:
            raise ReloadError("%s: %s" % (spec.name, e))
    obj.old, taken = base.take_assembly(obj.spec.name, plate_name, list(zip(obj.spec.parts, meshes)))
    obj.extra_volumes = []
    if obj.old is not None and obj.old.extras:
        # The places came from the old object's parts turned like its first copy (read_layout), so its extra
        # volumes go where they were with the same turn.
        obj.extra_volumes = _carry_extras(base, obj.old, (obj.old.rotation, (0.0, 0.0, 0.0)))
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for i, (spec, stl) in enumerate(zip(obj.spec.parts, meshes)):
        notes = []
        vol = _volume(spec, stl, taken.get(i), base, notes)
        obj.notes += ["%s: %s" % (spec.name, n) for n in notes]
        rows = _rotation(spec)
        middle = layout.apply(rows, vol.mesh.center())
        vol.transform = rows, tuple(p - m for p, m in zip(spec.place, middle))
        obj.volumes.append(vol)
        vlo, vhi = layout.footprint(vol.mesh.verts, rows)
        for k in range(3):
            lo[k] = min(lo[k], vlo[k] + vol.transform[1][k])
            hi[k] = max(hi[k], vhi[k] + vol.transform[1][k])
    obj.rotation = layout.IDENTITY
    obj.lo, obj.hi = tuple(lo), tuple(hi)


def _grouped(parts):
    """A plate's parts with the parts of each assembly gathered into one AssemblySpec, where its first part was."""
    out, groups = [], {}
    for part in parts:
        if not part.assembly:
            out.append(part)
        elif part.assembly in groups:
            groups[part.assembly].append(part)
        else:
            groups[part.assembly] = [part]
            out.append(part.assembly)
    return [AssemblySpec(p, groups[p]) if isinstance(p, str) else p for p in out]


def plan(spec, base, stl_cache=None):
    """Works out every object of the project and where its copies go. Returns (objects, problems, bed)."""
    stl_cache = {} if stl_cache is None else stl_cache
    bed = base.bed(spec.bed)
    x0, y0, width, depth = bed
    objects, problems = [], []
    count = len(spec.plates)
    if count > MAX_PLATES:
        problems.append("%d plates; Orca takes at most %d" % (count, MAX_PLATES))
    # Orca spaces the plates by the bed size in whole millimetres
    grid = (int(width), int(depth))
    for plate_no, plate in enumerate(spec.plates):
        plate_objects = []
        for part in _grouped(plate.parts):
            obj = _Object(part, plate_no)
            try:
                _prepare(obj, base, stl_cache, plate.name)
            except (ReloadError, OSError, ValueError) as e:
                problems.append("plate %d, %s: %s" % (plate_no + 1, part.name, e))
                continue
            plate_objects.append(obj)
        sizes, owners = [], []
        for obj in plate_objects:
            size = (obj.hi[0] - obj.lo[0], obj.hi[1] - obj.lo[1])
            for _ in range(obj.spec.copies):
                sizes.append(size)
                owners.append(obj)
        positions, left_out = layout.nest(sizes, (width, depth), plate.gap)
        for i in left_out:
            obj = owners[i]
            problems.append("plate %d, %s: does not fit on the plate (%.1f x %.1f mm with a %.1f mm gap)"
                            % (plate_no + 1, obj.spec.name, sizes[i][0], sizes[i][1], plate.gap))
        ox, oy = layout.plate_origin(plate_no, count, grid)
        for i, pos in enumerate(positions):
            if pos is None:
                continue
            obj = owners[i]
            t = (ox + x0 + pos[0] - obj.lo[0], oy + y0 + pos[1] - obj.lo[1], -obj.lo[2])
            obj.transforms.append((obj.rotation, t))
        objects += [obj for obj in plate_objects if obj.transforms]
    return objects, problems, bed


def _transform_text(transform):
    rows, t = transform
    values = [rows[r][c] for c in range(3) for r in range(3)] + list(t)
    return " ".join("%.9g" % (0.0 if abs(v) < 1e-12 else v) for v in values)


def _file_name(name, number):
    safe = re.sub(r"[^\w.\-]+", "_", name).strip("_") or "part"
    return "3D/Objects/%s_%d.model" % (safe, number)


def _model_head():
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n')


def _meta_line(indent, key, value):
    return '%s<metadata key=%s value=%s/>\n' % (indent, quoteattr(key), quoteattr(str(value)))


def _xml_text(root):
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _remap_txt_object_ids(data, remap):
    out = []
    for line in data.split("\n"):
        m = re.match(r"\s*(?:id|object_id)=(\d+)(\|.*)?$", line)
        if m and int(m.group(1)) in remap:
            key = "id" if line.lstrip().startswith("id=") else "object_id"
            line = "%s=%d%s" % (key, remap[int(m.group(1))], m.group(2) or "")
        out.append(line)
    return "\n".join(out)


def _remap_positioned_files(files, remap_obj, plate_remap):
    """Re-keys the position-addressed files (object index for the layer/profile/brim files, plate index for
    custom G-code) to the new object and plate order, in place."""
    for name in INDEX_OBJECT_FILES:
        if name not in files:
            continue
        if name.endswith(".xml"):
            try:
                root = ET.fromstring(files[name])
            except (ET.ParseError, ValueError):
                continue
            for obj in root.findall("object"):
                oid = obj.get("id")
                if oid and int(oid) in remap_obj:
                    obj.set("id", str(remap_obj[int(oid)]))
            files[name] = _xml_text(root)
        else:
            files[name] = _remap_txt_object_ids(files[name].decode("utf-8"), remap_obj)
    if CUSTOM_GCODE_FILE in files:
        try:
            root = ET.fromstring(files[CUSTOM_GCODE_FILE])
            for plate in root.findall("plate"):
                info = plate.find("plate_info")
                if info is not None:
                    pid = info.get("id")
                    if pid and int(pid) in plate_remap:
                        info.set("id", str(plate_remap[int(pid)]))
            files[CUSTOM_GCODE_FILE] = _xml_text(root)
        except (ET.ParseError, ValueError):
            pass


def write(spec, base, objects, out_path):
    """Writes the project to out_path (through a temporary file, so a failed write leaves the old file)."""
    remap_obj = {}
    for i, obj in enumerate(objects, 1):
        if obj.old is not None and obj.old.index:
            remap_obj[obj.old.index] = i
    new_plate_pos = {}
    for i, plate in enumerate(spec.plates, 1):
        new_plate_pos.setdefault(plate.name, i)
    plate_remap = {}
    for old_pos, (name, _) in enumerate(base.plates, 1):
        if name in new_plate_pos:
            plate_remap[old_pos] = new_plate_pos[name]

    files = {}
    root = [_model_head()]
    for key, value in base.metadata.items():
        root.append(' <metadata name=%s>%s</metadata>\n' % (quoteattr(key), escape(value)))
    root.append(" <resources>\n")
    sub_paths = []
    next_id = 1
    for n, obj in enumerate(objects, 1):
        comps = []
        for k, vol in enumerate(obj.volumes):
            vol.sub_id = next_id
            next_id += 1
            vol.path = _file_name(obj.spec.name if k == 0 and not obj.assembly
                                  else "%s_%d_%s" % (obj.spec.name, k + 1, vol.spec.name), n)
            sub_paths.append(vol.path)
            sub = [_model_head(), ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n',
                   '  <object id="%d" p:UUID="%08x-81cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
                   % (vol.sub_id, vol.sub_id),
                   "   ", reload3mf.mesh_xml(vol.mesh, "   ", vol.extras), "\n  </object>\n </resources>\n"
                   " <build/>\n</model>\n"]
            files[vol.path] = "".join(sub)
            comps.append('    <component p:path="/%s" objectid="%d" '
                         'p:UUID="%08x-b206-40ff-9872-83e8017abed1" transform="%s"/>\n'
                         % (escape(vol.path), vol.sub_id, vol.sub_id, _transform_text(vol.transform)))
        obj.id = next_id
        next_id += 1
        for k, extra in enumerate(obj.extra_volumes):
            extra.sub_id = next_id
            next_id += 1
            extra_path = _file_name("%s_extra%d" % (obj.spec.name, k), n)
            sub_paths.append(extra_path)
            esub = [_model_head(), ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n',
                    '  <object id="%d" p:UUID="%08x-8cb0-4c03-9d28-80fed5dfa1dc" type="model">\n' % (extra.sub_id, extra.sub_id),
                    "   ", extra.mesh_text, "\n  </object>\n </resources>\n <build/>\n</model>\n"]
            files[extra_path] = "".join(esub)
            comps.append('    <component p:path="/%s" objectid="%d" '
                         'p:UUID="%08x-b206-40ff-9872-83e8017abed1" transform="%s"/>\n'
                         % (escape(extra_path), extra.sub_id, extra.sub_id, _transform_text(extra.transform)))
        root.append('  <object id="%d" p:UUID="%08x-61cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
                    '   <components>\n%s   </components>\n  </object>\n' % (obj.id, obj.id, "".join(comps)))
    root.append(' </resources>\n <build p:UUID="%s">\n' % BUILD_UUID)
    item = 0
    for obj in objects:
        for transform in obj.transforms:
            item += 1
            root.append('  <item objectid="%d" p:UUID="%08x-b1ec-4553-aec9-835e5b724bb4" transform="%s" '
                        'printable="1"/>\n' % (obj.id, item, _transform_text(transform)))
    root.append(" </build>\n</model>\n")
    files[reload3mf.ROOT_MODEL] = "".join(root)
    files["3D/_rels/3dmodel.model.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "".join(' <Relationship Target="/%s" Id="rel-%d" '
                  'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
                  % (escape(path), i) for i, path in enumerate(sub_paths, 1))
        + "</Relationships>\n")
    files["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n')
    files["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n'
        ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n')
    files[reload3mf.MODEL_SETTINGS] = _model_settings(spec, base, objects)
    files["Metadata/slice_info.config"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <header>\n'
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
        '    <header_item key="X-BBL-Client-Version" value="%s"/>\n  </header>\n</config>\n'
        % escape(base.metadata.get("Application", APPLICATION).split("-")[-1]))
    files["Metadata/filament_sequence.json"] = json.dumps(
        {"plate_%d" % (i + 1): {"nozzle_sequence": [], "optimal_assignment": [], "sequence": []}
         for i in range(len(spec.plates))}, separators=(",", ":"))
    for name, data in base.files.items():
        files.setdefault(name, data)
    _remap_positioned_files(files, remap_obj, plate_remap)

    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".3mf", dir=out_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for name in ("[Content_Types].xml", "_rels/.rels"):
                z.writestr(name, files.pop(name))
            for name, data in files.items():
                z.writestr(name, data if isinstance(data, bytes) else data.encode("utf-8"))
        _check(tmp, objects)
        os.chmod(tmp, 0o644)
        base.close()  # Windows cannot replace a file that is still open
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _matrix_text(transform):
    """A part's place in its object as Orca's "matrix" setting: the 4 x 4 matrix, row by row."""
    rows, t = transform
    values = [v for r in range(3) for v in (rows[r][0], rows[r][1], rows[r][2], t[r])] + [0.0, 0.0, 0.0, 1.0]
    return " ".join("%.9g" % (0.0 if abs(v) < 1e-12 else v) for v in values)


def _model_settings(spec, base, objects):
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    for obj in objects:
        meta = obj.old.meta if obj.old else [("extruder", "1")]
        out.append('  <object id="%d">\n' % obj.id)
        out.append(_meta_line("    ", "name", obj.spec.name))
        out += [_meta_line("    ", k, v) for k, v in meta]
        for k, vol in enumerate(obj.volumes):
            part_meta = []
            if vol.old_part is not None:
                part_meta = [(key, v) for key, v in vol.old_part.meta.items() if key not in PART_KEYS]
            out.append('    <part id="%d" subtype="normal_part">\n' % vol.sub_id)
            out.append(_meta_line("      ", "name", vol.spec.name))
            out.append(_meta_line("      ", "matrix", _matrix_text(vol.transform)))
            out.append(_meta_line("      ", "source_file", vol.spec.source))
            out.append(_meta_line("      ", "source_object_id", "0"))
            out.append(_meta_line("      ", "source_volume_id", str(k)))
            for axis, value in zip("xyz", vol.offset):
                out.append(_meta_line("      ", "source_offset_" + axis, repr(float(value))))
            out += [_meta_line("      ", key, v) for key, v in part_meta]
            out.append('      <mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" '
                       'facets_reversed="0" backwards_edges="0"/>\n    </part>\n')
        for extra in obj.extra_volumes:
            out.append(re.sub(r'\bid="[^"]*"', 'id="%d"' % extra.sub_id, extra.raw_part, count=1) + "\n")
        out.append('  </object>\n')
    identify = 0
    for plate_no, plate in enumerate(spec.plates):
        out.append("  <plate>\n")
        out.append(_meta_line("    ", "plater_id", plate_no + 1))
        out.append(_meta_line("    ", "plater_name", plate.name))
        kept = base.take_plate(plate.name, plate_no)
        out += [_meta_line("    ", k, v) for k, v in kept]
        for obj in objects:
            if obj.plate_no != plate_no:
                continue
            for instance in range(len(obj.transforms)):
                identify += 1
                out.append("    <model_instance>\n")
                out.append(_meta_line("      ", "object_id", obj.id))
                out.append(_meta_line("      ", "instance_id", instance))
                out.append(_meta_line("      ", "identify_id", identify))
                out.append("    </model_instance>\n")
        out.append("  </plate>\n")
    out.append("  <assemble>\n")
    for obj in objects:
        for instance, transform in enumerate(obj.transforms):
            out.append('   <assemble_item object_id="%d" instance_id="%d" transform="%s" offset="0 0 0" />\n'
                       % (obj.id, instance, _transform_text(transform)))
    out.append("  </assemble>\n</config>\n")
    return "".join(out)


def _check(path, objects):
    """Re-reads the written file: the zip is sound, every XML file parses, meshes are complete."""
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise ReloadError("written file is damaged at %s" % bad)
        for name in z.namelist():
            if name.endswith((".model", ".rels", ".xml")) or name == reload3mf.MODEL_SETTINGS:
                ET.fromstring(z.read(name))
        for obj in objects:
            for vol in obj.volumes:
                text = z.read(vol.path).decode("utf-8")
                if (text.count("<vertex ") != vol.mesh.vertex_count
                        or text.count("<triangle ") != vol.mesh.triangle_count):
                    raise ReloadError("written mesh of %s does not match" % vol.spec.name)


def build(spec, stl_cache=None, dry_run=False):
    """Builds one project. Returns (report lines, written)."""
    created = not os.path.isfile(spec.path)
    base = Base(spec.path, spec.template)
    try:
        objects, problems, bed = plan(spec, base, stl_cache)
        lines = ["%s (%s)" % (spec.shown(spec.path), "new" if created else "rebuilt")]
        if base.settings_from:
            source = "the project" if os.path.normcase(os.path.abspath(base.settings_from)) == \
                os.path.normcase(os.path.abspath(spec.path)) else spec.shown(base.settings_from)
            lines.append("  settings from %s, bed %g x %g mm" % (source, bed[2], bed[3]))
        else:
            problems.append("no printer settings: set Settings from to an Orca project made for your printer "
                            "(Orca opens a project without them as bare parts, without plates)")
        warnings = base.bed_warnings()
        height = (base.settings() or {}).get("printable_height")
        if height is not None:
            try:
                height = float(height)
            except (TypeError, ValueError):
                warnings.append("the printer's printable_height cannot be read")
                height = None
            if height is not None:
                for obj in objects:
                    tall = obj.hi[2] - obj.lo[2]
                    if tall > height + 1e-6:
                        problems.append("%s: %.1f mm tall, above the printer's %.1f mm printable height"
                                        % (obj.spec.name, tall, height))
        for plate_no, plate in enumerate(spec.plates):
            on = [o for o in objects if o.plate_no == plate_no]
            copies = sum(len(o.transforms) for o in on)
            lines.append("  plate %d %s: %d part%s, %d cop%s" % (plate_no + 1, plate.name or "", len(on),
                                                                "" if len(on) == 1 else "s", copies,
                                                                "y" if copies == 1 else "ies"))
            for obj in on:
                size = tuple(h - l for h, l in zip(obj.hi, obj.lo))
                what = ("assembly of %d parts" % len(obj.volumes) if obj.assembly
                        else os.path.basename(obj.spec.stl))
                text = "    %s  %s  %.1f x %.1f x %.1f mm" % (obj.spec.name, what, *size)
                if len(obj.transforms) > 1:
                    text += "  x%d" % len(obj.transforms)
                if obj.old is not None and any(k != "extruder" for k, _ in obj.old.meta):
                    text += "  (kept its Orca settings)"
                if obj.notes:
                    text += "  " + "; ".join(obj.notes)
                lines.append(text)
        lines += ["  problem: %s" % p for p in problems]
        lines += ["  warning: %s" % w for w in warnings]
        if problems:
            lines.append("  not written")
            return lines, False
        if not objects:
            lines.append("  no parts, not written")
            return lines, False
        if not dry_run:
            write(spec, base, objects, spec.path)
        return lines, not dry_run
    finally:
        base.close()


# --------------------------------------------------------------------------
# Reading an existing project into projects > plates > parts


class ImportedPart:
    def __init__(self, name, source, up, angle, copies, stl=None, note="", mirror=False, assembly="",
                 place=(0.0, 0.0, 0.0)):
        self.name = name
        self.source = source  # source_file as stored in the project
        self.stl = stl  # the STL found for it, or None
        self.up = up
        self.angle = angle
        self.copies = copies
        self.note = note
        self.mirror = mirror
        self.assembly = assembly  # name of the Orca object of several parts it is part of, or ""
        self.place = place  # middle of its bounding box in that object, turned like the object's first copy


def _orientation(rows):
    """(up, angle, mirror) of a rotation, a mirror factored out as one across the part's own X axis."""
    mirror = layout.det(rows) < 0.0
    if mirror:
        # Factor out the reflection as a mirror across the part's own X axis (M = R * diag(-1, 1, 1)),
        # so the ImportedPart keeps the unmirrored up/turn and the mirror flag puts it back on Build.
        rows = tuple(tuple(-c if k == 0 else c for k, c in enumerate(row)) for row in rows)
    up, angle = layout.orientation_from_matrix(rows)
    up = tuple(0.0 if abs(c) < 1e-9 else c for c in up)
    angle = math.radians(round(math.degrees(angle), 4))  # drop float noise: 30.0000001 degrees is 30
    return up, angle, mirror


def read_layout(path, finder, stl_cache=None):
    """Plates of an existing project: [(plate name, [ImportedPart])], and problem lines.

    finder: reload3mf.SourceFinder for the STLs. A file name found in several folders (a part exported with
    several tolerances) is matched by its shape."""
    stl_cache = {} if stl_cache is None else stl_cache
    project = reload3mf.Project(path)
    problems = []
    try:
        root = ET.fromstring(project.zip.read(reload3mf.MODEL_SETTINGS))
        project_dir = os.path.dirname(os.path.abspath(path))
        plates = []
        for plate in root.findall("plate"):
            meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
            ids = [{m.get("key"): m.get("value") for m in inst.findall("metadata")}.get("object_id")
                   for inst in plate.findall("model_instance")]
            plates.append((int(meta.get("plater_id") or len(plates) + 1), meta.get("plater_name") or "", ids))
        plates.sort(key=lambda p: p[0])
        out = []
        used_names = set()  # assembly names given so far: each assembly gets its own
        found = {}  # {folder: parts whose STL was found there}
        for _, name, ids in plates:
            parts, seen = [], {}
            for oid in ids:
                if oid in seen:
                    for imported in seen[oid]:
                        imported.copies += 1
                    continue
                obj = project.objects.get(oid)
                if obj is None:
                    continue
                models = [p for p in obj.parts if p.is_model_part]
                if not models:
                    problems.append("%s: has no model part, left out" % obj.name)
                    continue
                transform = reload3mf.parse_transform(project.items[obj.items[0]][1] if obj.items else None)
                if len(models) == 1:
                    part = models[0]
                    stl, note, fit = _find_source(project, part, finder, project_dir, stl_cache, (found,))
                    rows, _ = reload3mf.compose(transform, reload3mf.parse_transform(part.transform))
                    if fit:
                        rows = layout.matmul(rows, fit[0])
                    up, angle, mirror = _orientation(rows)
                    _count(found, stl)
                    seen[oid] = [ImportedPart(obj.name, part.source, up, angle, 1, stl, note, mirror=mirror)]
                else:
                    assembly = _unique(obj.name, used_names)
                    turn = (transform[0], (0.0, 0.0, 0.0))  # the first copy's turn, without its move
                    seen[oid] = []
                    mine = {}  # folders of this assembly's parts found so far
                    # Parts whose stored path leads to their STL first: their folders help pick the others
                    found_first = sorted(models, key=lambda m: finder.find(m.source, project_dir)[0] is None)
                    sources = {}
                    for part in found_first:
                        sources[part.id] = _find_source(project, part, finder, project_dir, stl_cache, (mine, found))
                        _count(mine, sources[part.id][0])
                    for part in models:
                        try:
                            mesh = project.old_mesh(part)
                        except ReloadError as e:
                            problems.append("%s, %s: %s, left out" % (obj.name, part.name, e))
                            continue
                        stl, note, fit = sources[part.id]
                        _count(found, stl)
                        middle = mesh.center()
                        if fit:
                            # The STL's middle where the stored, turned mesh has it
                            middle = reload3mf.compose(fit, (layout.IDENTITY, _stl(stl, stl_cache).center()))[1]
                        rows, t = reload3mf.compose(turn, reload3mf.parse_transform(part.transform))
                        place = tuple(round(c, 4) + 0.0 for c in reload3mf.compose((rows, t), (
                            layout.IDENTITY, middle))[1])
                        if fit:
                            rows = layout.matmul(rows, fit[0])
                        up, angle, mirror = _orientation(rows)
                        seen[oid].append(ImportedPart(part.name, part.source, up, angle, 1, stl, note, mirror=mirror,
                                                      assembly=assembly, place=place))
                parts += seen[oid]
            out.append((name, parts))
        return out, problems
    finally:
        project.close()


def _count(counter, path):
    if path:
        folder = os.path.dirname(path)
        counter[folder] = counter.get(folder, 0) + 1


def _unique(name, used):
    """name, or name 2, name 3, ... when it is taken; the one returned is taken from now on."""
    unique, n = name, 1
    while unique in used:
        n += 1
        unique = "%s %d" % (name, n)
    used.add(unique)
    return unique


def _stl(path, stl_cache):
    key = os.path.normcase(os.path.abspath(path))
    if key not in stl_cache:
        stl_cache[key] = reload3mf.read_stl(path)
    return stl_cache[key][0]


def _fit(old, stl):
    """How Orca's stored mesh sits against the STL: None when it is the STL only moved, else the (rows, translation)
    that turns the STL into it (Orca may store a part's turn in the mesh itself, as for the parts of an assembly),
    or False when it is another shape."""
    shift = tuple(o - s for o, s in zip(old.center(), stl.center()))
    if reload3mf.same_shape(old, stl, (0.0, 0.0, 0.0), shift):
        return None
    if old.vertex_count != stl.vertex_count or old.triangle_count != stl.triangle_count or old.vertex_count < 4:
        return False
    ov, sv = old.verts, stl.verts
    n = stl.vertex_count
    sm = tuple(sum(sv[k::3]) / n for k in range(3))
    om = tuple(sum(ov[k::3]) / n for k in range(3))
    s_at = lambda i: tuple(sv[3 * i + k] - sm[k] for k in range(3))
    o_at = lambda i: tuple(ov[3 * i + k] - om[k] for k in range(3))
    # Three well spread vertices: far from the middle, far from the first, far off the line through both
    a = max(range(n), key=lambda i: sum(c * c for c in s_at(i)))
    pa = s_at(a)
    b = max(range(n), key=lambda i: sum((c - d) ** 2 for c, d in zip(s_at(i), pa)))
    pb = s_at(b)

    def off_line(i):
        q = s_at(i)
        return sum(c * c for c in _cross(pa, pb, q))
    c = max(range(n), key=off_line)
    zero = (0.0, 0.0, 0.0)
    u_s, v_s = (tuple(y - x for x, y in zip(s_at(a), s_at(i))) for i in (b, c))
    u_o, v_o = (tuple(y - x for x, y in zip(o_at(a), o_at(i))) for i in (b, c))
    w_s, w_o = _cross(zero, u_s, v_s), _cross(zero, u_o, v_o)
    try:
        inv = reload3mf.invert((tuple(zip(u_s, v_s, w_s)), zero))[0]
    except ReloadError:
        return False
    extent = max(max(stl.size()), 1.0)
    tol = max(reload3mf.SAME_TOL, 1e-5 * extent)
    for sign in (1.0, -1.0):  # a turn, or a mirrored one
        rows = layout.matmul(tuple(zip(u_o, v_o, (sign * x for x in w_o))), inv)
        if any(abs(sum(rows[k][i] * rows[k][j] for k in range(3)) - (1.0 if i == j else 0.0)) > 1e-3
               for i in range(3) for j in range(3)):
            continue  # a stretch: not this shape
        t = tuple(om[k] - sum(rows[k][j] * sm[j] for j in range(3)) for k in range(3))
        if all(abs(rows[k][0] * sv[3 * i] + rows[k][1] * sv[3 * i + 1] + rows[k][2] * sv[3 * i + 2] + t[k]
                   - ov[3 * i + k]) <= tol for i in range(n) for k in range(3)):
            return rows, t
    return False


def _cross(a, b, q):
    """(b - a) x (q - a)."""
    u = tuple(y - x for x, y in zip(a, b))
    v = tuple(y - x for x, y in zip(a, q))
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def _find_source(project, part, finder, project_dir, stl_cache, prefer=()):
    """(STL path or None, note, fit): fit as _fit gives it for the found STL (None: only moved).

    A file name found in several folders is matched by its shape. When several of them have that shape, the one in
    the folder of the `prefer` counters ({folder: parts found there}, first counter first) is taken."""
    path, problem = finder.find(part.source, project_dir)
    if path is not None:
        try:
            fit = _fit(project.old_mesh(part), _stl(path, stl_cache))
        except (ReloadError, OSError):
            fit = None
        return path, "", (fit or None)
    names = finder._name_index().get(os.path.basename(part.source.replace("\\", "/")).lower(), ())
    if not names:
        return None, problem or "STL not found", None
    try:
        old = project.old_mesh(part)
    except ReloadError as e:
        return None, str(e), None
    same = []
    for cand in sorted(names):
        try:
            fit = _fit(old, _stl(cand, stl_cache))
        except (ReloadError, OSError):
            continue
        if fit is not False:
            same.append((cand, fit))
    if not same:
        return None, problem or "STL not found", None
    name = os.path.basename(same[0][0])
    if len(same) == 1:
        return same[0][0], "matched by shape among %d files named %s" % (len(names), name), same[0][1]
    for counter in prefer:
        best = max(counter.get(os.path.dirname(c), 0) for c, _ in same)
        if best > 0:
            picked = [(c, f) for c, f in same if counter.get(os.path.dirname(c), 0) == best]
            if len(picked) == 1:
                return picked[0][0], "%d files named %s have this shape; took the one in the folder of the " \
                    "other parts" % (len(same), name), picked[0][1]
    return same[0][0], "%d files named %s have this shape; took the first" % (len(same), name), same[0][1]


def common_folder(paths):
    """The folder (with / between names) that all the relative paths start with, or ''."""
    split = [p.replace("\\", "/").split("/")[:-1] for p in paths]
    if not split:
        return ""
    common = split[0]
    for s in split[1:]:
        n = 0
        while n < len(common) and n < len(s) and common[n] == s[n]:
            n += 1
        common = common[:n]
    return "/".join(common)


def strip_folder(path, folder):
    path = path.replace("\\", "/")
    return path[len(folder) + 1:] if folder and path.startswith(folder + "/") else path

