// 通用挂起注入启动器：CreateProcess(CREATE_SUSPENDED) + 多 DLL LoadLibraryW + Resume
// 用于运行时补丁组合注入。
//
// LoadLibraryW 解析策略（2026-07-20）：
// - 本启动器与 Stage3 客户端同为 x86（-m32）。
// - Windows 对 kernel32 的 ASLR 在同一次启动、同架构进程间共享基址，
//   因此本进程 GetProcAddress(LoadLibraryW) 可直接作为远程线程入口。
// - Toolhelp/EnumProcessModules 在 CREATE_SUSPENDED 或连续 LoadLibrary 后常抖动
//   （ERROR_BAD_LENGTH / 空快照），仅作校验与诊断，不再作为唯一依赖。

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <psapi.h>

#include <cstdint>
#include <cstring>
#include <cwchar>
#include <cstdio>
#include <string>
#include <vector>
#include "py90_multi_instance.h"
#include "py90_mutex_scope.h"
#include "py90_client_protection.h"

#pragma comment(lib, "psapi.lib")

static const wchar_t* kDefaultExe =
    L"USDOF\\DFO.exe";
static const wchar_t* kDefaultWorkdir = L"USDOF";
static const wchar_t* kDefaultDfoArg =
    L"9?127.0.0.1?7001?3012345?e3210f9193c9558c3638523332a864a2d93222fc94a85a9d966ab135c0125814?0?0?0?0?0?2?0?0?0?0?0?0?0?0?0?0";
static const wchar_t* kNativeLoadoutThreadPathEnv =
    L"USDOF_NATIVE_LOADOUT_THREAD_PATH";
static const wchar_t* kFuncExtendThreadPathEnv =
    L"USDOF_FUNC_EXTEND_THREAD_PATH";
static const wchar_t* kStage3MainWindowClass = L"Dungeon Fighter Online";
static const DWORD kNativeLoadoutWindowTimeoutMs = 30000;

#pragma pack(push, 1)
struct NativeLoadoutThreadBootstrap {
    char magic[4];
    uint16_t version;
    uint16_t reserved;
    uint32_t process_id;
    uint32_t thread_id;
};
#pragma pack(pop)

static_assert(
    sizeof(NativeLoadoutThreadBootstrap) == 16,
    "native loadout thread bootstrap layout");

// 缓存：同 pid 多 DLL 注入时避免反复扫模块表
static DWORD g_cached_ll_pid = 0;
static FARPROC g_cached_ll = nullptr;

static std::wstring quote_arg(const std::wstring& value) {
    std::wstring out = L"\"";
    for (wchar_t ch : value) {
        if (ch == L'\"') {
            out += L"\\\"";
        } else {
            out += ch;
        }
    }
    out += L"\"";
    return out;
}

static bool file_exists(const std::wstring& path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

static bool dir_exists(const std::wstring& path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY) != 0;
}

static std::wstring full_path(const std::wstring& path) {
    DWORD required = GetFullPathNameW(path.c_str(), 0, nullptr, nullptr);
    if (required == 0) {
        return path;
    }
    std::wstring result(required, L'\0');
    DWORD written = GetFullPathNameW(path.c_str(), required, &result[0], nullptr);
    if (written == 0 || written >= required) {
        return path;
    }
    result.resize(written);
    return result;
}

static std::wstring environment_value(const wchar_t* name) {
    DWORD required = GetEnvironmentVariableW(name, nullptr, 0);
    if (required == 0) {
        return std::wstring();
    }
    std::wstring value(required, L'\0');
    DWORD written = GetEnvironmentVariableW(name, &value[0], required);
    if (written == 0 || written >= required) {
        return std::wstring();
    }
    value.resize(written);
    return value;
}

