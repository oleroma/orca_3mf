"""Tests for the Orca tab in Blender. Run under Blender itself, since the bpy module is only available inside
Blender (Blender 5.2 or newer):

    blender -b -P tests/test_blender.py

Grip.blend is not opened. Each test builds a box part and writes its STL like Combi Export does (world space), sets
up a project with the tab's operators and properties, and presses Build or Import.
"""
import atexit
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.join(os.path.dirname(HERE), "orca_3mf")
ROOT = os.path.dirname(HERE)

# Keep Blender's user folders out of the real ones, and no __pycache__ in the repository
_USER = tempfile.mkdtemp(prefix="orca_reload_test_")
atexit.register(shutil.rmtree, _USER, ignore_errors=True)
os.environ["BLENDER_USER_RESOURCES"] = _USER
sys.dont_write_bytecode = True

import bpy  # noqa: E402
import addon_utils  # noqa: E402  (comes with bpy)

sys.path.insert(0, HERE)
import test_reload as fixtures  # noqa: E402
from test_build import make_template  # noqa: E402

REPO_MODULE = "orca_test"
ADDON = f"bl_ext.{REPO_MODULE}.orca_3mf"


def enable_addon():
    """Stages the add-on at the folder name Blender expects and enables it from there.

    Blender finds an extension at <repo>/<id>/. Stage a clean copy there (without __pycache__) so the test
    does not depend on how the checkout is registered in Blender.
    """
    bpy.context.preferences.use_preferences_save = False
    repos = bpy.context.preferences.extensions.repos
    if not any(r.module == REPO_MODULE for r in repos):
        repo_dir = tempfile.mkdtemp(prefix="orca_3mf_repo_")
        atexit.register(shutil.rmtree, repo_dir, ignore_errors=True)
        shutil.copytree(EXT_DIR, os.path.join(repo_dir, "orca_3mf"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        repos.new(name=REPO_MODULE, module=REPO_MODULE, custom_directory=repo_dir, source='USER')
    if addon_utils.enable(ADDON, default_set=True, handle_error=None) is None:
        raise RuntimeError(f"could not enable {ADDON}")


def mesh_object(name, verts, edges=(), faces=(), location=(0, 0, 0)):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, list(edges), list(faces))
    obj = bpy.data.objects.new(name, me)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def curve_object(name, points, bezier=True):
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '3D'
    spline = cu.splines.new('BEZIER' if bezier else 'POLY')
    if bezier:
        spline.bezier_points.add(len(points) - 1)
        for bp, p in zip(spline.bezier_points, points):
            bp.co = p
            bp.handle_left_type = bp.handle_right_type = 'VECTOR'
    else:
        spline.points.add(len(points) - 1)
        for sp, p in zip(spline.points, points):
            sp.co = (*p, 1.0)
    obj = bpy.data.objects.new(name, cu)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def list_action(level, action):
    assert bpy.ops.orca_reload.list_action(level=level, action=action) == {'FINISHED'}


class OrcaTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_homefile(use_empty=True)
        enable_addon()

    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        # The part: a 10 mm box with its top face at z = 10, exported to stl/GripV/parts/box.stl
        verts, tris = fixtures.box((10, 10, 10), center=(30, -40, 5))
        self.part = mesh_object("box", verts, faces=tris)
        self.stl = os.path.join(self.dir, "stl", "GripV", "parts", "box.stl")
        fixtures.write_stl(self.stl, (verts, tris))
        self.template = os.path.join(self.dir, "printer.3mf")
        make_template(self.template)
        settings = bpy.context.scene.orca_reload
        settings.projects.clear()
        settings.stl_root = os.path.join(self.dir, "stl")
        settings.project_root = os.path.join(self.dir, "orca")
        bpy.context.window_manager.orca_reload_log.clear()

    def add_project(self, name="GripV_parts", plates=1):
        settings = bpy.context.scene.orca_reload
        list_action('PROJECT', 'ADD')
        project = settings.projects[settings.project_index]
        project.name = name
        project.folder = "GripV"
        project.stl_folder = "GripV"
        project.template = self.template
        project.overwrite = True  # rebuild in place; test_builds_are_numbered_unless_overwrite_is_on checks off
        for _ in range(plates - 1):
            list_action('PLATE', 'ADD')
        return project

    def add_part(self, path="parts/box.stl", **values):
        list_action('PART', 'ADD')  # without the file browser: an empty part
        project = bpy.context.scene.orca_reload.projects[bpy.context.scene.orca_reload.project_index]
        plate = project.plates[project.plate_index]
        part = plate.parts[plate.part_index]
        part.path = path
        part.name = os.path.basename(path)
        for key, value in values.items():
            setattr(part, key, value)
        return part

    def build(self, index=0):
        self.assertEqual(bpy.ops.orca_reload.build(index=index), {'FINISHED'})
        return bpy.data.texts["Orca build report"].as_string()

    def out(self):
        return os.path.join(self.dir, "orca", "GripV", "GripV_parts.3mf")

    def painted_triangles(self):
        with zipfile.ZipFile(self.out()) as z:
            name = [n for n in z.namelist() if n.startswith("3D/Objects/")][0]
            tris = re.findall(r"<triangle ([^>]*)/>", z.read(name).decode())
        return [i for i, t in enumerate(tris) if "paint_seam" in t]

    def top_face(self):
        """Indices of the STL's triangles on the top face (z = 10)."""
        mesh, _ = fixtures.orca_reload.read_stl(self.stl)
        return [t for t in range(mesh.triangle_count)
                if all(abs(mesh.verts[3 * mesh.tris[3 * t + k] + 2] - 10) < 1e-6 for k in range(3))]

    def test_build_writes_the_project_with_its_seam(self):
        self.add_project()
        seam = curve_object("SeamTop", [(26, -40, 10), (30, -40, 10.2), (34, -40, 10)])
        self.add_part(seam=seam, copies=3)
        report = self.build()
        self.assertIn("(new)", report)
        self.assertIn("seam on 2 triangles from SeamTop 100%", report)
        self.assertEqual(self.painted_triangles(), self.top_face())
        with zipfile.ZipFile(self.out()) as z:
            self.assertEqual(z.read("3D/3dmodel.model").decode().count("<item "), 3)
        log = [item.text for item in bpy.context.window_manager.orca_reload_log]
        self.assertTrue(log and log[0].endswith("(new)"))

    def test_build_all_skips_unticked_and_reports_problems(self):
        self.add_project()
        self.add_part()
        other = self.add_project("other")
        other.template = ""  # no printer settings: not written
        self.add_part()
        third = self.add_project("third")
        third.use = False
        self.assertEqual(bpy.ops.orca_reload.build(), {'FINISHED'})
        report = bpy.data.texts["Orca build report"].as_string()
        self.assertTrue(os.path.isfile(self.out()))
        self.assertIn("no printer settings", report)
        self.assertNotIn("third", report)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "orca", "GripV", "other.3mf")))

    def test_marked_edges_hidden_guide_and_edge_attribute(self):
        self.add_project()
        # An edges-only mesh on the front face (y = -45), hidden with the eye icon
        guide = mesh_object("SeamFront", [(26, -45, 5), (34, -45, 5)], edges=[(0, 1)])
        guide.hide_set(True)
        self.add_part(seam=guide)
        # A mesh with faces: only its marked edge counts (a strip along the right face, x = 35)
        strip = mesh_object("SeamRight", [(35, -44, 2), (35, -36, 2), (35, -36, 2.5), (35, -44, 2.5)],
                            faces=[(0, 1, 2, 3)])
        strip.data.edges[0].use_seam = True
        self.add_part(seam=strip)
        # Geometry Nodes style: a boolean edge attribute
        gn = mesh_object("SeamGN", [(35, -44, 2), (35, -36, 2), (35, -36, 2.5), (35, -44, 2.5)],
                         faces=[(0, 1, 2, 3)])
        gn.data.attributes.new("orca_seam", 'BOOLEAN', 'EDGE').data[0].value = True
        self.add_part(seam=gn)
        report = self.build()
        for name in ("SeamFront", "SeamRight", "SeamGN"):
            self.assertIn("from %s 100%%" % name, report)
        self.assertTrue(guide.hide_get())

    def test_seam_problems_stop_the_build(self):
        self.add_project()
        sheet = mesh_object("Sheet", [(35, -44, 2), (35, -36, 2), (35, -36, 8)], faces=[(0, 1, 2)])
        self.add_part(seam=sheet)
        report = self.build()
        self.assertIn("seam Sheet has faces but no edges marked as seam", report)
        self.assertFalse(os.path.exists(self.out()))
        # A line away from the part is written, with a warning
        sheet.data.edges[0].use_seam = True
        stray = curve_object("Stray", [(100, 0, 0), (110, 0, 0)])
        bpy.context.scene.orca_reload.projects[0].plates[0].parts[0].seam = stray
        report = self.build()
        self.assertIn("seam line Stray is not on this part", report)
        self.assertTrue(os.path.exists(self.out()))

    def test_nurbs_seam_follows_the_smooth_curve(self):
        from bl_ext.orca_test.orca_3mf import blender_seams
        cu = bpy.data.curves.new("SeamNurbs", 'CURVE')
        cu.dimensions = '3D'
        cu.bevel_depth = 0.5  # ignored: the seam is the bare line
        spline = cu.splines.new('NURBS')
        spline.points.add(3)
        for sp, p in zip(spline.points, [(0, 0, 0), (1, 2, 0), (3, 2, 0), (4, 0, 0)]):
            sp.co = (*p, 1.0)
        spline.order_u, spline.resolution_u, spline.use_endpoint_u = 4, 6, True
        obj = bpy.data.objects.new("SeamNurbs", cu)
        obj.location = (10, 0, 0)
        bpy.context.scene.collection.objects.link(obj)
        bpy.context.view_layer.update()
        curves, objects = len(bpy.data.curves), len(bpy.data.objects)
        line = blender_seams.curve_polylines(obj)[0]
        self.assertEqual((len(bpy.data.curves), len(bpy.data.objects)), (curves, objects))
        self.assertGreater(len(line), 4)
        self.assertEqual(tuple(round(c, 4) for c in line[0]), (10, 0, 0))
        self.assertEqual(tuple(round(c, 4) for c in line[-1]), (14, 0, 0))
        # The smooth curve stays inside the control polygon: it never reaches the control points' y = 2
        self.assertLess(max(p.y for p in line), 1.9)
        self.assertGreater(max(p.y for p in line), 1.0)
        # A closed spline comes back closed
        spline.use_cyclic_u = True
        line = blender_seams.curve_polylines(obj)[0]
        self.assertEqual(tuple(line[0]), tuple(line[-1]))
        self.assertEqual(len(line), len(set(tuple(round(c, 6) for c in p) for p in line)) + 1)

    def test_definition_is_saved_in_the_blend_file(self):
        project = self.add_project(plates=2)
        project.plates[1].gap = 8.5
        seam = curve_object("SeamTop", [(26, -40, 10), (34, -40, 10)], bezier=False)
        self.add_part(seam=seam, up=(0, -1, 0), angle=math.radians(45), copies=2)
        bpy.context.scene.orca_reload.seam_width = 0.8
        path = os.path.join(self.dir, "saved.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        self.addCleanup(bpy.ops.wm.read_homefile, use_empty=True)
        bpy.ops.wm.open_mainfile(filepath=path)
        settings = bpy.context.scene.orca_reload
        project = settings.projects[0]
        self.assertEqual([p.name for p in project.plates], ["Plate 1", "Plate 2"])
        self.assertAlmostEqual(project.plates[1].gap, 8.5, places=5)
        part = project.plates[1].parts[0]
        self.assertEqual((part.path, part.copies, part.seam.name), ("parts/box.stl", 2, "SeamTop"))
        self.assertEqual(tuple(round(c, 6) for c in part.up), (0, -1, 0))
        self.assertAlmostEqual(part.angle, math.radians(45), places=5)
        self.assertAlmostEqual(settings.seam_width, 0.8, places=5)

    def test_import_makes_a_project_that_rebuilds_the_file(self):
        self.add_project(plates=2)
        self.add_part(up=(1, 0, 0), angle=math.radians(90), copies=2)
        self.build()
        settings = bpy.context.scene.orca_reload
        settings.projects.remove(0)  # a 3MF not in the tab yet: a new project
        self.assertEqual(bpy.ops.orca_reload.import_project(filepath=self.out()), {'FINISHED'})
        imported = settings.projects[settings.project_index]
        self.assertEqual((imported.name, imported.folder, imported.stl_folder), ("GripV_parts.3mf", "GripV",
                                                                               "GripV/parts"))
        self.assertFalse(imported.overwrite)
        imported.overwrite = True
        self.assertEqual(len(imported.plates), 2)
        part = imported.plates[1].parts[0]
        self.assertEqual((part.name, part.path, part.copies), ("box.stl", "box.stl", 2))
        self.assertEqual(tuple(round(c, 6) for c in part.up), (1, 0, 0))
        self.assertAlmostEqual(part.angle, math.radians(90), places=6)
        self.assertEqual(len(imported.plates[0].parts), 0)
        self.assertEqual([c.placed for c in part.instances], [True, True])  # each copy keeps its spot
        self.assertIn("project_settings.config", imported.profiles)
        before = self.items()
        report = self.build(settings.project_index)
        self.assertIn("(rebuilt)", report)
        self.assertEqual(self.items(), before)  # nothing moved
        # Everything Build needs is in the .blend: the 3MF can go
        os.remove(self.out())
        imported.template = ""
        report = self.build(settings.project_index)
        self.assertIn("settings from the import of", report)
        self.assertEqual(self.items(), before)
        # A new copy is nested in free space by the next Build, which records its spot
        part.copies = 3
        self.assertFalse(part.instances[2].placed)
        self.build(settings.project_index)
        self.assertTrue(part.instances[2].placed)
        self.assertEqual(self.items()[:2], before)

    def items(self):
        """The transforms of the build items, in order."""
        with zipfile.ZipFile(self.out()) as z:
            return re.findall(r'<item [^>]*transform="([^"]*)"', z.read("3D/3dmodel.model").decode())

    def test_update_from_3mf_takes_the_newest_build_back(self):
        settings = bpy.context.scene.orca_reload
        project = self.add_project()
        self.add_part(copies=2)
        self.build()
        project.overwrite = False
        self.build()  # 3mf_builds/GripV_parts.3mf
        build = os.path.join(os.path.dirname(self.out()), "3mf_builds", "GripV_parts.3mf")
        # Moved in Orca: every item 5 mm to the right
        with zipfile.ZipFile(build) as z:
            entries = {n: z.read(n) for n in z.namelist()}

        def shift(m):
            v = m.group(2).split()
            v[9] = repr(float(v[9]) + 5.0)
            return m.group(1) + " ".join(v) + '"'
        entries["3D/3dmodel.model"] = re.sub(r'(<item [^>]*transform=")([^"]*)"', shift,
                                             entries["3D/3dmodel.model"].decode()).encode()
        with zipfile.ZipFile(build, "w") as z:
            for n, data in entries.items():
                z.writestr(n, data)
        part = project.plates[0].parts[0]
        xs = [c.x for c in part.instances]
        self.assertEqual(bpy.ops.orca_reload.update_from_3mf(filepath=build), {'FINISHED'})
        self.assertEqual(len(settings.projects), 1)
        self.assertEqual(project.name, "GripV_parts")  # still the project's own 3MF
        part = project.plates[0].parts[0]
        for old, new in zip(xs, [c.x for c in part.instances]):
            self.assertAlmostEqual(new, old + 5.0, places=3)

    def test_relink_finds_missing_parts_from_a_folder_and_names_the_plate(self):
        from bl_ext.orca_test.orca_3mf import operators
        scene = bpy.context.scene
        root = os.path.join(self.dir, "stl")
        for folder in ("buttons/tight", "buttons/tight/cap_loose", "buttons/loose/cap_loose"):
            name = "button.stl" if folder == "buttons/tight" else "dpad.stl"
            fixtures.write_stl(os.path.join(root, folder, name), fixtures.box((5, 5, 2)))
        project = self.add_project()
        project.stl_folder = ""
        self.add_part(path="../../../../button.stl")  # as Import leaves a part Orca kept only the name of
        self.add_part(path="dpad.stl")
        self.add_part(path="missing.stl")
        self.add_part()  # found already: left alone
        project.plates[0].parts[3].path = "GripV/parts/box.stl"
        plate = project.plates[0]
        self.assertEqual(len(operators.missing_parts(scene, project, plate)), 3)
        folder = os.path.join(root, "buttons", "tight", "cap_loose") + os.sep
        # "Plate names are folders" off: the picked folder only, and the plate keeps its name
        self.assertEqual(bpy.ops.orca_reload.relink(directory=folder), {'FINISHED'})
        self.assertEqual(plate.name, "Plate 1")
        self.assertEqual(len(operators.missing_parts(scene, project, plate)), 2)
        # On: the folders above it too, and the plate is named after the folder
        project.plate_paths = True
        self.assertEqual(bpy.ops.orca_reload.relink(directory=folder), {'FINISHED'})
        self.assertEqual(plate.name, "buttons/tight/cap_loose")  # the deepest folder its parts share
        from bl_ext.orca_test.orca_3mf import props
        got = [os.path.normpath(props.part_path(scene, project, plate, p)) for p in plate.parts]
        self.assertEqual(got[:2], [os.path.join(root, "buttons", "tight", "button.stl"),
                                   os.path.join(root, "buttons", "tight", "cap_loose", "dpad.stl")])
        self.assertEqual(got[3], self.stl)
        self.assertEqual([p.path for p in operators.missing_parts(scene, project, plate)], ["missing.stl"])
        self.assertEqual([props.part_mark(scene, project, plate, p)[0] for p in plate.parts],
                         ['NONE', 'NONE', 'ERROR', 'NONE'])  # a file picked by hand is never marked

    def test_builds_are_numbered_unless_overwrite_is_on(self):
        project = self.add_project()
        project.overwrite = False
        self.add_part()
        self.assertIn("(new)", self.build())  # the 3MF does not exist yet: written as the project itself
        before = os.path.getmtime(self.out())
        builds = os.path.join(os.path.dirname(self.out()), "3mf_builds")
        self.assertIn("3mf_builds/GripV_parts.3mf (new build of", self.build().replace(os.sep, "/"))
        self.build()
        self.build()
        self.assertEqual(sorted(os.listdir(builds)), ["GripV_parts.3mf", "GripV_parts_001.3mf", "GripV_parts_002.3mf"])
        self.assertEqual(os.path.getmtime(self.out()), before)  # never touched
        # Turned on (the confirmation shows only when clicked): written in place again
        self.assertEqual(bpy.ops.orca_reload.overwrite(), {'FINISHED'})
        self.assertTrue(project.overwrite)
        self.assertIn("(rebuilt)", self.build())

    def test_reimport_keeps_links_and_marks_what_is_not_sure(self):
        scene = bpy.context.scene
        settings = scene.orca_reload
        project = self.add_project()
        part = self.add_part()
        part.seam = None
        self.add_part(path="parts/lid.stl")
        fixtures.write_stl(os.path.join(self.dir, "stl", "GripV", "parts", "lid.stl"), fixtures.box((4, 4, 4)))
        project.plates[0].parts[1].use = False  # only the tab knows this
        self.build()
        # Orca keeps only the file names
        with zipfile.ZipFile(self.out()) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        text = entries["Metadata/model_settings.config"].decode()
        entries["Metadata/model_settings.config"] = re.sub(r'(key="source_file" value=")[^"]*/', r"\1",
                                                           text).encode()
        with zipfile.ZipFile(self.out(), "w") as z:
            for n, data in entries.items():
                z.writestr(n, data)
        self.assertEqual(bpy.ops.orca_reload.import_project(filepath=self.out()), {'FINISHED'})
        self.assertEqual(len(settings.projects), 1)  # the same project, imported again
        plate = settings.projects[0].plates[0]
        self.assertEqual([p.path for p in plate.parts], ["box.stl"])  # its link kept; lid was not built
        self.add_part(path="lid.stl")  # the imported plate's folder is GripV/parts
        self.build()
        self.assertEqual(sorted(os.path.basename(p) for p in self.source_files()), ["box.stl", "lid.stl"])
        # Taken off the tab: gone from the 3MF
        plate.parts.remove(1)
        self.build()
        self.assertEqual([os.path.basename(p) for p in self.source_files()], ["box.stl"])

    def test_plate_names_are_folders_switch_renames_and_moves_plates(self):
        from bl_ext.orca_test.orca_3mf import props
        scene = bpy.context.scene
        root = os.path.join(self.dir, "stl", "GripV")
        for tol in ("tight", "loose"):
            fixtures.write_stl(os.path.join(root, tol, "button.stl"), fixtures.box((5, 5, 2)))
            fixtures.write_stl(os.path.join(root, tol, "cap_loose", "dpad.stl"), fixtures.box((6, 4, 2)))
        project = self.add_project()  # its STL folder is GripV
        self.add_part(path="tight/button.stl")
        self.add_part(path="tight/cap_loose/dpad.stl")
        plate = project.plates[0]

        def files():
            return [os.path.relpath(props.part_path(scene, project, plate, p), root).replace(os.sep, "/")
                    for p in plate.parts]
        self.assertEqual(files(), ["tight/button.stl", "tight/cap_loose/dpad.stl"])
        project.plate_paths = True  # named after the deepest folder its parts share; same files
        self.assertEqual(plate.name, "tight/cap_loose")
        self.assertEqual([p.path for p in plate.parts], ["../button.stl", "dpad.stl"])
        self.assertEqual(files(), ["tight/button.stl", "tight/cap_loose/dpad.stl"])
        plate.name = "loose/cap_loose"  # renaming points the plate at another folder
        self.assertEqual(files(), ["loose/button.stl", "loose/cap_loose/dpad.stl"])
        # A plate whose name isn't a folder: picking its parts' files names it once all are found
        plate.name = "Plate 1"
        plate.parts[0].path = "button.stl"  # missing now
        plate.parts[1].path = "x/dpad.stl"
        bpy.ops.orca_reload.browse_part(filepath=os.path.join(root, "tight", "button.stl"))  # the active part
        self.assertEqual(plate.name, "Plate 1")  # dpad still missing
        # The warning in the part's row picks that part's file, whichever part is active
        bpy.ops.orca_reload.browse_part(filepath=os.path.join(root, "tight", "cap_loose", "dpad.stl"), part=1)
        self.assertEqual(plate.part_index, 1)
        plate.part_index = 0
        bpy.ops.orca_reload.browse_part(filepath=os.path.join(root, "tight", "button.stl"))
        self.assertEqual(plate.name, "tight/cap_loose")
        self.assertEqual(files(), ["tight/button.stl", "tight/cap_loose/dpad.stl"])
        plate.name = "loose/cap_loose"
        project.plate_paths = False  # its STL folder takes over; same files, the name stays
        self.assertEqual((plate.name, plate.stl_folder), ("loose/cap_loose", "loose/cap_loose"))
        self.assertEqual(files(), ["loose/button.stl", "loose/cap_loose/dpad.stl"])

    def test_import_saves_outside_and_missing_stls_from_the_plate_folder(self):
        from bl_ext.orca_test.orca_3mf import props
        scene = bpy.context.scene
        settings = scene.orca_reload
        project = self.add_project()
        part = self.add_part()
        outside = os.path.join(self.dir, "elsewhere", "lid.stl")
        fixtures.write_stl(outside, fixtures.box((10, 10, 10), center=(30, -40, 5)))
        part.path = outside
        self.build()
        os.makedirs(os.path.join(self.dir, "blender"))
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(self.dir, "blender", "grip.blend"))
        self.addCleanup(bpy.ops.wm.read_homefile, use_empty=True)
        settings.stl_root = "../stl"
        settings.projects.remove(0)  # a 3MF not in the tab yet: a new project
        # Found, but outside the STL root: saved from the plate's folder, not as a full path
        self.assertEqual(bpy.ops.orca_reload.import_project(filepath=self.out()), {'FINISHED'})
        imported = settings.projects[settings.project_index]
        plate = imported.plates[0]
        self.assertEqual(plate.parts[0].path, "../elsewhere/lid.stl")
        self.assertEqual(props.part_path(scene, imported, plate, plate.parts[0]), outside)
        # A 3MF outside the 3MF root: its folder is saved from the root too
        moved = os.path.join(self.dir, "other", "GripV_parts.3mf")
        os.makedirs(os.path.dirname(moved))
        shutil.copy(self.out(), moved)
        self.assertEqual(bpy.ops.orca_reload.import_project(filepath=moved), {'FINISHED'})
        imported = settings.projects[settings.project_index]
        self.assertEqual(imported.folder, "../other")
        self.assertEqual(props.project_path(scene, imported), moved)
        # Not found: Orca's stored path, saved the same way
        os.rename(outside, outside + ".moved")
        self.assertEqual(bpy.ops.orca_reload.import_project(filepath=self.out()), {'FINISHED'})
        imported = settings.projects[settings.project_index]
        self.assertEqual(imported.plates[0].parts[0].path, "../elsewhere/lid.stl")

    def source_files(self):
        with zipfile.ZipFile(self.out()) as z:
            return re.findall(r'key="source_file" value="([^"]*)"', z.read("Metadata/model_settings.config").decode())

    def test_paths_in_the_3mf_and_the_tab_start_at_the_roots(self):
        from bl_ext.orca_test.orca_3mf import props
        scene = bpy.context.scene
        project = self.add_project()
        self.add_part()
        outside = os.path.join(self.dir, "elsewhere", "lid.stl")
        fixtures.write_stl(outside, fixtures.box((10, 10, 10)))
        self.add_part(outside)
        os.makedirs(os.path.join(self.dir, "blender"))
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(self.dir, "blender", "grip.blend"))
        self.addCleanup(bpy.ops.wm.read_homefile, use_empty=True)
        scene = bpy.context.scene
        scene.orca_reload.stl_root = "../stl"
        report = self.build()
        # Below the STL root: from the 3MF's folder. Outside it: the full path
        self.assertEqual(self.source_files(), ["../../stl/GripV/parts/box.stl", outside])
        # Shown from the .blend file's folder, the full path only outside the roots
        self.assertIn("../orca/GripV/GripV_parts.3mf (new)", report)
        self.assertNotIn(self.out(), report)
        self.assertEqual(props.shown(scene, self.out()), "../orca/GripV/GripV_parts.3mf")
        self.assertEqual(props.shown(scene, self.stl), "../stl/GripV/parts/box.stl")
        self.assertEqual(props.shown(scene, outside), outside)
        # The relative source_file still finds the STL when the project is imported and rebuilt
        self.assertEqual(bpy.ops.orca_reload.import_project(filepath=self.out()), {'FINISHED'})
        imported = scene.orca_reload.projects[scene.orca_reload.project_index]
        self.assertEqual(props.part_path(scene, imported, imported.plates[0], imported.plates[0].parts[0]), self.stl)
        self.assertIn("(rebuilt)", self.build(scene.orca_reload.project_index))

    def test_build_steps_report_progress_part_by_part(self):
        from bl_ext.orca_test.orca_3mf import operators
        settings = bpy.context.scene.orca_reload
        self.add_project(plates=2)
        self.add_part()
        self.add_part(copies=3)
        list_action('PROJECT', 'COPY')
        list_action('PROJECT', 'PASTE')
        settings.projects[1].name = "second"

        class Reports:
            def report(self, kind, text):
                self.last = (kind, text)

        reports = Reports()
        steps = list(operators._build_steps(reports, bpy.context, list(settings.projects)))
        self.assertEqual([s[1] for s in steps], [6] * len(steps))  # 2 parts + writing, for each project
        done = [s[0] for s in steps]
        self.assertEqual(done, sorted(done))
        self.assertLess(done[-1], 6)
        self.assertEqual([s[2] for s in steps], ["Part 1/4", "Part 2/4", "3MF 1/2", "Part 3/4", "Part 4/4",
                                                 "3MF 2/2"])
        self.assertEqual(reports.last, ({'INFO'}, "2 of 2 projects written"))
        # Stopped half way: the log says so, and what was built is kept
        steps = operators._build_steps(reports, bpy.context, list(settings.projects))
        for step in steps:
            if step[2] == "Part 3/4":
                break
        steps.close()
        log = bpy.data.texts["Orca build report"].as_string()
        self.assertIn("Stopped: 1 of 2 projects written", log)
        # The modal: steps on timer ticks and passes every event on, as Blender's events come (they carry no
        # timer of their own); the Cancel button stops it
        import types
        from bl_ext.orca_test.orca_3mf import props
        self.addCleanup(props.set_building, props.BUILD_IDLE)
        Build = operators.ORCA_OT_build
        tick = types.SimpleNamespace(type='TIMER', value='NOTHING')
        mouse = types.SimpleNamespace(type='MOUSEMOVE', value='NOTHING')
        for cancel in (False, True):
            op = types.SimpleNamespace(_steps=operators._build_steps(reports, bpy.context, list(settings.projects)),
                                       _start=0.0, finished=False, report=reports.report)
            op._finish = lambda context, op=op: (setattr(op, "finished", True), props.set_building(props.BUILD_IDLE))
            op._step = types.MethodType(Build._step, op)
            props.set_building(props.BUILD_ALL)
            self.assertEqual(Build.modal(op, bpy.context, mouse), {'PASS_THROUGH'})
            results = []
            while not op.finished:
                results.append(Build.modal(op, bpy.context, tick))
                if cancel and len(results) == 2:
                    self.assertEqual(bpy.ops.orca_reload.cancel_build(), {'FINISHED'})
            if cancel:
                self.assertEqual(results, [{'PASS_THROUGH'}] * 2 + [{'CANCELLED'}])
                self.assertEqual(reports.last, ({'WARNING'}, "Build cancelled"))
                self.assertIn("Stopped: 0 of 2 projects written", bpy.data.texts["Orca build report"].as_string())
            else:
                self.assertEqual(results, [{'PASS_THROUGH'}] * 6 + [{'FINISHED'}])
                self.assertEqual(reports.last, ({'INFO'}, "2 of 2 projects written"))
                self.assertTrue(bpy.context.window_manager.orca_reload_status.startswith("3MF 2/2 | "))
            self.assertEqual(props.building(), props.BUILD_IDLE)
        # With no window (a script, or Blender in the background) the button builds at once
        self.assertEqual(bpy.ops.orca_reload.build('INVOKE_DEFAULT', index=0), {'FINISHED'})
        self.assertEqual(props.building(), props.BUILD_IDLE)

    def test_stl_root_falls_back_to_combi_export(self):
        from bl_ext.orca_test.orca_3mf import props
        scene = bpy.context.scene
        scene.orca_reload.stl_root = ""
        added = not hasattr(bpy.types.Scene, "batch_stl_root_dir")
        if added:
            bpy.types.Scene.batch_stl_root_dir = bpy.props.StringProperty(subtype='DIR_PATH')
            self.addCleanup(delattr, bpy.types.Scene, "batch_stl_root_dir")
        scene.batch_stl_root_dir = os.path.join(self.dir, "stl")
        self.assertEqual(props.stl_root(scene), os.path.join(self.dir, "stl"))
        self.add_project()
        self.add_part()
        self.assertIn("(new)", self.build())

    def test_relative_roots_start_at_the_blend_file(self):
        from bl_ext.orca_test.orca_3mf import props
        os.makedirs(os.path.join(self.dir, "blender"))
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(self.dir, "blender", "grip.blend"))
        self.addCleanup(bpy.ops.wm.read_homefile, use_empty=True)
        scene = bpy.context.scene
        scene.orca_reload.stl_root = "../stl"
        scene.orca_reload.project_root = "..\\orca\\"  # typed on Windows
        self.assertEqual(props.stl_root(scene), os.path.join(self.dir, "stl"))
        self.assertEqual(props.project_root(scene), os.path.join(self.dir, "orca"))
        project = self.add_project()
        project.template = "../printer.3mf"
        part = self.add_part()
        self.assertEqual(props.part_path(scene, project, project.plates[0], part), self.stl)
        self.assertIn("(new)", self.build())
        self.assertTrue(os.path.isfile(self.out()))
        scene.orca_reload.project_root = "//../orca"
        self.assertIn("(rebuilt)", self.build())

    def test_folder_buttons_save_relative_paths(self):
        scene = bpy.context.scene
        settings = scene.orca_reload
        project = self.add_project()
        part = self.add_part()
        other = os.path.join(self.dir, "stl", "shared", "box.stl")
        fixtures.write_stl(other, fixtures.box((10, 10, 10)))
        outside = os.path.join(self.dir, "elsewhere", "box.stl")
        fixtures.write_stl(outside, fixtures.box((10, 10, 10)))
        # Not saved yet: the picked folders stay absolute
        self.assertEqual(bpy.ops.orca_reload.browse_root(root='STL_ROOT', directory=os.path.join(self.dir, "stl") +
                                                         os.sep), {'FINISHED'})
        self.assertEqual(settings.stl_root, os.path.join(self.dir, "stl"))
        os.makedirs(os.path.join(self.dir, "blender"))
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(self.dir, "blender", "grip.blend"))
        self.addCleanup(bpy.ops.wm.read_homefile, use_empty=True)
        settings, project = scene.orca_reload, scene.orca_reload.projects[0]
        part = project.plates[0].parts[0]
        for root, prop, folder, saved in (('STL_ROOT', "stl_root", "stl", "../stl"),
                                          ('PROJECT_ROOT', "project_root", "orca", "../orca"),
                                          ('STL_ROOT', "stl_root", "blender", "."),
                                          ('STL_ROOT', "stl_root", os.path.join("stl", "GripV"), "../stl/GripV")):
            self.assertEqual(bpy.ops.orca_reload.browse_root(root=root, directory=os.path.join(self.dir, folder) +
                                                             os.sep), {'FINISHED'})
            self.assertEqual(getattr(settings, prop), saved)
        settings.stl_root = "../stl"
        self.assertEqual(bpy.ops.orca_reload.browse_template(filepath=self.template), {'FINISHED'})
        self.assertEqual(project.template, "../printer.3mf")
        # A part's STL is saved from its plate's folder, with ../ when it is outside it
        self.assertEqual(bpy.ops.orca_reload.browse_part(filepath=other), {'FINISHED'})
        self.assertEqual(part.path, "../shared/box.stl")
        self.assertEqual(bpy.ops.orca_reload.browse_part(filepath=outside), {'FINISHED'})
        self.assertEqual(part.path, "../../elsewhere/box.stl")
        self.assertEqual(bpy.ops.orca_reload.browse_part(filepath=self.stl), {'FINISHED'})
        self.assertEqual(part.path, "parts/box.stl")
        self.assertIn("(new)", self.build())
        self.assertTrue(os.path.isfile(self.out()))

    def test_copy_paste_and_move(self):
        project = self.add_project()
        self.add_part(copies=4)
        list_action('PLATE', 'COPY')
        list_action('PLATE', 'PASTE')
        self.assertEqual(len(project.plates), 2)
        self.assertEqual(project.plates[1].parts[0].copies, 4)
        project.plates[1].name = "second"
        list_action('PLATE', 'UP')
        self.assertEqual([p.name for p in project.plates], ["second", "Plate 1"])
        list_action('PROJECT', 'ADD')  # a new project takes the settings file of the one before
        settings = bpy.context.scene.orca_reload
        self.assertEqual(settings.projects[1].template, self.template)
        self.assertEqual(len(settings.projects[1].plates), 1)

    def test_panels_draw(self):
        """Draws every panel and list row into a stand-in layout that checks each property, operator and icon."""
        from bl_ext.orca_test.orca_3mf import ui
        icons = set(bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items.keys())
        self.assertFalse(set(ui.ICONS.values()) - icons)
        test = self

        class Layout:
            def __init__(self):
                self.drawn = []

            def __getattr__(self, name):
                return lambda *args, **kwargs: self._call(name, args, kwargs)

            def __setattr__(self, name, value):
                object.__setattr__(self, name, value)

            def _call(self, name, args, kwargs):
                icon = kwargs.get("icon")
                if icon:
                    test.assertIn(icon, icons)
                if name == "prop":
                    test.assertIn(args[1], args[0].bl_rna.properties.keys(), args[1])
                elif name == "operator":
                    category, op = args[0].split(".")
                    test.assertTrue(hasattr(getattr(bpy.ops, category), op), args[0])
                    return type("Props", (), {})()
                elif name == "template_list":
                    test.assertIn(args[3], args[2].bl_rna.properties.keys())
                    test.assertIn(args[5], args[4].bl_rna.properties.keys())
                self.drawn.append(name)
                return Layout()

        self.add_project(plates=2)
        self.add_part(seam=curve_object("SeamTop", [(26, -40, 10), (34, -40, 10)]), copies=2)
        self.add_part(path="parts/missing.stl")
        self.build()
        bpy.context.window_manager.orca_reload_tips = True
        context = bpy.context
        for cls in ui.classes:
            if issubclass(cls, bpy.types.Panel):
                if hasattr(cls, "poll"):
                    self.assertTrue(cls.poll(context), cls.__name__)
                panel = type("P", (), {"layout": Layout()})()
                if hasattr(cls, "draw_header"):
                    cls.draw_header(panel, context)
                cls.draw(panel, context)
        project = context.scene.orca_reload.projects[0]
        rows = [(ui.ORCA_UL_projects, context.scene.orca_reload, project),
                (ui.ORCA_UL_plates, project, project.plates[1]),
                (ui.ORCA_UL_parts, project.plates[1], project.plates[1].parts[0]),
                (ui.ORCA_UL_parts, project.plates[1], project.plates[1].parts[1]),
                (ui.ORCA_UL_log, context.window_manager, context.window_manager.orca_reload_log[0])]
        for cls, data, item in rows:
            cls.draw_item(None, context, Layout(), data, item, 0, None, "", 0)
        # While a build runs, its progress bar takes the place of the project row or of the 3MF root
        wm = context.window_manager
        from bl_ext.orca_test.orca_3mf import props
        self.addCleanup(props.set_building, props.BUILD_IDLE)
        for building in (0, -1):
            props.set_building(building)
            wm.orca_reload_status = "Part 1/2 | 0.4s"
            layout = Layout()
            ui.ORCA_UL_projects.draw_item(None, context, layout, context.scene.orca_reload, project, 0, None, "", 0)
            panel = type("P", (), {"layout": Layout()})()
            ui.VIEW3D_PT_orca_reload.draw(panel, context)

    def test_panels_and_operators_are_registered(self):
        for name in ("VIEW3D_PT_orca_reload", "VIEW3D_PT_orca_reload_info", "VIEW3D_PT_orca_reload_projects",
                     "VIEW3D_PT_orca_reload_plates", "VIEW3D_PT_orca_reload_parts"):
            self.assertTrue(hasattr(bpy.types, name), name)
        for op in ("list_action", "add_parts", "browse_part", "browse_root", "browse_template", "import_project",
                   "build", "cancel_build", "clear_log"):
            self.assertTrue(hasattr(bpy.ops.orca_reload, op), op)


if __name__ == "__main__":
    # Run through Blender (`blender -b -P test_blender.py`), whose Python exposes the full command line in
    # sys.argv; pass an explicit argv so unittest does not try to parse Blender's own flags.
    unittest.main(argv=[os.path.abspath(__file__)], verbosity=2)
