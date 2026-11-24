"""Encrypted administration transport for the owner's dungeon test helper."""
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys

DEPS = Path(__file__).resolve().parents[1] / '运维运行时' / 'admin-deps'
if DEPS.is_dir():
    sys.path.insert(0, str(DEPS))
import winrm

HERE = Path(__file__).resolve().parent
os.environ['TEMP'] = os.environ['TMP'] = str(HERE / 'scratch')
(HERE / 'scratch').mkdir(exist_ok=True)


EXPECTED_HOST = None

def computer_name():
    name = os.environ['USDOF_GM_WINRM_COMPUTER_NAME']
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,63}', name):
        raise ValueError('Invalid WinRM computer name')
    return name


def connect():
    credentials = Path(os.environ['USDOF_GM_WINRM_CREDENTIALS'])
    lines = credentials.read_text(encoding='utf-8-sig').splitlines()
    host = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', lines[0]).group()
    def value(line):
        return re.split(r'[:：=]', line, maxsplit=1)[1].strip()
    if EXPECTED_HOST is not None and host != EXPECTED_HOST: raise RuntimeError('Credential target differs from the selected GM')
    return winrm.Session('http://' + host + ':5985/wsman', auth=(value(lines[1]), value(lines[2])),
        transport='ntlm', message_encryption='always', operation_timeout_sec=40, read_timeout_sec=55)


def ps(code, private=False):
    client = connect()
    prefix = f"[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$ErrorActionPreference='Stop';if($env:COMPUTERNAME -cne '{computer_name()}'){{throw 'Server identity mismatch'}};"
    full = prefix + code
    payload = None
    encoded = base64.b64encode(full.encode('utf-16-le')).decode('ascii')
    if len(encoded) > 7000:
        runner = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$p=[Console]::In.ReadToEnd();& ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p))))"
        encoded = base64.b64encode(runner.encode('utf-16-le')).decode('ascii')
        payload = base64.b64encode(full.encode('utf-8'))
    protocol = client.protocol
    shell, command = protocol.open_shell(), None
    try:
        command = protocol.run_command(shell, 'powershell.exe',
            ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            console_mode_stdin=False, skip_cmd_shell=True)
        if payload is not None:
            for offset in range(0, len(payload), 65536):
                chunk = payload[offset:offset + 65536]
                protocol.send_command_input(shell, command, chunk, end=offset + len(chunk) == len(payload))
        stdout, stderr, status = protocol.get_command_output(shell, command)
    finally:
        if command is not None:
            protocol.cleanup_command(shell, command)
        protocol.close_shell(shell)
    if status:
        (HERE / 'last_remote_error.txt').write_bytes(b'Private remote operation failed; raw diagnostic suppressed.' if private else stderr)
        raise RuntimeError('Remote command failed; diagnostic saved in the tools directory')
    return stdout.decode('utf-8-sig').strip()


def python(code, private=False):
    import subprocess
    guarded=f"import socket;assert socket.gethostname().lower()=={computer_name().lower()!r}\n"+code
    payload=base64.b64encode(guarded.encode('utf-8'))
    short="import base64,sys;exec(compile(base64.b64decode(sys.stdin.buffer.read()),'<mysql-cutover>','exec'))"
    protocol=connect().protocol;shell=protocol.open_shell();command=None
    try:
        command=protocol.run_command(shell,r'C:\90US\server-pack-win10\cache\python-3.12.10-full\python.exe',['-E','-B','-X','utf8','-c',subprocess.list2cmdline([short])],console_mode_stdin=False,skip_cmd_shell=True)
        for offset in range(0,len(payload),196608):
            chunk=payload[offset:offset+196608]
            protocol.send_command_input(shell,command,chunk,end=offset+len(chunk)==len(payload))
        stdout,stderr,status=protocol.get_command_output(shell,command)
    finally:
        if command is not None:protocol.cleanup_command(shell,command)
        protocol.close_shell(shell)
    if status:
        (HERE/'last_remote_error.txt').write_bytes(b'Private remote operation failed; raw diagnostic suppressed.' if private else stderr)
        raise RuntimeError('Remote Python failed; diagnostic saved in the tools directory')
    return stdout.decode('utf-8-sig').strip()