static bool write_stage3_thread_bootstrap(
    const std::wstring& path,
    const char magic[4],
    const wchar_t* label,
    DWORD process_id,
    DWORD thread_id) {
    if (path.empty()) {
        return true;
    }

    NativeLoadoutThreadBootstrap bootstrap = {};
    memcpy(bootstrap.magic, magic, sizeof(bootstrap.magic));
    bootstrap.version = 1;
    bootstrap.process_id = process_id;
    bootstrap.thread_id = thread_id;

    const std::wstring temp_path =
        path + L".tmp." + std::to_wstring(process_id);
    HANDLE file = CreateFileW(
        temp_path.c_str(),
        GENERIC_WRITE,
        0,
        nullptr,
        CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        std::fwprintf(
            stderr,
            L"[launch] %ls bootstrap create failed err=%lu path=%ls\n",
            label,
            GetLastError(),
            path.c_str());
        return false;
    }

    DWORD written = 0;
    const bool write_ok = WriteFile(
            file,
            &bootstrap,
            sizeof(bootstrap),
            &written,
            nullptr) &&
        written == sizeof(bootstrap) && FlushFileBuffers(file);
    const DWORD write_error = write_ok ? ERROR_SUCCESS : GetLastError();
    CloseHandle(file);
    if (!write_ok) {
        DeleteFileW(temp_path.c_str());
        std::fwprintf(
            stderr,
            L"[launch] %ls bootstrap write failed err=%lu path=%ls\n",
            label,
            write_error,
            path.c_str());
        return false;
    }
    if (!MoveFileExW(
            temp_path.c_str(),
            path.c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        const DWORD error = GetLastError();
        DeleteFileW(temp_path.c_str());
        std::fwprintf(
            stderr,
            L"[launch] %ls bootstrap publish failed err=%lu path=%ls\n",
            label,
            error,
            path.c_str());
        return false;
    }

    std::fwprintf(
        stdout,
        L"[launch] %ls bootstrap pid=%lu tid=%lu path=%ls\n",
        label,
        process_id,
        thread_id,
        path.c_str());
    fflush(stdout);
    return true;
}

static bool write_native_loadout_thread_bootstrap(
    const std::wstring& path,
    DWORD process_id,
    DWORD thread_id) {
    return write_stage3_thread_bootstrap(
        path, "ULNT", L"native-loadout", process_id, thread_id);
}

static bool write_func_extend_thread_bootstrap(
    const std::wstring& path,
    DWORD process_id,
    DWORD thread_id) {
    return write_stage3_thread_bootstrap(
        path, "UTBT", L"func-extend", process_id, thread_id);
}

static void print_usage(const wchar_t* argv0) {
    std::fwprintf(
        stderr,
        L"Usage: %ls [--exe PATH] [--workdir DIR] [--dll PATH]... [--args DFO_ARG]\n"
        L"\n"
        L"  --dll  may be repeated; each DLL is injected before ResumeThread.\n"
        L"Defaults for exe/workdir/args are relative to the repository root.\n",
        argv0);
}

static bool parse_args(
    int argc,
    wchar_t** argv,
    std::wstring* exe,
    std::wstring* workdir,
    std::vector<std::wstring>* dlls,
    std::wstring* dfo_arg) {
    for (int i = 1; i < argc; ++i) {
        std::wstring opt = argv[i];
        if (opt == L"--help" || opt == L"-h") {
            print_usage(argv[0]);
            return false;
        }
        if (i + 1 >= argc) {
            std::fwprintf(stderr, L"Missing value for %ls\n", opt.c_str());
            print_usage(argv[0]);
            return false;
        }
        const wchar_t* value = argv[++i];
        if (opt == L"--exe") {
            *exe = value;
        } else if (opt == L"--workdir") {
            *workdir = value;
        } else if (opt == L"--dll") {
            dlls->push_back(value);
        } else if (opt == L"--args") {
            *dfo_arg = value;
        } else {
            std::fwprintf(stderr, L"Unknown option: %ls\n", opt.c_str());
            print_usage(argv[0]);
            return false;
        }
    }
    return true;
}

static bool enable_debug_privilege() {
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &token)) {
        return false;
    }

    TOKEN_PRIVILEGES tp = {};
    tp.PrivilegeCount = 1;
    if (!LookupPrivilegeValueW(nullptr, L"SeDebugPrivilege", &tp.Privileges[0].Luid)) {
        CloseHandle(token);
        return false;
    }
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;

    AdjustTokenPrivileges(token, FALSE, &tp, sizeof(tp), nullptr, nullptr);
    bool ok = GetLastError() == ERROR_SUCCESS;
    CloseHandle(token);
    return ok;
}

