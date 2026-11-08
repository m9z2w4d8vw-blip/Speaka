#include "VolumeControl.h"
#include <string.h>

void sp_volume_reset(SPVolume *s) { if(s) memset(s,0,sizeof *s); }
void sp_volume_bind(SPVolume *s,const uint8_t peer[6],uint64_t epoch,uint8_t initial) {
    sp_volume_reset(s);
    if(!s || !peer || !epoch || initial>SP_VOLUME_MAX) return;
    memcpy(s->peer,peer,6);s->epoch=epoch;s->value=initial;s->bound=1;
}
int sp_volume_rebind(SPVolume *s,const uint8_t peer[6],uint64_t epoch,uint8_t initial) {
    if(!s) return 0;
    if(peer && epoch && initial<=SP_VOLUME_MAX && s->bound && !memcmp(s->peer,peer,6)) {
        s->epoch=epoch;              /* the same AVRCP connection: subscribed, label, value, pending_local stay */
        return s->subscribed!=0;
    }
    sp_volume_bind(s,peer,epoch,initial);
    return 0;
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

static int sp_near(int a, int b, int tolerance) { return a >= b - tolerance && a <= b + tolerance; }
void sp_domain_init(SPVolumeDomain *d, int ios) {
    if(!d) return;
    memset(d,0,sizeof *d);d->ios=ios?1:0;d->target=d->pending=d->seen=-1;
}
int sp_domain_remote(SPVolumeDomain *d, int level, int current_ios, uint64_t now) {
    if(!d || !d->ios || level<0 || level>SP_VOLUME_MAX) return -1;
    if(current_ios>=0 && sp_near(current_ios,level,1)) { d->pending=-1; return -1; }   /* already there */
    if(d->last_set_ms && now-d->last_set_ms<SP_DOMAIN_SET_MS) { d->pending=level; return -1; }
    d->pending=-1;
    return level;
}
int sp_domain_due(SPVolumeDomain *d, int current_ios, uint64_t now) {
    if(!d || !d->ios || d->pending<0 || now-d->last_set_ms<SP_DOMAIN_SET_MS) return -1;
    int level=d->pending;d->pending=-1;
    return current_ios>=0 && sp_near(current_ios,level,1) ? -1 : level;
}
void sp_domain_set(SPVolumeDomain *d, int level, uint64_t now) {
    if(!d) return;
    d->target=level;d->target_ms=now?now:1u;d->last_set_ms=d->target_ms;d->verify=1;
}
int sp_domain_observed(SPVolumeDomain *d, int level, uint64_t now) {
    if(!d || level<0 || level>SP_VOLUME_MAX || level==d->seen) return 0;
    int first=d->seen<0;
    d->seen=level;
    if(first) return 2;
    if(!d->ios) return 0;
    if(d->target_ms && now-d->target_ms<SP_DOMAIN_ECHO_MS) return 0;   /* the domain's own set, settling */
    return 1;
}
int sp_domain_verify(SPVolumeDomain *d, int readback, uint64_t now) {
    if(!d || !d->ios || !d->verify || now-d->target_ms<SP_DOMAIN_VERIFY_MS) return 0;
    d->verify=0;
    if(d->pending<0 && sp_near(readback,d->target,2)) { d->failures=0; return 0; }
    if(d->pending>=0) return 0;                                          /* superseded: judge the next one */
    if(++d->failures<SP_DOMAIN_MAX_FAILURES) return 0;
    d->ios=0;d->pending=-1;
    return 1;
}

