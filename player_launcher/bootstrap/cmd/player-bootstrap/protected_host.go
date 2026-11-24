//go:build windows

package main

import (
 "bytes"
 "crypto/aes"
 "crypto/cipher"
 "encoding/hex"
 "fmt"
)

// Injected only for protected builds; garble also hides linker-injected literals.
var protectedHostKey string
const protectedHostMagic = "PY90_HOST_AESGCM_V1\n"

func decodeManagedHost(wire []byte) ([]byte,error) {
 if protectedHostKey=="" { return wire,nil } // Legacy test fixtures only; release gate requires a key.
 key,err:=hex.DecodeString(protectedHostKey)
 if err!=nil || len(key)!=32{return nil,fmt.Errorf("登录器保护密钥无效")}
 defer func(){for i:=range key{key[i]=0}}()
 if !bytes.HasPrefix(wire,[]byte(protectedHostMagic)){return nil,fmt.Errorf("缺少登录器保护封装")}
 block,err:=aes.NewCipher(key);if err!=nil{return nil,err}
 gcm,err:=cipher.NewGCM(block);if err!=nil{return nil,err}
 body:=wire[len(protectedHostMagic):]
 if len(body)<gcm.NonceSize()+gcm.Overhead(){return nil,fmt.Errorf("登录器保护封装不完整")}
 plain,err:=gcm.Open(nil,body[:gcm.NonceSize()],body[gcm.NonceSize():],[]byte(protectedHostMagic))
 if err!=nil||len(plain)<2||string(plain[:2])!="MZ"{return nil,fmt.Errorf("登录器主体认证失败")}
 return plain,nil
}
