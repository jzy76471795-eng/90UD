// Game-owned D3D9 panel/input. No code-site patch, gameplay packet or secret file.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <d3d9.h>
#include <bcrypt.h>
#include <stdint.h>
#include <stdio.h>
#include <string>
#include <algorithm>
#include <vector>
#include <gdiplus.h>
#include <share.h>
#include "native_assist_icon.h"
#include "native_adventure_icon.h"
#pragma comment(lib,"user32.lib")
#pragma comment(lib,"gdi32.lib")
#pragma comment(lib,"d3d9.lib")
#pragma comment(lib,"bcrypt.lib")
#pragma comment(lib,"gdiplus.lib")

namespace Assist {
constexpr uint32_t Magic=0x31414755;constexpr int PW=362,PH=384;
constexpr int AdventureTooltipWidth=94,AdventureTooltipHeight=21;
struct Settings{bool pickup=false,door=false,fire=false,sell=false,disjoint=false;int key='X',interval=80;uint32_t sellMask=0,disjointMask=0;};
static Settings config;
static HMODULE module,pinned;static HWND window;static WNDPROC previousProc;
static SRWLOCK stateLock=SRWLOCK_INIT;static bool open=false,dirty=true,connected=false;
static std::wstring message=L"功能默认关闭",ini,pipe;static BYTE cookie[32];
static HANDLE wake;static volatile LONG stop=0;static uint32_t pending=0;
static UINT screenW=800,screenH=600;static RECT panelRect,buttonRect;static bool mouseCaptured=false;
static bool buttonHover=false;
static bool adventureHover=false,adventureShown=false;
static RECT adventureButtonRect={},adventureTooltipRect={};
static IDirect3DTexture9* adventureIcons[2];
static IDirect3DTexture9* adventureTooltipTexture;
static const UINT MaintenanceMessage=WM_APP+0x673;
static bool NativeMenuVisible();
static void MaintainDevice();
static IDirect3DTexture9* panelTexture;static IDirect3DTexture9* buttonTexture;static IDirect3DDevice9* device;
static const int intervals[]={50,65,80,100,120,160,200};
static bool Held[256]={},Captured[256]={},fireDown=false;static WORD fireScan=0;static ULONGLONG fireDue=0;
static bool firePaused=false;
static FILE* trace;
#ifdef ASSIST_TEST
static bool testActive=true,testEditing=false,testModifier=false,testAlt=false,testOther=false;
static bool testMenuVisible=true;
static bool testTownHudVisible=false;
static unsigned testHudCount=0;
static std::vector<std::pair<WORD,bool>> testInput;
#endif
static void Log(const char* text){if(trace){fprintf(trace,"%s\n",text);fflush(trace);}}
struct Lock{bool exclusive;Lock(bool x=true):exclusive(x){if(x)AcquireSRWLockExclusive(&stateLock);else AcquireSRWLockShared(&stateLock);}~Lock(){if(exclusive)ReleaseSRWLockExclusive(&stateLock);else ReleaseSRWLockShared(&stateLock);}};
static void Status(const std::wstring& text,bool ready){Lock lock;message=text;connected=ready;dirty=true;}
static Settings Snapshot(){Lock lock(false);return config;}
static void Queue(uint32_t action){Lock lock;if(!pending||action!=1)pending=action;SetEvent(wake);}
static bool Inside(RECT r,int x,int y){return x>=r.left&&x<r.right&&y>=r.top&&y<r.bottom;}
static void Layout(UINT w,UINT h){screenW=w;screenH=h;int x=int(w)/2+164,y=int(h)/2-34;buttonRect={x,y,x+68,y+58};int px=(int(w)-PW)/2,py=std::max(4,(int(h)-PH)/2);panelRect={px,py,px+PW,py+PH};
}
template<class T>static bool Read(uintptr_t address,T& value){SIZE_T n=0;return ReadProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(address),&value,sizeof(value),&n)&&n==sizeof(value);}
#include "native_adventure.h"
struct HudSlot {unsigned index; int width,height;};
static bool NextTownHudSlot(HudSlot& slot, UINT width, UINT height){
#if defined(ASSIST_TEST)
 slot={testHudCount,int(width),int(height)};
 return testTownHudVisible;
#elif defined(ASSIST_HARNESS)
 auto count=reinterpret_cast<uintptr_t>(GetPropW(window,L"AssistHarnessHud"));
 if(!count)return false;
 slot={unsigned(count-1),int(width),int(height)};
 return true;
#else
 const uintptr_t base=reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr))-0x400000;
 // This is the stock information-button manager and town scene set by the game.
 constexpr BYTE expectedLayout[]={0x55,0x8b,0xec,0x83,0xec,0x1c,0x53,0x56,0x57,0x8d,0x71,0x04};
 BYTE layout[sizeof(expectedLayout)]={};
 int scene=0;
 uintptr_t manager=0;
 if(!Read(base+0x0411787c,scene)||scene!=1||!Read(base+0x0418903c,manager)||!manager||
    !Read(base+0x01881e50,layout)||memcmp(layout,expectedLayout,sizeof(layout))!=0)return false;
 struct {uintptr_t head;unsigned size;} buttonList={};
 if(!Read(base+0x04171b40,buttonList)||!buttonList.head||buttonList.size>512)return false;
 uintptr_t cursor=0;
 if(!Read(buttonList.head,cursor))return false;
 unsigned active=0;
 for(unsigned n=0;n<buttonList.size;++n){
  struct {uintptr_t next,prev;unsigned id;uintptr_t button;} node={};
  if(!cursor||cursor==buttonList.head||!Read(cursor,node))return false;
  if(node.button){
   struct {uintptr_t control,unused[3];BYTE shown;} button={};
   if(!Read(node.button,button))return false;
   if(button.control&&button.shown){
    if((node.id&0xffff)==1195)return false;
    ++active;
   }
  }
  cursor=node.next;
 }
 if(cursor!=buttonList.head||active>=33)return false;
 struct {int w,h;} viewport={};
 if(!Read(base+0x0412da68,viewport)||viewport.w<640||viewport.h<440)return false;
 slot={active,viewport.w,viewport.h};
 return true;
