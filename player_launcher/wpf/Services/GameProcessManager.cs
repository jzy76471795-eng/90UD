using System;
using System.Diagnostics;

namespace DNFLogin
{
    /// <summary>
    /// 游戏进程托管：主/副两个槽位，各自记录 DNF 进程与账号；
    /// 进程退出事件恢复登录状态，全部退出触发弹回登录器；退出登录器时结束所有 DNF 进程。
    /// </summary>
    public static class GameProcessManager
    {
        public const int MainSlot = 0;

        private sealed class SlotState
        {
            public int Slot;
            public Process? Process;
            public string Account = string.Empty;

            public bool Running => Process is not null && !Process.HasExited;
        }

        // TEMP-8BOX：多开槽位数组扩到 8 项（主/副 + 3 号~8 号），删功能时缩回两项。
        private static readonly SlotState[] Slots =
        {
            new() { Slot = MainSlot },
        };

        /// <summary>槽位总数（含临时多开槽位）。调用方用它做"槽位号是否合法"的边界判断。</summary>
        public static int SlotCount => Slots.Length;

        /// <summary>游戏进程退出（参数：槽位）</summary>
        public static event Action<int>? GameExited;

        /// <summary>全部游戏退出</summary>
        public static event Action? AllGamesExited;

        public static bool IsRunning(int slot)
        {
            return slot >= 0 && slot < Slots.Length && Slots[slot].Running;
        }

        // TEMP-8BOX：原来只判断主/副两个槽位，扩到 8 槽后必须整体遍历，
        // 否则"只剩 3 号~8 号还在跑"会被误判成全部退出（登录器会提前弹回前台并释放会话）。
        public static bool AnyRunning
        {
            get
            {
                foreach (var state in Slots)
                {
                    if (state.Running)
                    {
                        return true;
                    }
                }

                return false;
            }
        }

        public static string GetAccount(int slot)
        {
            return slot >= 0 && slot < Slots.Length ? Slots[slot].Account : string.Empty;
        }

        /// <summary>获取指定槽位已托管的进程（未启动或无则返回 null）</summary>
        public static Process? GetProcess(int slot)
        {
            return slot >= 0 && slot < Slots.Length ? Slots[slot].Process : null;
        }

        /// <summary>登记一个已启动的 DNF 进程到指定槽位，并挂接退出事件</summary>
        public static void RegisterProcess(int slot, Process process, string account)
        {
            if (slot < 0 || slot >= Slots.Length || process is null)
            {
                return;
            }

            var state = Slots[slot];
            state.Process = process;
            state.Account = account;
            void NotifyExit()
            {
                var dispatcher = System.Windows.Application.Current?.Dispatcher;
                if (dispatcher is null || dispatcher.HasShutdownStarted) return;
                dispatcher.BeginInvoke(new Action(() =>
                {
                    if (!ReferenceEquals(state.Process, process)) return;
                    state.Process = null;
                    state.Account = string.Empty;
                    GameExited?.Invoke(slot);
                    if (!AnyRunning) AllGamesExited?.Invoke();
                }));
            }
            process.Exited += (_, _) => NotifyExit();
            process.EnableRaisingEvents = true;
            // The launch code can already be observing Exited when this slot is registered.
            if (process.HasExited) NotifyExit();
        }

        /// <summary>
        /// 结束指定槽位的 DNF 进程（GM 封禁踢人 / 单账号下线用）。
        /// 进程退出会走 Exited 事件统一恢复状态与触发 GameExited，无需手动清理。
        /// </summary>
        public static void KillSlot(int slot)
        {
            if (slot < 0 || slot >= Slots.Length)
            {
                return;
            }

            var state = Slots[slot];
            if (state.Process is null)
            {
                return;
            }

            try
            {
                if (!state.Process.HasExited)
                {
                    state.Process.Kill();
                    state.Process.WaitForExit(3000);
                }
            }
            catch
            {
                // 进程已退出或无权限，交由 Exited 事件清理
            }
        }

        /// <summary>
        /// 按账号结束其所在槽位的 DNF 进程。
        /// </summary>
        public static bool KillByAccount(string username)
        {
            if (string.IsNullOrWhiteSpace(username))
            {
                return false;
            }

            var killed = false;
            for (var i = 0; i < Slots.Length; i++)
            {
                var state = Slots[i];
                if (state.Running &&
                    string.Equals(state.Account, username, StringComparison.OrdinalIgnoreCase))
                {
                    KillSlot(i);
                    killed = true;
                }
            }
            return killed;
        }

        /// <summary>结束所有托管的 DNF 进程（退出登录器时调用）</summary>
        public static void KillAll()
        {
            foreach (var state in Slots)
            {
                if (state.Process is null)
                {
                    continue;
                }

                // 先摘除槽位引用，避免 Exited 回调与 Dispose 竞态导致 NullReferenceException
                var proc = state.Process;
                state.Process = null;
                state.Account = string.Empty;

                try
                {
                    if (!proc.HasExited)
                    {
                        proc.Kill();
                        proc.WaitForExit(3000);
                    }
                }
                catch
                {
                    // 进程已退出或无权限
                }
                finally
                {
                    try
                    {
                        proc.Dispose();
                    }
                    catch
                    {
                        // Dispose 竞态兜底，忽略即可
                    }
                }
            }
        }
    }
}
