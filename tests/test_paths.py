"""Tests for paths.py (the tab's roots and folders as absolute paths). Plain Python, no Blender:

    python -I tests/test_paths.py

Each test runs twice, with Linux path rules and with Windows path rules, whichever system runs the tests.
"""
import ntpath
import os
import posixpath
import sys
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orca_3mf"))

import paths  # noqa: E402


class LinuxPaths(unittest.TestCase):
    ospath = posixpath
    blend_dir = "/home/olex/Grip/blender"
    grip = "/home/olex/Grip"
    other = "/data/stl"

    def setUp(self):
        patcher = mock.patch.object(paths, "ospath", self.ospath)
        patcher.start()
        self.addCleanup(patcher.stop)

    def p(self, *names):
        """The expected path: names below the Grip folder, with this system's separator."""
        return self.ospath.join(self.grip, *names)

    def test_relative_root_starts_at_the_blend_folder(self):
        for root in ("../stl", "../stl/", "..\\stl", "..\\stl\\", "./../stl", "../blender/../stl"):
            self.assertEqual(paths.absolute(root, self.blend_dir), self.p("stl"), root)
        self.assertEqual(paths.absolute("../dir/other_dir", self.blend_dir), self.p("dir", "other_dir"))
        self.assertEqual(paths.absolute("..\\dir\\other_dir", self.blend_dir), self.p("dir", "other_dir"))
        self.assertEqual(paths.absolute("stl", self.blend_dir), self.p("blender", "stl"))
        self.assertEqual(paths.absolute("../../x", self.blend_dir), self.ospath.join(self.ospath.dirname(self.grip),
                                                                                       "x"))

    def test_blender_relative_root(self):
        self.assertEqual(paths.absolute("//../stl", self.blend_dir), self.p("stl"))
        self.assertEqual(paths.absolute("//..\\stl\\", self.blend_dir), self.p("stl"))  # saved on Windows
        self.assertEqual(paths.absolute("//", self.blend_dir), self.p("blender"))
        self.assertEqual(paths.absolute("//../orca_slicer/", self.blend_dir), self.p("orca_slicer"))

    def test_absolute_root_is_kept(self):
        self.assertEqual(paths.absolute(self.other, self.blend_dir), self.ospath.normpath(self.other))
        self.assertEqual(paths.absolute(self.other + "/", self.blend_dir), self.ospath.normpath(self.other))

    def test_empty_and_unsaved(self):
        self.assertEqual(paths.absolute("", self.blend_dir), "")
        # Not saved yet: relative to the current folder, like Blender's own //
        self.assertEqual(paths.absolute("//", ""), self.ospath.curdir)
        self.assertEqual(paths.absolute("../stl", ""), self.ospath.join("..", "stl"))

    def test_join_folders_below_a_root(self):
        root = self.p("stl")
        self.assertEqual(paths.join(root, "GripV/parts", "buttons\\default", "cap.stl"),
                         self.p("stl", "GripV", "parts", "buttons", "default", "cap.stl"))
        self.assertEqual(paths.join(root, "../shared", "", "cap.stl"), self.p("shared", "cap.stl"))
        self.assertEqual(paths.join(root, "GripV", self.other, "cap.stl"),
                         self.ospath.join(self.ospath.normpath(self.other), "cap.stl"))
        self.assertEqual(paths.join("", ""), "")

    def test_relative_to(self):
        root = self.p("stl")
        self.assertEqual(paths.relative_to(self.p("stl", "GripV", "box.stl"), root), "GripV/box.stl")
        outside = self.p("other", "box.stl")
        self.assertEqual(paths.relative_to(outside, root), outside)
        self.assertEqual(paths.relative_to(self.p("stl", "..hidden", "box.stl"), root), "..hidden/box.stl")
        self.assertEqual(paths.relative_to(self.p("stl"), self.p("stl", "GripV")), self.p("stl"))


    def test_relative_form(self):
        self.assertEqual(paths.relative_form(self.p("stl"), self.blend_dir), "../stl")
        self.assertEqual(paths.relative_form(self.p("dir", "other_dir"), self.blend_dir), "../dir/other_dir")
        self.assertEqual(paths.relative_form(self.p("blender"), self.blend_dir), ".")
        self.assertEqual(paths.relative_form(self.p("blender", "stl", "box.stl"), self.blend_dir), "stl/box.stl")
        self.assertEqual(paths.relative_form(self.p("stl"), ""), self.p("stl"))  # the .blend is not saved
        # What is saved resolves back to the same folder
        for folder in (self.p("stl"), self.p("blender"), self.ospath.normpath(self.other)):
            self.assertEqual(paths.absolute(paths.relative_form(folder, self.blend_dir), self.blend_dir), folder)


class WindowsPaths(LinuxPaths):
    ospath = ntpath
    blend_dir = "D:\\Grip\\blender"
    grip = "D:\\Grip"
    other = "E:\\data\\stl"

    def test_drive_and_forward_slashes(self):
        self.assertEqual(paths.absolute("E:/data/stl", self.blend_dir), "E:\\data\\stl")
        # A lone \\ or / starts at the top of the drive the .blend or root is on
        self.assertEqual(paths.absolute("/stl", self.blend_dir), "D:\\stl")
        self.assertEqual(paths.join(self.p("stl"), "\\GripV", "parts"), "D:\\GripV\\parts")
        self.assertEqual(paths.relative_to("E:\\data\\box.stl", self.p("stl")), "E:\\data\\box.stl")

    def test_relative_form_on_another_drive(self):
        self.assertEqual(paths.relative_form("E:\\data\\stl", self.blend_dir), "E:\\data\\stl")
        self.assertEqual(paths.relative_form("\\\\server\\share\\stl", self.blend_dir), "\\\\server\\share\\stl")
        self.assertEqual(paths.relative_form("d:\\grip\\STL", self.blend_dir), "../STL")

    def test_network_share_is_kept(self):
        self.assertEqual(paths.absolute("\\\\server\\share\\stl", self.blend_dir), "\\\\server\\share\\stl")
        self.assertEqual(paths.join(self.p("stl"), "\\\\server\\share\\box.stl"), "\\\\server\\share\\box.stl")


if __name__ == "__main__":
    unittest.main(verbosity=2)
