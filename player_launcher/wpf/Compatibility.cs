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

namespace DNFLogin.Services {
 internal static class CompatHash {
  internal static byte[] Stream(System.IO.Stream stream) { using var algorithm=System.Security.Cryptography.SHA256.Create(); return algorithm.ComputeHash(stream); }
 }
}
namespace System.Security.Cryptography.X509Certificates {
 internal static class SanHostnameCompatibility {
  // Exact SAN matching only. No wildcard or legacy CN fallback is allowed.
  internal static bool MatchesHostname(this X509Certificate2 certificate,string hostname,bool allowWildcards=false,bool allowCommonName=false) {
   if(allowWildcards||allowCommonName)throw new ArgumentException("Unsupported hostname policy");
   try {
    X509Extension? san=null;
    foreach(X509Extension extension in certificate.Extensions)if(extension.Oid?.Value=="2.5.29.17") { if(san is not null)return false; san=extension; }
    if(san is null)return false;
    bool isIp=System.Net.IPAddress.TryParse(hostname,out var ip);bool matched=false;
    string dns=isIp?"":new System.Globalization.IdnMapping().GetAscii(hostname).ToLowerInvariant();
    var der=new System.Formats.Asn1.AsnReader(san.RawData,System.Formats.Asn1.AsnEncodingRules.DER);
    var names=der.ReadSequence();der.ThrowIfNotEmpty();
    while(names.HasData) {
     var tag=names.PeekTag();
     if(tag.HasSameClassAndValue(new System.Formats.Asn1.Asn1Tag(System.Formats.Asn1.TagClass.ContextSpecific,2))) {
      string name=names.ReadCharacterString(System.Formats.Asn1.UniversalTagNumber.IA5String,tag);
      if(!isIp && name.IndexOf('*')<0 && string.Equals(name,dns,StringComparison.OrdinalIgnoreCase))matched=true;
     } else if(tag.HasSameClassAndValue(new System.Formats.Asn1.Asn1Tag(System.Formats.Asn1.TagClass.ContextSpecific,7))) {
      byte[] bytes=names.ReadOctetString(tag);
      if(bytes.Length!=4&&bytes.Length!=16)return false;
      if(isIp&&ip!.GetAddressBytes().AsSpan().SequenceEqual(bytes))matched=true;
     } else names.ReadEncodedValue();
    }
    return matched;
   } catch(Exception e) when(e is System.Formats.Asn1.AsnContentException or ArgumentException) { return false; }
  }
 }
}
