#pragma once
#include <windows.h>
#include <cstdint>
#include <cstring>
#include <cwchar>
#include <vector>

// Preserve the existing two-account server policy. Only the frozen game's
// one instance mutex gets a per-child name; unrelated mutexes pass unchanged.
// No named object handles belonging to another process are opened or closed.
static std::vector<BYTE> stage3_mutex_thunk(uint32_t original,uint32_t expected,uint32_t scoped,uint32_t chars){
    std::vector<BYTE> b;
    auto bytes=[&](std::initializer_list<BYTE> value){b.insert(b.end(),value);};
    auto word=[&](uint32_t value){for(unsigned i=0;i<4;++i)b.push_back(static_cast<BYTE>(value>>(8*i)));};
    bytes({0x55,0x8b,0xec,0x56,0x57,0x8b,0x75,0x10,0x81,0xfe});word(0x10000);
    bytes({0x72,0});size_t low=b.size()-1;
    bytes({0xbf});word(expected);bytes({0xb9});word(chars);
    bytes({0xfc,0xf3,0x66,0xa7,0x75,0});size_t different=b.size()-1;
    bytes({0xbe});word(scoped);bytes({0xeb,0});size_t jump=b.size()-1;
    size_t unchanged=b.size();bytes({0x8b,0x75,0x10});size_t invoke=b.size();
    bytes({0x56,0xff,0x75,0x0c,0xff,0x75,0x08,0xb8});word(original);
    bytes({0xff,0xd0,0x5f,0x5e,0x8b,0xe5,0x5d,0xc2,0x0c,0x00});
    b[low]=static_cast<BYTE>(unchanged-low-1);b[different]=static_cast<BYTE>(unchanged-different-1);b[jump]=static_cast<BYTE>(invoke-jump-1);
    return b;
}

static bool stage3_write_word(HANDLE child,uintptr_t slot,uint32_t expected,uint32_t replacement){
    uint32_t seen=0;SIZE_T done=0;DWORD before=0,ignored=0;
    if((slot&3)||!ReadProcessMemory(child,reinterpret_cast<void*>(slot),&seen,4,&done)||done!=4||seen!=expected)return false;
    if(!VirtualProtectEx(child,reinterpret_cast<void*>(slot),4,PAGE_READWRITE,&before))return false;
    bool okay=WriteProcessMemory(child,reinterpret_cast<void*>(slot),&replacement,4,&done)&&done==4;
    if(!VirtualProtectEx(child,reinterpret_cast<void*>(slot),4,before,&ignored))okay=false;
    return okay&&ReadProcessMemory(child,reinterpret_cast<void*>(slot),&seen,4,&done)&&done==4&&seen==replacement;
}

static bool install_stage3_mutex_scope(HANDLE child,DWORD pid){
    static_assert(sizeof(void*)==4,"stage3 x86 only");
    constexpr uintptr_t publicSlot=0x0571cd04,privateStart=0x03627000;
    auto kernel=GetModuleHandleW(L"kernel32.dll");auto original=GetProcAddress(kernel,"CreateMutexW");
    if(!original)return false;uint32_t function=reinterpret_cast<uintptr_t>(original),seen=0;SIZE_T done=0;
    if(!ReadProcessMemory(child,reinterpret_cast<void*>(publicSlot),&seen,4,&done)||done!=4||seen!=function)return false;
    FILETIME created={},exit={},cpu={},user={};if(!GetProcessTimes(child,&created,&exit,&cpu,&user))return false;
    wchar_t name[112]={};swprintf_s(name,L"Local\\90US-Py90-%lu-%08lX%08lX",pid,created.dwHighDateTime,created.dwLowDateTime);
    static const wchar_t expected[]=L"dbefeuate_ccen_khxfor_lcar_blr";
    BYTE block[512]={};void* remote=VirtualAllocEx(child,nullptr,sizeof(block),MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
    if(!remote)return false;uintptr_t address=reinterpret_cast<uintptr_t>(remote);
    auto code=stage3_mutex_thunk(function,static_cast<uint32_t>(address+128),static_cast<uint32_t>(address+256),static_cast<uint32_t>(sizeof(expected)/sizeof(wchar_t)));
    if(code.size()>128||sizeof(expected)>128||sizeof(name)>256){VirtualFreeEx(child,remote,0,MEM_RELEASE);return false;}
    memcpy(block,code.data(),code.size());memcpy(block+128,expected,sizeof(expected));memcpy(block+256,name,sizeof(name));
    DWORD prior=0;
    if(!WriteProcessMemory(child,remote,block,sizeof(block),&done)||done!=sizeof(block)||!VirtualProtectEx(child,remote,sizeof(block),PAGE_EXECUTE_READ,&prior)||!FlushInstructionCache(child,remote,sizeof(block))){VirtualFreeEx(child,remote,0,MEM_RELEASE);return false;}
    std::vector<uintptr_t> slots={publicSlot};uint32_t table[0x700/4]={};
    if(ReadProcessMemory(child,reinterpret_cast<void*>(privateStart),table,sizeof(table),&done)&&done==sizeof(table))
        for(unsigned i=0;i<sizeof(table)/sizeof(table[0]);++i)if(table[i]==function)slots.push_back(privateStart+i*4);
    unsigned installed=0;
    for(;installed<slots.size();++installed)if(!stage3_write_word(child,slots[installed],function,static_cast<uint32_t>(address)))break;
    if(installed!=slots.size()){
        // A failed write can already have changed its slot (for example when
        // restoring page protection fails). Include that slot in rollback and
        // never free code still referenced by any slot. The caller terminates
        // this suspended child on every installation failure.
        bool allOriginal=true;
        for(auto slot:slots){
            seen=0;done=0;
            if(!ReadProcessMemory(child,reinterpret_cast<void*>(slot),&seen,4,&done)||done!=4){allOriginal=false;continue;}
            if(seen==static_cast<uint32_t>(address))stage3_write_word(child,slot,static_cast<uint32_t>(address),function);
            seen=0;done=0;
            if(!ReadProcessMemory(child,reinterpret_cast<void*>(slot),&seen,4,&done)||done!=4||seen!=function)allOriginal=false;
        }
        if(allOriginal)VirtualFreeEx(child,remote,0,MEM_RELEASE);
        return false;
    }
    std::fwprintf(stdout,L"[launch] scoped one game instance mutex slots=%u\n",static_cast<unsigned>(slots.size()));
    return true; // The bounded RX block belongs to this owned child until exit.
}
