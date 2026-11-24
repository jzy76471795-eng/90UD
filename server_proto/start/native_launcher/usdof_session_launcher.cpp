#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cwctype>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

namespace {

constexpr wchar_t kSessionRelativePath[] =
    L"server_proto\\start\\launch_game_session.py";
constexpr wchar_t kClientRelativePath[] = L"USDOF\\DFO.exe";
constexpr int kPortWaitReady = 0;
constexpr int kPortWaitServerExited = 10;
constexpr int kPortWaitTimeout = 11;
constexpr int kPortWaitRuntimeError = 12;
constexpr int kPortWaitUsageError = 64;
std::atomic<HANDLE> g_session_job{nullptr};

HANDLE take_session_job() {
    return g_session_job.exchange(nullptr, std::memory_order_acq_rel);
}

void close_session_job() {
    HANDLE job = take_session_job();
    if (job != nullptr) {
        CloseHandle(job);
    }
}

BOOL WINAPI session_console_handler(DWORD control_type) {
    switch (control_type) {
        case CTRL_C_EVENT:
        case CTRL_BREAK_EVENT:
            // The Python child receives the same console event and performs
            // its normal ordered cleanup. Keep the native host alive until it
            // exits so the job remains the final process-tree backstop.
            return TRUE;
        case CTRL_CLOSE_EVENT:
        case CTRL_LOGOFF_EVENT:
        case CTRL_SHUTDOWN_EVENT:
            close_session_job();
            return TRUE;
        default:
            return FALSE;
    }
}

HANDLE create_session_job() {
    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    if (job == nullptr) {
        return nullptr;
    }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION information{};
    information.BasicLimitInformation.LimitFlags =
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &information,
            sizeof(information))) {
        CloseHandle(job);
        return nullptr;
    }
    return job;
}

bool is_file(const std::wstring& path);
std::wstring make_command_line(const std::vector<std::wstring>& arguments);

struct PortWaitOptions {
    ULONGLONG timeout_ms = 0;
    DWORD server_pid = 0;
    std::vector<unsigned short> ports;
};

struct PortWaitState {
    unsigned short port = 0;
    bool ready = false;
};

struct ServerProcessOptions {
    std::wstring application;
    std::wstring workdir;
    std::vector<std::wstring> arguments;
};

bool parse_unsigned(
    const wchar_t* value,
    unsigned long long maximum,
    unsigned long long* parsed) {
    if (value == nullptr || value[0] == L'\0' || value[0] == L'-') {
        return false;
    }
    wchar_t* end = nullptr;
    const unsigned long long number = _wcstoui64(value, &end, 10);
    if (end == value || *end != L'\0' || number > maximum) {
        return false;
    }
    *parsed = number;
    return true;
}

bool parse_port_wait_options(
    int argc,
    wchar_t** argv,
    PortWaitOptions* options) {
    bool timeout_seen = false;
    bool server_pid_seen = false;
    for (int index = 2; index < argc; ++index) {
        const std::wstring argument = argv[index];
        if (argument == L"--timeout-ms" && index + 1 < argc) {
            unsigned long long value = 0;
            if (!parse_unsigned(argv[++index], 24ULL * 60ULL * 60ULL * 1000ULL, &value)) {
                return false;
            }
            options->timeout_ms = static_cast<ULONGLONG>(value);
            timeout_seen = true;
        } else if (argument == L"--server-pid" && index + 1 < argc) {
            unsigned long long value = 0;
            if (!parse_unsigned(argv[++index], 0xFFFFFFFFULL, &value)) {
                return false;
            }
            options->server_pid = static_cast<DWORD>(value);
            server_pid_seen = true;
        } else if (argument == L"--port" && index + 1 < argc) {
            unsigned long long value = 0;
            if (!parse_unsigned(argv[++index], 65535ULL, &value) || value == 0) {
                return false;
            }
            const auto port = static_cast<unsigned short>(value);
            if (std::find(options->ports.begin(), options->ports.end(), port) ==
                options->ports.end()) {
                options->ports.push_back(port);
            }
        } else {
            return false;
        }
    }
    return timeout_seen && server_pid_seen && !options->ports.empty() &&
           options->ports.size() <= FD_SETSIZE;
}

