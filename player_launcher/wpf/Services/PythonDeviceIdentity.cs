using System.Security.Cryptography;
using System.Text;
using Microsoft.Win32;
namespace DNFLogin.Services;
internal static class PythonDeviceIdentity
{
    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern uint GetSystemFirmwareTable(uint provider, uint table, byte[]? data, uint length);

    internal static byte[]? SystemUuid(byte[] data)
    {
        if (data.Length < 12) return null;
        var length = BitConverter.ToUInt32(data,4);
        if (length > data.Length-8) return null;
        int end = 8+(int)length;
        for (int i=8; i+4<=end; )
        {
            int size=data[i+1];
            if(size<4||i+size>end) return null;
            if(data[i]==1 && size>=24)
            {
                byte[] uuid=data.AsSpan(i+8,16).ToArray();
                return uuid.All(v=>v==0)||uuid.All(v=>v==255) ? null : uuid;
            }
            if(data[i]==127) break;
            i+=size;
            while(i+1<end && (data[i]!=0||data[i+1]!=0)) i++;
            i+=2;
        }
        return null;
    }

    internal static string Read()
    {
        using var root=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64);
        using var key=root.OpenSubKey(@"SOFTWARE\Microsoft\Cryptography");
        var value=key?.GetValue("MachineGuid") as string;
        if(string.IsNullOrWhiteSpace(value))throw new InvalidOperationException("无法读取本机标识，请检查 Windows 注册表权限。");
        string identity="Py90-device-v1:"+value.Trim().ToLowerInvariant();
        const uint table=0x52534d42;
        uint length=GetSystemFirmwareTable(table,0,null,0);
        if(length>=12 && length<=1048576)
        {
            var data=new byte[length];
            if(GetSystemFirmwareTable(table,0,data,length)==length && SystemUuid(data) is byte[] uuid)
                identity+="|system:"+Convert.ToHexString(uuid);
        }
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant();
    }
}

