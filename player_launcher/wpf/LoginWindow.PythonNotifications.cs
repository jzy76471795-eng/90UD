#if PYTHON90
using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using DNFLogin.Services;

namespace DNFLogin;
public partial class LoginWindow
{
    private CancellationTokenSource? pythonNotifications;
    private readonly List<PythonAnnouncementOverlay> pythonAnnouncementOverlays = new();

    private static JsonElement PythonData(JsonElement doc) => doc.TryGetProperty("data", out var data) ? data : doc;

    private void StartPythonNotifications()
    {
        if (pythonNotifications is not null) return;
        pythonNotifications = new CancellationTokenSource();
        Closed += (_, _) =>
        {
            pythonNotifications.Cancel();
            foreach (var window in pythonAnnouncementOverlays.ToArray()) window.Close();
            pythonAnnouncementOverlays.Clear();
        };
        _ = PythonNoticesLoopAsync(pythonNotifications.Token);
        _ = PythonAnnouncementsLoopAsync(pythonNotifications.Token);
    }

    private async Task PythonNoticesLoopAsync(CancellationToken cancel)
    {
        while (!cancel.IsCancellationRequested)
        {
            try
            {
                using var api = PythonClientLauncher.CreateApi();
                var data = PythonData(await api.NoticesAsync());
                var rows = new List<PlayerNotice>();
                foreach (var row in data.GetProperty("notices").EnumerateArray())
                {
                    var title = NoticeText(row, "title");
                    if (!string.IsNullOrWhiteSpace(title))
                        rows.Add(PlayerNotice.FromTitle(title, NoticeText(row, "notice_date"), NoticeText(row, "body")));
                }
                if (!cancel.IsCancellationRequested) ApplyNoticesToPanel(rows);
            }
            catch (OperationCanceledException) { break; }
            catch
            {
                if (!cancel.IsCancellationRequested && _serverNotices is null)
                    ServerNoticeStatus.Text = "暂时无法获取公告";
            }
            try { await Task.Delay(TimeSpan.FromSeconds(15), cancel); } catch (OperationCanceledException) { break; }
        }
    }

    private static string NoticeText(JsonElement row, string key) =>
        row.ValueKind == JsonValueKind.Object && row.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? "" : "";

    private async Task PythonAnnouncementsLoopAsync(CancellationToken cancel)
    {
        string currentToken = "";
        long cursor = -1;
        while (!cancel.IsCancellationRequested)
        {
            try
            {
                var active = pythonSessions.OrderBy(pair => pair.Key).FirstOrDefault(pair => GameProcessManager.IsRunning(pair.Key));
                if (active.Value.Api is not null)
                {
                    var token = active.Value.Session.AccessToken;
                    if (token != currentToken) { currentToken = token; cursor = -1; }
                    var data = PythonData(await active.Value.Api.AnnouncementsAsync(token, cursor));
                    cursor = data.GetProperty("cursor").GetInt64();
                    if (data.TryGetProperty("announcement", out var announcement) && announcement.ValueKind == JsonValueKind.Object)
                    {
                        var content = announcement.GetProperty("content").GetString() ?? "";
                        var colorText = announcement.GetProperty("color").GetString() ?? "0xFFFFFFFF";
                        if (content.Length is > 0 and <= 400 && colorText.Length == 10 && uint.TryParse(colorText.AsSpan(2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var color))
                        {
                            var identity = announcement.GetProperty("id").GetInt64();
                            foreach (var pair in pythonSessions.ToArray())
                            {
                                var game = GameProcessManager.GetProcess(pair.Key);
                                if (game is null || game.HasExited) continue;
                                game.Refresh();
                                if (game.MainWindowHandle == IntPtr.Zero) continue;
                                if (pythonAnnouncementOverlays.Count >= 32) pythonAnnouncementOverlays[0].Close();
                                var overlay = new PythonAnnouncementOverlay(game.MainWindowHandle, content, color);
                                var receivingApi = pair.Value.Api;
                                var receivingToken = pair.Value.Session.AccessToken;
                                pythonAnnouncementOverlays.Add(overlay);
                                overlay.Closed += (_, _) => pythonAnnouncementOverlays.Remove(overlay);
                                overlay.FirstDisplayed += async () => { try { await receivingApi.AcknowledgeAnnouncementAsync(receivingToken, identity); } catch { } };
                                overlay.Begin();
                            }
                        }
                    }
                }
                else { currentToken = ""; cursor = -1; }
            }
            catch (OperationCanceledException) { break; }
            catch { /* Tokens and payloads must not enter startup logs. */ }
            try { await Task.Delay(TimeSpan.FromSeconds(1), cancel); } catch (OperationCanceledException) { break; }
        }
    }
}
#endif
