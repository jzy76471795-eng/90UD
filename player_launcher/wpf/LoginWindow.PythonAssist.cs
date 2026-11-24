using System.Net.Http;
using System.Text.Json;
using System.Windows.Threading;
using DNFLogin.Services;

namespace DNFLogin;

public partial class LoginWindow
{
    private sealed class AssistSessionState
    {
        public string Target = "";
        public bool Pickup;
        public bool Syncing;
        public bool Busy;
        public long LastSync;
        public long NextBatch;
        public string DoorTarget = "";
        public bool DoorEnabled;
        public long NextDoor;
    }

    private readonly Dictionary<string, AssistSessionState> assistSessions = new();
    private PythonAnnouncementOverlay? assistOverlay;

    private void StartPythonAssist()
    {
        var input = new PythonAssistInput((slot, window, action) => _ = RunAssistBatchAsync(slot, window, action));
        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        timer.Tick += (_, _) =>
        {
            input.Refresh();
            SyncAssistSessions();
        };
        timer.Start();
        Closed += (_, _) =>
        {
            timer.Stop();
            input.Dispose();
            assistSessions.Clear();
            assistOverlay?.Close();
        };
    }

    private void SyncAssistSessions()
    {
        var settings = AppConfig.Settings.GameAssist;
        bool sync = settings.Pickup || settings.Sell || settings.Disjoint || settings.InstantDoor;
        foreach (string key in assistSessions.Keys.ToArray())
            if (!pythonSessions.Values.Any(entry => entry.Session.SessionId == key))
                assistSessions.Remove(key);
        foreach (var pair in pythonSessions)
        {
            if (!GameProcessManager.IsRunning(pair.Key)) continue;
            var (api, session, _, _) = pair.Value;
            if (!assistSessions.TryGetValue(session.SessionId, out var state))
            {
                if (!sync) continue;
                state = new AssistSessionState();
                assistSessions.Add(session.SessionId, state);
            }
            if (state.DoorEnabled && !settings.InstantDoor)
            {
                state.DoorEnabled = false;
                if (state.DoorTarget.Length != 0) _ = CancelDoorAsync(api,session,state.DoorTarget);
            }
            if (state.Syncing || (!sync && !state.Pickup)) continue;
            long now = Environment.TickCount64;
            if (state.Pickup == settings.Pickup && state.LastSync != 0 && now - state.LastSync < 3000) continue;
            _ = SyncAssistAsync(api, session, state, settings.Pickup, settings.InstantDoor);
        }
    }