static FARPROC local_loadlibraryw() {
    HMODULE local_kernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!local_kernel32) {
        return nullptr;
    }
    return GetProcAddress(local_kernel32, "LoadLibraryW");
}

// Toolhelp：模块列表变动时会 ERROR_BAD_LENGTH / 空快照；仅作辅助。
static uintptr_t find_remote_module_base_toolhelp(DWORD pid, const wchar_t* module_name) {
    for (int attempt = 0; attempt < 40; ++attempt) {
        HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
        if (snap == INVALID_HANDLE_VALUE) {
            Sleep(15);
            continue;
        }

        MODULEENTRY32W entry = {};
        entry.dwSize = sizeof(entry);
        uintptr_t result = 0;
        if (Module32FirstW(snap, &entry)) {
            do {
                if (_wcsicmp(entry.szModule, module_name) == 0) {
                    result = reinterpret_cast<uintptr_t>(entry.modBaseAddr);
                    break;
                }
            } while (Module32NextW(snap, &entry));
        }
        CloseHandle(snap);
        if (result != 0) {
            return result;
        }
        Sleep(15);
    }
    return 0;
}

// PSAPI EnumProcessModules：对已初始化进程通常比 Toolhelp 稳。
static uintptr_t find_remote_module_base_psapi(HANDLE process, const wchar_t* module_name) {
    if (!process) {
        return 0;
    }
    HMODULE mods[512];
    DWORD needed = 0;
    if (!EnumProcessModulesEx(process, mods, sizeof(mods), &needed, LIST_MODULES_32BIT | LIST_MODULES_DEFAULT)) {
        // 部分环境无 Ex：回退
        if (!EnumProcessModules(process, mods, sizeof(mods), &needed)) {
            return 0;
        }
    }
    const DWORD count = needed / sizeof(HMODULE);
    wchar_t name[MAX_PATH];
    for (DWORD i = 0; i < count && i < 512; ++i) {
        if (GetModuleBaseNameW(process, mods[i], name, MAX_PATH) == 0) {
            continue;
        }
        if (_wcsicmp(name, module_name) == 0) {
            return reinterpret_cast<uintptr_t>(mods[i]);
        }
    }
    return 0;
}

// 解析远程 LoadLibraryW：
// 1) 优先本进程地址（同 x86 + 系统 DLL 共享基址）—— CREATE_SUSPENDED 也可用
// 2) 若能扫到远程 kernel32，用 offset 校验/纠正（极端 ASLR 差异）
// 3) 结果按 pid 缓存，避免 6 个 DLL 连注时反复扫表失败
static FARPROC remote_loadlibraryw_address(HANDLE process, DWORD pid) {
    if (g_cached_ll != nullptr && g_cached_ll_pid == pid) {
        return g_cached_ll;
    }

    FARPROC local_ll = local_loadlibraryw();
    if (!local_ll) {
        return nullptr;
    }

    HMODULE local_kernel32 = GetModuleHandleW(L"kernel32.dll");
    uintptr_t local_base = reinterpret_cast<uintptr_t>(local_kernel32);
    uintptr_t local_ll_u = reinterpret_cast<uintptr_t>(local_ll);
    if (local_base == 0 || local_ll_u < local_base) {
        return nullptr;
    }
    const uintptr_t offset = local_ll_u - local_base;

    // 尝试用远程 kernel32 基址做 offset 纠正（成功则更保险）
    uintptr_t remote_base = find_remote_module_base_psapi(process, L"kernel32.dll");
    if (remote_base == 0) {
        remote_base = find_remote_module_base_toolhelp(pid, L"kernel32.dll");
    }

    FARPROC result = local_ll;
    if (remote_base != 0) {
        result = reinterpret_cast<FARPROC>(remote_base + offset);
    }
    // remote_base==0：CREATE_SUSPENDED 常见；依赖系统 DLL 共享基址，用 local_ll

    g_cached_ll_pid = pid;
    g_cached_ll = result;
    return result;
}

