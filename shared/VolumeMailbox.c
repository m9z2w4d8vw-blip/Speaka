#include "VolumeMailbox.h"
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <string.h>
_Static_assert(__atomic_always_lock_free(8,0),"volume heartbeat must be lock free");
_Static_assert(sizeof(SPVolumeMailbox)==40,"volume mailbox ABI");
uint64_t sp_volume_now_ms(void) {
    struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)) return 0;
    return (uint64_t)t.tv_sec*1000u+(uint64_t)t.tv_nsec/1000000u;
}
SPVolumeMailbox *sp_volume_mailbox_open(const char *path,uint32_t pid,uint64_t token,int create) {
    if(!path || !pid || !token) return NULL;
    int fd=open(path,O_RDWR|O_NOFOLLOW|(create?(O_CREAT|O_EXCL):0),0600);
    if(fd<0) return NULL;
    struct stat st;SPVolumeMailbox *m=MAP_FAILED;
    if(!fstat(fd,&st) && S_ISREG(st.st_mode) && st.st_uid==getuid() && st.st_nlink==1 &&
       !(st.st_mode&077) && (create?ftruncate(fd,sizeof *m)==0:st.st_size==(off_t)sizeof *m))
        m=mmap(NULL,sizeof *m,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    close(fd);
    if(m==MAP_FAILED) {if(create) unlink(path);return NULL;}
    if(create) {
        memset(m,0,sizeof *m);m->version=1;m->pid=pid;m->token=token;m->size=sizeof *m;
        __atomic_store_n(&m->magic,SP_VOLUME_MAILBOX_MAGIC,__ATOMIC_RELEASE);
    } else if(__atomic_load_n(&m->magic,__ATOMIC_ACQUIRE)!=SP_VOLUME_MAILBOX_MAGIC ||
              m->version!=1 || m->pid!=pid || m->token!=token || m->size!=sizeof *m) {
        munmap(m,sizeof *m);return NULL;
    }
    return m;
}
void sp_volume_mailbox_close(SPVolumeMailbox *m) {if(m) munmap(m,sizeof *m);}
int sp_volume_mailbox_ready(const SPVolumeMailbox *m) {
    if(!m) return 0;
    uint64_t seen=__atomic_load_n(&m->heartbeat_ms,__ATOMIC_ACQUIRE),now=sp_volume_now_ms();
    return seen && now>=seen && now-seen<1000;
}
void sp_volume_mailbox_set(SPVolumeMailbox *m,int enabled,unsigned value) {
    if(m) __atomic_store_n(&m->control,enabled?(0x100u|(value>127?127:value)):0,__ATOMIC_RELEASE);
}
unsigned sp_volume_mailbox_gain(const SPVolumeMailbox *m) {
    if(!m) return 127;
    uint32_t value=__atomic_load_n(&m->control,__ATOMIC_ACQUIRE);
    return value&0x100u?value&0x7fu:127;
}
