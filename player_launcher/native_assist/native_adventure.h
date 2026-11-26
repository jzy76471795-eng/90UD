// Matches the stock controls in the Chinese battle-pass event popup.
constexpr uint32_t AdventureReplyCapacity = 16384;
struct AdventureMission {
 uint32_t id=0,current=0,target=0,xp=0,flags=0;
 std::wstring name;
};
struct AdventureItem{uint32_t id=0,count=0;};
struct AdventureReward{
 uint32_t flags=0,size=0;
 AdventureItem items[4]={};
};
struct AdventureState {
 bool active=false;
 uint32_t token=0,level=0,xp=0,nextXp=0,days=0;
 std::vector<AdventureMission> missions[3];
 AdventureReward rewards[81]={};
};
struct AdventureRequest {
 uint32_t action=0,selection=0,token=0;
 uint64_t windowEpoch=0;
};
static AdventureRequest adventureRequest;
static AdventureState adventureResult;
static uint64_t adventureResultEpoch=0;
static bool adventureResultReady=false;
static uint32_t adventureInFlight=0;

class AdventureReader {
 const BYTE* at;
 DWORD left;
public:
 AdventureReader(const BYTE* data,DWORD size):at(data),left(size){}
 bool Value(uint32_t& value){
  if(left<4)return false;
  memcpy(&value,at,4);at+=4;left-=4;
  return true;
 }
 bool Name(std::wstring& value){
  uint32_t size=0;
  if(!Value(size)||!size||size>256||size>left)return false;
  auto chars=reinterpret_cast<const char*>(at);
  int length=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,chars,size,nullptr,0);
  if(!length)return false;
  value.resize(length);
  MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,chars,size,&value[0],length);
  at+=size;left-=size;
  return true;
 }
 bool Done()const{return !left;}
};
static bool DecodeAdventure(const BYTE* data,DWORD size,AdventureState& state){
 AdventureReader reader(data,size);
 uint32_t version=0,active=0,count=0;
 if(!reader.Value(version)||version!=2||!reader.Value(active)||active>1)return false;
 state.active=active==1;
 if(!state.active)return reader.Done();
 if(!reader.Value(state.token)||!state.token||!reader.Value(state.level)||!state.level||state.level>80||
    !reader.Value(state.xp)||!reader.Value(state.nextXp)||!reader.Value(state.days)||state.days>100)return false;
 for(auto& rows:state.missions){
  if(!reader.Value(count)||count>32)return false;
  for(unsigned i=0;i<count;++i){
   AdventureMission row;
   if(!reader.Value(row.id)||!row.id||row.id>65535||!reader.Value(row.current)||
      !reader.Value(row.target)||!reader.Value(row.xp)||!reader.Value(row.flags)||row.flags>15||
      !reader.Name(row.name))return false;
   rows.push_back(std::move(row));
  }
 }
 if(!reader.Value(count)||count>80)return false;
 for(unsigned i=0;i<count;++i){
  uint32_t level=0;
  if(!reader.Value(level)||!level||level>80)return false;
  auto& reward=state.rewards[level];
  if(reward.size||!reader.Value(reward.flags)||reward.flags>3||
     !reader.Value(reward.size)||!reward.size||reward.size>4)return false;
  for(unsigned j=0;j<reward.size;++j){
   auto& item=reward.items[j];
   if(!reader.Value(item.id)||!item.id||!reader.Value(item.count)||!item.count)return false;
  }
 }
 return reader.Done();
}

