package launcherruntime
import("crypto/sha256";"encoding/hex";"encoding/json";"strings";"testing")
func TestWindows7RuntimeManifest(t *testing.T){
 _,m:=fixture(t);m.Version="6.0.36"
 for i:=range m.Files{m.Files[i].Path=strings.ReplaceAll(m.Files[i].Path,"8.0.32","6.0.36")}
 b,_:=json.Marshal(m.Files);h:=sha256.Sum256(b);m.Directory="runtime/launcher-dotnet6/"+hex.EncodeToString(h[:])
 if e:=m.Validate();e!=nil{t.Fatal(e)}
 m.Directory=strings.Replace(m.Directory,"dotnet6","dotnet8",1);if m.Validate()==nil{t.Fatal("accepted mismatched runtime major and directory")}
}
