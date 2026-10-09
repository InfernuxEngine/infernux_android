# Infernux Android Platform

The official Android build plugin for [Infernux](https://github.com/ChenlizheMe/Infernux). It adds APK/AAB export for ARM64 devices and x64 emulators, with precompiled Vulkan Players and the Android host files required by an Infernux game.

[简体中文](README.zh-CN.md) · [Infernux Engine](https://github.com/ChenlizheMe/Infernux) · [Plugin Template](https://github.com/InfernuxEngine/infernux_plugin_template) · [Releases](https://github.com/InfernuxEngine/infernux_android/releases)

![Infernux Android export workflow](package/plugin_pages/media/overview.png)

## What this plugin provides

- `android-arm64` for physical devices and `android-x64-emulator` for emulators
- Precompiled native Players, CPython 3.13 runtimes, and SDL Java host sources
- Vulkan rendering, Gradle project generation, APK development builds, and AAB release builds
- Android API 26 as the minimum application level

| Package | Version | Compatible engine | Build hosts | Targets |
| --- | --- | --- | --- | --- |
| `infernux/platform-android` | 0.2.3 | Infernux 0.4.1 | Windows/Linux x64 | Android ARM64/x64 |

## Install and use

First install **Android Support** from **Infernux Hub → Installs**. The Hub-managed support kit contains the large shared JDK, Gradle, Android SDK/NDK, and Android CPython payloads; it is downloaded from the Infernux distribution service and reused by every project.

Then open **Plugins** in the Editor, select **Infernux Android Platform** from the official catalog, and import it. The Import button remains unavailable until Hub Android Support is ready. Plugin downloads use the Infernux distribution service first and GitHub Releases as the network fallback.

Choose `android-arm64` for a physical device or `android-x64-emulator` for an emulator. Development builds produce APK files and release builds produce AAB files by default. A release AAB remains unsigned until you configure `INFERNUX_ANDROID_KEYSTORE`, `INFERNUX_ANDROID_KEY_ALIAS`, and the corresponding password variables.

Ordinary users do not run CMake or download SDK components during import/export. The Player requires Vulkan; there is no OpenGL ES rendering path.

## Repository guide

The installable platform plugin lives in `package/`; the much larger shared Android Support kit is deliberately not duplicated here. Native sources, release scripts, tests, and CI remain outside the package. Maintainer builds publish both ABI payloads directly into `package/`, and a matching `v<version>` tag releases the `.inxpkg` and manifest through GitHub Actions.

## License

[MIT](LICENSE). Bundled third-party components and Android tooling retain their own licenses.
