#pragma once
#include <windows.h>
#include <cstring>

// Frozen Stage3 DFO.exe: FindWindowW(class, NULL) returned an existing window
// and exited before CreateWindowExW. Change only the branch in the suspended
// child. Do not rename other windows, close mutexes, or modify the on-disk EXE.
static bool allow_stage3_instance(HANDLE child) {
    static const unsigned char expected[] = {
        0xff,0x15,0x84,0x75,0x62,0x03,0x85,0xc0,0x74,0x0e,
        0x50,0xff,0x15,0xcc,0x74,0x62,0x03,0x32,0xc0,0x5e,
        0x8b,0xe5,0x5d,0xc3
    };
    auto address = reinterpret_cast<void*>(0x02c3709a);
    unsigned char original[sizeof(expected)] = {};
    SIZE_T count = 0;
    if (!ReadProcessMemory(child,address,original,sizeof(original),&count) ||
        count!=sizeof(original) || memcmp(original,expected,sizeof(expected))!=0) return false;
    auto branch = reinterpret_cast<void*>(0x02c370a2);
    DWORD old = 0, ignored = 0;
    if (!VirtualProtectEx(child,branch,1,PAGE_EXECUTE_READWRITE,&old)) return false;
    const unsigned char jump = 0xeb;
    bool ok = WriteProcessMemory(child,branch,&jump,1,&count) && count==1;
    if (!VirtualProtectEx(child,branch,1,old,&ignored)) ok=false;
    if (!FlushInstructionCache(child,branch,1)) ok=false;
    unsigned char actual = 0;
    return ok && ReadProcessMemory(child,branch,&actual,1,&count) && count==1 && actual==jump;
}