    private static async Task SyncAssistAsync(PythonLauncherApi api, PythonSession session, AssistSessionState state, bool pickup, bool door)
    {
        state.Syncing = true;
        state.LastSync = Environment.TickCount64;
        try
        {
            var result = await api.AssistAsync(session, new { action = "configure", pickup, request_id = Guid.NewGuid().ToString("N") });
            state.Target = result.GetProperty("target").GetString() ?? "";
            state.Pickup = pickup;
            state.DoorTarget = result.TryGetProperty("door_target",out var target) ? target.GetString() ?? "" : "";
            state.DoorEnabled = door;
        }
        catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException
            or JsonException or KeyNotFoundException)
        {
            state.Target = "";
        }
        finally { state.Syncing = false; }
    }

    private async Task RunAssistBatchAsync(int slot, IntPtr window, string action)
    {
        if (action is "door-left" or "door-right" or "door-up" or "door-down")
        {
            await RunInstantDoorAsync(slot,window,action);
            return;
        }
        if (PythonAssistInput.ForegroundSlot(window) != slot || !pythonSessions.TryGetValue(slot, out var entry)) return;
        var settings = AppConfig.Settings.GameAssist;
        bool sell = action == "sell";
        if (!(sell ? settings.Sell : settings.Disjoint)) return;
        var rarities = sell ? settings.SellRarities : settings.DisjointRarities;
        string label = sell ? "出售" : "分解";
        if (rarities is not { Length: > 0 })
        {
            ShowAssistResult(window, $"请先在登录器设置中勾选要{label}的装备品阶");
            return;
        }
        if (!assistSessions.TryGetValue(entry.Session.SessionId, out var state) || state.Target.Length == 0
            || Environment.TickCount64 - state.LastSync > 8000)
        {
            ShowAssistResult(window, "请进入角色的城镇，等待游戏辅助连接后再试");
            return;
        }
        if (state.Busy || Environment.TickCount64 < state.NextBatch) return;
        state.Busy = true;
        try
        {
            var result = await entry.Api.AssistAsync(entry.Session,
                new { action, rarities, target = state.Target, request_id = Guid.NewGuid().ToString("N") });
            int count = result.GetProperty("processed").GetInt32();
            string message = count == 0 ? $"没有符合条件的装备可{label}" : $"已{label} {count} 件装备";
            if (sell && count > 0) message += $"，获得 {result.GetProperty("gold").GetInt64():N0} 金币";
            ShowAssistResult(window, message);
        }
        catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException
            or JsonException or KeyNotFoundException)
        {
            ShowAssistResult(window, $"{label}结果未确认。{ex.Message}");
        }
        finally
        {
            state.Busy = false;
            state.NextBatch = Environment.TickCount64 + 800;
        }
    }

    private void ShowAssistResult(IntPtr window, string message)
    {
        if (PythonAssistInput.ForegroundSlot(window) < 0) return;
        assistOverlay?.Close();
        var overlay = new PythonAnnouncementOverlay(window, message, 0xFFF6EDBB);
        assistOverlay = overlay;
        overlay.Closed += (_, _) => { if (ReferenceEquals(assistOverlay, overlay)) assistOverlay = null; };
        overlay.Begin();
    }

    private static async Task CancelDoorAsync(PythonLauncherApi api,PythonSession session,string target)
    {
        try { await api.AssistAsync(session,new {action="door-cancel",target,request_id=Guid.NewGuid().ToString("N")}); }
        catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException or JsonException) { }
    }

    private async Task RunInstantDoorAsync(int slot,IntPtr window,string action)
    {
        if (!AppConfig.Settings.GameAssist.InstantDoor || PythonAssistInput.ForegroundSlot(window)!=slot
            || !pythonSessions.TryGetValue(slot,out var entry)) return;
        if (!assistSessions.TryGetValue(entry.Session.SessionId,out var state))
            assistSessions[entry.Session.SessionId]=state=new AssistSessionState();
        if (state.Busy || Environment.TickCount64<state.NextDoor) return;
        state.Busy=true;
        try
        {
            var configured=await entry.Api.AssistAsync(entry.Session,new {action="configure",pickup=AppConfig.Settings.GameAssist.Pickup,request_id=Guid.NewGuid().ToString("N")});
            string target=configured.TryGetProperty("door_target",out var value) ? value.GetString() ?? "" : "";
            state.DoorTarget=target;state.DoorEnabled=AppConfig.Settings.GameAssist.InstantDoor;
            if (!state.DoorEnabled || PythonAssistInput.ForegroundSlot(window)!=slot) return;
            if (target.Length==0) { ShowAssistResult(window,"请在副本房间加载完成后使用瞬图");return; }
            var result=await entry.Api.AssistAsync(entry.Session,new {action,target,request_id=Guid.NewGuid().ToString("N")});
            string status=result.GetProperty("state").GetString() ?? "";
            if (status=="waiting")
                ShowAssistResult(window,$"等待队友同方向过门（{result.GetProperty("ready").GetInt32()}/{result.GetProperty("required").GetInt32()}）");
        }
        catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException or JsonException or KeyNotFoundException)
        { ShowAssistResult(window,ex.Message); }
        finally { state.Busy=false;state.NextDoor=Environment.TickCount64+450; }
    }
}
