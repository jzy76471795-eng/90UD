from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import BOTH, LEFT, RIGHT, StringVar, Tk, filedialog
from tkinter import ttk

try:
    from .launcher_sdk import LauncherApi, LauncherApiError, launcher_base_url
except ImportError:
    from launcher_sdk import LauncherApi, LauncherApiError, launcher_base_url


REQUIRED_PYTHON = (3, 12, 10)
FROZEN_STAGE3_SHA256 = (
    "14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634"
)
STAGE3_LAUNCH_TAIL = "0?0?0?0?0?2?0?0?0?0?0?0?0?0?0?0"
SUPPORTED_PVF_PROFILES = {
    "utility-guarded": True,
    "us-zimo": False,
}
COMPONENT_RELATIVE_PATHS = (
    (
        "injector",
        "server_proto/patches/client_injector/build/Release-Win32/"
        "launch_usdof_client.exe",
    ),
    (
        "func_extend",
        "server_proto/patches/usdof_func_extend/build/Release-Win32/"
        "usdof_func_extend.dll",
    ),
    (
        "codec_passthrough",
        "server_proto/patches/usdof_codec_passthrough/build/Release-Win32/"
        "usdof_codec_passthrough.dll",
    ),
    (
        "desktop_focus_guard",
        "server_proto/patches/usdof_desktop_focus_guard/build/Release-Win32/"
        "usdof_desktop_focus_guard.dll",
    ),
    (
        "forbidden_func",
        "server_proto/patches/forbidden-func/build/Release-Win32/"
        "forbidden-func.dll",
    ),
    (
        "weapon_avatar_dungeon",
        "server_proto/patches/usdof_weapon_avatar_dungeon/build/Release-Win32/"
        "usdof_weapon_avatar_dungeon.dll",
    ),
    (
        "native_loadout",
        "server_proto/patches/usdof_native_loadout/build/Release-Win32/"
        "usdof_native_loadout.dll",
    ),
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resource_root() -> Path:
    if bool(getattr(sys, "frozen", False)):
        return Path(getattr(sys, "_MEIPASS")).resolve() / "launcher_runtime"
    return _source_root()


def _component_paths(resource_root: Path | None = None) -> dict[str, Path]:
    root = Path(resource_root or _resource_root()).resolve()
    return {
        label: root.joinpath(*relative.split("/"))
        for label, relative in COMPONENT_RELATIVE_PATHS
    }


def _managed_external_dlls(client_dir: Path) -> list[Path]:
    directory = Path(client_dir).resolve() / "us_extend_dll"
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(
        (
            path.resolve()
            for path in entries
            if path.is_file() and path.suffix.casefold() == ".dll"
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def _default_client_dir() -> Path:
    bases = []
    if bool(getattr(sys, "frozen", False)):
        bases.append(Path(sys.executable).resolve().parent)
    bases.extend((Path.cwd(), _source_root()))
    seen: set[str] = set()
    for base in bases:
        for candidate in (base, base / "USDOF"):
            key = os.path.normcase(str(candidate.resolve()))
            if key in seen:
                continue
            seen.add(key)
            if (candidate / "DFO.exe").is_file():
                return candidate.resolve()
    return bases[0].resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_sha256(value: object, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise RuntimeError("%s SHA-256 无效" % label)
    return normalized


def _validated_launch_argument(stage3: dict[str, object]) -> str:
    try:
        host = str(ipaddress.IPv4Address(str(stage3.get("channel_host") or "")))
    except ValueError as exc:
        raise RuntimeError("服务端返回的 Channel IPv4 无效") from exc
    try:
        port = int(stage3.get("channel_port") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("服务端返回的 Channel 端口无效") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("服务端返回的 Channel 端口无效")

    opaque_account = str(stage3.get("opaque_account") or "").strip()
    opaque_token = str(stage3.get("opaque_token") or "").strip()
    for label, value in (
        ("opaque_account", opaque_account),
        ("opaque_token", opaque_token),
    ):
        if (
            not value
            or len(value) > 256
            or "?" in value
            or any(char in value for char in "\r\n\x00")
        ):
            raise RuntimeError("服务端返回的 %s 无效" % label)
    return "9?%s?%s?%s?%s?%s" % (
        host,
        port,
        opaque_account,
        opaque_token,
        STAGE3_LAUNCH_TAIL,
    )


def _validate_client_files(
    client_dir: Path,
    stage3: dict[str, object],
) -> tuple[Path, Path, str, str, bool]:
    root = Path(client_dir).expanduser().resolve()
    client_exe = root / "DFO.exe"
    client_pvf = root / "Script.pvf"
    if not client_exe.is_file():
        raise FileNotFoundError("客户端目录缺少 DFO.exe：%s" % root)
    if not client_pvf.is_file():
        raise FileNotFoundError("客户端目录缺少 Script.pvf：%s" % root)

    expected_exe = _required_sha256(stage3.get("exe_sha256"), "DFO.exe")
    if expected_exe != FROZEN_STAGE3_SHA256:
        raise RuntimeError("服务端冻结客户端摘要不匹配")
    actual_exe = _sha256(client_exe)
    if actual_exe != FROZEN_STAGE3_SHA256:
        raise RuntimeError(
            "DFO.exe SHA-256 不匹配：expected=%s actual=%s"
            % (FROZEN_STAGE3_SHA256, actual_exe)
        )

    expected_pvf = _required_sha256(stage3.get("pvf_sha256"), "Script.pvf")
    actual_pvf = _sha256(client_pvf)
    if actual_pvf != expected_pvf:
        raise RuntimeError(
            "Script.pvf SHA-256 与服务端不一致：expected=%s actual=%s"
            % (expected_pvf, actual_pvf)
        )
    profile = str(stage3.get("pvf_profile") or "").strip()
    if profile not in SUPPORTED_PVF_PROFILES:
        raise RuntimeError("服务端 PVF profile 不受此登录器支持：%s" % profile)
    return (
        client_exe,
        client_pvf,
        actual_pvf,
        profile,
        SUPPORTED_PVF_PROFILES[profile],
    )


def _client_environment(
    client_dir: Path,
    client_exe: Path,
    client_pvf: Path,
    pvf_sha256: str,
    profile: str,
    bridge_required: bool,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("USDOF_"):
            env.pop(name, None)

    runtime_temp = client_dir / "runtime" / "temp"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    sidechannels = {
        "USDOF_NATIVE_LOADOUT_PLAN_PATH": "USDOFLoadoutNativePlan.bin",
        "USDOF_NATIVE_LOADOUT_ACK_PATH": "USDOFLoadoutNativeAck.bin",
        "USDOF_NATIVE_LOADOUT_THREAD_PATH": "USDOFNativeLoadoutThread.bin",
        "USDOF_FUNC_EXTEND_TOAST_REQUEST_PATH": "USDOFFuncExtendToastRequest.bin",
        "USDOF_FUNC_EXTEND_THREAD_PATH": "USDOFFuncExtendThread.bin",
        "USDOF_FUNC_EXTEND_VIDEO_COMMAND_PATH": "USDOFFuncExtendVideoCommand.bin",
    }
    for name, filename in sidechannels.items():
        env[name] = str(runtime_temp / filename)

    env.update(
        {
            "__COMPAT_LAYER": "DPIUNAWARE",
            "TEMP": str(runtime_temp),
            "TMP": str(runtime_temp),
            "TMPDIR": str(runtime_temp),
            "USDOF_CLIENT_DIR": str(client_dir),
            "USDOF_CLIENT_EXE": str(client_exe),
            "USDOF_PVF_PATH": str(client_pvf),
            "USDOF_EXPECTED_PVF_PROFILE": profile,
            "USDOF_EXPECTED_PVF_SHA256": pvf_sha256,
            "USDOF_PVF_BRIDGE": "1" if bridge_required else "0",
            "USDOF_SKIP_PVF_STARTUP_PREPROCESS": "1",
            "USDOF_FUNC_EXTEND_VIDEO": "1",
            "USDOF_FUNC_EXTEND_VIDEO_HOOK": "1",
            "USDOF_GAME_PLAIN_WIRE": "0",
            "USDOF_GAME_C2S_PLAIN_WIRE": "1",
            "USDOF_GAME_S2C_PLAIN_WIRE": "0",
            "USDOF_GAME_PLAIN_WIRE_HEADER": "16",
        }
    )
    return env


def probe_dependencies(resource_root: Path | None = None) -> dict[str, object]:
    components = _component_paths(resource_root)
    missing = [label for label, path in components.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError("登录器缺少启动组件：%s" % ", ".join(missing))
    invalid = [
        label
        for label, path in components.items()
        if path.stat().st_size < 4096 or path.read_bytes()[:2] != b"MZ"
    ]
    if invalid:
        raise RuntimeError("登录器启动组件无效：%s" % ", ".join(invalid))
    return {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "bits": 64 if sys.maxsize > 2**32 else 32,
        "component_count": len(components),
        "components": {label: _sha256(path) for label, path in components.items()},
    }


def launch_stage3(
    client_dir: Path,
    stage3: dict[str, object],
    *,
    resource_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    (
        client_exe,
        client_pvf,
        pvf_sha256,
        profile,
        bridge_required,
    ) = _validate_client_files(client_dir, stage3)
    components = _component_paths(resource_root)
    probe_dependencies(resource_root)
    argument = _validated_launch_argument(stage3)
    command = [
        str(components["injector"]),
        "--exe",
        str(client_exe),
        "--workdir",
        str(client_exe.parent),
    ]
    for label, _relative in COMPONENT_RELATIVE_PATHS:
        if label != "injector":
            command.extend(("--dll", str(components[label])))
    for dll_path in _managed_external_dlls(client_exe.parent):
        command.extend(("--dll", str(dll_path)))
    command.extend(("--args", argument))
    env = _client_environment(
        client_exe.parent,
        client_exe,
        client_pvf,
        pvf_sha256,
        profile,
        bridge_required,
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(
        command,
        cwd=str(client_exe.parent),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=creationflags,
        check=False,
    )


class MinimalHttpLauncher:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.api: LauncherApi | None = None
        self.busy = False
        self.client_dir = StringVar(value=str(_default_client_dir()))
        self.server = StringVar(value="127.0.0.1")
        self.port = StringVar(value="17602")
        self.account = StringVar(value="10038")
        self.password = StringVar(value="")
        self.status = StringVar(value="就绪")
        self._build()

    def _build(self) -> None:
        self.root.title("USDOF HTTP 登录器")
        self.root.resizable(False, False)
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="客户端目录").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.client_dir, width=42).grid(
            row=0, column=1, sticky="ew", padx=(10, 6), pady=4
        )
        ttk.Button(frame, text="浏览...", command=self.choose_client_dir).grid(
            row=0, column=2, sticky="ew", pady=4
        )

        fields = (
            ("服务端 IPv4", self.server, False),
            ("HTTP 端口", self.port, False),
            ("账号", self.account, False),
            ("密码", self.password, True),
        )
        for offset, (label, variable, secret) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=offset, column=0, sticky="w", pady=4)
            entry = ttk.Entry(
                frame,
                textvariable=variable,
                width=42,
                show="*" if secret else "",
            )
            entry.grid(
                row=offset,
                column=1,
                columnspan=2,
                sticky="ew",
                padx=(10, 0),
                pady=4,
            )
            if secret:
                entry.bind("<Return>", lambda _event: self.login_and_launch())

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.login_button = ttk.Button(
            buttons, text="登录并启动", command=self.login_and_launch
        )
        self.login_button.pack(side=LEFT)
        self.logout_button = ttk.Button(
            buttons, text="退出登录", command=self.logout, state="disabled"
        )
        self.logout_button.pack(side=RIGHT)
        ttk.Separator(frame).grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(4, 8)
        )
        ttk.Label(
            frame,
            textvariable=self.status,
            width=62,
            wraplength=520,
            justify=LEFT,
        ).grid(row=7, column=0, columnspan=3, sticky="w")

    def choose_client_dir(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择包含 DFO.exe 和 Script.pvf 的客户端目录",
            initialdir=self.client_dir.get() or str(Path.cwd()),
            mustexist=True,
        )
        if selected:
            self.client_dir.set(selected)

    def _set_busy(self, value: bool) -> None:
        self.busy = bool(value)
        self.login_button.configure(state="disabled" if value else "normal")

    def _finish(self, message: str, *, logged_in: bool = False) -> None:
        self.status.set(str(message))
        self._set_busy(False)
        self.logout_button.configure(state="normal" if logged_in else "disabled")

    def login_and_launch(self) -> None:
        if self.busy:
            return
        values = (
            self.client_dir.get(),
            self.server.get(),
            self.port.get(),
            self.account.get(),
            self.password.get(),
        )
        self._set_busy(True)
        self.status.set("正在连接 HTTP 登录服务...")
        threading.Thread(
            target=self._login_worker, args=values, daemon=True
        ).start()

    def _login_worker(
        self,
        client_dir: str,
        server: str,
        port: str,
        account: str,
        password: str,
    ) -> None:
        api: LauncherApi | None = None
        logged_in = False
        try:
            base_url = launcher_base_url(server, int(port, 10))
            api = LauncherApi(base_url)
            meta = api.meta()
            if int(meta.get("api_version") or 0) != 1:
                raise RuntimeError("登录接口版本不匹配")
            if str(meta.get("transport") or "") != "http-direct-tcp":
                raise RuntimeError("服务端传输模式不匹配")
            if not bool(meta.get("server_ready")):
                raise RuntimeError("服务端尚未完成启动")
            stage3_meta = dict(meta.get("stage3") or {})
            if (
                str(stage3_meta.get("exe_sha256") or "").lower()
                != FROZEN_STAGE3_SHA256
            ):
                raise RuntimeError("服务端冻结客户端摘要不匹配")
            response = api.login(account, password)
            logged_in = True
            stage3 = dict(response.get("stage3") or {})
            completed = launch_stage3(Path(client_dir), stage3)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(detail or "客户端启动失败")
            self.api = api
            self.root.after(
                0,
                lambda: self._finish(
                    "登录成功，客户端已启动：%s:%s"
                    % (stage3.get("channel_host"), stage3.get("channel_port")),
                    logged_in=True,
                ),
            )
        except (LauncherApiError, OSError, RuntimeError, ValueError) as exc:
            if logged_in and api is not None:
                try:
                    api.logout()
                except LauncherApiError:
                    pass
            message = "%s: %s" % (type(exc).__name__, exc)
            self.root.after(0, lambda: self._finish(message))

    def logout(self) -> None:
        api = self.api
        self.api = None
        if api is None:
            self._finish("当前没有登录会话")
            return
        self._set_busy(True)
        self.status.set("正在退出登录...")

        def worker() -> None:
            try:
                api.logout()
                message = "登录会话已撤销"
            except LauncherApiError as exc:
                message = "%s: %s" % (exc.code, exc.message)
            self.root.after(0, lambda: self._finish(message))

        threading.Thread(target=worker, daemon=True).start()


def _write_probe(path: Path) -> int:
    try:
        result = {"ok": True, **probe_dependencies()}
    except Exception as exc:
        result = {
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return 0 if bool(result.get("ok")) else 1


def _require_runtime() -> None:
    version = sys.version_info[:3]
    bits = 64 if sys.maxsize > 2**32 else 32
    if version != REQUIRED_PYTHON or bits != 64:
        raise RuntimeError(
            "登录器要求 CPython 3.12.10 x64；当前为 %d.%d.%d %d-bit"
            % (*version, bits)
        )


def main(argv: list[str] | None = None) -> int:
    _require_runtime()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--usdof-launcher-probe":
        return _write_probe(Path(args[1]))
    if args:
        return 2
    root = Tk()
    MinimalHttpLauncher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
