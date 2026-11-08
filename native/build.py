"""Release engineering: cross-build the plugin-owned Android native payload."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

import pybind11


ENGINE_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ENGINE_ROOT / "packaging"))
from android_support import ANDROID_CMAKE, ANDROID_NDK, ANDROID_PYTHON_SERIES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", required=True, type=Path)
    parser.add_argument("--python-prefix", required=True, type=Path)
    parser.add_argument("--abi", choices=("arm64-v8a", "x86_64"), required=True)
    parser.add_argument("--build-root", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--player-output", type=Path)
    args = parser.parse_args()
    sdk, prefix, build = (p.resolve() for p in (args.sdk, args.python_prefix, args.build_root))
    suffix = ".exe" if sys.platform == "win32" else ""
    cmake_bin = sdk / "cmake" / ANDROID_CMAKE / "bin"
    cmake = str(cmake_bin / f"cmake{suffix}")
    configure = [
        cmake, "-S", str(ENGINE_ROOT), "-B", str(build), "-G", "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={cmake_bin / ('ninja' + suffix)}",
        f"-DCMAKE_TOOLCHAIN_FILE={sdk / 'ndk' / ANDROID_NDK / 'build/cmake/android.toolchain.cmake'}",
        f"-DANDROID_ABI={args.abi}", "-DANDROID_PLATFORM=android-26",
        "-DCMAKE_BUILD_TYPE=Release", f"-DPython3_EXECUTABLE={sys.executable}",
        f"-DINFERNUX_HOST_PYTHON_EXECUTABLE={sys.executable}",
        f"-Dpybind11_DIR={pybind11.get_cmake_dir()}",
        "-DINFERNUX_USE_TARGET_PYTHON=ON",
        f"-DINFERNUX_TARGET_PYTHON_INCLUDE_DIR={prefix / 'include' / ('python' + ANDROID_PYTHON_SERIES)}",
        f"-DINFERNUX_TARGET_PYTHON_LIBRARY={prefix / 'lib' / ('libpython' + ANDROID_PYTHON_SERIES + '.so')}",
        f"-DINFERNUX_PYTHON_SYNC_DIR={build / 'python-sync'}",
        "-DINFERNUX_BUILD_PLAYER_HOST=OFF", "-DINFERNUX_BUILD_TESTS=OFF",
        "-DINFERNUX_BUILD_GPU_JIT_COMPILER=OFF",
        "-DINFERNUX_RELEASE_LTO=OFF", "-DINFERNUX_ENABLE_VULKAN_VALIDATION=OFF",
    ]
    if args.player_output is not None:
        configure.append(f"-DINFERNUX_ANDROID_PLAYER_OUTPUT_DIR={args.player_output.resolve()}")
    subprocess.run(configure, cwd=ENGINE_ROOT, check=True)
    subprocess.run([cmake, "--build", str(build), "--target", "prebuild_android_player",
                    "--parallel", str(args.jobs)], cwd=ENGINE_ROOT, check=True)


if __name__ == "__main__":
    main()
