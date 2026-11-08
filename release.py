"""Build this repository's standalone InxPackage and GitHub release manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from runpy import run_path

import package


def _require_native_payloads(root: Path, engine_version: str) -> None:
    player = root / "package/editor/infernux_android/player"
    libraries = run_path(str(player.parent / "native_payload.py"))["NATIVE_LIBRARIES"]
    release = None
    for abi, machine in (("arm64-v8a", 183), ("x86_64", 62)):
        directory = player / abi
        manifest = json.loads((directory / "Player.inxmanifest").read_text(encoding="utf-8"))
        expected = {"engine_version": engine_version, "platform": "android", "abi": abi,
                    "python_abi": "cp313", "minimum_api": 26, "configuration": "Release"}
        if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in expected.items()):
            raise ValueError(f"Android {abi} payload must match the {engine_version} Release contract")
        current = manifest.get("engine_release")
        if not isinstance(current, str) or not (current == engine_version or current.startswith(engine_version + "-v")):
            raise ValueError(f"Android {abi} payload has no matching hotfix release identity")
        if release is not None and release != current:
            raise ValueError("Android Player ABIs must come from the same engine hotfix")
        release = current
        declared = manifest.get("native_libraries")
        if (not isinstance(declared, list)
                or any(not isinstance(name, str) for name in declared)
                or len(declared) != len(set(declared))
                or set(declared) not in (set(libraries),
                                        set(libraries) | {"libc++_shared.so"})):
            raise ValueError(f"Android {abi} native library manifest is incomplete")
        for name in declared:
            with (directory / "jniLibs" / name).open("rb") as stream:
                header = stream.read(20)
            if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != machine:
                raise ValueError(f"Android native payload has the wrong ELF ABI: {abi}/{name}")
    if not (player / "java/org/libsdl/app/SDLActivity.java").is_file():
        raise FileNotFoundError("Android plugin is missing the SDL Java host")


def build_release(tag: str | None = None, output: Path | None = None) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parent
    metadata = json.loads((root / "package/inx_package.json").read_text(encoding="utf-8"))
    expected = f"v{metadata['version']}"
    if tag is None:
        tag = expected
    if tag != expected:
        raise ValueError(f"Release tag must match package version: {expected}")
    _require_native_payloads(root, metadata["engine"].removeprefix(">="))
    destination = output if output is not None else root / "dist"
    destination.mkdir(parents=True, exist_ok=True)
    stem = metadata["reference"].replace("/", ".")
    artifact = package.build(destination / f"{stem}.inxpkg")
    manifest = destination / f"{stem}.release.json"
    document = {
        "$schema": "infernux.plugin_release",
        "reference": metadata["reference"],
        "version": metadata["version"],
        "engine": metadata["engine"],
        "engine_release": json.loads((root / "package/editor/infernux_android/player/arm64-v8a/Player.inxmanifest").read_text(encoding="utf-8"))["engine_release"],
        "artifact": {"name": artifact.name},
        "generator": "Infernux platform package release.py",
        "release_tag": tag,
    }
    manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact, manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="v followed by package/inx_package.json version")
    arguments = parser.parse_args()
    for path in build_release(arguments.tag):
        print(path)
