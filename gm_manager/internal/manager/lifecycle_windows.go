//go:build windows

package manager

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
)

func psLiteral(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }
func (s *Service) runTask(ctx context.Context, command string) (taskState, error) {
	if s.config.GameTaskName == "" || !filepath.IsAbs(s.config.ServerRoot) {
		return taskState{}, fail(503, "lifecycle_unconfigured", "未配置本机游戏计划任务")
	}
	if command != "status" && command != "start" && command != "stop" {
		return taskState{}, fail(400, "invalid_command", "不接受任意系统命令")
	}
	script := `$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$root=` + psLiteral(filepath.Clean(s.config.ServerRoot)) + `;$taskName=` + psLiteral(s.config.GameTaskName) + `;
$task=Get-ScheduledTask -TaskName $taskName
$entry=Join-Path $root '启动公网服务端.cmd'
if(@($task.Actions).Count-ne 1 -or [IO.Path]::GetFileName($task.Actions[0].Execute)-ine 'cmd.exe' -or $task.Actions[0].Arguments-notlike ('*'+$entry+'*')){throw 'Task entry mismatch'}
function OwnedRuntime {
 $all=@(Get-CimInstance Win32_Process);$owned=@{}
 foreach($p in $all){if($p.ExecutablePath -and ($p.ExecutablePath-ieq (Join-Path $root 'server_proto\start\usdof_session_launcher.exe') -or ($p.Name-ieq 'python.exe' -and $p.CommandLine-like ('*'+(Join-Path $root 'server_proto\start\launch_game_session.py')+'*')))){$owned[[int]$p.ProcessId]=$p}}
 for($pass=0;$pass-lt 5;$pass++){foreach($p in $all){if($owned.ContainsKey([int]$p.ParentProcessId)-and $p.ExecutablePath -and $p.ExecutablePath.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){$owned[[int]$p.ProcessId]=$p}}}
 @($owned.Values)
}
$before=@(OwnedRuntime);$info=$task|Get-ScheduledTaskInfo
`
	switch command {
	case "start":
		script += `if($task.State-eq 'Running'-and $before.Count-eq 0 -and ([DateTime]::Now-$info.LastRunTime).TotalSeconds-ge 20){Stop-ScheduledTask -TaskName $taskName;Start-Sleep -Milliseconds 250;$task=Get-ScheduledTask -TaskName $taskName}
if($task.State-ne 'Running'){if(@(OwnedRuntime).Count){throw 'Owned runtime already exists'};Start-ScheduledTask -TaskName $taskName}
`
	case "stop":
		script += `if($task.State-eq 'Running'){Stop-ScheduledTask -TaskName $taskName}
foreach($p in $before){$current=Get-CimInstance Win32_Process -Filter ('ProcessId='+$p.ProcessId);if($current){if($current.CreationDate-ne $p.CreationDate -or $current.ExecutablePath-cne $p.ExecutablePath -or $current.CommandLine-cne $p.CommandLine){throw 'Process identity changed'};Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue}}
`
	}
	script += `$task=Get-ScheduledTask -TaskName $taskName;$info=$task|Get-ScheduledTaskInfo;@{task=$taskName;state=[string]$task.State;runtime_count=@(OwnedRuntime).Count;last_result=[long]$info.LastTaskResult;last_run=([DateTimeOffset]$info.LastRunTime).ToUnixTimeSeconds()}|ConvertTo-Json -Compress`
	commandPath := filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
	cmd := exec.CommandContext(ctx, commandPath, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	raw, err := cmd.Output()
	if err != nil {
		return taskState{}, fail(502, "task_failed", "计划任务操作失败，请核对固定任务入口与 Windows 权限")
	}
	var result taskState
	if json.Unmarshal(raw, &result) != nil {
		return result, fail(502, "task_response_invalid", "计划任务回执无效")
	}
	return result, nil
}
