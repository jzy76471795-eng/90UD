#pragma once
#include <windows.h>
#include <bcrypt.h>
#include <array>
#include <vector>
#include <string>
#include <cstring>
#include <cstdint>
#pragma comment(lib,"bcrypt.lib")

namespace py90_protection {
constexpr uint32_t kOriginalBytes=87183360,kHeaderBytes=4096,kCodeRva=4096,kCodeBytes=52584448;
constexpr uint32_t kMetadataOffset=512,kSectionBytes=8192,kBase=0x400000;
constexpr char kMagic[8]={'U','S','9','0','E','N','C','2'};
#pragma pack(push,1)
struct Metadata {
 char magic[8];uint32_t version,original_bytes,image_base,entry_rva,header_bytes,code_rva,code_offset,code_bytes;
 unsigned char key_id[16],nonce[12],tag[16],original_sha256[32];
};
#pragma pack(pop)
static_assert(sizeof(Metadata)==116,"protection metadata ABI");

inline bool read_key(std::array<unsigned char,32>& key) {
 char value[66]={};DWORD count=GetEnvironmentVariableA("DNF_PYTHON_CLIENT_CODE_KEY",value,sizeof(value));
 SetEnvironmentVariableA("DNF_PYTHON_CLIENT_CODE_KEY",nullptr);
 auto digit=[](char c)->int {return c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:c>='A'&&c<='F'?c-'A'+10:-1;};
 bool ok=count==64;
 for(size_t i=0;i<32&&ok;++i){int a=digit(value[i*2]),b=digit(value[i*2+1]);if(a<0||b<0)ok=false;else key[i]=static_cast<unsigned char>(a*16+b);}
 SecureZeroMemory(value,sizeof(value));return ok;
}

struct Image {
 HANDLE file=INVALID_HANDLE_VALUE;
 std::vector<unsigned char> disk,code,original_headers;
 Metadata metadata={};
 DWORD nt_offset=0;
 ~Image(){if(file!=INVALID_HANDLE_VALUE)CloseHandle(file);if(!code.empty())SecureZeroMemory(code.data(),code.size());}
 bool load(const std::wstring& path,const std::array<unsigned char,32>& secret) {
  file=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
  if(file==INVALID_HANDLE_VALUE)return false;
  LARGE_INTEGER size={};if(!GetFileSizeEx(file,&size)||size.QuadPart!=kOriginalBytes+kSectionBytes)return false;
  disk.resize(static_cast<size_t>(size.QuadPart));DWORD received=0;
  if(!ReadFile(file,disk.data(),static_cast<DWORD>(disk.size()),&received,nullptr)||received!=disk.size())return false;
  IMAGE_DOS_HEADER dos={};memcpy(&dos,disk.data(),sizeof(dos));
  if(dos.e_magic!=IMAGE_DOS_SIGNATURE||dos.e_lfanew<64||static_cast<uint32_t>(dos.e_lfanew)>kHeaderBytes-sizeof(IMAGE_NT_HEADERS32))return false;
  nt_offset=static_cast<DWORD>(dos.e_lfanew);
  IMAGE_NT_HEADERS32 nt={};memcpy(&nt,disk.data()+nt_offset,sizeof(nt));
  if(nt.Signature!=IMAGE_NT_SIGNATURE||nt.FileHeader.Machine!=IMAGE_FILE_MACHINE_I386||nt.FileHeader.NumberOfSections!=11||nt.OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR32_MAGIC||nt.OptionalHeader.ImageBase!=kBase)return false;
  DWORD sections=nt_offset+24+nt.FileHeader.SizeOfOptionalHeader;
  if(sections+11*sizeof(IMAGE_SECTION_HEADER)>kHeaderBytes)return false;
  IMAGE_SECTION_HEADER section={};memcpy(&section,disk.data()+sections+10*sizeof(section),sizeof(section));
  if(memcmp(section.Name,".uslock",7)||section.PointerToRawData!=kOriginalBytes||section.VirtualAddress!=kOriginalBytes||section.SizeOfRawData!=kSectionBytes||nt.OptionalHeader.AddressOfEntryPoint!=section.VirtualAddress)return false;
  memcpy(&metadata,disk.data()+kOriginalBytes+kMetadataOffset,sizeof(metadata));
  if(memcmp(metadata.magic,kMagic,8)||metadata.version!=2||metadata.original_bytes!=kOriginalBytes||metadata.image_base!=kBase||metadata.header_bytes!=kHeaderBytes||metadata.code_rva!=kCodeRva||metadata.code_offset!=kCodeRva||metadata.code_bytes!=kCodeBytes)return false;
  original_headers.assign(disk.begin()+kOriginalBytes+kMetadataOffset+sizeof(metadata),disk.begin()+kOriginalBytes+kMetadataOffset+sizeof(metadata)+kHeaderBytes);
  Metadata authenticated=metadata;memset(authenticated.tag,0,sizeof(authenticated.tag));
  std::vector<unsigned char> aad;
  aad.insert(aad.end(),reinterpret_cast<unsigned char*>(&authenticated),reinterpret_cast<unsigned char*>(&authenticated)+sizeof(authenticated));
  aad.insert(aad.end(),original_headers.begin(),original_headers.end());
  aad.insert(aad.end(),disk.begin(),disk.begin()+kHeaderBytes);
  aad.insert(aad.end(),disk.begin()+kOriginalBytes,disk.begin()+kOriginalBytes+kMetadataOffset);
  aad.insert(aad.end(),disk.begin()+kCodeRva+kCodeBytes,disk.begin()+kOriginalBytes);
  BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_KEY_HANDLE key=nullptr;DWORD object_bytes=0,property_bytes=0,result=0;
  std::vector<unsigned char> object;
  bool ok=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_AES_ALGORITHM,nullptr,0)>=0;
  if(ok)ok=BCryptSetProperty(algorithm,BCRYPT_CHAINING_MODE,reinterpret_cast<PUCHAR>(const_cast<wchar_t*>(BCRYPT_CHAIN_MODE_GCM)),sizeof(BCRYPT_CHAIN_MODE_GCM),0)>=0;
  if(ok)ok=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&object_bytes),sizeof(object_bytes),&property_bytes,0)>=0;
  if(ok){object.resize(object_bytes);ok=BCryptGenerateSymmetricKey(algorithm,&key,object.data(),object_bytes,const_cast<PUCHAR>(secret.data()),static_cast<ULONG>(secret.size()),0)>=0;}
  if(ok){
   BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO info;BCRYPT_INIT_AUTH_MODE_INFO(info);
   info.pbNonce=metadata.nonce;info.cbNonce=sizeof(metadata.nonce);info.pbTag=metadata.tag;info.cbTag=sizeof(metadata.tag);info.pbAuthData=aad.data();info.cbAuthData=static_cast<ULONG>(aad.size());
   code.resize(kCodeBytes);
   ok=BCryptDecrypt(key,disk.data()+kCodeRva,kCodeBytes,&info,nullptr,0,code.data(),kCodeBytes,&result,0)>=0&&result==kCodeBytes;
  }
  if(key)BCryptDestroyKey(key);if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0);
  if(!object.empty())SecureZeroMemory(object.data(),object.size());
  if(!ok&&!code.empty())SecureZeroMemory(code.data(),code.size());
  return ok;
 }
 static bool write(HANDLE process,uintptr_t address,const void* data,SIZE_T bytes) {
  DWORD before=0,ignored=0;SIZE_T written=0;
  if(!VirtualProtectEx(process,reinterpret_cast<void*>(address),bytes,PAGE_EXECUTE_READWRITE,&before))return false;
  bool ok=WriteProcessMemory(process,reinterpret_cast<void*>(address),data,bytes,&written)&&written==bytes;
  if(!VirtualProtectEx(process,reinterpret_cast<void*>(address),bytes,before,&ignored))ok=false;
  if(!FlushInstructionCache(process,reinterpret_cast<void*>(address),bytes))ok=false;
  return ok;
 }
 bool apply(HANDLE process) {
  unsigned char check[64]={};SIZE_T read=0;
  if(!ReadProcessMemory(process,reinterpret_cast<void*>(kBase),check,sizeof(check),&read)||read!=sizeof(check)||memcmp(check,disk.data(),sizeof(check)))return false;
  if(!write(process,kBase+kCodeRva,code.data(),code.size()))return false;
  DWORD entry_offset=nt_offset+24+16,tls_offset=nt_offset+24+96+9*8;
  if(!write(process,kBase+entry_offset,original_headers.data()+entry_offset,4)||!write(process,kBase+tls_offset,original_headers.data()+tls_offset,8))return false;
  const unsigned char ready=0xa5;
  return write(process,kBase+kOriginalBytes+0x1f0,&ready,1);
 }
 bool diagnostic_sha(unsigned char digest[32]) {
  BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
  bool ok=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)>=0;
  if(ok)ok=BCryptCreateHash(algorithm,&hash,nullptr,0,nullptr,0,0)>=0;
  if(ok)ok=BCryptHashData(hash,original_headers.data(),static_cast<ULONG>(original_headers.size()),0)>=0;
  if(ok)ok=BCryptHashData(hash,code.data(),static_cast<ULONG>(code.size()),0)>=0;
  if(ok)ok=BCryptHashData(hash,disk.data()+kCodeRva+kCodeBytes,kOriginalBytes-kCodeRva-kCodeBytes,0)>=0;
  if(ok)ok=BCryptFinishHash(hash,digest,32,0)>=0;
  if(hash)BCryptDestroyHash(hash);if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0);return ok;
 }
};
}
