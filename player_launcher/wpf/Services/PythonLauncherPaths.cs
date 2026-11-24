#if PYTHON90
using System.IO;

namespace DNFLogin.Services;

// The single-exe host runs managed dependencies from a private cache while the
// game remains beside the original launcher. Working directory is never used.
public static class PythonLauncherPaths
{
    private static bool SingleFileHost => Environment.GetEnvironmentVariable("YH_SINGLE_FILE_HOST") == "1";
    public static string LauncherDirectory => Environment.GetEnvironmentVariable("PY90_PORTABLE_ROOT") ?? HostPath("YH_LAUNCHER_DIRECTORY") ?? AppContext.BaseDirectory;
    public static string? DataDirectory => HostPath("YH_LAUNCHER_DATA");
    private static string? HostPath(string name)
    {
        if (!SingleFileHost) return null;
        var value = Environment.GetEnvironmentVariable(name);
        if (string.IsNullOrWhiteSpace(value) || !Path.IsPathFullyQualified(value) || !Directory.Exists(value))
            throw new InvalidOperationException("登录器运行目录不可用，请重新打开单文件登录器。");
        return Path.GetFullPath(value);
    }
}
#endif