static bool inject_dll(HANDLE process, DWORD pid, const std::wstring& dll_path) {
    FARPROC remote_loadlibrary = nullptr;
    for (int attempt = 0; attempt < 20; ++attempt) {
        remote_loadlibrary = remote_loadlibraryw_address(process, pid);
        if (remote_loadlibrary) {
            break;
        }
        // 清缓存，允许下一轮重新解析
        g_cached_ll = nullptr;
        g_cached_ll_pid = 0;
        Sleep(50);
    }
    if (!remote_loadlibrary) {
        std::fwprintf(stderr, L"Could not resolve remote LoadLibraryW (pid=%lu)\n", pid);
        return false;
    }

    SIZE_T bytes = (dll_path.size() + 1) * sizeof(wchar_t);
    void* remote_mem = VirtualAllocEx(process, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remote_mem) {
        std::fwprintf(stderr, L"VirtualAllocEx failed: %lu for %ls\n", GetLastError(), dll_path.c_str());
        return false;
    }

    SIZE_T written = 0;
    if (!WriteProcessMemory(process, remote_mem, dll_path.c_str(), bytes, &written) || written != bytes) {
        std::fwprintf(stderr, L"WriteProcessMemory failed: %lu for %ls\n", GetLastError(), dll_path.c_str());
        VirtualFreeEx(process, remote_mem, 0, MEM_RELEASE);
        return false;
    }

    HANDLE thread = CreateRemoteThread(
        process,
        nullptr,
        0,
        reinterpret_cast<LPTHREAD_START_ROUTINE>(remote_loadlibrary),
        remote_mem,
        0,
        nullptr);
    if (!thread) {
        const DWORD err = GetLastError();
        std::fwprintf(
            stderr,
            L"CreateRemoteThread failed: %lu for %ls (LoadLibraryW=%p)\n",
            err,
            dll_path.c_str(),
            reinterpret_cast<void*>(remote_loadlibrary));
        // 地址可能无效：清缓存下次重试
        g_cached_ll = nullptr;
        g_cached_ll_pid = 0;
        VirtualFreeEx(process, remote_mem, 0, MEM_RELEASE);
        return false;
    }

    DWORD wait_result = WaitForSingleObject(thread, 20000);
    DWORD remote_ret = 0;
    GetExitCodeThread(thread, &remote_ret);
    CloseHandle(thread);
    VirtualFreeEx(process, remote_mem, 0, MEM_RELEASE);

    if (wait_result != WAIT_OBJECT_0) {
        std::fwprintf(stderr, L"LoadLibraryW remote thread timed out for %ls\n", dll_path.c_str());
        return false;
    }
    if (remote_ret == 0) {
        std::fwprintf(stderr, L"Remote LoadLibraryW returned NULL for %ls\n", dll_path.c_str());
        return false;
    }

    std::fwprintf(stdout, L"[launch] injected %ls (remote_module=0x%lX)\n", dll_path.c_str(), remote_ret);
    fflush(stdout);
    // 注入成功后模块表会变；保留 LoadLibraryW 缓存（地址本身不变），不强制清
    return true;
}

static bool wait_until_remote_loader_ready(HANDLE process, DWORD pid, DWORD timeout_ms) {
    DWORD elapsed = 0;
    while (elapsed <= timeout_ms) {
        // 进程仍存活？
        DWORD code = 0;
        if (GetExitCodeProcess(process, &code) && code != STILL_ACTIVE) {
            std::fwprintf(stderr, L"Target process exited during loader wait (code=%lu)\n", code);
            return false;
        }
        // 任一路径能拿到 LoadLibraryW 即就绪
        g_cached_ll = nullptr;
        g_cached_ll_pid = 0;
        if (remote_loadlibraryw_address(process, pid)) {
            return true;
        }
        Sleep(50);
        elapsed += 50;
    }
    return false;
}

