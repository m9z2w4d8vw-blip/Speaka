#include "shared/VolumeControl.h"
#include "shared/VolumeMailbox.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <limits.h>
typedef struct {int sends,applies,fail_send,fail_apply;uint8_t label,code,bytes[264],gain;size_t n;} Fake;
static int apply(void *context,uint8_t value) {
    Fake *f=context;f->applies++;if(f->fail_apply)return 0;f->gain=value;return 1;
}
static int send_response(void *context,const uint8_t *peer,uint8_t label,uint8_t code,const uint8_t *p,size_t n) {
    Fake *f=context;assert(peer[0]==1);assert(n<=sizeof f->bytes);f->sends++;f->label=label;f->code=code;
    f->n=n;memcpy(f->bytes,p,n);return !f->fail_send;
}
static const uint8_t peer[]={1,2,3,4,5,6},other[]={2,2,3,4,5,6},events[]={1,2,8,9,10,11,12};
static int command(SPVolume *s,SPVolumeIO *io,uint8_t label,uint8_t ctype,const uint8_t *p,size_t n) {
    return sp_volume_command(s,io,peer,9,label,ctype,p,n,events,sizeof events);
}
static void protocol(void) {
    SPVolume s;Fake f={0};SPVolumeIO io={apply,send_response,&f};sp_volume_bind(&s,peer,9,127);
    uint8_t caps[]={0,0x19,0x58,0x10,0,0,1,3};
    assert(command(&s,&io,5,SP_AVRCP_STATUS,caps,sizeof caps));
    const uint8_t expected[]={0,0x19,0x58,0x10,0,0,10,3,8,1,2,8,9,10,11,12,13};
    assert(f.code==SP_AVRCP_STABLE && f.label==5 && f.n==sizeof expected && !memcmp(f.bytes,expected,sizeof expected));
    uint8_t reg[]={0,0x19,0x58,0x31,0,0,5,13,0,0,0,0};
    assert(command(&s,&io,11,SP_AVRCP_NOTIFY,reg,sizeof reg));
    assert(f.code==SP_AVRCP_INTERIM && f.bytes[7]==13 && f.bytes[8]==127 && s.subscribed);
    uint8_t set[]={0,0x19,0x58,0x50,0,0,1,64};
    int sends=f.sends;
    assert(command(&s,&io,3,SP_AVRCP_CONTROL,set,sizeof set));
    assert(f.sends==sends+1 && f.code==SP_AVRCP_ACCEPTED && f.bytes[7]==64 && s.value==64 && f.gain==64 && s.subscribed);
    assert(!sp_volume_flush(&s,&io)); /* remote changes never produce a CHANGED echo */
    assert(sp_volume_local(&s,&io,32));
    assert(f.code==SP_AVRCP_CHANGED && f.label==11 && f.bytes[8]==32 && !s.subscribed);
    sends=f.sends;assert(sp_volume_local(&s,&io,20));assert(f.sends==sends); /* one-shot */
    assert(command(&s,&io,15,SP_AVRCP_NOTIFY,reg,sizeof reg));
    sends=f.sends;assert(sp_volume_local(&s,&io,20));assert(f.sends==sends); /* unchanged */
    f.fail_send=1;assert(sp_volume_local(&s,&io,21));assert(s.subscribed && s.pending_local);
    f.fail_send=0;assert(sp_volume_flush(&s,&io));assert(!s.subscribed && f.label==15);
    f.fail_send=1;assert(command(&s,&io,2,SP_AVRCP_NOTIFY,reg,sizeof reg));assert(!s.subscribed);
    f.fail_send=0;assert(command(&s,&io,2,SP_AVRCP_NOTIFY,reg,sizeof reg));
    f.fail_apply=1;set[7]=90;assert(command(&s,&io,3,SP_AVRCP_CONTROL,set,sizeof set));
    assert(f.code==SP_AVRCP_REJECTED && f.bytes[7]==3 && s.value==21 && s.subscribed);
    assert(!sp_volume_local(&s,&io,90));assert(s.value==21);f.fail_apply=0;
    set[7]=255;assert(command(&s,&io,3,SP_AVRCP_CONTROL,set,sizeof set));assert(s.value==127); /* ignore RFA */
    f.fail_send=1;assert(sp_volume_local(&s,&io,42));assert(s.pending_local && s.subscribed);
    f.fail_send=0;set[7]=70;assert(command(&s,&io,4,SP_AVRCP_CONTROL,set,sizeof set));
    assert(s.value==70 && s.subscribed && !s.pending_local && !sp_volume_flush(&s,&io));
    for(unsigned value=0;value<128;value++) {
        set[7]=(uint8_t)value;assert(command(&s,&io,3,SP_AVRCP_CONTROL,set,sizeof set));
        assert(s.value==value && f.gain==value && f.bytes[7]==value && f.code==9);
    }
    sends=f.sends;
    assert(!sp_volume_command(&s,&io,other,9,3,0,set,sizeof set,events,7));
    assert(!sp_volume_command(&s,&io,peer,8,3,0,set,sizeof set,events,7));
    assert(!sp_volume_command(&s,&io,peer,9,16,0,set,sizeof set,events,7));
    assert(f.sends==sends);
    for(size_t n=0;n<sizeof set;n++) {
        sends=f.applies;(void)command(&s,&io,3,0,set,n);assert(f.applies==sends);
    }
    set[4]=1;assert(command(&s,&io,3,0,set,8));assert(f.code==10 && f.bytes[7]==1);set[4]=0;
    set[5]=1;assert(command(&s,&io,3,0,set,8));assert(f.code==10);set[5]=0;
    set[6]=2;assert(command(&s,&io,3,0,set,8));assert(f.code==10);set[6]=1;
    assert(command(&s,&io,3,1,set,8));assert(f.code==10);
    assert(command(&s,&io,3,0,set,SIZE_MAX));assert(f.code==10); /* no count-based overread */
    uint8_t untouched[][12]={{0,0x19,0x58,0x31,0,0,5,1}, /* track/playback events stay native */
                            {0,0x19,0x58,0x10,0,0,1,2}, /* company capability stays native */
                            {0,0x19,0x58,0x20,0,0,0},
                            {0,0x19,0x59,0x50,0,0,1,64}};
    for(size_t i=0;i<4;i++) assert(!command(&s,&io,3,0,untouched[i],12));
    sp_volume_reset(&s);sends=f.sends;assert(!command(&s,&io,3,0,set,8));assert(!sp_volume_local(&s,&io,12));assert(f.sends==sends);
    sp_volume_bind(&s,other,10,127);assert(!s.subscribed && s.epoch==10);
    /* Deterministic malformed-input corpus under sanitizers. */
    uint32_t r=1;uint8_t fuzz[64];sp_volume_bind(&s,peer,9,127);
    for(unsigned i=0;i<50000;i++) {
        for(size_t j=0;j<sizeof fuzz;j++) {r=r*1664525u+1013904223u;fuzz[j]=(uint8_t)(r>>24);}
        if(i&1) {fuzz[0]=0;fuzz[1]=0x19;fuzz[2]=0x58;fuzz[3]=(i&2)?0x50:0x31;}
        (void)command(&s,&io,i%16,i%16,fuzz,i%65);
    }
    puts("AVRCP: peer/epoch isolation, capabilities, every volume, malformed inputs, echo suppression, one-shot notifications, failures passed");
}
static void pcm(void) {
    int16_t all[65536],copy[65536];for(int i=0;i<65536;i++)all[i]=(int16_t)(i-32768);
    memcpy(copy,all,sizeof all);sp_volume_pcm(copy,65536,127);assert(!memcmp(copy,all,sizeof all));
    sp_volume_pcm(copy,65536,0);for(size_t i=0;i<65536;i++)assert(!copy[i]);
    for(unsigned gain=1;gain<127;gain++) {
        memcpy(copy,all,sizeof all);sp_volume_pcm(copy,65536,gain);
        for(size_t i=0;i<65536;i++) {
            assert(copy[i]==(int32_t)all[i]*(int32_t)gain/127);
            if(i)assert(copy[i]>=copy[i-1]);
        }
    }
    puts("PCM: all 65,536 int16 values at every 0..127 gain, exact mute and bit-identical unity passed");
}
static void mailbox_test(const char *dir) {
    char path[512],link[512];snprintf(path,sizeof path,"%s/volume",dir);snprintf(link,sizeof link,"%s/link",dir);
    SPVolumeMailbox *a=sp_volume_mailbox_open(path,123,456,1);assert(a);assert(!sp_volume_mailbox_ready(a));
    assert(!sp_volume_mailbox_open(path,123,456,1));assert(!sp_volume_mailbox_open(path,123,457,0));
    assert(!sp_volume_mailbox_open(path,124,456,0));assert(sp_volume_mailbox_gain(a)==127);
    assert(!symlink(path,link));assert(!sp_volume_mailbox_open(link,123,456,0));unlink(link);
    int ready[2],done[2];assert(!pipe(ready));assert(!pipe(done));pid_t child=fork();assert(child>=0);
    if(!child) {
        close(ready[0]);close(done[1]);SPVolumeMailbox *b=sp_volume_mailbox_open(path,123,456,0);assert(b);
        __atomic_store_n(&b->heartbeat_ms,sp_volume_now_ms(),__ATOMIC_RELEASE);
        char ch='x';assert(write(ready[1],&ch,1)==1);assert(read(done[0],&ch,1)==1);
        assert(sp_volume_mailbox_gain(b)==32);sp_volume_mailbox_close(b);_exit(0);
    }
    close(ready[1]);close(done[0]);char ch;assert(read(ready[0],&ch,1)==1);assert(sp_volume_mailbox_ready(a));
    sp_volume_mailbox_set(a,1,32);assert(write(done[1],"x",1)==1);int status;assert(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
    close(ready[0]);close(done[1]);__atomic_store_n(&a->heartbeat_ms,1,__ATOMIC_RELEASE);assert(!sp_volume_mailbox_ready(a));
    sp_volume_mailbox_set(a,0,0);assert(sp_volume_mailbox_gain(a)==127);
    assert(!chmod(path,0666));assert(!sp_volume_mailbox_open(path,123,456,0));chmod(path,0600);
    sp_volume_mailbox_close(a);unlink(path);
    puts("IPC: independent processes, wrong tokens/PIDs, permissions, symlinks, heartbeat expiry and unity fallback passed");
}
int main(int argc,char **argv) {assert(argc==2);protocol();pcm();mailbox_test(argv[1]);return 0;}
