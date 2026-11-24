//go:build windows
package main
import("bytes";"crypto/aes";"crypto/cipher";"encoding/hex";"testing")
func TestProtectedHostAuthentication(t *testing.T){
 old:=protectedHostKey;defer func(){protectedHostKey=old}()
 key:=bytes.Repeat([]byte{17},32);protectedHostKey=hex.EncodeToString(key)
 block,_:=aes.NewCipher(key);gcm,_:=cipher.NewGCM(block);nonce:=make([]byte,12)
 wire:=append([]byte(protectedHostMagic),nonce...);wire=append(wire,gcm.Seal(nil,nonce,[]byte("MZ protected test"),[]byte(protectedHostMagic))...)
 got,err:=decodeManagedHost(wire);if err!=nil||string(got)!="MZ protected test"{t.Fatal("round trip failed",err)}
 for _,kind:=range []string{"tampered","truncated","plaintext","wrong-key"}{t.Run(kind,func(t *testing.T){
  bad:=append([]byte(nil),wire...);protectedHostKey=hex.EncodeToString(key)
  switch kind{case "tampered":bad[len(bad)-1]^=1;case "truncated":bad=bad[:24];case "plaintext":bad=[]byte("MZ plaintext");case "wrong-key":protectedHostKey=hex.EncodeToString(bytes.Repeat([]byte{18},32))}
  if _,e:=decodeManagedHost(bad);e==nil{t.Fatal("accepted",kind)}
 })}
}
