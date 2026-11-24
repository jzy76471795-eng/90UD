using System.Text.RegularExpressions;

namespace DNFLogin.Services;

// Translate only fixed protocol reasons. Never display a raw response body or
// an arbitrary server message, which could contain credentials or diagnostics.
internal static class PythonSessionErrors
{
    public static string SafeCode(string? value) => value is not null && Regex.IsMatch(value, "\\A[A-Z0-9_]{1,64}\\z") ? value : "UNKNOWN";

    public static string Describe(string? code, string? reason, string? message, string fallback)
    {
        if (code == "MULTI_OPEN_LIMIT") return "此设备已达到服务器允许的同时登录数量（含正在启动的游戏），请退出一个游戏后重试。（MULTI_OPEN_LIMIT）";
        if (code == "INVALID_DEVICE") return "本机设备标识无效，请使用配套登录器。（INVALID_DEVICE）";
        if (code == "PASSWORD_NOT_SET") return "此账号尚未设置远程登录密码，请联系管理员。（PASSWORD_NOT_SET）";
        if (code != "SESSION_CONFLICT") return fallback;
        if (string.IsNullOrEmpty(reason)) reason = message switch
        {
            "this account already has a running game; exit it before logging in again" => "ACCOUNT_GAME_RUNNING",
            "this account already has an active session on another device" => "ACCOUNT_SESSION_OTHER_DEVICE",
            "this account already has an active session from another source IP" => "ACCOUNT_SESSION_OTHER_IP",
            "exit the legacy launcher session before using multi-account login" => "LEGACY_IP_SESSION",
            "this source IP already owns another active account session; use the updated launcher" => "LEGACY_IP_SESSION",
            _ => "UNKNOWN"
        };
        return reason switch
        {
            "ACCOUNT_GAME_RUNNING" => "该账号的游戏连接仍在运行，请先退出原有游戏后再登录。（ACCOUNT_GAME_RUNNING）",
            "ACCOUNT_SESSION_OTHER_DEVICE" => "该账号在其他设备或网络上仍有登录会话。确认退出后稍候重试；持续报错请联系管理员核对残留会话。（ACCOUNT_SESSION_OTHER_DEVICE）",
            "ACCOUNT_SESSION_OTHER_IP" => "该账号仍有来自其他网络的登录会话，请确认旧登录已退出。（ACCOUNT_SESSION_OTHER_IP）",
            "LEGACY_IP_SESSION" => "同一公网IP下仍有旧版登录会话，已阻止新登录。请使用配套登录器；持续报错请联系管理员核对旧会话。（LEGACY_IP_SESSION）",
            _ => "登录会话冲突，请确认旧游戏及登录器已退出；持续报错请联系管理员核对会话。（SESSION_CONFLICT）"
        };
    }
}
