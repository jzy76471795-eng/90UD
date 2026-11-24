#if PYTHON90
using System.IO;

namespace DNFLogin.Services;

public static class PythonClientLocation
{
    public static string FindExecutable(string launcherDirectory)
    {
        var root = Path.GetFullPath(launcherDirectory);
        var exe = Path.Combine(root, "DFO.exe");
        return File.Exists(exe) && File.Exists(Path.Combine(root, "Script.pvf")) ? exe : "";
    }

    public static string RequireExecutable()
    {
        var exe = FindExecutable(PythonLauncherPaths.LauncherDirectory);
        if (exe.Length == 0) throw new InvalidOperationException("请将登录器放到包含 DFO.exe 和 Script.pvf 的客户端根目录后重新打开。");
        return exe;
    }
}
#endif
