#include "shared/AudioRing.h"
#include "tweak/Tweak.h"
#include "audiohelper/SBCDecoder.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

/* The reference: all 258 recorded frames decoded in one go with libsbc, to compare the streamed decode against.
 * (Formerly the helper's own, used only by an SBC test mode since removed; kept here as the reference.) */
#define REFERENCE_MAX_FRAMES 300u
#define REFERENCE_MAX_PER_CHANNEL (300u * SBC_MAX_SAMPLES)
static int reference_decode_sbc(const uint8_t *src,size_t src_bytes,int16_t *pcm,size_t pcm_slots,
                        size_t *frames_out,size_t *samples_out,unsigned *rate_out,unsigned *channels_out) {
    if(frames_out)*frames_out=0;
    if(samples_out)*samples_out=0;
    if(rate_out)*rate_out=0;
    if(channels_out)*channels_out=0;
    if(!src||!pcm||!frames_out||!samples_out||!rate_out||!channels_out||
       src_bytes==0||src_bytes>20000u||pcm_slots>REFERENCE_MAX_PER_CHANNEL*2u)return -1;
    sbc_t decoder;
    memset(&decoder,0,sizeof decoder);sbc_reset(&decoder);
    size_t offset=0,frames=0,per_channel=0;
    while(offset<src_bytes){
        struct sbc_frame frame;
        if(src_bytes-offset < SBC_HEADER_SIZE || sbc_probe(src+offset,&frame)!=0)return -2;
        unsigned frame_len=sbc_get_frame_size(&frame);
        unsigned channels=frame.mode==SBC_MODE_MONO?1u:2u;
        unsigned sr=(unsigned)sbc_get_freq_hz(frame.freq);
        size_t n=(size_t)frame.nblocks*(size_t)frame.nsubbands;
        if(frame_len<SBC_HEADER_SIZE || frame_len>src_bytes-offset ||
           channels!=2u||sr!=44100u||n==0||n>SBC_MAX_SAMPLES)return -3;
        if(frames>=REFERENCE_MAX_FRAMES || per_channel > REFERENCE_MAX_PER_CHANNEL-n ||
           n>pcm_slots/2u || per_channel > (pcm_slots/2u)-n) return -4;
        /* SBC API accepts output pitch for channel-interleaved PCM. */
        if(sbc_decode(&decoder,src+offset,frame_len,&frame,
                      pcm+per_channel*2u,2,pcm+per_channel*2u+1u,2)!=0)return -5;
        offset+=frame_len;per_channel+=n;frames++;
    }
    if(frames!=258u || per_channel!=33024u || offset!=src_bytes)return -6;
    *frames_out=frames;*samples_out=per_channel;*rate_out=44100u;*channels_out=2u;
    return 0;
}