#endif
}
static void LayoutAdventure(const HudSlot& slot, UINT w, UINT h){
 // Frozen 90US arranges native information buttons 11 across, 20px apart.
 int x=275+(slot.width-800)/2+int(slot.index%11)*20;
 int y=525+slot.height-600-int(slot.index/11)*20;
 adventureButtonRect={MulDiv(x,w,slot.width),MulDiv(y,h,slot.height),
                      MulDiv(x+20,w,slot.width),MulDiv(y+20,h,slot.height)};
 int left=std::min<int>(adventureButtonRect.left,int(w)-AdventureTooltipWidth);
 int top=adventureButtonRect.top-AdventureTooltipHeight-3;
 adventureTooltipRect={left,top,left+AdventureTooltipWidth,top+AdventureTooltipHeight};
}
static bool NativeMenuVisible(){
#if defined(ASSIST_TEST)
 return testMenuVisible;
#elif defined(ASSIST_HARNESS)
 return window&&GetPropW(window,L"AssistHarnessMenu")!=nullptr;
#else
 uintptr_t base=reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr)),manager=0,begin=0,end=0,object=0,vt=0;
 if(!Read(base+0x04189ac0-0x400000,manager)||!manager)return false;
 uintptr_t slot=manager+(0x389*5+0x37)*4;
 if(!Read(slot+4,begin)||!Read(slot+8,end)||!begin||end<=begin||end-begin>64||!Read(begin,object)||!object||!Read(object,vt))return false;
 return vt==base+0x0371dcf4-0x400000;
