"""Packaging tests run without installing Infernux or a platform SDK."""

import json
from pathlib import Path
import tempfile
import unittest
import shutil
from runpy import run_path
from unittest.mock import patch

import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name) / "repository"
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(source / "package", self.root / "package", ignore=shutil.ignore_patterns("player"))
        metadata = json.loads((self.root / "package/inx_package.json").read_text(encoding="utf-8"))
        self.player = self.root / "package/editor/infernux_android/player"
        libraries = run_path(str(self.player.parent / "native_payload.py"))["NATIVE_LIBRARIES"]
        for abi, machine in (("arm64-v8a", 183), ("x86_64", 62)):
            native = self.player / abi / "jniLibs"
            native.mkdir(parents=True)
            header = b"\x7fELF\x02\x01" + bytes(12) + machine.to_bytes(2, "little")
            for name in libraries:
                (native / name).write_bytes(header)
            modules = native.parent / "slang-standard-module-2026.21"
            modules.mkdir()
            (modules / "module.slang-module").write_bytes(b"fixture")
            (native.parent / "Player.inxmanifest").write_text(json.dumps({
                "engine_version": metadata["engine"].removeprefix("=="), "platform": "android", "abi": abi,
                "engine_release": metadata["engine"].removeprefix("==") + "-v8",
                "python_abi": "cp313", "minimum_api": 26, "configuration": "Release",
                "native_libraries": list(libraries),
                "compiler_modules": modules.name,
            }), encoding="utf-8")
        java = self.player / "java/org/libsdl/app/SDLActivity.java"
        java.parent.mkdir(parents=True)
        java.write_text("// fixture", encoding="utf-8")
        for module in (release, release.package):
            override = patch.object(module, "__file__", str(self.root / Path(module.__file__).name))
            override.start()
            self.addCleanup(override.stop)

    def test_release_requires_both_abis(self):
        (self.player / "x86_64/jniLibs/libmain.so").unlink()
        with self.assertRaises(FileNotFoundError):
            release.build_release()
        self.assertFalse((self.root / "dist").exists())

    def test_release_requires_split_runtime_libraries(self):
        for name in ("libInfernuxAudioRuntime.so", "libInfernuxAssetRuntime.so"):
            with self.subTest(name=name):
                missing = self.player / "arm64-v8a/jniLibs" / name
                original = missing.read_bytes()
                missing.unlink()
                with self.assertRaises(FileNotFoundError):
                    release.build_release()
                missing.write_bytes(original)
        self.assertFalse((self.root / "dist").exists())

    def test_release_rejects_wrong_elf_architecture(self):
        (self.player / "x86_64/jniLibs/libmain.so").write_bytes(
            (self.player / "arm64-v8a/jniLibs/libmain.so").read_bytes())
        with self.assertRaisesRegex(ValueError, "ELF ABI"):
            release.build_release()

    def test_cmake_entry_produces_no_zip(self):
        artifact, manifest = release.build_release()
        self.assertEqual(set((self.root / "dist").iterdir()), {artifact, manifest})
        self.assertEqual(artifact.suffix, ".inxpkg")

    def test_package_and_manifest(self):
        root = Path(__file__).resolve().parents[1]
        source = json.loads((root / "package/inx_package.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            artifact, manifest = release.build_release(f"v{source['version']}", Path(temporary))
            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(document["artifact"]["name"], artifact.name)
            self.assertEqual(document["reference"], source["reference"])
            self.assertEqual(document["engine"], source["engine"])
            self.assertEqual(artifact.read_bytes()[:8], b"INXPKG\0\0")
            self.assertGreater(artifact.stat().st_size, 1024)
        self.assertTrue((root / "package/plugin_pages/media/overview.png").is_file())
        for name in ("README.md", "README.zh-CN.md"):
            self.assertIn("package/plugin_pages/media/overview.png", (root / name).read_text(encoding="utf-8"))

    def test_reject_mismatched_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Release tag must match"):
                release.build_release("v999.0.0", Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