void probe_pending_ports(std::vector<PortWaitState>* states) {
    struct ConnectAttempt {
        SOCKET socket = INVALID_SOCKET;
        std::size_t state_index = 0;
    };

    fd_set writable{};
    fd_set failed{};
    FD_ZERO(&writable);
    FD_ZERO(&failed);
    std::vector<ConnectAttempt> attempts;

    for (std::size_t index = 0; index < states->size(); ++index) {
        PortWaitState& state = (*states)[index];
        if (state.ready) {
            continue;
        }
        SOCKET connection = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (connection == INVALID_SOCKET) {
            continue;
        }
        u_long nonblocking = 1;
        if (ioctlsocket(connection, FIONBIO, &nonblocking) != 0) {
            closesocket(connection);
            continue;
        }
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = htons(state.port);
        if (connect(
                connection,
                reinterpret_cast<const sockaddr*>(&address),
                sizeof(address)) == 0) {
            state.ready = true;
            closesocket(connection);
            continue;
        }
        const int error = WSAGetLastError();
        if (error != WSAEWOULDBLOCK && error != WSAEINPROGRESS &&
            error != WSAEINVAL) {
            closesocket(connection);
            continue;
        }
        FD_SET(connection, &writable);
        FD_SET(connection, &failed);
        attempts.push_back({connection, index});
    }

    if (!attempts.empty()) {
        timeval timeout{};
        timeout.tv_usec = 400000;
        const int selected = select(0, nullptr, &writable, &failed, &timeout);
        if (selected > 0) {
            for (const ConnectAttempt& attempt : attempts) {
                if (!FD_ISSET(attempt.socket, &writable)) {
                    continue;
                }
                int socket_error = 0;
                int option_length = sizeof(socket_error);
                if (getsockopt(
                        attempt.socket,
                        SOL_SOCKET,
                        SO_ERROR,
                        reinterpret_cast<char*>(&socket_error),
                        &option_length) == 0 &&
                    socket_error == 0) {
                    (*states)[attempt.state_index].ready = true;
                }
            }
        }
    }
    for (const ConnectAttempt& attempt : attempts) {
        closesocket(attempt.socket);
    }
}

bool all_ports_ready(const std::vector<PortWaitState>& states) {
    return std::all_of(states.begin(), states.end(), [](const PortWaitState& state) {
        return state.ready;
    });
}

std::wstring pending_ports_text(const std::vector<PortWaitState>& states) {
    std::wstring result = L"[";
    bool first = true;
    for (const PortWaitState& state : states) {
        if (state.ready) {
            continue;
        }
        if (!first) {
            result += L", ";
        }
        result += std::to_wstring(state.port);
        first = false;
    }
    result += L"]";
    return result;
}

