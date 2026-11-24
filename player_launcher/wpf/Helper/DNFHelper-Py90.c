/* Py90 PVF key loader, solely for the SHA-pinned frozen DFO.exe. No game-assistant patches. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include "py90_pvf_keys.h"
static const uintptr_t addresses[6]={0x03d3ae60,0x03d3ae74,0x03d3ae88,0x03d3ae9c,0x03d57db8,0x03d57dcc};
static const unsigned char frozen[6][20]={
 {0x00,0x54,0x51,0x2a,0x96,0x4c,0x3e,0x9a,0x2a,0xf0,0x5d,0x4f,0x56,0x5a,0,0,0,0,0,0},
 {0x00,0x54,0x51,0x2a,0x9c,0x4c,0x34,0x9a,0x35,0xf0,0x7e,0x4f,0xcd,0x9b,0,0,0,0,0,0},
 {0x00,0x54,0x51,0x2a,0x93,0x4c,0x03,0x9a,0x03,0xf0,0x2e,0x4f,0xbd,0x2b,0,0,0,0,0,0},
 {0x00,0x54,0x51,0x2a,0x9c,0x4c,0x10,0x9a,0x07,0xf0,0x5e,0x4f,0xff,0xf9,0,0,0,0,0,0},
 {0x00,0x54,0x51,0x2a,0xa7,0x4c,0x05,0x9a,0x0d,0xf0,0x3e,0x4f,0xb3,0xe5,0,0,0,0,0,0},
 {0x00,0x54,0x51,0x2a,0xa7,0x4c,0x05,0x9a,0x0d,0xf0,0x28,0x4f,0xb3,0xe5,0,0,0,0,0,0}};
static const unsigned char header_call[5]={0xe8,0x98,0xb0,0xff,0xff};
static int readable(const void* p,SIZE_T n){
    MEMORY_BASIC_INFORMATION info;
    if(!VirtualQuery(p,&info,sizeof(info))||info.State!=MEM_COMMIT||(info.Protect&(PAGE_NOACCESS|PAGE_GUARD)))return 0;
    return (uintptr_t)p+n>=(uintptr_t)p && (uintptr_t)p+n<=(uintptr_t)info.BaseAddress+info.RegionSize;
}
static int validate_pvf_header(const unsigned char keys[48]){
    wchar_t path[32768];unsigned char header[32]={0};LARGE_INTEGER length;DWORD read=0;
    DWORD n=GetEnvironmentVariableW(L"USDOF_PVF_PATH",path,32768);
    if(n==0||n>=32768)return 0;
    HANDLE file=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
    if(file==INVALID_HANDLE_VALUE)return 0;
    int ok=GetFileSizeEx(file,&length)&&length.QuadPart>=32&&ReadFile(file,header,32,&read,NULL)&&read==32;
    CloseHandle(file);
    if(!ok)return 0;
    py90_header_crypt(header,keys);
    ok=py90_u32(header)==0x69706b6eu&&py90_u32(header+28)>=32&&(LONGLONG)py90_u32(header+28)<=length.QuadPart;
    SecureZeroMemory(header,sizeof(header));return ok;
}
static int apply_keys_at_base(uintptr_t base,const unsigned char keys[48]) {
    unsigned char* targets[6]; DWORD old[6]; unsigned protected_count=0; int ok=0;
    const unsigned char* call=(const unsigned char*)(base+(0x030194a3-0x00400000));
    if(!readable(call,5)||memcmp(call,header_call,5))return 0;
    for(unsigned i=0;i<6;i++){
        targets[i]=(unsigned char*)(base+(addresses[i]-0x00400000));
        if(!readable(targets[i],20)||memcmp(targets[i],frozen[i],20))return 0;
    }
    /* No writes until the complete signature gate and all page protections succeed. */
    for(unsigned i=0;i<6;i++){
        if(!VirtualProtect(targets[i],20,PAGE_READWRITE,&old[i]))goto done;
        protected_count++;
    }
    for(unsigned i=0;i<6;i++){
        unsigned char blob[20];py90_pack_key(keys+8*py90_key_index[i],blob);
        memcpy(targets[i],blob,20);
        if(memcmp(targets[i],blob,20)){SecureZeroMemory(blob,sizeof(blob));goto rollback;}
        SecureZeroMemory(blob,sizeof(blob));
    }
    FlushInstructionCache(GetCurrentProcess(),NULL,0);ok=1;goto done;
rollback:
    for(unsigned i=0;i<6;i++)memcpy(targets[i],frozen[i],20);
done:
    while(protected_count){unsigned i=--protected_count;DWORD ignored;VirtualProtect(targets[i],20,old[i],&ignored);}
    return ok;
}
BOOL WINAPI DllMain(HINSTANCE module,DWORD reason,LPVOID reserved){
    (void)reserved;
    if(reason==DLL_PROCESS_ATTACH){
        char key[97]={0};unsigned char raw[48]={0};wchar_t event_name[160]={0};
        DisableThreadLibraryCalls(module);
        DWORD n=GetEnvironmentVariableA("DNF_PYTHON_PVF_KEYSET",key,sizeof(key));
        DWORD en=GetEnvironmentVariableW(L"DNF_PYTHON_PVF_READY_EVENT",event_name,160);
        SetEnvironmentVariableA("DNF_PYTHON_PVF_KEYSET",NULL);
        if(n!=96||en==0||en>=160||!py90_parse_keys(key,raw)){
            SecureZeroMemory(key,sizeof(key));SecureZeroMemory(raw,sizeof(raw));return FALSE;
        }
        HANDLE ready=OpenEventW(EVENT_MODIFY_STATE,FALSE,event_name);
        if(!ready){SecureZeroMemory(key,sizeof(key));SecureZeroMemory(raw,sizeof(raw));return FALSE;}
        int ok=validate_pvf_header(raw)&&apply_keys_at_base((uintptr_t)GetModuleHandleW(NULL),raw);
        SecureZeroMemory(key,sizeof(key));SecureZeroMemory(raw,sizeof(raw));
        if(!ok){CloseHandle(ready);return FALSE;}
        BOOL signaled=SetEvent(ready);CloseHandle(ready);return signaled;
    }
    return TRUE;
}
