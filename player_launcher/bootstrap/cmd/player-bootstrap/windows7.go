//go:build windows
package main
import("fmt";"syscall";"unsafe")
func checkWindowsPrerequisites() error {
 var info struct{Size,Major,Minor,Build,Platform uint32;CSD [128]uint16;ServicePackMajor,ServicePackMinor,Suite uint16;Product,Reserved byte}
 info.Size=uint32(unsafe.Sizeof(info))
 result,_,_:=syscall.NewLazyDLL("ntdll.dll").NewProc("RtlGetVersion").Call(uintptr(unsafe.Pointer(&info)))
 if result!=0{return fmt.Errorf("无法确认 Windows 系统版本")}
 if info.Major<6 || (info.Major==6&&info.Minor<1) || (info.Major==6&&info.Minor==1&&info.Build<7601){return fmt.Errorf("登录器要求 Windows 7 SP1 或更新系统")}
 if info.Major==6&&info.Minor==1{
  if syscall.NewLazyDLL("kernel32.dll").NewProc("AddDllDirectory").Find()!=nil{return fmt.Errorf("Win7 缺少安全加载补丁，请安装 KB3063858 或等效系统补丁后重试")}
  name,_:=syscall.UTF16PtrFromString("ucrtbase.dll")
  crt,_,_:=syscall.NewLazyDLL("kernel32.dll").NewProc("LoadLibraryExW").Call(uintptr(unsafe.Pointer(name)),0,0x800)
  if crt==0{return fmt.Errorf("Win7 缺少通用 C 运行库，请安装 KB2999226 或 VC++ 2015-2019 运行库后重试")};syscall.FreeLibrary(syscall.Handle(crt))
 }
 return nil
}