#endif
}
static void Load(){
 auto n=[](const wchar_t* key,int d){return GetPrivateProfileIntW(L"Assist",key,d,ini.c_str());};
 config.pickup=n(L"Pickup",0)==1;config.door=n(L"InstantDoor",0)==1;config.fire=n(L"AutoFire",0)==1;config.sell=n(L"Sell",0)==1;config.disjoint=n(L"Disjoint",0)==1;
 config.key=n(L"FireKey",'X');if(config.key<'A'||config.key>'Z')config.key='X';config.interval=n(L"IntervalMs",80);if(std::find(std::begin(intervals),std::end(intervals),config.interval)==std::end(intervals))config.interval=80;
 config.sellMask=n(L"SellMask",0)&127;config.disjointMask=n(L"DisjointMask",0)&127;
}
static void Save(){Settings saved=Snapshot();
 auto put=[](const wchar_t* key,int value){wchar_t buf[24];swprintf_s(buf,L"%d",value);WritePrivateProfileStringW(L"Assist",key,buf,ini.c_str());};
 put(L"Pickup",saved.pickup);put(L"InstantDoor",saved.door);put(L"AutoFire",saved.fire);put(L"Sell",saved.sell);put(L"Disjoint",saved.disjoint);put(L"FireKey",saved.key);put(L"IntervalMs",saved.interval);put(L"SellMask",saved.sellMask);put(L"DisjointMask",saved.disjointMask);
}
static void Fill(HDC dc,int x,int y,int w,int h,COLORREF c){RECT r={x,y,x+w,y+h};HBRUSH b=CreateSolidBrush(c);FillRect(dc,&r,b);DeleteObject(b);}
static void Line(HDC dc,int x,int y,int xx,int yy,COLORREF c){HPEN p=CreatePen(PS_SOLID,1,c);auto old=SelectObject(dc,p);MoveToEx(dc,x,y,nullptr);LineTo(dc,xx,yy);SelectObject(dc,old);DeleteObject(p);}
static void Text(HDC dc,int x,int y,const wchar_t* s,COLORREF c,int width=340){SetTextColor(dc,c);RECT r={x,y,x+width,y+21};DrawTextW(dc,s,-1,&r,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_NOPREFIX|DT_END_ELLIPSIS);}
static void Check(HDC dc,int x,int y,bool value){Fill(dc,x,y,12,12,RGB(137,113,69));Fill(dc,x+1,y+1,10,10,RGB(12,16,20));if(value){Line(dc,x+2,y+5,x+5,y+9,RGB(243,208,121));Line(dc,x+5,y+9,x+10,y+2,RGB(243,208,121));}}
static void Button(HDC dc,int x,int y,int w,const wchar_t* text){Fill(dc,x,y,w,24,RGB(116,91,51));Fill(dc,x+1,y+1,w-2,22,RGB(40,37,32));Line(dc,x+2,y+2,x+w-2,y+2,RGB(79,68,49));Text(dc,x+8,y+1,text,RGB(230,211,166),w-10);}
static SIZE ArtSize(bool panel,bool adventure){
 if(adventure)return SIZE{AdventureTooltipWidth,AdventureTooltipHeight};
 return panel?SIZE{PW,PH}:SIZE{68,58};
}
static HBITMAP Paint(bool panel,void** pixels,bool adventure=false){
 const auto size=ArtSize(panel,adventure);
 int w=size.cx,h=size.cy;
 BITMAPINFO bi={};bi.bmiHeader.biSize=sizeof(BITMAPINFOHEADER);bi.bmiHeader.biWidth=w;bi.bmiHeader.biHeight=-h;bi.bmiHeader.biPlanes=1;bi.bmiHeader.biBitCount=32;bi.bmiHeader.biCompression=BI_RGB;
 HDC dc=CreateCompatibleDC(nullptr);HBITMAP bitmap=CreateDIBSection(dc,&bi,DIB_RGB_COLORS,pixels,nullptr,0);if(!bitmap){DeleteDC(dc);return nullptr;}auto old=SelectObject(dc,bitmap);
 HFONT font=CreateFontW(-12,0,0,0,FW_NORMAL,FALSE,FALSE,FALSE,DEFAULT_CHARSET,OUT_DEFAULT_PRECIS,CLIP_DEFAULT_PRECIS,NONANTIALIASED_QUALITY,DEFAULT_PITCH,L"宋体");auto oldFont=SelectObject(dc,font);SetBkMode(dc,TRANSPARENT);
 if(panel){Fill(dc,0,0,w,h,RGB(99,79,48));Fill(dc,1,1,w-2,h-2,RGB(19,22,27));Fill(dc,3,3,w-6,h-6,RGB(25,29,34));Line(dc,3,3,w-3,3,RGB(142,119,77));Line(dc,3,3,3,h-3,RGB(142,119,77));Line(dc,w-3,4,w-3,h-3,RGB(6,9,13));}
 if(adventure){
  Fill(dc,0,0,w,h,RGB(111,89,52));
  Fill(dc,1,1,w-2,h-2,RGB(14,16,22));
  Text(dc,5,0,L"阿拉德探险记",RGB(248,230,178),w-10);
 }else if(!panel){
  Fill(dc,0,0,w,h,RGB(1,0,1));
  Text(dc,11,37,L"游戏辅助",RGB(0,0,0),56);Text(dc,10,36,L"游戏辅助",buttonHover?RGB(255,242,179):RGB(240,219,131),56);
 }
 else {
  Fill(dc,5,5,w-10,29,RGB(34,33,31));Text(dc,16,9,L"游戏辅助",RGB(240,214,163));Text(dc,w-24,8,L"×",RGB(184,154,98),20);Line(dc,12,35,w-12,35,RGB(91,76,49));
  auto row=[&](int y,const wchar_t* label,bool value){Check(dc,17,y+4,value);Text(dc,37,y,label,RGB(222,217,203));};
  row(47,L"自动拾取",config.pickup);Text(dc,162,47,L"仅单人副本",RGB(151,153,147),180);
  row(81,L"瞬图",config.door);Text(dc,162,81,L"Alt + 方向键",RGB(183,162,114),180);Text(dc,37,102,L"组队需存活队员选择同一方向",RGB(137,143,145));
  row(133,L"连发",config.fire);wchar_t key[8]={wchar_t(config.key),0};Button(dc,162,131,50,key);wchar_t interval[32];swprintf_s(interval,L"%d ms",config.interval);Button(dc,224,131,111,interval);
  Line(dc,16,165,w-16,165,RGB(67,65,56));row(179,L"一键出售  F9",config.sell);Button(dc,249,177,86,L"立即出售");
  row(246,L"一键分解  F10",config.disjoint);Button(dc,249,244,86,L"立即分解");
  const wchar_t* labels[]={L"普通",L"高级",L"稀有",L"神器",L"史诗",L"勇者",L"传说"};COLORREF colors[]={RGB(209,209,207),RGB(98,166,239),RGB(171,125,219),RGB(228,136,204),RGB(232,185,64),RGB(188,139,82),RGB(238,136,57)};
  for(int i=0;i<7;++i){int x=17+i*47;Check(dc,x,211,(config.sellMask&(1u<<i))!=0);Text(dc,x+14,207,labels[i],colors[i],32);Check(dc,x,278,(config.disjointMask&(1u<<i))!=0);Text(dc,x+14,274,labels[i],colors[i],32);}
  Line(dc,16,302,w-16,302,RGB(67,65,56));Text(dc,17,310,L"锁定、强化增幅及已穿戴装备受保护",RGB(140,149,151));Text(dc,17,330,L"出售、分解仅在城镇可用",RGB(140,149,151));
  Fill(dc,12,355,w-24,19,RGB(15,19,23));Text(dc,20,354,message.c_str(),connected?RGB(155,180,141):RGB(194,162,112),w-40);
 }
 SelectObject(dc,oldFont);DeleteObject(font);SelectObject(dc,old);DeleteDC(dc);
 auto data=static_cast<uint32_t*>(*pixels);for(int i=0;i<w*h;++i){if(!panel&&(data[i]&0xffffff)==0x010001)data[i]=0;else data[i]|=0xff000000;}
 // Use the game's own shaded tools sprite, preserving its original alpha.
 if(!panel&&!adventure){constexpr int iw=32,ih=32;for(int y=0;y<ih;++y)for(int x=0;x<iw;++x){uint32_t pixel=NativeAssistArt::Pixels[(y*NativeAssistArt::Height/ih)*NativeAssistArt::Width+x*NativeAssistArt::Width/iw];if(pixel>>24)data[(y+3)*w+x+18]=pixel;}}
 return bitmap;
}
static bool Click(int x,int y){
 bool menu=NativeMenuVisible();
 uint32_t action=0;bool save=false;
 {Lock lock;
  if(!menu&&adventureShown&&Inside(adventureButtonRect,x,y))return true;
  if(!menu)return false;
  if(Inside(buttonRect,x,y)){open=!open;dirty=true;action=1;}
  else if(!open||!Inside(panelRect,x,y))return false;
  else {
   x-=panelRect.left;y-=panelRect.top;
   if(x>PW-36&&y<35){open=false;dirty=true;return true;}
   if(x>=249&&x<335&&y>=177&&y<201){if(config.sell)action=2;else message=L"请先启用一键出售";}
   else if(x>=249&&x<335&&y>=244&&y<268){if(config.disjoint)action=3;else message=L"请先启用一键分解";}
   else if(y>=207&&y<231){int i=(x-17)/47;if(x>=17&&i<7)config.sellMask^=1u<<i;}
   else if(y>=274&&y<298){int i=(x-17)/47;if(x>=17&&i<7)config.disjointMask^=1u<<i;}
   else if(y>=131&&y<156&&x>=162&&x<212)config.key=config.key=='Z'?'A':config.key+1;
   else if(y>=131&&y<156&&x>=224&&x<335){auto p=std::find(std::begin(intervals),std::end(intervals),config.interval);config.interval=*(p+1==std::end(intervals)?std::begin(intervals):p+1);}
   else if(x<235){if(y>=45&&y<69)config.pickup=!config.pickup;else if(y>=79&&y<103)config.door=!config.door;else if(y>=131&&y<156)config.fire=!config.fire;else if(y>=177&&y<201)config.sell=!config.sell;else if(y>=244&&y<268)config.disjoint=!config.disjoint;}
   dirty=true;save=true;if(!action)action=1;
  }
 }
 if(save)Save();if(action)Queue(action);return true;
}
static LRESULT CALLBACK WindowProc(HWND hwnd,UINT msg,WPARAM wp,LPARAM lp){
 if(msg==MaintenanceMessage){MaintainDevice();return 0;}
 if(msg==WM_MOUSELEAVE){
  Lock l;
  if(buttonHover){buttonHover=false;dirty=true;}
  adventureHover=false;
 }
 if(msg==WM_LBUTTONDOWN||msg==WM_LBUTTONUP||msg==WM_MOUSEMOVE||msg==WM_MOUSEWHEEL){
  POINT p={short(LOWORD(lp)),short(HIWORD(lp))};
  if(msg==WM_MOUSEWHEEL)ScreenToClient(hwnd,&p);
  RECT c={};GetClientRect(hwnd,&c);
  bool menu=NativeMenuVisible(),hit=false,track=false;
  {
   Lock l;
   if(c.right&&c.bottom){p.x=MulDiv(p.x,screenW,c.right);p.y=MulDiv(p.y,screenH,c.bottom);}
   bool hover=menu&&Inside(buttonRect,p.x,p.y);
   bool compass=!menu&&adventureShown&&Inside(adventureButtonRect,p.x,p.y);
   hit=hover||compass||(menu&&open&&Inside(panelRect,p.x,p.y));
   if(msg==WM_MOUSEMOVE){
    if(buttonHover!=hover){buttonHover=hover;dirty=true;}
    adventureHover=compass;
    track=hover||compass;
   }
  }
  if(track){TRACKMOUSEEVENT tracking={sizeof(tracking),TME_LEAVE,hwnd,0};TrackMouseEvent(&tracking);}
  if(msg==WM_LBUTTONDOWN&&hit&&Click(p.x,p.y)){mouseCaptured=true;return 0;}
  if(msg==WM_LBUTTONUP&&mouseCaptured){mouseCaptured=false;return 0;}
  if(hit)return 0;
 }
 if(msg==WM_KEYDOWN&&wp==VK_ESCAPE){Lock l;if(open){open=false;dirty=true;return 0;}}
 return CallWindowProcW(previousProc,hwnd,msg,wp,lp);
}
static bool Editing(){
#ifdef ASSIST_TEST
 return false;
#else
 DWORD focus=0;SIZE_T n=0;uintptr_t base=reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));return !ReadProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(base+0x0412bc28-0x00400000),&focus,4,&n)||n!=4||focus!=0;