int run_port_wait(int argc, wchar_t** argv) {
    PortWaitOptions options;
    if (!parse_port_wait_options(argc, argv, &options)) {
        std::wcerr
            << L"[start_game/native] usage: --wait-ports --timeout-ms N "
            << L"--server-pid PID --port PORT [--port PORT ...]\n";
        return kPortWaitUsageError;
    }

    WSADATA winsock{};
    const int startup_error = WSAStartup(MAKEWORD(2, 2), &winsock);
    if (startup_error != 0) {
        std::wcerr << L"[start_game/native] WSAStartup failed error="
                   << startup_error << L"\n";
        return kPortWaitRuntimeError;
    }

    HANDLE server_process = nullptr;
    if (options.server_pid != 0) {
        server_process = OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
            FALSE,
            options.server_pid);
        if (server_process == nullptr) {
            std::wcerr << L"[start_game/native] OpenProcess failed pid="
                       << options.server_pid << L" error=" << GetLastError() << L"\n";
            WSACleanup();
            return kPortWaitRuntimeError;
        }
    }

    std::vector<PortWaitState> states;
    states.reserve(options.ports.size());
    for (const unsigned short port : options.ports) {
        states.push_back({port, false});
    }

    const ULONGLONG started = GetTickCount64();
    ULONGLONG next_log = started + 5000;
    while (GetTickCount64() - started < options.timeout_ms) {
        if (server_process != nullptr &&
            WaitForSingleObject(server_process, 0) == WAIT_OBJECT_0) {
            DWORD exit_code = ERROR_PROCESS_ABORTED;
            GetExitCodeProcess(server_process, &exit_code);
            std::wcout << L"[session] server exited before ports were ready (rc="
                       << exit_code << L")\n"
                       << L"[start_game/native] port_wait=server-exit\n";
            CloseHandle(server_process);
            WSACleanup();
            return kPortWaitServerExited;
        }

        std::vector<bool> was_ready;
        was_ready.reserve(states.size());
        for (const PortWaitState& state : states) {
            was_ready.push_back(state.ready);
        }
        probe_pending_ports(&states);
        const ULONGLONG now = GetTickCount64();
        for (std::size_t index = 0; index < states.size(); ++index) {
            if (!was_ready[index] && states[index].ready) {
                const double elapsed_seconds =
                    static_cast<double>(now - started) / 1000.0;
                std::wcout << L"[session] port " << states[index].port
                           << L" ready (+" << std::fixed << std::setprecision(1)
                           << elapsed_seconds << L"s)\n";
            }
        }
        if (all_ports_ready(states)) {
            std::wcout << L"[start_game/native] port_wait=ready\n";
            if (server_process != nullptr) {
                CloseHandle(server_process);
            }
            WSACleanup();
            return kPortWaitReady;
        }
        if (now >= next_log) {
            const double elapsed_seconds =
                static_cast<double>(now - started) / 1000.0;
            const double timeout_seconds =
                static_cast<double>(options.timeout_ms) / 1000.0;
            std::wcout << L"[session] waiting ports " << pending_ports_text(states)
                       << L" (+" << std::fixed << std::setprecision(0)
                       << elapsed_seconds << L"s / " << timeout_seconds
                       << L"s; PVF boot may take ~40s)\n";
            next_log = now + 5000;
        }
        const ULONGLONG elapsed = GetTickCount64() - started;
        if (elapsed < options.timeout_ms) {
            const ULONGLONG remaining = options.timeout_ms - elapsed;
            Sleep(static_cast<DWORD>(std::min<ULONGLONG>(200, remaining)));
        }
    }

    std::wcout << L"[start_game/native] port_wait=timeout pending="
               << pending_ports_text(states) << L"\n";
    if (server_process != nullptr) {
        CloseHandle(server_process);
    }
    WSACleanup();
    return kPortWaitTimeout;
}

bool parse_server_process_options(
    int argc,
    wchar_t** argv,
    ServerProcessOptions* options) {
    bool application_seen = false;
    bool workdir_seen = false;
    for (int index = 2; index < argc; ++index) {
        const std::wstring argument = argv[index];
        if (argument == L"--application" && index + 1 < argc) {
            options->application = argv[++index];
            application_seen = !options->application.empty();
        } else if (argument == L"--workdir" && index + 1 < argc) {
            options->workdir = argv[++index];
            workdir_seen = !options->workdir.empty();
        } else if (argument == L"--arg" && index + 1 < argc) {
            options->arguments.emplace_back(argv[++index]);
        } else {
            return false;
        }
    }
    return application_seen && workdir_seen && !options->arguments.empty();
}

bool valid_standard_handle(HANDLE handle) {
    return handle != nullptr && handle != INVALID_HANDLE_VALUE;
}

