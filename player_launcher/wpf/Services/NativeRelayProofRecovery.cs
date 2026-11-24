using System.Buffers.Binary;

namespace DNFLogin.Services;

// Observes authenticated IPC only. A recovery requests a new authorization;
// it never creates or changes the native handshake evidence in a frame.
internal sealed class NativeRelayProofRecovery
{
    internal const long GraceMilliseconds=6000,CooldownMilliseconds=30000;
    internal const int MaximumAttempts=3;
    private readonly HashSet<uint> confirmedKeys=[];
    private uint actor;
    private long? missingSince;
    private long nextAttempt;
    internal int Attempts {get;private set;}

    internal bool Observe(ReadOnlySpan<byte> snapshot,ReadOnlySpan<byte> reply,long now)
    {
        if(now<0||!Header(snapshot,1)||!Header(reply,2)||snapshot.Length<64||reply.Length<64
            ||U64(snapshot,16)!=U64(reply,16)||U32(snapshot,24)!=U32(reply,24)
            ||U64(snapshot,32)!=U64(reply,32)||U32(snapshot,44)!=U32(reply,28))
            return Pause();
        uint selected=U32(snapshot,28),context=U32(snapshot,44),count=U32(reply,60);
        int peers=BinaryPrimitives.ReadUInt16LittleEndian(snapshot[58..]);
        if(selected==0||selected>=65535||context==0||U32(snapshot,40) is 0 or uint.MaxValue
            ||BinaryPrimitives.ReadUInt16LittleEndian(snapshot[56..])==0||count==0||count>3||peers>10
            ||snapshot.Length!=64+16*peers||reply.Length!=64+40*count
            ||U32(reply,56)!=1||U64(reply,48)<=(ulong)now
            ||U64(snapshot,48)!=U64(reply,40))
            return Pause();
        if(actor!=selected){actor=selected;confirmedKeys.Clear();missingSince=null;}
        var expected=new HashSet<uint>();
        for(int i=0;i<count;i++){
            int offset=64+40*i;uint key=U32(reply,offset+8);
            if(U32(reply,offset)==0||U32(reply,offset+4) is 0 or >=65535||key==0||!expected.Add(key)
                ||reply[offset+12]!=198||reply[offset+13] is not (18 or 19)
                ||BinaryPrimitives.ReadUInt16LittleEndian(reply[(offset+16)..])==0
                ||BinaryPrimitives.ReadUInt16LittleEndian(reply[(offset+18)..])==0
                ||U64(reply,offset+20)==0||U64(reply,offset+28)==0||U32(reply,offset+36)!=0)return Pause();
        }
        var actual=new Dictionary<uint,bool>();
        for(int i=0;i<peers;i++){
            int offset=64+16*i;uint key=U32(snapshot,offset+4),state=U32(snapshot,offset+8),proof=U32(snapshot,offset+12);
            if(key==0||state>3||proof>1||proof==1&&state!=0||!actual.TryAdd(key,proof==1))return Pause();
        }
        // A departure or a different team cannot inherit another peer's timer.
        confirmedKeys.IntersectWith(expected);
        bool missing=false;
        foreach(uint key in expected){
            bool confirmed=actual.GetValueOrDefault(key);
            if(confirmed)confirmedKeys.Add(key);
            else if(confirmedKeys.Contains(key))missing=true;
        }
        if(!missing)return Pause();
        missingSince??=now;
        if(now-missingSince.Value<GraceMilliseconds||now<nextAttempt||Attempts>=MaximumAttempts)return false;
        Attempts++;nextAttempt=now+CooldownMilliseconds;missingSince=null;
        return true;
    }

    private bool Pause(){missingSince=null;return false;}
    private static uint U32(ReadOnlySpan<byte> value,int offset)=>BinaryPrimitives.ReadUInt32LittleEndian(value[offset..]);
    private static ulong U64(ReadOnlySpan<byte> value,int offset)=>BinaryPrimitives.ReadUInt64LittleEndian(value[offset..]);
    private static bool Header(ReadOnlySpan<byte> value,ushort kind)=>value.Length>=24
        &&value[..4].SequenceEqual("RIPC"u8)&&BinaryPrimitives.ReadUInt16LittleEndian(value[4..])==1
        &&BinaryPrimitives.ReadUInt16LittleEndian(value[6..])==kind&&U32(value,8)==value.Length-24
        &&U32(value,12)==0&&U64(value,16)!=0;
}
