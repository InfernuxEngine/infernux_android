#include <Python.h>
#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>

#include <core/platform/AndroidPresentationLifecycle.h>

#include <jni.h>

#include <string>

namespace
{
bool initialize_python()
{
    const char *home = SDL_getenv("INFERNUX_PYTHON_HOME");
    if (home == nullptr || *home == '\0') {
        SDL_Log("Infernux Android host has no extracted Python home");
        return false;
    }

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.install_signal_handlers = 0;
    config.parse_argv = 0;
    PyStatus status = PyConfig_SetBytesString(&config, &config.home, home);
    if (!PyStatus_Exception(status)) {
        status = Py_InitializeFromConfig(&config);
    }
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        SDL_Log("Infernux Android Python initialization failed: %s", status.err_msg);
        return false;
    }

    PyObject *version = PySys_GetObject("version");
    const char *version_text = version != nullptr ? PyUnicode_AsUTF8(version) : nullptr;
    SDL_Log("INFERNUX_ANDROID_PYTHON_READY version=%s", version_text != nullptr ? version_text : "unknown");

    const char *native_library_dir = SDL_getenv("INFERNUX_NATIVE_LIBRARY_DIR");
    PyObject *search_path = PySys_GetObject("path");
    std::string site_packages_path = std::string(home) + "/site-packages";
    PyObject *site_packages = PyUnicode_DecodeFSDefault(site_packages_path.c_str());
    PyObject *native_path = PyUnicode_DecodeFSDefault(native_library_dir);
    if (search_path == nullptr || site_packages == nullptr || native_path == nullptr ||
        PyList_Append(search_path, site_packages) != 0 || PyList_Append(search_path, native_path) != 0) {
        Py_XDECREF(site_packages);
        Py_XDECREF(native_path);
        PyErr_Print();
        SDL_Log("Infernux Android host failed to configure Python package paths");
        return false;
    }
    Py_DECREF(site_packages);
    Py_DECREF(native_path);

    // Runtime modules are audited during Cook and imported by their actual
    // owners. Eagerly importing them here duplicated Player bootstrap work on
    // every launch and delayed the first frame without strengthening the
    // runtime contract.
    SDL_Log("INFERNUX_ANDROID_PYTHON_PATH_READY");

    return true;
}

bool run_player()
{
    const char *package_root = SDL_getenv("INFERNUX_PLAYER_ASSET_ROOT");
    const char *cache_root = SDL_getenv("INFERNUX_PLAYER_CACHE_ROOT");
    if (package_root == nullptr || *package_root == '\0' || cache_root == nullptr || *cache_root == '\0') {
        SDL_Log("Infernux Android host has no cooked Player package paths");
        return false;
    }

    PyObject *module = PyImport_ImportModule("infernux.engine.platform_player_bootstrap");
    if (module == nullptr) {
        PyErr_Print();
        SDL_Log("Infernux Android host failed to import the platform Player bootstrap");
        return false;
    }
    SDL_Log("INFERNUX_ANDROID_ENGINE_MODULE_READY");
    PyObject *entry = PyObject_GetAttrString(module, "run_platform_player");
    Py_DECREF(module);
    if (entry == nullptr || !PyCallable_Check(entry)) {
        Py_XDECREF(entry);
        PyErr_Print();
        SDL_Log("Infernux Android platform Player entry point is unavailable");
        return false;
    }
    SDL_Log("INFERNUX_ANDROID_PLAYER_BOOTSTRAP_START graphics=vulkan");
    PyObject *result = PyObject_CallFunction(entry, "ss", package_root, cache_root);
    Py_DECREF(entry);
    if (result == nullptr) {
        PyErr_Print();
        SDL_Log("Infernux Android platform Player bootstrap failed");
        return false;
    }
    Py_DECREF(result);
    SDL_Log("Infernux Android platform Player returned unexpectedly");
    return false;
}
} // namespace

extern "C" JNIEXPORT void JNICALL
Java_com_infernux_bootstrap_InfernuxActivity_nativeWaitForPresentationSuspended(JNIEnv *, jclass)
{
    SDL_Log("INFERNUX_ANDROID_SURFACE_DESTROY_WAIT_BEGIN");
    infernux::AndroidPresentationLifecycle::WaitForPresentationSuspended();
    SDL_Log("INFERNUX_ANDROID_SURFACE_DESTROY_WAIT_COMPLETE");
}

int main(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    const char *orientations = SDL_getenv("INFERNUX_ANDROID_ORIENTATIONS");
    if (orientations == nullptr || *orientations == '\0') {
        SDL_Log("Infernux Android host has no authored orientation policy");
        return 1;
    }
    if (!SDL_SetHint(SDL_HINT_ORIENTATIONS, orientations)) {
        SDL_Log("Infernux Android host could not apply its orientation policy");
    }
    if (!SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS)) {
        SDL_Log("Infernux Android host failed to initialize SDL: %s", SDL_GetError());
        return 1;
    }
    SDL_setenv_unsafe("_INFERNUX_PLAYER_MODE", "1", 1);
    SDL_setenv_unsafe("PYTHONDONTWRITEBYTECODE", "1", 1);
    const char *native_library_dir = SDL_getenv("INFERNUX_NATIVE_LIBRARY_DIR");
    if (native_library_dir == nullptr || *native_library_dir == '\0' ||
        SDL_setenv_unsafe("INFERNUX_NATIVE_MODULE_DIR", native_library_dir, 1) != 0) {
        SDL_Log("Infernux Android host failed to configure the native module directory");
        SDL_Quit();
        return 2;
    }
    if (!initialize_python()) {
        SDL_Quit();
        return 3;
    }
    SDL_Log("INFERNUX_ANDROID_HOST_READY graphics=vulkan mode=cooked-player");
    const bool player_started = run_player();
    if (Py_IsInitialized()) {
        Py_FinalizeEx();
    }
    SDL_Quit();
    return player_started ? 0 : 4;
}
