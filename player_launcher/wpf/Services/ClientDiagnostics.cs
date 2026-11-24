using System.IO;
using System.Text;

namespace DNFLogin.Services;

internal static class ClientDiagnostics
{
    private const int LimitBytes = 256 * 1024;
    private static readonly object Gate = new();

    internal static bool Detailed(string directory) =>
        File.Exists(Path.Combine(directory, "diagnostics.enable"));

    internal static void Append(string directory, string name, string detail)
    {
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(directory);
                string path = Path.Combine(directory, name);
                string line = $"{DateTimeOffset.Now:O} {detail}{Environment.NewLine}";
                if (File.Exists(path) && new FileInfo(path).Length + Encoding.UTF8.GetByteCount(line) > LimitBytes)
                {
                    if (new FileInfo(path).Length <= LimitBytes)
                        File.Move(path, path + ".previous", overwrite: true);
                    else
                        File.Delete(path);
                }
                File.AppendAllText(path, line, Encoding.UTF8);
            }
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