int run_server_process(int argc, wchar_t** argv) {
    ServerProcessOptions options;
    if (!parse_server_process_options(argc, argv, &options)) {
        std::wcerr
            << L"[start_game/native] usage: --server-process --application EXE "
            << L"--workdir DIR --arg ARG [--arg ARG ...]\n";
        return ERROR_INVALID_PARAMETER;
    }
    if (!is_file(options.application)) {
        std::wcerr << L"[start_game/native] missing server application="
                   << options.application << L"\n";
        return ERROR_FILE_NOT_FOUND;
    }

    std::wstring command_line = make_command_line(options.arguments);
    std::vector<wchar_t> mutable_command(command_line.begin(), command_line.end());
    mutable_command.push_back(L'\0');

    HANDLE job = create_session_job();
    const bool process_suspended = job != nullptr;
    const DWORD creation_flags = process_suspended ? CREATE_SUSPENDED : 0;

    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    const HANDLE standard_input = GetStdHandle(STD_INPUT_HANDLE);
    const HANDLE standard_output = GetStdHandle(STD_OUTPUT_HANDLE);
    const HANDLE standard_error = GetStdHandle(STD_ERROR_HANDLE);
    const bool use_standard_handles =
        valid_standard_handle(standard_input) &&
        valid_standard_handle(standard_output) &&
        valid_standard_handle(standard_error);
    if (use_standard_handles) {
        startup.dwFlags |= STARTF_USESTDHANDLES;
        startup.hStdInput = standard_input;
        startup.hStdOutput = standard_output;
        startup.hStdError = standard_error;
    }

    PROCESS_INFORMATION process{};
    const BOOL created = CreateProcessW(
        options.application.c_str(),
        mutable_command.data(),
        nullptr,
        nullptr,
        use_standard_handles ? TRUE : FALSE,
        creation_flags,
        nullptr,
        options.workdir.c_str(),
        &startup,
        &process);
    if (!created) {
        const DWORD error = GetLastError();
        if (job != nullptr) {
            CloseHandle(job);
        }
        std::wcerr << L"[start_game/native] CreateProcessW server failed error="
                   << error << L" application=" << options.application << L"\n";
        return static_cast<int>(error);
    }

    if (process_suspended) {
        if (!AssignProcessToJobObject(job, process.hProcess)) {
            // The outer session host may already own this process tree. In that
            // case the outer Job Object remains the lifecycle owner.
            std::wcerr << L"[start_game/native] server job assignment warning error="
                       << GetLastError() << L"; outer owner remains active\n";
            CloseHandle(job);
            job = nullptr;
        } else {
            g_session_job.store(job, std::memory_order_release);
            std::wcout << L"[start_game/native] server_process=job-owned pid="
                       << process.dwProcessId << L"\n" << std::flush;
        }
        if (ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
            const DWORD error = GetLastError();
            TerminateProcess(process.hProcess, error);
            close_session_job();
            CloseHandle(process.hThread);
            CloseHandle(process.hProcess);
            return static_cast<int>(error);
        }
    }

    CloseHandle(process.hThread);
    const DWORD wait_result = WaitForSingleObject(process.hProcess, INFINITE);
    DWORD exit_code = ERROR_PROCESS_ABORTED;
    if (wait_result == WAIT_OBJECT_0) {
        if (!GetExitCodeProcess(process.hProcess, &exit_code)) {
            exit_code = ERROR_PROCESS_ABORTED;
        }
    }
    CloseHandle(process.hProcess);
    if (job != nullptr) {
        close_session_job();
    }
    std::wcout << L"[start_game/native] server_process ended rc=" << exit_code
               << L"\n" << std::flush;
    return static_cast<int>(exit_code);
}