#endif
}
static bool Active(){
#ifdef ASSIST_TEST
 return testActive&&!testEditing;
#else
 return window&&GetForegroundWindow()==window&&!IsIconic(window)&&!Editing();
#endif
}
static bool Modifier(){
#ifdef ASSIST_TEST
 return testModifier;
#else
 return (GetAsyncKeyState(VK_SHIFT)|GetAsyncKeyState(VK_CONTROL)|GetAsyncKeyState(VK_MENU)|GetAsyncKeyState(VK_LWIN)|GetAsyncKeyState(VK_RWIN))&0x8000;
#endif
}
static void Send(WORD scan,bool up){
#ifdef ASSIST_TEST
 testInput.emplace_back(scan,up);
#else
 INPUT input={};input.type=INPUT_KEYBOARD;input.ki.wScan=scan;input.ki.dwFlags=KEYEVENTF_SCANCODE|(up?KEYEVENTF_KEYUP:0);input.ki.dwExtraInfo=Magic;SendInput(1,&input,sizeof(input));
#endif
}
static void StopFire(){if(fireScan&&fireDown)Send(fireScan,true);fireScan=0;fireDown=false;}
static void Pulse(){Settings s=Snapshot();bool opened;{Lock l(false);opened=open;}if(!fireScan)return;if(!s.fire||opened||!Active()||Modifier()||!Held[s.key]){StopFire();return;}if(GetTickCount64()>=fireDue){fireDown=!fireDown;Send(fireScan,!fireDown);fireDue=GetTickCount64()+s.interval/2;}}
static LRESULT CALLBACK Keyboard(int code,WPARAM kind,LPARAM value){
 if(code<0)return CallNextHookEx(nullptr,code,kind,value);auto k=reinterpret_cast<KBDLLHOOKSTRUCT*>(value);UINT key=k->vkCode;if(key>=256||(k->flags&LLKHF_INJECTED))return CallNextHookEx(nullptr,code,kind,value);
 bool down=kind==WM_KEYDOWN||kind==WM_SYSKEYDOWN;if(!down){Held[key]=false;if(key==Snapshot().key){StopFire();firePaused=false;}if(Captured[key]){Captured[key]=false;return 1;}}
 else {bool first=!Held[key];Held[key]=true;if(Captured[key])return 1;Settings s=Snapshot();bool opened;{Lock l(false);opened=open;}
  bool allowed=Active()&&!opened;
  if(allowed&&s.fire&&fireScan&&key!=UINT(s.key)&&!(key>=VK_LEFT&&key<=VK_DOWN)){StopFire();firePaused=true;}
  if(allowed&&s.fire&&key==UINT(s.key)&&firePaused&&!first)return 1;
  if(first&&allowed){uint32_t action=0;bool alt=(GetAsyncKeyState(VK_MENU)&0x8000)!=0,other=(GetAsyncKeyState(VK_SHIFT)|GetAsyncKeyState(VK_CONTROL)|GetAsyncKeyState(VK_LWIN)|GetAsyncKeyState(VK_RWIN))&0x8000;
#ifdef ASSIST_TEST
   alt=testAlt;other=testOther;
#endif
   if(s.door&&alt&&!other&&key>=VK_LEFT&&key<=VK_DOWN)action=4+(key-VK_LEFT);
   else if(!Modifier()){if(key==VK_F9&&s.sell)action=2;else if(key==VK_F10&&s.disjoint)action=3;else if(key==UINT(s.key)&&s.fire&&k->scanCode){firePaused=false;fireScan=WORD(k->scanCode);fireDown=true;fireDue=GetTickCount64()+s.interval/2;}}
   if(action){Captured[key]=true;Queue(action);return 1;}
  }
 }
 return CallNextHookEx(nullptr,code,kind,value);
}
static DWORD WINAPI InputLoop(void*){HHOOK hook=SetWindowsHookExW(WH_KEYBOARD_LL,Keyboard,module,0);if(!hook){Log("STOP input hook unavailable");return 1;}SetTimer(nullptr,1,20,nullptr);MSG m;while(!stop&&GetMessageW(&m,nullptr,0,0)>0){if(m.message==WM_TIMER)Pulse();TranslateMessage(&m);DispatchMessageW(&m);}StopFire();UnhookWindowsHookEx(hook);return 0;}
static DWORD WINAPI TransportLoop(void*){
 ULONGLONG nextAssist=0;
 while(!stop){
  WaitForSingleObject(wake,3000);
  if(stop)break;
  Settings s=Snapshot();
  AdventureRequest battlePass{};
  uint32_t action=0;
  const auto now=GetTickCount64();
  {
   Lock lock;
   action=pending;pending=0;
   if(!action&&now>=nextAssist&&(s.pickup||s.door||s.sell||s.disjoint))action=1;
   if(action&&adventureRequest.action)SetEvent(wake);
   if(!action&&adventureRequest.action){
    battlePass=adventureRequest;adventureRequest={};
    adventureInFlight=battlePass.action;action=battlePass.action;
   }
  }
  if(!action)continue;
  if(!battlePass.action)nextAssist=now+3000;
  if(pipe.empty()&&!battlePass.action){Status(L"等待配套登录器启动连接",false);continue;}
  BYTE request[48]={};
  memcpy(request,&Magic,4);memcpy(request+4,cookie,32);memcpy(request+36,&action,4);
  uint32_t pickup=battlePass.action?battlePass.selection:(s.pickup?1:0);
  uint32_t mask=battlePass.action?battlePass.token:(action==2?s.sellMask:action==3?s.disjointMask:0);
  memcpy(request+40,&pickup,4);memcpy(request+44,&mask,4);
  BYTE response[AdventureReplyCapacity]={};DWORD n=0;BOOL completed=FALSE;
  ULONGLONG deadline=now+5000;
  while(!pipe.empty()&&!stop){
   completed=CallNamedPipeW(pipe.c_str(),request,48,response,sizeof(response),&n,10000);
   if(completed)break;
   DWORD error=GetLastError();
   if((error!=ERROR_FILE_NOT_FOUND&&error!=ERROR_PIPE_BUSY)||GetTickCount64()>=deadline)break;
   Sleep(20);
  }
  // Retry only a connection that never existed/became available. Never replay
  // a request after a broken/uncertain transaction response.
  if(battlePass.action){
   uint32_t magic=0,result=0,length=0;
   AdventureState snapshot;
   bool received=false;
   if(completed&&n>=12){
    memcpy(&magic,response,4);memcpy(&result,response+4,4);memcpy(&length,response+8,4);
    received=magic==Magic&&!result&&length==n-12&&DecodeAdventure(response+12,length,snapshot);
   }
   Lock lock;
   if(received){
    adventureResult=std::move(snapshot);adventureResultReady=true;adventureResultEpoch=battlePass.windowEpoch;
   }else if(battlePass.action!=9){
    Log("ADVENTURE action requires status refresh");
   }
   adventureInFlight=0;
   continue;
  }
  if(!completed||n<12){Status(L"辅助连接中，请稍候",false);continue;}
  uint32_t magic=0,result=0,length=0;memcpy(&magic,response,4);memcpy(&result,response+4,4);memcpy(&length,response+8,4);if(magic!=Magic||length>n-12||length>900){Status(L"辅助连接返回异常",false);continue;}
  int count=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,reinterpret_cast<char*>(response+12),length,nullptr,0);std::wstring text(count,L'\0');if(count)MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,reinterpret_cast<char*>(response+12),length,&text[0],count);Status(text,result==0);
 }
 return 0;
}
struct Vertex{float x,y,z,rhw;DWORD color;float u,v;};
static bool UploadTexture(IDirect3DDevice9* d,const void* pixels,int w,int h,IDirect3DTexture9** out){
 if(FAILED(d->CreateTexture(w,h,1,0,D3DFMT_A8R8G8B8,D3DPOOL_MANAGED,out,nullptr)))return false;
 D3DLOCKED_RECT locked;
 if(FAILED((*out)->LockRect(0,&locked,nullptr,0))){(*out)->Release();*out=nullptr;return false;}
 for(int y=0;y<h;++y)memcpy(static_cast<BYTE*>(locked.pBits)+y*locked.Pitch,static_cast<const BYTE*>(pixels)+y*w*4,w*4);
 (*out)->UnlockRect(0);
 return true;
}
static bool Texture(IDirect3DDevice9* d,bool panel,IDirect3DTexture9** out,bool adventure=false){
 void* pixels=nullptr;
 HBITMAP bitmap=Paint(panel,&pixels,adventure);
 if(!bitmap)return false;
 const auto size=ArtSize(panel,adventure);
 bool uploaded=UploadTexture(d,pixels,size.cx,size.cy,out);
 DeleteObject(bitmap);
 return uploaded;
}
static void Quad(IDirect3DDevice9* d,IDirect3DTexture9* texture,RECT r){if(!texture)return;float x=float(r.left)-.5f,y=float(r.top)-.5f,xx=float(r.right)-.5f,yy=float(r.bottom)-.5f;Vertex v[]={{x,y,0,1,0xffffffff,0,0},{xx,y,0,1,0xffffffff,1,0},{x,yy,0,1,0xffffffff,0,1},{xx,yy,0,1,0xffffffff,1,1}};d->SetTexture(0,texture);d->DrawPrimitiveUP(D3DPT_TRIANGLESTRIP,2,v,sizeof(Vertex));}
static void ReleaseMenuTextures(){
 for(auto slot:{&panelTexture,&buttonTexture})
  if(*slot){(*slot)->Release();*slot=nullptr;}
}
static void ReleaseTextures(){
 ReleaseMenuTextures();
 for(auto slot:{&adventureIcons[0],&adventureIcons[1],&adventureTooltipTexture})
  if(*slot){(*slot)->Release();*slot=nullptr;}
 adventureShown=false;
}
static void UpdateVisibility(bool menu,bool hud){
 if(!menu&&(open||buttonHover)){open=false;buttonHover=false;dirty=true;}
 if(!hud){adventureShown=false;adventureHover=false;}
}
static void Render(IDirect3DDevice9* d,bool menu,const HudSlot* slot=nullptr){
 if(!window||d!=device)return;
 D3DVIEWPORT9 vp;
 if(FAILED(d->GetViewport(&vp))||vp.Width<640||vp.Height<440)return;
 Lock l;
 Layout(vp.Width,vp.Height);
 UpdateVisibility(menu,slot!=nullptr);
 if(menu){
  if(dirty){ReleaseMenuTextures();dirty=false;}
  if(!buttonTexture)Texture(d,false,&buttonTexture);
  if(open&&!panelTexture)Texture(d,true,&panelTexture);
 }
 if(slot){
  LayoutAdventure(*slot,vp.Width,vp.Height);
  POINT mouse={};
  RECT client={};
  adventureHover=GetCursorPos(&mouse)&&ScreenToClient(window,&mouse)&&GetClientRect(window,&client)&&
      client.right&&client.bottom&&Inside(adventureButtonRect,MulDiv(mouse.x,vp.Width,client.right),MulDiv(mouse.y,vp.Height,client.bottom));
  if(!adventureIcons[0])UploadTexture(d,AdventureIcon::Normal,AdventureIcon::Width,AdventureIcon::Height,&adventureIcons[0]);
  if(adventureHover&&!adventureIcons[1])UploadTexture(d,AdventureIcon::Hover,AdventureIcon::Width,AdventureIcon::Height,&adventureIcons[1]);
  if(adventureHover&&!adventureTooltipTexture)Texture(d,false,&adventureTooltipTexture,true);
 }
 IDirect3DStateBlock9* state=nullptr;if(FAILED(d->CreateStateBlock(D3DSBT_ALL,&state)))return;if(FAILED(state->Capture())){state->Release();return;}
 d->SetVertexShader(nullptr);d->SetPixelShader(nullptr);d->SetFVF(D3DFVF_XYZRHW|D3DFVF_DIFFUSE|D3DFVF_TEX1);d->SetRenderState(D3DRS_ZENABLE,FALSE);d->SetRenderState(D3DRS_ZWRITEENABLE,FALSE);d->SetRenderState(D3DRS_CULLMODE,D3DCULL_NONE);d->SetRenderState(D3DRS_LIGHTING,FALSE);d->SetRenderState(D3DRS_ALPHATESTENABLE,FALSE);d->SetRenderState(D3DRS_ALPHABLENDENABLE,TRUE);d->SetRenderState(D3DRS_SRCBLEND,D3DBLEND_SRCALPHA);d->SetRenderState(D3DRS_DESTBLEND,D3DBLEND_INVSRCALPHA);d->SetRenderState(D3DRS_SCISSORTESTENABLE,FALSE);
 d->SetRenderState(D3DRS_COLORWRITEENABLE,15);d->SetRenderState(D3DRS_FOGENABLE,FALSE);d->SetRenderState(D3DRS_STENCILENABLE,FALSE);d->SetRenderState(D3DRS_SEPARATEALPHABLENDENABLE,FALSE);d->SetTextureStageState(0,D3DTSS_TEXTURETRANSFORMFLAGS,D3DTTFF_DISABLE);d->SetTextureStageState(0,D3DTSS_TEXCOORDINDEX,0);d->SetTextureStageState(0,D3DTSS_COLOROP,D3DTOP_MODULATE);d->SetTextureStageState(0,D3DTSS_COLORARG1,D3DTA_TEXTURE);d->SetTextureStageState(0,D3DTSS_COLORARG2,D3DTA_DIFFUSE);d->SetTextureStageState(0,D3DTSS_ALPHAOP,D3DTOP_MODULATE);d->SetTextureStageState(0,D3DTSS_ALPHAARG1,D3DTA_TEXTURE);d->SetTextureStageState(0,D3DTSS_ALPHAARG2,D3DTA_DIFFUSE);d->SetTextureStageState(1,D3DTSS_COLOROP,D3DTOP_DISABLE);d->SetSamplerState(0,D3DSAMP_MINFILTER,D3DTEXF_LINEAR);d->SetSamplerState(0,D3DSAMP_MAGFILTER,D3DTEXF_LINEAR);
 if(menu)Quad(d,buttonTexture,buttonRect);
 if(open)Quad(d,panelTexture,panelRect);
 if(slot&&adventureIcons[0]){
  Quad(d,adventureHover&&adventureIcons[1]?adventureIcons[1]:adventureIcons[0],adventureButtonRect);
  if(!adventureShown)Log("READY adventure bottom entry");
  adventureShown=true;
  if(adventureHover)Quad(d,adventureTooltipTexture,adventureTooltipRect);
 }
 state->Apply();state->Release();
}
using Create9=IDirect3D9*(WINAPI*)(UINT);using CreateDevice=HRESULT(WINAPI*)(IDirect3D9*,UINT,D3DDEVTYPE,HWND,DWORD,D3DPRESENT_PARAMETERS*,IDirect3DDevice9**);using EndScene=HRESULT(WINAPI*)(IDirect3DDevice9*);using Reset=HRESULT(WINAPI*)(IDirect3DDevice9*,D3DPRESENT_PARAMETERS*);
using Present=HRESULT(WINAPI*)(IDirect3DDevice9*,const RECT*,const RECT*,HWND,const RGNDATA*);
static Create9 realCreate9;static CreateDevice realCreateDevice;static Reset realReset;
struct DeviceChain{IDirect3DDevice9* owner;void** table;Reset reset;Present present;};
static DeviceChain* currentChain;
static HRESULT WINAPI PresentDevice(IDirect3DDevice9*,const RECT*,const RECT*,HWND,const RGNDATA*);
static HRESULT WINAPI ResetDevice(IDirect3DDevice9*,D3DPRESENT_PARAMETERS*);
static bool InstallDeviceHooks(IDirect3DDevice9* d);
static bool Replace(void** slot,void* expected,void* value){DWORD old=0;if(!VirtualProtect(slot,sizeof(void*),PAGE_READWRITE,&old))return false;void* before=InterlockedCompareExchangePointer(slot,value,expected);DWORD ignored;bool restored=VirtualProtect(slot,sizeof(void*),old,&ignored)!=0;return before==expected&&restored;}
static IDirect3DDevice9* CurrentGameDevice(){
#if defined(ASSIST_TEST)
 return device;
#elif defined(ASSIST_HARNESS)
 using Getter=IDirect3DDevice9*(__cdecl*)();auto get=reinterpret_cast<Getter>(GetProcAddress(GetModuleHandleW(nullptr),"AssistHarnessDevice"));return get?get():nullptr;
#else
 IDirect3DDevice9* value=nullptr;uintptr_t base=reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));Read(base+0x04300b0c-0x400000,value);return value;
