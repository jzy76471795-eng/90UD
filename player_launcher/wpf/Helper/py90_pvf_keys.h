#ifndef PY90_PVF_KEYS_H
#define PY90_PVF_KEYS_H
#include <stdint.h>
#include <string.h>
/* Protocol order head/hash/group/body/utf8/utf16; frozen addresses use body/head/group/hash/utf16/utf8. */
static const unsigned py90_key_index[6]={3,0,2,1,5,4};
static int py90_hex(char ch) {
    if(ch>='0' && ch<='9')return ch-'0';
    if(ch>='a' && ch<='f')return ch-'a'+10;
    if(ch>='A' && ch<='F')return ch-'A'+10;
    return -1;
}
static int py90_parse_keys(const char* text,unsigned char out[48]) {
    if(!text || strlen(text)!=96)return 0;
    for(unsigned i=0;i<48;i++){
        int h=py90_hex(text[2*i]),l=py90_hex(text[2*i+1]);
        if(h<0||l<0){memset(out,0,48);return 0;}
        out[i]=(unsigned char)((h<<4)|l);
    }
    return 1;
}
static uint32_t py90_client_seed(uint16_t a,uint16_t b,uint16_t c,uint16_t d) {
    return 866031377u*a+915u*(d+915u*(c+915u*b));
}
static void py90_pack_key(const unsigned char raw[8],unsigned char out[20]) {
    memset(out,0,20);out[0]=0xa1;out[1]=0x2c;memcpy(out+4,raw,8);
}
static uint16_t py90_u16(const unsigned char* p){return (uint16_t)(p[0]|(p[1]<<8));}
static uint32_t py90_u32(const unsigned char* p){return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void py90_header_crypt(unsigned char header[32],const unsigned char key[8]){
    unsigned char blob[20];py90_pack_key(key,blob);
    uint32_t seed=py90_client_seed(py90_u16(blob+4),py90_u16(blob+6),py90_u16(blob+8),py90_u16(blob+10));
    for(unsigned i=0;i<32;i+=4){
        uint32_t s1=214013u*seed+2531011u;seed=214013u*s1+2531011u;
        uint32_t mask=(s1&0xffff0000u)|(seed>>16);
        for(unsigned j=0;j<4;j++)header[i+j]^=(unsigned char)(mask>>(8*j));
    }
    memset(blob,0,sizeof(blob));
}
#endif
