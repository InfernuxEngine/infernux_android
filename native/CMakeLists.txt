# Release-engineering target: compile the host and engine with one SDL producer.
# Ordinary Android exports consume this payload; they do not invoke this target.
if(NOT ANDROID)
    return()
endif()

set(_android_plugin "${CMAKE_CURRENT_LIST_DIR}/..")
set(_android_player "${_android_plugin}/package/editor/infernux_android/player/${ANDROID_ABI}")
add_library(InfernuxAndroidHost SHARED
    "${CMAKE_CURRENT_LIST_DIR}/main.cpp")
set_target_properties(InfernuxAndroidHost PROPERTIES OUTPUT_NAME main)
target_compile_features(InfernuxAndroidHost PRIVATE cxx_std_17)
# main.cpp bridges the Android lifecycle into the engine's presentation
# synchronisation primitive.  Keep the engine include root explicit here:
# InfernuxAndroidHost is a small plugin-side target and does not inherit the
# private include directories used by the core targets it links against.
target_include_directories(InfernuxAndroidHost PRIVATE
    "${CMAKE_SOURCE_DIR}/cpp/infernux")
target_link_libraries(InfernuxAndroidHost PRIVATE
    SDL3::SDL3
    InfernuxFoundation
    InfernuxTargetPython
    log)

set(_android_player_targets
    InfernuxAndroidHost _Infernux _InfernuxBootstrap
    ${INFERNUX_RUNTIME_DLL_TARGETS} SDL3-shared assimp Jolt)
if(INFERNUX_VULKAN_LOADER_TARGET)
    list(APPEND _android_player_targets ${INFERNUX_VULKAN_LOADER_TARGET})
endif()
set(_android_player_files)
set(_android_player_manifest_entries)
foreach(_target IN LISTS _android_player_targets)
    list(APPEND _android_player_files "$<TARGET_FILE:${_target}>")
    list(APPEND _android_player_manifest_entries "\"$<TARGET_FILE_NAME:${_target}>\"")
endforeach()

# libc++ is needed only when the selected NDK build uses the shared STL.
if(CMAKE_ANDROID_STL_TYPE STREQUAL "c++_shared")
    execute_process(
        COMMAND "${CMAKE_CXX_COMPILER}" --target=${CMAKE_CXX_COMPILER_TARGET}
            -print-file-name=libc++_shared.so
        OUTPUT_VARIABLE _android_cxx_runtime OUTPUT_STRIP_TRAILING_WHITESPACE
        COMMAND_ERROR_IS_FATAL ANY)
    if(NOT EXISTS "${_android_cxx_runtime}")
        message(FATAL_ERROR "The selected Android NDK has no shared C++ runtime")
    endif()
    list(APPEND _android_player_files "${_android_cxx_runtime}")
    get_filename_component(_android_cxx_runtime_name "${_android_cxx_runtime}" NAME)
    list(APPEND _android_player_manifest_entries "\"${_android_cxx_runtime_name}\"")
endif()

list(JOIN _android_player_manifest_entries ", " _android_player_manifest_libraries)
file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/android-player-$<CONFIG>.json"
    CONTENT "{\n  \"engine_version\": \"${INFERNUX_PACKAGE_VERSION}\",\n  \"platform\": \"android\",\n  \"abi\": \"${ANDROID_ABI}\",\n  \"python_abi\": \"cp313\",\n  \"minimum_api\": 26,\n  \"configuration\": \"$<CONFIG>\",\n  \"native_libraries\": [${_android_player_manifest_libraries}]\n}\n")
add_custom_target(prebuild_android_player
    COMMAND "${CMAKE_COMMAND}"
        "-DPLAYER_FILES=$<JOIN:${_android_player_files},|>"
        "-DPLAYER_DIR=${_android_player}"
        "-DSYMBOLS_DIR=${CMAKE_CURRENT_BINARY_DIR}/symbols/$<CONFIG>/${ANDROID_ABI}"
        "-DOBJCOPY=${CMAKE_OBJCOPY}" "-DSTRIP=${CMAKE_STRIP}"
        -P "${CMAKE_CURRENT_LIST_DIR}/publish_libraries.cmake"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "${CMAKE_CURRENT_BINARY_DIR}/android-player-$<CONFIG>.json"
        "${_android_player}/Player.inxmanifest"
    COMMAND "${CMAKE_COMMAND}" -E copy_directory
        "${CMAKE_SOURCE_DIR}/external/sdl_for_infernux/android-project/app/src/main/java"
        "${_android_plugin}/package/editor/infernux_android/player/java"
    DEPENDS ${_android_player_targets}
    COMMENT "Publishing precompiled Android Player directly into its plugin"
    COMMAND_EXPAND_LISTS VERBATIM)

# Invoke after both ABI build trees have published their native payloads.
add_custom_target(package_android_plugin
    COMMAND "${Python3_EXECUTABLE}" "${_android_plugin}/release.py"
    DEPENDS prebuild_android_player
    COMMENT "Packaging the complete Android InxPackage in its subrepository"
    VERBATIM)
