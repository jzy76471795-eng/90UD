// Backports with the same EOF and cancellation semantics as Stream.ReadExactly.
using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
namespace System.IO {
 internal static class ExactReadCompatibility {
  internal static void ReadExactly(this Stream stream, Span<byte> buffer) {
   while (!buffer.IsEmpty) { int n=stream.Read(buffer); if(n==0)throw new EndOfStreamException(); buffer=buffer.Slice(n); }
  }
  internal static async ValueTask ReadExactlyAsync(this Stream stream, Memory<byte> buffer, CancellationToken cancellationToken=default) {
   while (!buffer.IsEmpty) { int n=await stream.ReadAsync(buffer,cancellationToken).ConfigureAwait(false); if(n==0)throw new EndOfStreamException(); buffer=buffer.Slice(n); }
  }
 }
}
namespace System.Runtime.CompilerServices {
 [AttributeUsage(AttributeTargets.All, Inherited=false)] internal sealed class RequiredMemberAttribute:Attribute {}
 [AttributeUsage(AttributeTargets.All, AllowMultiple=true, Inherited=false)] internal sealed class CompilerFeatureRequiredAttribute:Attribute { public string FeatureName{get;} public bool IsOptional{get;init;} public CompilerFeatureRequiredAttribute(string featureName){FeatureName=featureName;} }
}
namespace System.Diagnostics.CodeAnalysis {
 [AttributeUsage(AttributeTargets.Constructor, Inherited=false)] internal sealed class SetsRequiredMembersAttribute:Attribute {}
}
