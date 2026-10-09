# Infernux Android 平台插件

这是 [Infernux](https://github.com/ChenlizheMe/Infernux) 游戏引擎的官方 Android 构建插件。它为编辑器提供 APK/AAB 导出能力，支持 ARM64 真机和 x64 模拟器，并包含运行游戏所需的预编译 Vulkan Player 与 Android 宿主文件。

[English](README.md) · [Infernux 引擎](https://github.com/ChenlizheMe/Infernux) · [插件模板](https://github.com/InfernuxEngine/infernux_plugin_template) · [发布制品](https://github.com/InfernuxEngine/infernux_android/releases)

![Infernux Android 导出流程](package/plugin_pages/media/overview.png)

## 插件提供什么

- 面向真机的 `android-arm64` 与面向模拟器的 `android-x64-emulator`
- 预编译原生 Player、CPython 3.13 运行时和 SDL Java 宿主源码
- Vulkan 渲染、Gradle 工程生成、开发 APK 与发布 AAB
- 最低 Android API 26

| 包标识 | 版本 | 适配引擎 | 构建环境 | 目标平台 |
| --- | --- | --- | --- | --- |
| `infernux/platform-android` | 0.2.3 | Infernux 0.4.1 | Windows/Linux x64 | Android ARM64/x64 |

## 安装与导出

先在 **Infernux Hub → 安装** 中安装**安卓支持**。这套 Hub 级组件包含 JDK、Gradle、Android SDK/NDK 和 Android CPython 等体积较大的公共依赖，由 Infernux 分发服务下载，并在所有项目之间复用。

安卓支持准备完成后，在编辑器的**插件**窗口选择 **Infernux Android Platform** 并导入。在此之前，导入按钮会保持不可用。插件本身优先从 Infernux 分发服务下载；该渠道出现网络故障时，编辑器会改用 GitHub Releases。

真机请选择 `android-arm64`，x64 模拟器请选择 `android-x64-emulator`。开发构建默认生成 APK，发布构建默认生成 AAB。需要签名时，配置 `INFERNUX_ANDROID_KEYSTORE`、`INFERNUX_ANDROID_KEY_ALIAS` 和对应密码变量。

普通用户不需要运行 CMake，导入或导出过程中也不会临时下载 SDK。Player 使用 Vulkan，不提供 OpenGL ES 渲染路径。

## 仓库说明

可安装的平台插件位于 `package/`；体积更大的安卓支持由 Hub 单独管理，不会在每个插件和项目里重复一份。原生源码、发布脚本、测试与 CI 留在包外。维护者构建会把两个 ABI 的载荷直接写入 `package/`，推送与版本一致的 `v<version>` 标签后，GitHub Actions 自动发布 `.inxpkg` 和 manifest。

## 许可证

[MIT](LICENSE)。随包提供的第三方组件和 Android 工具链继续遵守各自的许可证。