#if !defined(ASSIST_TEST) && !defined(ASSIST_HARNESS)
namespace AdventureUI {
constexpr uintptr_t PopupManager=0x04189ac0, PopupVtable=0x03944d54, TextVtable=0x03ce2fbc;
constexpr uintptr_t GaugeVtable=0x03ce79fc, ScrollVtable=0x03ce652c, ThumbVtable=0x03ce69e4;
constexpr uintptr_t ItemVtable=0x038b9c3c;
constexpr uintptr_t NativeEventSlot=0x03ce188c, StockEvent=0x02c9a020;
constexpr unsigned PopupType=0xe8, MissionSlots=4, RewardSlots=8, RewardPages=10;
constexpr unsigned Advanced=1,Wired=2,Completed=4,CanComplete=8;
constexpr unsigned Claimed=1,CanClaim=2;
struct Shared{void* child;void* counter;};
using EventProcedure=int(__thiscall*)(void*,Shared,unsigned,int,int);
static AdventureState state;
static void* root;
static void* pressed;
static DWORD uiThread;
static uint64_t epoch=0;
static ULONGLONG refreshDue=0;
static unsigned tab=0,missionOffset=0,rewardPage=0,drawnLevel=0,drawnPage=0;
static bool ready=false,eventsAttached=false;
static std::string lastLiveStatus;

static uintptr_t Address(uintptr_t native){
 return reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr))+native-0x400000;
}
template<class T>static T Function(uintptr_t native){return reinterpret_cast<T>(Address(native));}
static bool HasType(const void* object,uintptr_t expected){
 uintptr_t table=0;
 return Read(reinterpret_cast<uintptr_t>(object),table)&&table==Address(expected);
}
static void* Child(void* parent,const wchar_t* label){
 if(!parent)return nullptr;
 Shared shared{};
 Function<Shared*(__thiscall*)(void*,Shared*,const wchar_t*)>(0x02c84b50)(parent,&shared,label);
 void* child=shared.child;
 Function<void(__thiscall*)(Shared*)>(0x010a9b90)(&shared);
 return child;
}
static void* Indexed(const wchar_t* label,unsigned index){
 wchar_t name[64];
 swprintf_s(name,L"%ls_%u",label,index);
 return Child(root,name);
}
static void Visible(void* child,bool value){
 if(child)Function<void(__thiscall*)(void*,bool)>(0x02c9abf0)(child,value);
}
static void Visible(const wchar_t* label,bool value){Visible(Child(root,label),value);}
static void Visible(const wchar_t* label,unsigned index,bool value){Visible(Indexed(label,index),value);}
static void Enabled(const wchar_t* label,bool value){
 if(auto* child=Child(root,label))Function<void(__thiscall*)(void*,bool)>(0x019cf380)(child,value);
}
static void Text(void* control,const wchar_t* value){
 uintptr_t setter=0;
 if(HasType(control,TextVtable)&&Read(Address(TextVtable)+0xe0,setter))
  reinterpret_cast<void(__thiscall*)(void*,const wchar_t*)>(setter)(control,value);
}
static void* ActiveRoot(){
 uintptr_t manager=0;
 if(!Read(Address(PopupManager),manager)||!manager)return nullptr;
 const auto popup=Function<void*(__thiscall*)(void*,unsigned,unsigned)>(0x02788740)(
     reinterpret_cast<void*>(manager),PopupType,0);
 const auto position=reinterpret_cast<uintptr_t>(popup);
 BYTE shown=0;
 void* candidate=nullptr;
 if(!HasType(popup,PopupVtable)||!Read(position+0x100,shown)||!shown||
    !Read(position+0x30,candidate)||!candidate)return nullptr;
 return candidate;
}
static bool QueueAdventure(uint32_t action,uint32_t selection=0){
 Lock lock;
 if(!wake||(action==9&&(adventureInFlight||adventureRequest.action))||
    adventureInFlight>9||adventureRequest.action>9)return false;
 adventureRequest={action,selection,action==9?0:state.token,epoch};
 SetEvent(wake);
 return true;
}
static bool AnyClaimable(){
 for(const auto& reward:state.rewards)if(reward.flags&CanClaim)return true;
 return false;
}
static void* NativeScroll(void*& track){
 void* control=Child(root,L"BP_MISSION_SCROLL");
 if(!HasType(control,ScrollVtable)||
    !Read(reinterpret_cast<uintptr_t>(control)+0x398,track)||!HasType(track,ThumbVtable))return nullptr;
 return control;
}
static unsigned ScrollPosition(bool reset=false){
 void* track=nullptr;
 void* control=NativeScroll(track);
 if(!control)return 0;
 const auto count=state.missions[tab].size();
 unsigned maximum=count>MissionSlots?unsigned(count)-MissionSlots:0;
 if(Function<unsigned(__thiscall*)(void*)>(0x02ceafb0)(track)!=maximum)
  Function<void(__thiscall*)(void*,unsigned)>(0x02cbbe50)(control,maximum);
 if(reset)Function<void(__thiscall*)(void*,unsigned,bool)>(0x02ceb640)(track,0,false);
 return std::min(maximum,Function<unsigned(__thiscall*)(void*)>(0x02ceafa0)(track));
}
static void ApplyMissions(){
 const auto& rows=state.missions[tab];
 if(missionOffset+MissionSlots>rows.size())missionOffset=rows.size()>MissionSlots?unsigned(rows.size())-MissionSlots:0;
 for(unsigned i=0;i<3;++i){
  Visible(L"BP_TAB_SELECTED",i,tab==i);
  bool hasReady=false;
  for(const auto& row:state.missions[i])hasReady=hasReady||(row.flags&CanComplete)!=0;
  Visible(L"BP_TAB_REDPOINT",i,hasReady);
 }
 for(unsigned i=0;i<MissionSlots;++i){
  const AdventureMission* row=missionOffset+i<rows.size()?&rows[missionOffset+i]:nullptr;
  unsigned flags=row?row->flags:0;
  bool tagged=row&&tab==0;
  Visible(L"BP_MISSION_TIER",i,tagged);
  Visible(L"BP_MISSION_TITLE",i,tagged);
  Visible(L"BP_MISSION_PLAIN_TITLE",i,!tagged&&row);
  Visible(L"BP_MISSION_COUNT",i,row!=nullptr);
  Visible(L"BP_MISSION_EXP",i,row!=nullptr);
  Visible(L"BP_MISSION_CLAIM",i,false);
  Visible(L"BP_MISSION_ACTIVE",i,(flags&CanComplete)!=0);
  Visible(L"BP_MISSION_DONE",i,(flags&Completed)!=0);
  Visible(L"BP_MISSION_UNAVAILABLE",i,row&&!(flags&Completed)&&((flags&Advanced)||!(flags&Wired)));
  Visible(L"BP_MISSION_IN_PROGRESS",i,row&&(flags&Wired)&&!(flags&(Advanced|Completed|CanComplete)));
  if(!row)continue;
  wchar_t count[32],xp[32];
  swprintf_s(count,L"%u / %u",row->current,row->target);
  swprintf_s(xp,L"%u EXP",row->xp);
  Text(Indexed(tagged?L"BP_MISSION_TITLE":L"BP_MISSION_PLAIN_TITLE",i),row->name.c_str());
  Text(Indexed(L"BP_MISSION_COUNT",i),count);
  Text(Indexed(L"BP_MISSION_EXP",i),xp);
 }
}
static void ApplyRewards(){
 if(drawnPage!=rewardPage){
  Visible(L"BP_LEVEL_PAGE",drawnPage,false);
  drawnPage=rewardPage;
 }
 Visible(L"BP_LEVEL_PAGE",rewardPage,true);
 wchar_t page[24];
 swprintf_s(page,L"%u / %u",rewardPage+1,RewardPages);
 Text(Child(root,L"BP_REWARD_PAGE"),page);
 Enabled(L"BP_REWARD_PREV",rewardPage>0);
 Enabled(L"BP_REWARD_NEXT",rewardPage+1<RewardPages);
 for(unsigned i=0;i<RewardSlots;++i){
  unsigned level=rewardPage*RewardSlots+i+1;
  const auto& reward=state.rewards[level];
  unsigned flags=reward.flags;
  Visible(L"BP_NORMAL_CLAIM",i,reward.size&&!(flags&(Claimed|CanClaim)));
  Visible(L"BP_NORMAL_ACTIVE",i,(flags&CanClaim)!=0);
  Visible(L"BP_NORMAL_SUCCESS",i,(flags&Claimed)!=0);
  Visible(L"BP_NORMAL_EDGE",i,state.level==level);
  constexpr unsigned itemStarts[]={0,0,1,3,6};
  const unsigned first=itemStarts[reward.size];
  for(unsigned slot=0;slot<10;++slot){
   wchar_t id[48];
   swprintf_s(id,L"BP_NORMAL_ITEM_%u_%u",i,slot);
   void* item=Child(root,id);
   if(!HasType(item,ItemVtable))continue;
   bool shown=slot>=first&&slot<first+reward.size;
   if(shown){
    const auto& entry=reward.items[slot-first];
    // Stock ItemControl reads ItemIndex before rebinding the displayed item.
    auto* nativeId=reinterpret_cast<uint32_t*>(reinterpret_cast<uintptr_t>(item)+0x388);
    if(*nativeId!=entry.id){
     *nativeId=entry.id;
     Function<void(__thiscall*)(void*,void*)>(0x0141c6c0)(item,nullptr);
    }
    auto* quantity=reinterpret_cast<const uint32_t*>(reinterpret_cast<uintptr_t>(item)+0x4bc);
    if(*quantity!=entry.count)Function<void(__thiscall*)(void*,uint32_t)>(0x0141c100)(item,entry.count);
   }
   Visible(item,shown);
  }
 }
 const bool claimable=AnyClaimable();
 Visible(L"BP_CLAIM_ALL",!claimable);
 Visible(L"BP_CLAIM_ALL_ACTIVE",claimable);
}
static void Apply(){
 // The probe is specific to this XUI. Other popup types can use the same native class.
 if(!root||!HasType(Child(root,L"BP_EXP_VALUE"),TextVtable))return;
 if(drawnLevel!=state.level){
  Visible(L"BP_LIVE_LEVEL",drawnLevel,false);
  drawnLevel=state.level;
 }
 if(state.level!=1)Visible(L"BP_LIVE_LEVEL",1,false);
 Visible(L"BP_LIVE_LEVEL",drawnLevel,state.active);
 Visible(L"BP_LEVEL_TENS",!state.active);
 Visible(L"BP_LEVEL_ONES",!state.active);
 Visible(L"BP_SEASON_BANNER",!state.active);
 Visible(L"BP_SEASON_DIGIT",!state.active);
 Visible(L"BP_DAYS_DIGIT",!state.active);
 if(void* gauge=Child(root,L"BP_EXP_GAUGE");HasType(gauge,GaugeVtable)){
  unsigned maximum=state.nextXp?state.nextXp:1;
  unsigned value=state.nextXp?std::min(state.xp,maximum):(state.active?maximum:0);
  Function<void(__thiscall*)(void*,unsigned,unsigned)>(0x02cfee50)(gauge,value,maximum);
 }
 if(!state.active){
  Text(Child(root,L"BP_STATUS"),L"赛季尚未开放");
  Text(Child(root,L"BP_EXP_VALUE"),L"-- / --");
  Text(Child(root,L"BP_LIVE_SEASON_DETAIL"),L"");
 }else{
  wchar_t detail[64],xp[32];
  swprintf_s(detail,L"第1赛季 · 剩余 %u 天",state.days);
  if(state.nextXp)swprintf_s(xp,L"%u / %u",state.xp,state.nextXp);
  else swprintf_s(xp,L"%u",state.xp);
  Text(Child(root,L"BP_LIVE_SEASON_DETAIL"),detail);
  Text(Child(root,L"BP_EXP_VALUE"),xp);
  Text(Child(root,L"BP_STATUS"),L"");
 }
 missionOffset=ScrollPosition();
 ApplyMissions();
 ApplyRewards();
}
static void Activate(void* child){
 for(unsigned i=0;i<3;++i){
  if(child!=Indexed(L"BP_TAB",i))continue;
  tab=i;missionOffset=ScrollPosition(true);ApplyMissions();return;
 }
 if(child==Child(root,L"BP_REWARD_PREV")){
  if(rewardPage){--rewardPage;ApplyRewards();}
  return;
 }
 if(child==Child(root,L"BP_REWARD_NEXT")){
  if(rewardPage+1<RewardPages){++rewardPage;ApplyRewards();}
  return;
 }
 if(!state.active)return;
 if(child==Child(root,L"BP_CLAIM_ALL_ACTIVE")&&AnyClaimable()){
  QueueAdventure(12);return;
 }
 for(unsigned i=0;i<RewardSlots;++i){
  unsigned level=rewardPage*RewardSlots+i+1;
  if(child==Indexed(L"BP_NORMAL_ACTIVE",i)&&(state.rewards[level].flags&CanClaim)){
   QueueAdventure(11,level);return;
  }
 }
 const auto& rows=state.missions[tab];
 for(unsigned i=0;i<MissionSlots&&missionOffset+i<rows.size();++i){
  const auto& row=rows[missionOffset+i];
  if(child==Indexed(L"BP_MISSION_ACTIVE",i)&&(row.flags&CanComplete)){
   QueueAdventure(10,(tab<<16)|row.id);return;
  }
 }
}
static int __fastcall Event(void* instance,void*,Shared source,unsigned kind,int first,int second){
 const auto original=Function<EventProcedure>(StockEvent);
 bool native=ready&&source.child&&GetCurrentThreadId()==uiThread&&(kind==8||kind==10)&&ActiveRoot()==root;
 const int result=original(instance,source,kind,first,second);
 if(!native)return result;
 if(kind==8)pressed=source.child;
 if(kind==10){
  const bool clicked=pressed==source.child;
  pressed=nullptr;
  if(clicked&&ActiveRoot()==root)Activate(source.child);
 }
 return result;
}
static void AttachEvents(){
 if(eventsAttached)return;
 uintptr_t handler=0;
 const uintptr_t address=Address(NativeEventSlot);
 if(!Read(address,handler)||handler!=Address(StockEvent))return;
 auto* slot=reinterpret_cast<void* volatile*>(address);
 DWORD previous=0;
 if(!VirtualProtect(const_cast<void**>(slot),sizeof(*slot),PAGE_READWRITE,&previous))return;
 void* original=reinterpret_cast<void*>(handler);
 eventsAttached=InterlockedCompareExchangePointer(slot,reinterpret_cast<void*>(&Event),original)==original;
 DWORD discarded=0;
 VirtualProtect(const_cast<void**>(slot),sizeof(*slot),previous,&discarded);
 if(eventsAttached)Log("ADVENTURE native controls attached");
}
static void Tick(){
 auto* candidate=ActiveRoot();
 if(candidate!=root){
  root=candidate;ready=false;pressed=nullptr;state={};tab=missionOffset=rewardPage=0;
  refreshDue=0;++epoch;
  if(root)ready=HasType(Child(root,L"BP_EXP_VALUE"),TextVtable);
  if(ready){lastLiveStatus.clear();uiThread=GetCurrentThreadId();AttachEvents();}
 }
 if(!ready)return;
 bool updated=false;
 {
  Lock lock;
  if(adventureResultReady){
   if(adventureResultEpoch==epoch){state=std::move(adventureResult);updated=true;}
   adventureResultReady=false;
  }
 }
 if(updated){
  Apply();
  if(state.active){
   char line[120];
   sprintf_s(line,"ADVENTURE live level=%u xp=%u/%u missions=%zu/%zu/%zu",state.level,state.xp,state.nextXp,
             state.missions[0].size(),state.missions[1].size(),state.missions[2].size());
   if(lastLiveStatus!=line){lastLiveStatus=line;Log(line);}
  }
 }
 unsigned nativeOffset=ScrollPosition();
 if(nativeOffset!=missionOffset){missionOffset=nativeOffset;ApplyMissions();}
 auto now=GetTickCount64();
 if(now>=refreshDue&&QueueAdventure(9))refreshDue=now+3000;
}
}
#else
namespace AdventureUI{static void Tick(){}}
#endif
