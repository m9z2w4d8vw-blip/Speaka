/* StreamControl.c - tracks each AVDTP stream from the PC's signalling (configure, open, start, suspend, close),
 * so the relay forwards audio only for the stream that's really playing. Pure, so the host tests run it. */
#include "Tweak.h"
#include <string.h>


int stream_control_signal(StreamControl *s,unsigned direction,uint64_t cid,
                    const uint8_t *p,size_t length) {
    if(direction>1 || length<2 || ((p[0]>>2)&3u) || (p[1]&0xc0u)) return -1;
    unsigned tx=p[0]>>4,msg=p[0]&3u,sig=p[1]&63u;
    if(msg==0) {
        StreamTransaction *t=&s->pending[direction][tx];
        t->valid=0; /* transaction label reused */
        if(sig!=7 && sig!=8 && sig!=9 && sig!=10) return -1;
        unsigned seid=direction?s->remote_seid:s->local_seid;
        int matches=0;
        for(size_t i=2;i<length;i++) if(seid && p[i]==(seid<<2)) matches=1;
        if(!matches) return -1;
        t->cid=cid;t->signal=sig;t->valid=1;
        /* Suspend/close/abort silence immediately, even if later rejected. */
        return sig==7?-1:0;
    }
    StreamTransaction *t=&s->pending[1u-direction][tx];
    if(!t->valid || t->cid!=cid || t->signal!=sig) return -1;
    t->valid=0;
    if(msg==2 && length==2) return sig==7?1:0;
    return -1;
}

/* AVDTP 8.9 SET_CONFIGURATION, as the PC sends it: [header][signal 0x03][ACP SEID<<2][INT SEID<<2], then service
 * capabilities, each [category][LOSC][LOSC bytes] (AVDTP 8.21). Media Codec is category 7: [media type<<4][codec type]
 * [codec-specific information]. This is the codec the PC chose - from the protocol, not from bluetoothd's memory. */
int avdtp_setconfig_media_codec(const uint8_t *p,size_t length,unsigned *acp_seid,unsigned *media_type,
                                unsigned *codec_type,uint8_t *info,size_t info_cap,size_t *info_len) {
    if(!p || length<4u || !acp_seid || !media_type || !codec_type || !info_len) return 0;
    if((p[0]&3u) || ((p[0]>>2)&3u) || (p[1]&63u)!=3u) return 0;   /* a single-packet SET_CONFIGURATION command */
    *acp_seid=p[2]>>2;
    size_t i=4;
    while(i+2u<=length) {
        size_t category=p[i],losc=p[i+1];
        if(i+2u+losc>length) return 0;                         /* runs past the PDU: malformed */
        if(category==7u) {
            if(losc<2u || losc-2u>info_cap || (losc>2u && !info)) return 0;
            *media_type=p[i+2]>>4;
            *codec_type=p[i+3];
            if(losc>2u) memcpy(info,p+i+4,losc-2u);
            *info_len=losc-2u;
            return 1;
        }
        i+=2u+losc;
    }
    return 0;
}