std::wstring module_path() {
    std::vector<wchar_t> buffer(1024);
    for (;;) {
        const DWORD length = GetModuleFileNameW(
            nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
        if (length == 0) {
            return L"";
        }
        if (length < buffer.size() - 1) {
            return std::wstring(buffer.data(), length);
        }
        buffer.resize(buffer.size() * 2);
    }
}

std::wstring parent_path(const std::wstring& path) {
    const std::wstring::size_type end = path.find_last_not_of(L"\\/");
    if (end == std::wstring::npos) {
        return L"";
    }
    const std::wstring::size_type slash = path.find_last_of(L"\\/", end);
    if (slash == std::wstring::npos) {
        return L"";
    }
    if (slash == 2 && path.size() >= 3 && path[1] == L':') {
        return path.substr(0, 3);
    }
    return path.substr(0, slash);
}

std::wstring join_path(const std::wstring& base, const std::wstring& child) {
    if (base.empty()) {
        return child;
    }
    const wchar_t tail = base.back();
    if (tail == L'\\' || tail == L'/') {
        return base + child;
    }
    return base + L"\\" + child;
}

bool is_file(const std::wstring& path) {
    const DWORD attributes = GetFileAttributesW(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES &&
           (attributes & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

std::wstring get_environment(const wchar_t* name) {
    const DWORD needed = GetEnvironmentVariableW(name, nullptr, 0);
    if (needed == 0) {
        return L"";
    }
    std::vector<wchar_t> buffer(needed);
    const DWORD written = GetEnvironmentVariableW(
        name, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (written == 0 || written >= buffer.size()) {
        return L"";
    }
    return std::wstring(buffer.data(), written);
}

std::wstring quote_argument(const std::wstring& value) {
    if (!value.empty() && value.find_first_of(L" \t\n\v\"") == std::wstring::npos) {
        return value;
    }

    std::wstring result = L"\"";
    std::size_t backslashes = 0;
    for (const wchar_t ch : value) {
        if (ch == L'\\') {
            ++backslashes;
            continue;
        }
        if (ch == L'\"') {
            result.append(backslashes * 2 + 1, L'\\');
            result.push_back(L'\"');
        } else {
            result.append(backslashes, L'\\');
            result.push_back(ch);
        }
        backslashes = 0;
    }
    result.append(backslashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}

std::wstring make_command_line(const std::vector<std::wstring>& arguments) {
    std::wstring command;
    for (const std::wstring& argument : arguments) {
        if (!command.empty()) {
            command.push_back(L' ');
        }
        command += quote_argument(argument);
    }
    return command;
}

DWORD run_process(
    const std::wstring& application,
    const std::vector<std::wstring>& arguments,
    const std::wstring& working_directory,
    DWORD creation_flags,
    bool inherit_handles,
    bool own_process_tree = false) {
    std::wstring command_line = make_command_line(arguments);
    std::vector<wchar_t> mutable_command(command_line.begin(), command_line.end());
    mutable_command.push_back(L'\0');

    HANDLE job = nullptr;
    bool process_suspended = false;
    if (own_process_tree) {
        job = create_session_job();
        if (job == nullptr) {
            std::wcerr << L"[start_game/native] warning: job object unavailable error="
                       << GetLastError() << L"; Python cleanup remains active\n";
        } else {
            creation_flags |= CREATE_SUSPENDED;
            process_suspended = true;
        }
    }

    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    const BOOL created = CreateProcessW(
        application.c_str(),
        mutable_command.data(),
        nullptr,
        nullptr,
        inherit_handles ? TRUE : FALSE,
        creation_flags,
        nullptr,
        working_directory.c_str(),
        &startup,
        &process);
    if (!created) {
        if (job != nullptr) {
            CloseHandle(job);
        }
        std::wcerr << L"[start_game/native] CreateProcessW failed error="
                   << GetLastError() << L" application=" << application << L"\n";
        return ERROR_PROCESS_ABORTED;
    }

    if (process_suspended) {
        if (job != nullptr && !AssignProcessToJobObject(job, process.hProcess)) {
            std::wcerr << L"[start_game/native] warning: AssignProcessToJobObject failed error="
                       << GetLastError() << L"; Python cleanup remains active\n";
            CloseHandle(job);
            job = nullptr;
        } else if (job != nullptr) {
            g_session_job.store(job, std::memory_order_release);
            std::wcout << L"[start_game/native] process_tree=job-owned pid="
                       << process.dwProcessId << L"\n";
        }
        if (ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
            const DWORD error = GetLastError();
            TerminateProcess(process.hProcess, error);
            close_session_job();
            CloseHandle(process.hThread);
            CloseHandle(process.hProcess);
            std::wcerr << L"[start_game/native] ResumeThread failed error="
                       << error << L"\n";
            return ERROR_PROCESS_ABORTED;
        }
    }

    CloseHandle(process.hThread);
    const DWORD wait_result = WaitForSingleObject(process.hProcess, INFINITE);
    DWORD exit_code = ERROR_PROCESS_ABORTED;
    if (wait_result == WAIT_OBJECT_0) {
        if (!GetExitCodeProcess(process.hProcess, &exit_code)) {
            exit_code = ERROR_PROCESS_ABORTED;
        }
    }
    CloseHandle(process.hProcess);
    if (job != nullptr) {
        close_session_job();
    }
    return exit_code;
}

bool verify_python(const std::wstring& python, const std::wstring& root) {
    const std::wstring probe =
        L"import struct,sys;raise SystemExit(0 if sys.version_info[:3]==(3,12,10) "
        L"and struct.calcsize('P')*8==64 else 1)";
    const DWORD exit_code = run_process(
        python,
        {python, L"-c", probe},
        root,
        CREATE_NO_WINDOW,
        false,
        false);
    return exit_code == 0;
}

void append_candidate(
    std::vector<std::wstring>* candidates,
    const std::wstring& candidate) {
    if (candidate.empty() || !is_file(candidate)) {
        return;
    }
    const auto same_path = [&candidate](const std::wstring& existing) {
        return _wcsicmp(existing.c_str(), candidate.c_str()) == 0;
    };
    if (std::find_if(candidates->begin(), candidates->end(), same_path) ==
        candidates->end()) {
        candidates->push_back(candidate);
    }
}

std::wstring resolve_python(const std::wstring& root) {
    std::vector<std::wstring> candidates;
    append_candidate(&candidates, get_environment(L"USDOF_PYTHON_EXE_OVERRIDE"));
    append_candidate(&candidates, get_environment(L"USDOF_PYTHON_EXE"));
    append_candidate(
        &candidates,
        join_path(root, L"server-pack-win10\\cache\\python-3.12.10-full\\python.exe"));
    append_candidate(&candidates, join_path(root, L"runtime\\python\\python.exe"));

    const std::wstring local_app_data = get_environment(L"LOCALAPPDATA");
    if (!local_app_data.empty()) {
        append_candidate(
            &candidates,
            join_path(local_app_data, L"Programs\\Python\\Python312\\python.exe"));
    }
    append_candidate(&candidates, L"C:\\Python312\\python.exe");

    for (const std::wstring& candidate : candidates) {
        if (verify_python(candidate, root)) {
            return candidate;
        }
    }
    return L"";
}

bool has_argument(int argc, wchar_t** argv, const wchar_t* expected) {
    for (int index = 1; index < argc; ++index) {
        if (_wcsicmp(argv[index], expected) == 0) {
            return true;
        }
    }
    return false;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
    SetConsoleCtrlHandler(session_console_handler, TRUE);

    const std::wstring executable = module_path();
    const std::wstring start_directory = parent_path(executable);
    const std::wstring server_directory = parent_path(start_directory);
    const std::wstring root = parent_path(server_directory);
    const std::wstring session = join_path(root, kSessionRelativePath);
    const std::wstring client = join_path(root, kClientRelativePath);

    if (root.empty() || !SetCurrentDirectoryW(root.c_str())) {
        std::wcerr << L"[start_game/native] invalid repository root error="
                   << GetLastError() << L"\n";
        return 2;
    }
    if (argc > 1 && _wcsicmp(argv[1], L"--wait-ports") == 0) {
        return run_port_wait(argc, argv);
    }
    if (argc > 1 && _wcsicmp(argv[1], L"--server-process") == 0) {
        return run_server_process(argc, argv);
    }
    if (!is_file(session)) {
        std::wcerr << L"[start_game/native] missing session=" << session << L"\n";
        return 3;
    }
    if (!is_file(client)) {
        std::wcerr << L"[start_game/native] missing client=" << client << L"\n";
        return 4;
    }

    const std::wstring python = resolve_python(root);
    if (python.empty()) {
        std::wcerr
            << L"[start_game/native] CPython 3.12.10 x64 was not found\n"
            << L"[start_game/native] set USDOF_PYTHON_EXE_OVERRIDE to python.exe\n";
        return 5;
    }

    SetEnvironmentVariableW(L"USDOF_PYTHON_EXE", python.c_str());
    const std::wstring server_python_path = join_path(root, L"server_proto");
    const std::wstring previous_python_path = get_environment(L"PYTHONPATH");
    const std::wstring python_path = previous_python_path.empty()
        ? server_python_path
        : server_python_path + L";" + previous_python_path;
    SetEnvironmentVariableW(L"PYTHONPATH", python_path.c_str());

    // These unified runtime components are part of the frozen local runtime.  Override
    // inherited values so an old shell profile cannot silently disable them.
    SetEnvironmentVariableW(L"USDOF_CN_FIX", L"1");
    SetEnvironmentVariableW(L"USDOF_CODEC_PASSTHROUGH", L"1");
    SetEnvironmentVariableW(L"USDOF_FUNC_EXTEND", L"1");
    SetEnvironmentVariableW(L"USDOF_FUNC_EXTEND_VIDEO", L"1");
    SetEnvironmentVariableW(L"USDOF_FUNC_EXTEND_VIDEO_HOOK", L"1");

    std::wcout << L"[start_game/native] host=" << executable << L"\n"
               << L"[start_game/native] root=" << root << L"\n"
               << L"[start_game/native] python=" << python << L"\n"
               << L"[start_game/native] session=" << session << L"\n";

    if (has_argument(argc, argv, L"--check")) {
        std::wcout << L"[start_game/native] check=ok\n";
        return 0;
    }
    if (has_argument(argc, argv, L"--job-check")) {
        const DWORD check_code = run_process(
            python,
            {python, L"-c", L"raise SystemExit(0)"},
            root,
            0,
            true,
            true);
        if (check_code == 0) {
            std::wcout << L"[start_game/native] job_check=ok\n";
        }
        return static_cast<int>(check_code);
    }

    std::vector<std::wstring> arguments{python, session};
    for (int index = 1; index < argc; ++index) {
        arguments.emplace_back(argv[index]);
    }
    const DWORD exit_code = run_process(python, arguments, root, 0, true, true);
    std::wcout << L"[start_game/native] session ended rc=" << exit_code << L"\n";
    return static_cast<int>(exit_code);
}