static void queues(void) {
    AudioRing *s=mmap(NULL,sizeof *s,PROT_READ|PROT_WRITE,MAP_ANON|MAP_SHARED,-1,0);
    assert(s!=MAP_FAILED);audio_ring_init(s,123,456);
    AudioPacket p={0},out;
    p.length=AUDIO_RING_PAYLOAD;p.generation=3;
    for(unsigned i=0;i<100;i++) {p.timestamp=i;memset(p.data,i,sizeof p.data);assert(audio_ring_enqueue(s,&p));}
    assert(s->media.dropped==68);
    for(unsigned i=68;i<100;i++) {
        assert(audio_ring_dequeue(s,&out));assert(out.timestamp==i);
        for(unsigned j=0;j<out.length;j++) assert(out.data[j]==(uint8_t)i);
    }
    assert(!audio_ring_dequeue(s,&out));
    /* A consumer that already claimed slot 0 owns its bytes until release.
       Force producer wraparound and verify it never touches that slot. */
    audio_ring_init(s,123,456);
    for(unsigned i=0;i<32;i++) {p.timestamp=i;assert(audio_ring_enqueue(s,&p));}
    __atomic_store_n(&s->media.read,1,__ATOMIC_RELAXED);
    AudioPacket held=s->packets[0].value;
    for(unsigned i=0;i<64;i++) (void)audio_ring_enqueue(s,&p);
    assert(!memcmp(&held,&s->packets[0].value,sizeof held));
    __atomic_store_n(&s->packets[0].turn,32,__ATOMIC_RELEASE);
    assert(audio_ring_enqueue(s,&p));
    audio_ring_init(s,123,456);
    pid_t child=fork();assert(child>=0);
    if(!child) {
        unsigned seen=0,last=0;
        for(;;) {
            if(audio_ring_dequeue(s,&out)) {
                assert(out.length==AUDIO_RING_PAYLOAD);
                assert(!seen || out.timestamp>last);last=out.timestamp;seen++;
                for(unsigned j=0;j<out.length;j++) assert(out.data[j]==(uint8_t)last);
            } else if(__atomic_load_n(&s->helper_heartbeat,__ATOMIC_ACQUIRE) &&
                      s->media.read==s->media.write) break;
        }
        assert(seen);_exit(0);
    }
    for(unsigned i=0;i<100000;i++) {
        p.timestamp=i;memset(p.data,(uint8_t)i,sizeof p.data);audio_ring_enqueue(s,&p);
    }
    __atomic_store_n(&s->helper_heartbeat,1,__ATOMIC_RELEASE);
    int status;assert(waitpid(child,&status,0)==child);assert(WIFEXITED(status) && WEXITSTATUS(status)==0);
    munmap(s,sizeof *s);puts("queue overflow, pinned reader, forked shared-memory stress: PASS");
}
static void control(void) {
    StreamControl c={0};c.local_seid=7;c.remote_seid=52;
    uint8_t start[]={0x30,7,0x1c},accept[]={0x32,7},wrong[]={0x42,7},reject[]={0x33,7,0x31};
    assert(stream_control_signal(&c,0,0xc0b,start,3)==-1);
    assert(stream_control_signal(&c,1,0xc0b,wrong,2)==-1);
    assert(stream_control_signal(&c,1,0x60a,accept,2)==-1);
    assert(stream_control_signal(&c,1,0xc0b,accept,2)==1);
    assert(stream_control_signal(&c,1,0xc0b,accept,2)==-1);
    assert(stream_control_signal(&c,0,0xc0b,start,3)==-1);
    assert(stream_control_signal(&c,1,0xc0b,reject,3)==-1);
    assert(stream_control_signal(&c,1,0xc0b,accept,2)==-1);
    uint8_t suspend[]={0x50,9,0xd0};assert(stream_control_signal(&c,1,0xc0b,suspend,3)==0);
    start[2]=0xd0;assert(stream_control_signal(&c,1,0xc0b,start,3)==-1);
    assert(stream_control_signal(&c,0,0xc0b,accept,2)==1);
    /* Works beyond the old 128-line log sampling cutoff, including label wrap. */
    for(unsigned i=0;i<500;i++) {
        start[0]=(i&15)<<4;accept[0]=start[0]|2;
        stream_control_signal(&c,1,0xc0b,start,3);
        assert(stream_control_signal(&c,0,0xc0b,accept,2)==1);
    }
    puts("START correlation, reject, direction, CID, suspend, 500 restarts: PASS");
}
static void decode(const char *path, const char *pcm_out) {
    FILE *f=fopen(path,"rb");assert(f);uint8_t src[20000];size_t size=fread(src,1,sizeof src,f);fclose(f);
    int16_t *reference=calloc(33024*2,2),*actual=calloc(33024*2,2);assert(reference && actual);
    size_t total_frames=0,total_samples=0;unsigned rate=0,channels=0;
    assert(!reference_decode_sbc(src,size,reference,33024*2,&total_frames,&total_samples,&rate,&channels));
    SBCDecoder d;sbc_decoder_reset(&d);AudioPacket p={0};p.generation=3;
    size_t offset=0,written=0,frames=0;
    int16_t pcm[SBC_DECODER_PCM_SAMPLES];unsigned nf,ns,hz;
    while(offset<size) {
        p.length=1;p.data[0]=0;
        for(unsigned i=0;i<7 && offset<size;i++) {
            struct sbc_frame frame;assert(!sbc_probe(src+offset,&frame));unsigned n=sbc_get_frame_size(&frame);
            assert(p.length+n<=AUDIO_RING_PAYLOAD);memcpy(p.data+p.length,src+offset,n);
            p.length+=n;p.data[0]++;offset+=n;
        }
        assert(!sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz));
        assert(hz==44100);memcpy(actual+written*2,pcm,ns*4);written+=ns;frames+=nf;p.sequence++;
    }
    assert(frames==258 && written==33024);assert(!memcmp(actual,reference,written*4));
    if(pcm_out) {   /* the live decoder's output, which tests/test_audio.py compares with ffmpeg's decode */
        FILE *o=fopen(pcm_out,"wb");assert(o);assert(fwrite(actual,4,written,o)==written);fclose(o);
    }
    /* Fragmented frame, sequence gap, truncated frame and bad CRC. */
    struct sbc_frame first;assert(!sbc_probe(src,&first));unsigned len=sbc_get_frame_size(&first);
    sbc_decoder_reset(&d);p.sequence=65535;p.data[0]=0xc0;p.length=1+len/2;
    memcpy(p.data+1,src,len/2);
    assert(sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz)==1);
    p.sequence=0;p.data[0]=0xa0;p.length=1+len-len/2;memcpy(p.data+1,src+len/2,len-len/2);
    assert(!sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz));assert(nf==1);
    p.sequence=2;assert(sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz)<0);
    p.sequence++;p.length=len;p.data[0]=1;memcpy(p.data+1,src,len-1);
    assert(sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz)<0);
    p.sequence++;p.length=len+1;memcpy(p.data+1,src,len);p.data[4]^=0xff;
    assert(sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz)<0);
    /* Real encoded 48kHz stereo and mono, not a mutated 44.1kHz header. */
    sbc_t encoder;memset(&encoder,0,sizeof encoder);sbc_reset(&encoder);
    struct sbc_frame frame={.freq=SBC_FREQ_48K,.mode=SBC_MODE_STEREO,.bam=SBC_BAM_LOUDNESS,
                             .nblocks=16,.nsubbands=8,.bitpool=32};
    int16_t input[256];for(unsigned i=0;i<256;i++) input[i]=(i%32)*200-3000;
    for(unsigned mono=0;mono<2;mono++) {
        frame.mode=mono?SBC_MODE_MONO:SBC_MODE_STEREO;
        p.sequence++;p.generation+=2;p.data[0]=1;p.length=1+sbc_get_frame_size(&frame);
        assert(!sbc_encode(&encoder,input,2,input+1,2,&frame,p.data+1,p.length-1));
        assert(!sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz));
        assert(nf==1 && ns==128 && hz==48000);
        if(mono) for(unsigned i=0;i<ns;i++) assert(pcm[i*2]==pcm[i*2+1]);
    }
    /* Bounded malformed input corpus, including every payload-header bit. */
    unsigned seed=0x12345;
    for(unsigned i=0;i<10000;i++) {
        p.sequence++;p.length=1+i%AUDIO_RING_PAYLOAD;
        for(unsigned j=0;j<p.length;j++) {seed=1664525u*seed+1013904223u;p.data[j]=seed>>24;}
        int r=sbc_decoder_decode(&d,&p,pcm,SBC_DECODER_PCM_SAMPLES,&nf,&ns,&hz);
        assert(r || (nf<=15 && ns<=15*128));
    }
    free(reference);free(actual);puts("continuous reference PCM, fragments, gaps, CRC, 48k mono/stereo, malformed corpus: PASS");
}
int main(int argc,char **argv) {assert(argc==2||argc==3);queues();control();decode(argv[1],argc==3?argv[2]:NULL);return 0;}