static bool inject_all(HANDLE process, DWORD pid, const std::vector<std::wstring>& dlls) {
    for (const auto& dll : dlls) {
        bool ok = false;
        for (int attempt = 0; attempt < 6; ++attempt) {
            if (inject_dll(process, pid, dll)) {
                ok = true;
                break;
            }
            // 单 DLL 失败：清缓存 + 稍等，避免 Toolhelp 抖动后连续误判
            g_cached_ll = nullptr;
            g_cached_ll_pid = 0;
            Sleep(80);
        }
        if (!ok) {
            std::fwprintf(stderr, L"[launch] giving up on DLL: %ls\n", dll.c_str());
            return false;
        }
        // 连续 LoadLibrary 之间略歇，降低远程加载器/模块表抖动
        Sleep(30);
    }
    return true;
}

struct Stage3MainWindowProbe {
    DWORD process_id = 0;
    HWND hwnd = nullptr;
    DWORD thread_id = 0;
    LONG area = -1;
};

static BOOL CALLBACK find_stage3_main_window_callback(HWND hwnd, LPARAM value) {
    Stage3MainWindowProbe* probe =
        reinterpret_cast<Stage3MainWindowProbe*>(value);
    if (!probe || !hwnd || !IsWindowVisible(hwnd) ||
        GetWindow(hwnd, GW_OWNER) != nullptr) {
        return TRUE;
    }

    DWORD process_id = 0;
    const DWORD thread_id = GetWindowThreadProcessId(hwnd, &process_id);
    if (thread_id == 0 || process_id != probe->process_id) {
        return TRUE;
    }

    wchar_t class_name[64] = {};
    if (GetClassNameW(
            hwnd,
            class_name,
            static_cast<int>(sizeof(class_name) / sizeof(class_name[0]))) == 0 ||
        wcscmp(class_name, kStage3MainWindowClass) != 0) {
        return TRUE;
    }

    RECT client = {};
    if (!GetClientRect(hwnd, &client)) {
        return TRUE;
    }
    const LONG width = client.right - client.left;
    const LONG height = client.bottom - client.top;
    if (width < 200 || height < 150) {
        return TRUE;
    }
    const LONG area = width * height;
    if (area > probe->area) {
        probe->hwnd = hwnd;
        probe->thread_id = thread_id;
        probe->area = area;
    }
    return TRUE;
}

static bool wait_for_stage3_main_window(
    DWORD process_id,
    DWORD timeout_ms,
    DWORD* thread_id) {
    if (!thread_id || process_id == 0) {
        return false;
    }
    DWORD elapsed = 0;
    while (elapsed <= timeout_ms) {
        Stage3MainWindowProbe probe = {};
        probe.process_id = process_id;
        if (EnumWindows(
                find_stage3_main_window_callback,
                reinterpret_cast<LPARAM>(&probe)) &&
            probe.hwnd && probe.thread_id != 0) {
            *thread_id = probe.thread_id;
            return true;
        }
        Sleep(50);
        elapsed += 50;
    }
    return false;
}