#endif
}
static void RenderOnPresent(IDirect3DDevice9* d){
 bool menu=NativeMenuVisible();
 HudSlot slot={};
 bool hud=!menu&&NextTownHudSlot(slot,screenW,screenH);
 if(!menu&&!hud){Lock l;UpdateVisibility(false,false);return;}
 IDirect3DSurface9 *back=nullptr,*target=nullptr,*depth=nullptr;D3DVIEWPORT9 saved={};
 if(FAILED(d->GetBackBuffer(0,0,D3DBACKBUFFER_TYPE_MONO,&back))||FAILED(d->GetRenderTarget(0,&target))||FAILED(d->GetViewport(&saved))){if(back)back->Release();if(target)target->Release();return;}
 d->GetDepthStencilSurface(&depth);D3DSURFACE_DESC desc={};back->GetDesc(&desc);
 d->SetDepthStencilSurface(nullptr);HRESULT selected=d->SetRenderTarget(0,back);D3DVIEWPORT9 full={0,0,desc.Width,desc.Height,0,1};d->SetViewport(&full);
 if(SUCCEEDED(selected)&&SUCCEEDED(d->BeginScene())){
  #ifdef ASSIST_HARNESS
  slot.width=int(desc.Width);
  slot.height=int(desc.Height);
  #endif
  Render(d,menu,hud?&slot:nullptr);
  d->EndScene();
 }
 d->SetRenderTarget(0,target);d->SetDepthStencilSurface(depth);d->SetViewport(&saved);
 if(depth)depth->Release();target->Release();back->Release();
}
static HRESULT WINAPI PresentDevice(IDirect3DDevice9* d,const RECT* source,const RECT* dest,HWND overrideWindow,const RGNDATA* dirtyRegion){
 auto chain=currentChain;if(!chain)return D3DERR_INVALIDCALL;
 // DFO reuses this device to present its independent chat/info windows.
 // Drawing on those presentations duplicates the panel into their contents.
 if(chain->owner==d&&(!overrideWindow||overrideWindow==window))RenderOnPresent(d);return chain->present(d,source,dest,overrideWindow,dirtyRegion);
}
static HRESULT WINAPI ResetDevice(IDirect3DDevice9* d,D3DPRESENT_PARAMETERS* p){
 auto chain=currentChain;Reset next=chain&&chain->owner==d?chain->reset:realReset;if(!next)return D3DERR_INVALIDCALL;
 {Lock l;ReleaseTextures();dirty=true;}HRESULT result=next(d,p);if(chain&&SUCCEEDED(result))InstallDeviceHooks(d);return result;
}
static bool InstallDeviceHooks(IDirect3DDevice9* d){
 void** original=nullptr;if(!d||!Read(reinterpret_cast<uintptr_t>(d),original)||!original)return false;
 void *observedReset=nullptr,*observedPresent=nullptr;
 if(!Read(reinterpret_cast<uintptr_t>(original+16),observedReset)||!Read(reinterpret_cast<uintptr_t>(original+17),observedPresent))return false;
 if(currentChain&&currentChain->owner==d&&original==currentChain->table&&observedReset==reinterpret_cast<void*>(&ResetDevice)&&observedPresent==reinterpret_cast<void*>(&PresentDevice))return true;
 D3DDEVICE_CREATION_PARAMETERS params={};if(FAILED(d->GetCreationParameters(&params))||params.hFocusWindow!=window)return false;
 auto chain=new DeviceChain{};chain->table=original;chain->owner=d;chain->reset=reinterpret_cast<Reset>(observedReset);chain->present=reinterpret_cast<Present>(observedPresent);
 if(chain->reset==&ResetDevice&&currentChain)chain->reset=currentChain->reset;
 if(chain->present==&PresentDevice&&currentChain)chain->present=currentChain->present;
 if(!chain->reset||!chain->present){delete chain;return false;}
 // Windows compatibility devices retain private metadata around their method
 // table. Preserve that table and exchange only the two observed entries.
 if(!Replace(original+16,observedReset,reinterpret_cast<void*>(&ResetDevice))){delete chain;return false;}
 if(!Replace(original+17,observedPresent,reinterpret_cast<void*>(&PresentDevice))){Replace(original+16,reinterpret_cast<void*>(&ResetDevice),observedReset);delete chain;return false;}
 {Lock l;if(device!=d)ReleaseTextures();device=d;dirty=true;}currentChain=chain;Log("READY compatibility-preserving Present/Reset hooks installed");return true;
}
static void MaintainDevice(){if(!window||GetCurrentThreadId()!=GetWindowThreadProcessId(window,nullptr))return;auto d=CurrentGameDevice();if(d)InstallDeviceHooks(d);}
static void CALLBACK DeviceTimer(HWND,UINT,UINT_PTR,DWORD){MaintainDevice();AdventureUI::Tick();}
static volatile LONG attachOnce=0;
static void Attach(IDirect3DDevice9* d,HWND hwnd){
 DWORD pid=0;DWORD tid=GetWindowThreadProcessId(hwnd,&pid);RECT r={};GetClientRect(hwnd,&r);if(pid!=GetCurrentProcessId()||r.right<640||r.bottom<440)return;
 if(InterlockedCompareExchange(&attachOnce,1,0)==0){
  previousProc=reinterpret_cast<WNDPROC>(GetWindowLongPtrW(hwnd,GWLP_WNDPROC));if(!previousProc){InterlockedExchange(&attachOnce,0);return;}
  SetLastError(0);auto original=reinterpret_cast<WNDPROC>(SetWindowLongPtrW(hwnd,GWLP_WNDPROC,reinterpret_cast<LONG_PTR>(WindowProc)));if(!original){InterlockedExchange(&attachOnce,0);Log("STOP window owner unavailable");return;}
  previousProc=original;window=hwnd;Queue(1);HANDLE input=CreateThread(nullptr,0,InputLoop,nullptr,0,nullptr);if(input)CloseHandle(input);
  SetTimer(hwnd,reinterpret_cast<UINT_PTR>(module)^Magic,200,DeviceTimer);Log("READY native Escape menu input and device recovery timer");
 }
 if(window!=hwnd)return;if(tid==GetCurrentThreadId())InstallDeviceHooks(d);else PostMessageW(hwnd,MaintenanceMessage,0,0);
}
static HRESULT WINAPI Created(IDirect3D9* api,UINT adapter,D3DDEVTYPE type,HWND hwnd,DWORD flags,D3DPRESENT_PARAMETERS* params,IDirect3DDevice9** output){
 HRESULT result=realCreateDevice(api,adapter,type,hwnd,flags,params,output);if(SUCCEEDED(result)&&output&&*output)Attach(*output,hwnd);return result;
}
static IDirect3D9* WINAPI Direct9(UINT version){IDirect3D9* api=realCreate9(version);if(api){auto v=*reinterpret_cast<void***>(api);if(v[16]!=reinterpret_cast<void*>(&Created)){realCreateDevice=reinterpret_cast<CreateDevice>(v[16]);if(!Replace(v+16,reinterpret_cast<void*>(realCreateDevice),reinterpret_cast<void*>(&Created)))Log("STOP D3D9 device hook ownership changed");}}return api;}
static bool Identity(){
 wchar_t exe[32768];DWORD size=GetModuleFileNameW(nullptr,exe,32768);if(!size||size>=32768)return false;HANDLE file=CreateFileW(exe,GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr);if(file==INVALID_HANDLE_VALUE)return false;
 BCRYPT_ALG_HANDLE alg=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;BYTE digest[32]={},buffer[65536];bool ok=false;
 if(BCryptOpenAlgorithmProvider(&alg,BCRYPT_SHA256_ALGORITHM,nullptr,0)>=0&&BCryptCreateHash(alg,&hash,nullptr,0,nullptr,0,0)>=0){DWORD n=0;ok=true;while(ReadFile(file,buffer,sizeof(buffer),&n,nullptr)&&n)if(BCryptHashData(hash,buffer,n,0)<0){ok=false;break;}if(BCryptFinishHash(hash,digest,32,0)<0)ok=false;}
 if(hash)BCryptDestroyHash(hash);if(alg)BCryptCloseAlgorithmProvider(alg,0);CloseHandle(file);char hex[65];for(int i=0;i<32;++i)sprintf_s(hex+i*2,65-i*2,"%02x",digest[i]);return ok&&(!strcmp(hex,"14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634")||!strcmp(hex,"89b786f9cd8932c90edd9bb548352fa6ea73369c3cf25ae28fb3ed69009fe236")
 ||!strcmp(hex,"0d2da067512bc812d2430afeadb2c13a4d53bd0e7f76801fab228bef52a1ee8e")
 ||!strcmp(hex,"5385a7c47d49517d5834c1c0000d28e363165e411f5ef2079e9e627595883097")
#ifdef ASSIST_HARNESS
 ||!strcmp(hex,ASSIST_HARNESS_HOST_SHA)
#endif
 );
}
static DWORD WINAPI Install(void*){
 wchar_t path[32768];DWORD n=GetModuleFileNameW(module,path,32768);if(!n||n>=32768)return 1;wchar_t* slash=wcsrchr(path,L'\\');if(!slash)return 1;*slash=0;
 std::wstring dataDirectory(path);
 DWORD rootLength=GetEnvironmentVariableW(L"USDOF_CLIENT_DIR",path,32768);
 if(rootLength&&rootLength<32768)dataDirectory=std::wstring(path)+L"\\us_extend_dll";
 ini=dataDirectory+L"\\GameAssist.ini";std::wstring log=dataDirectory+L"\\GameAssist.log";trace=_wfsopen(log.c_str(),L"a",_SH_DENYNO);
 if(!Identity()){Log("STOP frozen image mismatch");return 2;}Load();wake=CreateEventW(nullptr,FALSE,FALSE,nullptr);wchar_t name[256],secret[70];if(GetEnvironmentVariableW(L"USDOF_ASSIST_PIPE",name,256)&&GetEnvironmentVariableW(L"USDOF_ASSIST_COOKIE",secret,70)==64){pipe=L"\\\\.\\pipe\\"+std::wstring(name);for(int i=0;i<32;++i){unsigned byte=0;if(swscanf_s(secret+i*2,L"%2x",&byte)!=1){pipe.clear();break;}cookie[i]=BYTE(byte);}}SecureZeroMemory(secret,sizeof(secret));SetEnvironmentVariableW(L"USDOF_ASSIST_COOKIE",nullptr);
 if(pipe.empty()){Log("INACTIVE paired launcher bootstrap absent; old launcher retains ownership");return 7;}
 auto base=reinterpret_cast<BYTE*>(GetModuleHandleW(nullptr));auto dos=reinterpret_cast<IMAGE_DOS_HEADER*>(base);auto nt=reinterpret_cast<IMAGE_NT_HEADERS*>(base+dos->e_lfanew);auto directory=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];if(!directory.VirtualAddress)return 3;auto entry=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base+directory.VirtualAddress);
 for(;entry->Name;++entry){if(_stricmp(reinterpret_cast<char*>(base+entry->Name),"d3d9.dll"))continue;auto names=reinterpret_cast<IMAGE_THUNK_DATA*>(base+entry->OriginalFirstThunk);auto slots=reinterpret_cast<IMAGE_THUNK_DATA*>(base+entry->FirstThunk);for(;names->u1.AddressOfData;++names,++slots){if(IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))continue;auto imp=reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base+names->u1.AddressOfData);if(strcmp(reinterpret_cast<char*>(imp->Name),"Direct3DCreate9"))continue;
   realCreate9=reinterpret_cast<Create9>(slots->u1.Function);if(realCreate9!=reinterpret_cast<Create9>(GetProcAddress(GetModuleHandleW(L"d3d9.dll"),"Direct3DCreate9"))){Log("STOP D3D9 import ownership changed");return 4;}
   if(!Replace(reinterpret_cast<void**>(&slots->u1.Function),reinterpret_cast<void*>(realCreate9),reinterpret_cast<void*>(&Direct9)))return 5;
   HANDLE worker=CreateThread(nullptr,0,TransportLoop,nullptr,0,nullptr);if(worker)CloseHandle(worker);Log("INSTALLED D3D9 import; waiting for game device");
   // Frozen renderer stores its actual device at 04300B0C. Handle the case
   // where the game finished creating it while the pinned file was checked.
   for(int i=0;i<300&&!device&&!stop;++i){IDirect3DDevice9* current=CurrentGameDevice();if(current){D3DDEVICE_CREATION_PARAMETERS creation={};if(SUCCEEDED(current->GetCreationParameters(&creation)))Attach(current,creation.hFocusWindow);}Sleep(100);}return 0;
  }}Log("STOP D3D9 import unavailable");return 6;
}
}
#ifndef ASSIST_TEST
BOOL WINAPI DllMain(HINSTANCE mod,DWORD reason,LPVOID){if(reason==DLL_PROCESS_ATTACH){Assist::module=mod;DisableThreadLibraryCalls(mod);if(!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,reinterpret_cast<LPCWSTR>(mod),&Assist::pinned))return FALSE;HANDLE thread=CreateThread(nullptr,0,Assist::Install,nullptr,0,nullptr);if(thread)CloseHandle(thread);}else if(reason==DLL_PROCESS_DETACH)InterlockedExchange(&Assist::stop,1);return TRUE;}
#endif
