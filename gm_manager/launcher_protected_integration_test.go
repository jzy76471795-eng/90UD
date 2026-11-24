package main

import (
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "newadmin/internal/launcherbundle"
 "os"
 "path/filepath"
 "testing"
)

func TestOptionalProtectedTemplateTwoConfigurations(t *testing.T) {
 base:=os.Getenv("GM_PROTECTED_TEMPLATE_BASE");if base==""{t.Skip("explicit protected template only")}
 template,e:=LoadCurrentLauncherTemplate(base);if e!=nil{t.Fatal(e)}
 f,e:=os.Open(template.Path);if e!=nil{t.Fatal(e)};info,_:=f.Stat()
 body,_,e:=launcherbundle.Read(f,info.Size(),512<<20,launcherbundle.HostMagic);f.Close();if e!=nil{t.Fatal(e)}
 want:=sha256.Sum256(body);rows:=[]map[string]any{}
 for _,name:=range []string{"保护测试甲","保护测试乙"}{
  o:=fixturePackage(t);o.TemplatePath=template.Path;o.TemplateSHA256=template.SHA256;o.DisplayName=name
  result,e:=BuildLauncherPackage(o);if e!=nil{t.Fatal(e)}
  f,e:=os.Open(result.LauncherPath);if e!=nil{t.Fatal(e)};s,_:=f.Stat()
  public,end,e:=launcherbundle.Read(f,s.Size(),launcherbundle.MaxConfig,launcherbundle.ConfigMagic);if e!=nil{t.Fatal(e)}
  var config launcherPublicConfig;if json.Unmarshal(public,&config)!=nil||config.DisplayName!=name{t.Fatal("generated settings changed")}
  b,_,e:=launcherbundle.Read(f,end,512<<20,launcherbundle.HostMagic);f.Close()
  if e!=nil||sha256.Sum256(b)!=want{t.Fatal("protected payload changed",e)}
  for _,p:=range []string{"保护证明.json","client-code-key.bin","Mapping.txt","ClientProtectionSecrets.cs"}{if _,e:=os.Stat(filepath.Join(result.Directory,p));e==nil{t.Fatal("private or GM-only file shipped",p)}}
  rows=append(rows,map[string]any{"display_name":name,"managed_sha256":hex.EncodeToString(want[:]),"generated_sha256":result.LauncherSHA256,"protected_payload_preserved":true,"private_files_excluded":true})
 }
 if path:=os.Getenv("GM_PROTECTED_TEMPLATE_REPORT");path!=""{b,_:=json.MarshalIndent(rows,"","  ");if e:=os.WriteFile(path,b,0600);e!=nil{t.Fatal(e)}}
}