int wmain(int argc, wchar_t** argv) {
    if(argc==3 && wcscmp(argv[1],L"--verify-client-protection")==0) {
        std::array<unsigned char,32> key={};py90_protection::Image image;
        bool valid=py90_protection::read_key(key)&&image.load(argv[2],key);
        SecureZeroMemory(key.data(),key.size());
        if(!valid){std::fputs("[protection] authentication failed\n",stderr);return 6;}
        unsigned char digest[32]={};if(!image.diagnostic_sha(digest))return 6;
        std::fputs("[protection] decoded_sha256=",stdout);
        for(unsigned char value:digest)std::printf("%02x",value);
        std::printf(" code_bytes=%u\n",py90_protection::kCodeBytes);return 0;
    }
    std::wstring exe = kDefaultExe;
    std::wstring workdir = kDefaultWorkdir;
    std::vector<std::wstring> dlls;
    std::wstring dfo_arg = kDefaultDfoArg;

    if (!parse_args(argc, argv, &exe, &workdir, &dlls, &dfo_arg)) {
        return 2;
    }

    if (dlls.empty()) {
        std::fwprintf(stderr, L"At least one --dll is required\n");
        print_usage(argv[0]);
        return 2;
    }

    exe = full_path(exe);
    workdir = full_path(workdir);
    for (auto& d : dlls) {
        d = full_path(d);
    }

    if (!file_exists(exe)) {
        std::fwprintf(stderr, L"Missing DFO exe: %ls\n", exe.c_str());
        return 1;
    }
    if (!dir_exists(workdir)) {
        std::fwprintf(stderr, L"Missing workdir: %ls\n", workdir.c_str());
        return 1;
    }
    for (const auto& d : dlls) {
        if (!file_exists(d)) {
            std::fwprintf(stderr, L"Missing DLL: %ls\n", d.c_str());
            return 1;
        }
    }

    const std::wstring native_loadout_thread_path =
        environment_value(kNativeLoadoutThreadPathEnv);
    const std::wstring func_extend_thread_path =
        environment_value(kFuncExtendThreadPathEnv);
    if (!native_loadout_thread_path.empty() &&
        !DeleteFileW(native_loadout_thread_path.c_str())) {
        const DWORD error = GetLastError();
        if (error != ERROR_FILE_NOT_FOUND) {
            std::fwprintf(
                stderr,
                L"[launch] native-loadout stale bootstrap remove failed err=%lu path=%ls\n",
                error,
                native_loadout_thread_path.c_str());
            return 1;
        }
    }
    if (!func_extend_thread_path.empty() &&
        !DeleteFileW(func_extend_thread_path.c_str())) {
        const DWORD error = GetLastError();
        if (error != ERROR_FILE_NOT_FOUND) {
            std::fwprintf(
                stderr,
                L"[launch] func-extend stale bootstrap remove failed err=%lu path=%ls\n",
                error,
                func_extend_thread_path.c_str());
            return 1;
        }
    }

    std::array<unsigned char,32> code_key={};py90_protection::Image protected_image;
    bool code_ready=py90_protection::read_key(code_key)&&protected_image.load(exe,code_key);
    SecureZeroMemory(code_key.data(),code_key.size());
    if(!code_ready){std::fwprintf(stderr,L"[launch] protected client is missing, damaged or does not match this launcher\n");return 6;}
    enable_debug_privilege();

    // WinMain receives the raw command tail. Stage3's 9?... protocol must
    // start with 9, including when the executable path itself contains spaces.
    // These fields are one whitespace-free token, validated by the HTTP client.
    if (dfo_arg.empty() ||
        dfo_arg.find_first_of(L" \t\r\n\v\f\"") != std::wstring::npos) {
        std::fwprintf(stderr, L"Invalid native launch argument format\n");
        return 2;
    }
    std::wstring cmdline = quote_arg(exe) + L" " + dfo_arg;
    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};

    std::fwprintf(stdout, L"[launch] exe=%ls\n", exe.c_str());
    std::fwprintf(stdout, L"[launch] workdir=%ls\n", workdir.c_str());
    if (!native_loadout_thread_path.empty()) {
        std::fwprintf(
            stdout,
            L"[launch] native-loadout bootstrap path=%ls\n",
            native_loadout_thread_path.c_str());
    }
    if (!func_extend_thread_path.empty()) {
        std::fwprintf(
            stdout,
            L"[launch] func-extend bootstrap path=%ls\n",
            func_extend_thread_path.c_str());
    }
    for (const auto& d : dlls) {
        std::fwprintf(stdout, L"[launch] dll=%ls\n", d.c_str());
    }
    fflush(stdout);

    if (!CreateProcessW(
            exe.c_str(),
            &cmdline[0],
            nullptr,
            nullptr,
            FALSE,
            CREATE_SUSPENDED,
            nullptr,
            workdir.c_str(),
            &si,
            &pi)) {
        std::fwprintf(stderr, L"CreateProcessW failed: %lu\n", GetLastError());
        return 1;
    }

    if(!protected_image.apply(pi.hProcess)) {
        std::fwprintf(stderr,L"[launch] protected image initialization failed before DLL loading\n");
        TerminateProcess(pi.hProcess,6);CloseHandle(pi.hThread);CloseHandle(pi.hProcess);return 6;
    }
    std::printf("[launch] protected code restored bytes=%u\n",py90_protection::kCodeBytes);
    if (environment_value(L"DNF_PYTHON_ALLOW_SECOND_INSTANCE") == L"1" &&
        !allow_stage3_instance(pi.hProcess)) {
        std::fwprintf(stderr,L"[launch] instance signature check failed; suspended child stopped\n");
        TerminateProcess(pi.hProcess,1);
        CloseHandle(pi.hThread);CloseHandle(pi.hProcess);return 1;
    }
    g_cached_ll = nullptr;
    g_cached_ll_pid = 0;

    // Py90 fails closed while the main thread is still suspended. No early-resume retry.
    SetEnvironmentVariableA("DNF_PYTHON_PVF_KEYSET", nullptr);
    bool resumed = false;
    bool injected = inject_all(pi.hProcess, pi.dwProcessId, dlls);
    bool pvf_ready = true;
    if (environment_value(L"DNF_PYTHON_PVF_REQUIRED") == L"1") {
        const std::wstring event_name = environment_value(L"DNF_PYTHON_PVF_READY_EVENT");
        HANDLE ready = event_name.empty() ? nullptr : OpenEventW(SYNCHRONIZE, FALSE, event_name.c_str());
        pvf_ready = ready && WaitForSingleObject(ready, 5000) == WAIT_OBJECT_0;
        if (ready) CloseHandle(ready);
    }
    if (!injected || !pvf_ready) {
        std::fwprintf(stderr, L"[launch] Py90 suspended injection / PVF signature gate failed; game stopped before resume\n");
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return 1;
    }
    if(environment_value(L"DNF_PYTHON_ALLOW_SECOND_INSTANCE")==L"1") {
        if(!install_stage3_mutex_scope(pi.hProcess,pi.dwProcessId)){
            std::fwprintf(stderr,L"[launch] instance mutex scope ownership failed; suspended child stopped\n");
            TerminateProcess(pi.hProcess,1);CloseHandle(pi.hThread);CloseHandle(pi.hProcess);return 1;
        }
        unsigned char branch=0;SIZE_T done=0;
        if(ReadProcessMemory(pi.hProcess,reinterpret_cast<void*>(0x02c370a2),&branch,1,&done)&&done==1)
            std::fwprintf(stdout,L"[launch] instance branch before resume=%02X\n",static_cast<unsigned>(branch));
    }
    if (!resumed && ResumeThread(pi.hThread) == static_cast<DWORD>(-1)) {
        std::fwprintf(stderr, L"ResumeThread failed: %lu\n", GetLastError());
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return 1;
    }
    resumed = true;

    if (!native_loadout_thread_path.empty() || !func_extend_thread_path.empty()) {
        DWORD window_thread_id = 0;
        if (!wait_for_stage3_main_window(
                pi.dwProcessId,
                kNativeLoadoutWindowTimeoutMs,
                &window_thread_id)) {
            std::fwprintf(
                stderr,
                L"[launch] WARN main-window bootstrap timeout pid=%lu\n",
                pi.dwProcessId);
        } else {
            if (!native_loadout_thread_path.empty() &&
                !write_native_loadout_thread_bootstrap(
                    native_loadout_thread_path,
                    pi.dwProcessId,
                    window_thread_id)) {
                std::fwprintf(
                    stderr,
                    L"[launch] WARN native-loadout bootstrap unavailable pid=%lu\n",
                    pi.dwProcessId);
            }
            if (!func_extend_thread_path.empty() &&
                !write_func_extend_thread_bootstrap(
                    func_extend_thread_path,
                    pi.dwProcessId,
                    window_thread_id)) {
                std::fwprintf(
                    stderr,
                    L"[launch] WARN func-extend bootstrap unavailable pid=%lu\n",
                    pi.dwProcessId);
            }
        }
    }

    DWORD child_exit=STILL_ACTIVE;
    if(!GetExitCodeProcess(pi.hProcess,&child_exit)||child_exit!=STILL_ACTIVE){
        std::fwprintf(stderr,L"[launch] child exited before startup acknowledgement pid=%lu code=%08lX\n",pi.dwProcessId,child_exit);
        CloseHandle(pi.hThread);CloseHandle(pi.hProcess);return 7;
    }
    std::fwprintf(
        stdout,
        L"[launch] injected and resumed pid=%lu dlls=%u\n",
        pi.dwProcessId,
        static_cast<unsigned>(dlls.size()));
    fflush(stdout);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
