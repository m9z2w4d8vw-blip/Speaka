#include "VolumeControl.h"
#include <string.h>

void sp_volume_reset(SPVolume *s) { if(s) memset(s,0,sizeof *s); }
void sp_volume_bind(SPVolume *s,const uint8_t peer[6],uint64_t epoch,uint8_t initial) {
    sp_volume_reset(s);
    if(!s || !peer || !epoch || initial>SP_VOLUME_MAX) return;
    memcpy(s->peer,peer,6);s->epoch=epoch;s->value=initial;s->bound=1;
}
static int reply(const SPVolume *s,const SPVolumeIO *io,uint8_t label,uint8_t code,
                 uint8_t pdu,const uint8_t *data,size_t length) {
    uint8_t bytes[264]={0,0x19,0x58,0,0,0,0};
    if(!io || !io->send || length>sizeof bytes-7) return 0;
    bytes[3]=pdu;bytes[5]=(uint8_t)(length>>8);bytes[6]=(uint8_t)length;
    if(length) memcpy(bytes+7,data,length);
    return io->send(io->context,s->peer,label,code,bytes,7+length)!=0;
}
static int reject(const SPVolume *s,const SPVolumeIO *io,uint8_t label,uint8_t pdu,uint8_t error) {
    (void)reply(s,io,label,SP_AVRCP_REJECTED,pdu,&error,1);return 1;
}
int sp_volume_command(SPVolume *s,const SPVolumeIO *io,const uint8_t peer[6],
                      uint64_t epoch,uint8_t label,uint8_t ctype,
                      const uint8_t *p,size_t n,const uint8_t *events,size_t count) {
    if(!s || !s->bound || !peer || epoch!=s->epoch || memcmp(peer,s->peer,6) ||
       label>15 || !p || n<4 || p[0]!=0 || p[1]!=0x19 || p[2]!=0x58) return 0;
    uint8_t id=p[3];
    /* Only volume and the events capability belong to this layer. */
    if(id!=0x50 && id!=0x31 && id!=0x10) return 0;
    if(id==0x31 && (n<8 || p[7]!=0x0d)) return 0;
    if(id==0x10 && (n<8 || p[7]!=3)) return 0;
    uint8_t expected=id==0x50?SP_AVRCP_CONTROL:id==0x31?SP_AVRCP_NOTIFY:SP_AVRCP_STATUS;
    size_t parameters=id==0x31?5:1;
    if(ctype!=expected || n!=7+parameters || p[4]!=0 || p[5]!=0 || p[6]!=parameters)
        return reject(s,io,label,id,1); /* invalid parameter */
    if(id==0x50) {
        uint8_t value=p[7]&0x7f; /* AVRCP bit 7 is reserved; ignore it on receipt. */
        if(!io || !io->apply || !io->apply(io->context,value)) return reject(s,io,label,id,3);
        s->value=value;s->pending_local=0;
        /* Remote SetAbsoluteVolume must NOT complete the outstanding subscription. */
        (void)reply(s,io,label,SP_AVRCP_ACCEPTED,id,&value,1);
    } else if(id==0x31) {
        uint8_t payload[2]={0x0d,s->value};
        if(reply(s,io,label,SP_AVRCP_INTERIM,id,payload,2)) {
            s->subscribed=1;s->label=label;s->pending_local=0;
        }
    } else {
        uint8_t payload[257]={3,0};
        if(count>254 || (count && !events)) return reject(s,io,label,id,3);
        for(size_t i=0;i<count;i++) {
            int found=0;
            for(size_t j=0;j<payload[1];j++) if(payload[2+j]==events[i]) found=1;
            if(!found) payload[2+payload[1]++]=events[i];
        }
        int found=0;
        for(size_t i=0;i<payload[1];i++) if(payload[2+i]==0x0d) found=1;
        if(!found) payload[2+payload[1]++]=0x0d;
        (void)reply(s,io,label,SP_AVRCP_STABLE,id,payload,2u+payload[1]);
    }
    return 1;
}
int sp_volume_flush(SPVolume *s,const SPVolumeIO *io) {
    if(!s || !s->bound || !s->subscribed || !s->pending_local) return 0;
    uint8_t payload[2]={0x0d,s->value};
    if(!reply(s,io,s->label,SP_AVRCP_CHANGED,0x31,payload,2)) return 0;
    s->subscribed=0;s->pending_local=0;return 1;
}
int sp_volume_local(SPVolume *s,const SPVolumeIO *io,unsigned value) {
    if(!s || !s->bound || value>SP_VOLUME_MAX || !io || !io->apply) return 0;
    if(value==s->value) { (void)sp_volume_flush(s,io);return 1; }
    if(!io->apply(io->context,(uint8_t)value)) return 0;
    s->value=(uint8_t)value;s->pending_local=s->subscribed;
    (void)sp_volume_flush(s,io);return 1;
}
void sp_volume_pcm(int16_t *pcm,size_t samples,unsigned value) {
    if(!pcm || value>=SP_VOLUME_MAX) return;
    if(!value) {memset(pcm,0,samples*sizeof *pcm);return;}
    for(size_t i=0;i<samples;i++) pcm[i]=(int16_t)((int32_t)pcm[i]*(int32_t)value/SP_VOLUME_MAX);
}
