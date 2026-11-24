# Native session launcher

`usdof_session_launcher.cpp` is the x64 C++ host used by the repository-level
`启动游戏.cmd`. It owns only the outer entry boundary:

- resolve the repository root relative to the executable;
- require the frozen client files;
- locate and verify CPython 3.12.10 x64;
- apply the same default launcher environment;
- start `launch_game_session.py` in the current console and return its exit code;
- own the Python session and all descendants in a Windows Job Object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` as the final close-window cleanup guard;
- batch-probe the required loopback listeners under one monotonic shared deadline,
  while also watching the Python server PID for early exit;
- create the service server process in `--server-process` mode and inherit its
  standard output/error handles unchanged.

PVF preprocessing, command construction, client injection, log file ownership,
and session cleanup remain in `launch_game_session.py` for this phase. Python
invokes the native `--server-process` mode for the server child and retains the
same Popen-compatible pipes. Python also invokes the native
`--wait-ports` mode for the high-frequency readiness loop and retains the final
ready/failure branch. The native
host inherits the same console so the existing Python signal and close handlers
continue to run their ordered cleanup first. The native Job Object then removes
any residual server, injector, or client process when the host exits.

Build and run the non-launching verification probe:

```powershell
powershell -File server_proto\start\native_launcher\build_with_llvm_mingw.ps1 -Verify
```

The x64 static executable is written to:

```text
server_proto/start/usdof_session_launcher.exe
```

`usdof_session_launcher.exe --check` validates paths, the frozen client, and
the exact Python runtime without starting the server or client.

`usdof_session_launcher.exe --job-check` additionally starts one immediate-exit
Python probe in the Job Object and validates suspended creation, assignment,
resume, wait, and close without starting the game session.

Internal session mode:

```text
usdof_session_launcher.exe --wait-ports --timeout-ms N --server-pid PID --port PORT [...]
```

It probes all pending ports in one Winsock batch every 200 ms, reports the same
five-second progress lines as the Python implementation, and returns immediately
when the server process exits. `USDOF_NATIVE_PORT_WAIT=0` selects the preserved
Python compatibility branch for one A/B session.

`USDOF_NATIVE_SERVER_PROCESS=0` selects the preserved Python `Popen(start.py)`
branch for one A/B session. Both switches are local diagnostics and default to
the native branches when the release executable exists.
