"""The audio path: the shared ring, stream control, the relay inside bluetoothd, the helper's decoder,
playback and output policy.

Sections, in order:
  - the ring, stream control and the decoder, run on this machine
  - the idle guard: keeping a playing stream alive
  - what the log records about the audio
  - the helper's playback callback
  - headphones mode and route changes
  - holding the output through stream changes
"""
import hashlib
import os
import os, pathlib, shutil, subprocess, tempfile, unittest
import pathlib
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---- the ring, stream control and the decoder, run on this machine ----
# Portable runtime regressions; real iOS playback is a separate device test.

P=pathlib.Path(__file__).resolve().parents[1]


class AudioRuntime(unittest.TestCase):
 def test_shared_ring_control_and_continuous_decode(self):
  cc=shutil.which('cc')
  self.assertIsNotNone(cc)
  with tempfile.TemporaryDirectory() as td:
   exe=str(pathlib.Path(td)/'v88')
   src=['tests/audio_runtime.c','shared/AudioRing.c','tweak/StreamControl.c',
        'audiohelper/SBCDecoder.c',
        'audiohelper/vendor_sbc/sbc.c','audiohelper/vendor_sbc/bits.c']
   flags=['-std=gnu11','-O2','-Wall','-Wextra','-Werror','-pthread','-I'+str(P),'-I'+str(P/'audiohelper/vendor_sbc')]
   if os.environ.get('BTPS_SANITIZE'): flags+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
   objects=[]
   for i,file in enumerate(src):
    obj=str(pathlib.Path(td)/('%d.o'%i));objects.append(obj)
    # Upstream fixed-point SBC deliberately shifts signed negatives. Check
    # upstream memory with ASan; use UBSan on our new queue/control/adapter.
    opts=[f.replace('-fsanitize=address,undefined','-fsanitize=address') for f in flags if f!='-Werror'] if '/vendor_sbc/' in file else flags
    r=subprocess.run([cc,*opts,'-c',str(P/file),'-o',obj],capture_output=True,text=True)
    self.assertEqual(r.returncode,0,r.stderr)
   r=subprocess.run([cc,*flags,*objects,'-o',exe],capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
   env=dict(os.environ)
   if os.environ.get('BTPS_SANITIZE'):
    env['ASAN_OPTIONS']='detect_leaks=0' # LSan cannot inspect /proc under this runner
    env['UBSAN_OPTIONS']='halt_on_error=1'
   out=str(pathlib.Path(td)/'decoded.pcm')
   r=subprocess.run([exe,str(P/'tests/fixtures/recorded_258_frames.sbc'),out],capture_output=True,text=True,timeout=30,env=env)
   self.assertEqual(r.returncode,0,r.stdout+r.stderr)
   print(r.stdout,end='')
   # The live decoder's output against ffmpeg's own decode of the same recording, so a wrong libsbc can't pass
   # just by matching itself (this check came with the v7.6 adapter, removed in v9.8): no clipping, a similar
   # level, and the same waveform once past the start.
   import array, math, sys, wave
   x=array.array('h');x.frombytes(pathlib.Path(out).read_bytes())
   self.assertEqual(len(x),33024*2)
   with wave.open(str(P/'tests/fixtures/recorded_258_frames_ffmpeg.wav')) as wav:
    self.assertEqual((wav.getnchannels(),wav.getframerate(),wav.getnframes()),(2,44100,33024))
    ref=array.array('h');ref.frombytes(wav.readframes(wav.getnframes()))
   if sys.byteorder!='little':ref.byteswap()
   ref_rms=math.sqrt(sum(y*y for y in ref)/len(ref))/32768
   rms=math.sqrt(sum(y*y for y in x)/len(x))/32768
   clipped=sum(abs(y)>=32000 for y in x)/len(x)
   self.assertLess(clipped,0.01,'Decoded output clipped, fail rather than play broken audio')
   self.assertGreater(rms,ref_rms*.65)
   self.assertLess(rms,ref_rms*1.6)
   for ch in (0,1):
    a=x[4096*2+ch::2];b=ref[4096*2+ch::2]
    dot=sum(float(u)*v for u,v in zip(a,b));sa=sum(float(u)*u for u in a);sb=sum(float(v)*v for v in b)
    corr=dot/(math.sqrt(sa*sb)+1e-30)
    self.assertGreater(abs(corr),0.80,'Decoded waveform does not resemble reference channel %d: corr %g'%(ch,corr))
 def test_receive_thread_has_no_io_allocation_or_tls(self):
  s=(P/'tweak/AudioRelay.c').read_text().split('void AudioRelayForward(',1)[1].split('static void drain_logs',1)[0]
  for term in ('SpeakaLogLine','malloc(', 'calloc(', 'send(', 'recv(', 'write(', 'pthread_', 'dispatch_', 'safe_peek', '__thread'):
   self.assertNotIn(term,s)
  queue=(P/'shared/AudioRing.c').read_text()
  for term in ('malloc(', 'pthread_', 'usleep(', 'write(', 'syscall', 'while('): self.assertNotIn(term,queue)
 def test_activation_is_one_shot_and_callback_is_table_swap(self):
  s=(P/'tweak/Hooks.xm').read_text()
  self.assertIn('if(g_sink_armed && uuid_ok) g_relay_ready=AudioRelayPrepare();',s)
  block=s.split('static int install_media_callback(',1)[1].split('/* v8.3 one-shot:',1)[0]
  self.assertNotIn('MSHookFunction',block)
  self.assertIn('ptrauth_sign_unauthenticated(raw,ptrauth_key_function_pointer,0)',block)
  self.assertIn('g_media_callbacks[22]',block)
 def test_tx_packet_captured_before_native_ownership_transfer(self):
  s=(P/'tweak/Hooks.xm').read_text().split('static uint64_t hook_transmit_signal(',1)[1].split('static void install_signal_tx(',1)[0]
  self.assertLess(s.index('int control_ok='),s.index('uint64_t ret = g_signal_tx_orig'))
  self.assertIn('if(ret==0u && control_ok)',s)
 def test_launchd_plist(self):
  import plistlib
  p=plistlib.loads((P/'layout/Library/LaunchDaemons/com.parv.speaka.audio.plist').read_bytes())
  self.assertEqual(p['UserName'],'mobile')
  self.assertEqual(p['Sockets']['Stream']['SockNodeName'],'127.0.0.1')
  self.assertEqual(p['ProgramArguments'],['/var/jb/usr/bin/BTPSAudioHelper','--stream-service'])
  self.assertNotIn('KeepAlive',p)


# ---- the idle guard: keeping a playing stream alive ----
# Exercise freshness and connection scoping; verify exact native code gates.

class IdleGuard(unittest.TestCase):
    def compile_run(self, source, cpp=False):
        compiler = shutil.which("c++" if cpp else "cc")
        self.assertIsNotNone(compiler)
        flags = ["-std=c++17" if cpp else "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT)]
        env = dict(os.environ)
        if os.environ.get("BTPS_SANITIZE"):
            flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            env.update(ASAN_OPTIONS="detect_leaks=0", UBSAN_OPTIONS="halt_on_error=1")
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / ("guard.cpp" if cpp else "health.c")
            exe = pathlib.Path(td) / "test"
            src.write_text(source)
            built = subprocess.run([compiler, *flags, str(src), "-o", str(exe)], text=True, capture_output=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(exe)], text=True, capture_output=True, timeout=10, env=env)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_holding_lease_follows_decode_progress_and_counter_switches(self):
        # v9.9.3 (the v9.8.90 regression): gated on a starved input, the helper's output renders nothing, so the
        # render-count lease lapsed 1.5 s in (12:05:57.443 render_age_ms=1516) and Apple suspended the stream. Holding
        # now reports decoded frames. Real Tweak.h: the lease must follow whichever counter is reported, treat a switch
        # between the two as a new baseline (not progress), and still lapse when reception or decoding stops.
        self.compile_run(r"""
#include <assert.h>
#include "tweak/Tweak.h"
static SinkHealth h; static uint64_t media, beat; static uint32_t rendered, decoded;
static void start(uint64_t t) { sink_health_reset(&h,3,100,1000); media=100; beat=1000; rendered=5000; decoded=900000;
                                (void)t; }
static void tick(uint64_t t, int got_media, int alive) { if (got_media) media++; if (alive) beat++;
                                                       sink_health_sample(&h,media,beat,t); }
static uint64_t lapse_after(uint64_t from, uint64_t to, int got_media, int advance, unsigned state) {
    for (uint64_t t = from; t <= to; t += 200) {
        tick(t, got_media, 1);
        if (advance) { if (state == 2) decoded += 1024; else rendered += 1024; }
        sink_health_render(&h,3,state==2?decoded:rendered,state,t);
        if (!sink_health_ready(&h,1,t)) return t;
    }
    return 0;
}
int main(void) {
    /* Playing normally. */
    start(0);
    assert(lapse_after(200,2000,1,1,1) == 0);
    /* A: the gate engages - holding, decoded advancing, rendering frozen: healthy for as long as it lasts. */
    assert(lapse_after(2200,20000,1,1,2) == 0);
    /* C: input recovers - back to playing: no false timeout across the counter switch. */
    assert(lapse_after(20200,24000,1,1,1) == 0);
    /* B: holding again, then the input stops (no media, no decoding; the helper still answers): lapses in ~1.5 s. */
    assert(lapse_after(24200,26000,1,1,2) == 0);
    uint64_t gone = lapse_after(26200,30000,0,0,2);
    assert(gone && gone - 26000 <= 1700);
    /* B: media still arriving but decoding stalled (a stuck decoder): the holding counter is frozen, so it lapses too. */
    start(0); assert(lapse_after(200,2000,1,1,2) == 0);
    uint64_t stalled = lapse_after(2200,6000,1,0,2);
    assert(stalled && stalled - 2000 <= 1700);
    /* E: alternating states every report with BOTH counters frozen is not progress, even with media flowing. */
    start(0); assert(lapse_after(200,2000,1,1,1) == 0);
    uint64_t t, flipped = 0;
    for (t = 2200; t <= 6000; t += 200) {
        tick(t,1,1);
        unsigned state = ((t / 200) % 2) ? 2u : 1u;
        sink_health_render(&h,3,state==2?decoded:rendered,state,t);   /* different numbers, neither advancing */
        if (!sink_health_ready(&h,1,t)) { flipped = t; break; }
    }
    assert(flipped && flipped - 2000 <= 1700);
    /* E: a switch where both counters hold the same value is not a false timeout if the new counter advances. */
    start(0); rendered = decoded = 7777;
    assert(lapse_after(200,2000,1,1,1) == 0);
    decoded = rendered;
    assert(lapse_after(2200,8000,1,1,2) == 0);
    /* Not playing (stream inactive / no queue) never keeps the lease. */
    tick(8200,1,1); sink_health_render(&h,3,decoded+1024,0,8200); assert(!sink_health_ready(&h,1,8200));
    return 0;
}
""")

    def test_health_requires_current_media_heartbeat_and_rendering(self):
        self.compile_run(r'''
#include <assert.h>
#include "tweak/Tweak.h"
int main(void) {
    SinkHealth h;
    sink_health_reset(&h,3,100,1000);
    assert(!sink_health_ready(&h,1,100));
    sink_health_sample(&h,101,1001,200);
    sink_health_render(&h,3,5,1,200);
    assert(sink_health_ready(&h,1,200));
    assert(!sink_health_ready(&h,0,200));
    assert(!sink_health_ready(&h,1,199));
    /* Repeated reports with a stalled callback are not playback progress. */
    sink_health_sample(&h,102,1002,1701);
    sink_health_render(&h,3,5,1,1701);
    assert(!sink_health_ready(&h,1,1701));
    sink_health_render(&h,3,6,1,1701);
    assert(sink_health_ready(&h,1,1701));
    /* Stale heartbeat, then stale incoming media, both release the guard. */
    sink_health_sample(&h,103,1002,2702);
    sink_health_render(&h,3,7,1,2702);
    assert(!sink_health_ready(&h,1,2702));
    sink_health_sample(&h,103,1003,4203);
    sink_health_render(&h,3,8,1,4203);
    assert(!sink_health_ready(&h,1,4203));
    sink_health_sample(&h,104,1004,4204);
    assert(sink_health_ready(&h,1,4204));
    sink_health_render(&h,3,8,0,4205);
    assert(!sink_health_ready(&h,1,4205));
    /* STOP/new START must discard the old generation's health. */
    sink_health_reset(&h,4,104,1004);
    sink_health_render(&h,3,9,1,4206);
    assert(!sink_health_ready(&h,1,4206));
    sink_health_reset(&h,5,104,1004);
    sink_health_sample(&h,105,1005,4300);
    sink_health_render(&h,3,10,1,4300);
    assert(!sink_health_ready(&h,1,4300));
    sink_health_render(&h,5,UINT32_MAX,1,4300);
    assert(sink_health_ready(&h,1,4300));
    sink_health_render(&h,5,0,1,4400); /* enqueue counter wrap */
    assert(sink_health_ready(&h,1,4400));
    return 0;
}
''')

    def test_actual_idle_wrapper_only_keeps_the_configured_healthy_sink(self):
        source = (ROOT / "tweak/Hooks.xm").read_text()
        wrapper = source.split("typedef void (*idle_check_fn_t)", 1)[1].split("static void install_idle_guard", 1)[0]
        self.compile_run(r'''
#include <stdint.h>
#include <assert.h>
static unsigned g_relay_ready=1,g_sink_armed=1,g_sink_accepted=1,g_sink_stream_handle=1;
static uint64_t g_sink_owner=0x1234;
static int healthy=1,health_checks=0,native_calls=0;
static uint64_t last_owner,last_key;
static int AudioRelaySinkHealthy(void) {health_checks++;return healthy;}
static void SpeakaLogNow(const char *,...) {}
static void SpeakaSetStatus(const char *,const char *,...) {}
static void native_idle(uint64_t owner,uint64_t key) {native_calls++;last_owner=owner;last_key=key;}
''' + "typedef void (*idle_check_fn_t)" + wrapper + r'''
int main() {
    g_idle_orig=native_idle;
    hook_idle_check(0x1234,1);assert(native_calls==0 && health_checks==1);
    hook_idle_check(0x1234,2);assert(native_calls==1 && last_key==2 && health_checks==1);
    hook_idle_check(0x5678,1);assert(native_calls==2 && last_owner==0x5678 && health_checks==1);
    healthy=0;hook_idle_check(0x1234,1);assert(native_calls==3 && health_checks==2);
    healthy=1;g_sink_armed=0;hook_idle_check(0x1234,1);assert(native_calls==4 && health_checks==2);
    g_sink_armed=1;g_sink_accepted=0;hook_idle_check(0x1234,1);assert(native_calls==5);
    g_sink_accepted=1;g_relay_ready=0;hook_idle_check(0x1234,1);assert(native_calls==6);
    g_relay_ready=1;g_sink_stream_handle=0;hook_idle_check(0x1234,0);assert(native_calls==7);
    return 0;
}
''', cpp=True)

    def test_exact_binary_gates(self):
        path = os.environ.get("BTPS_BLUETOOTHD")
        if not path:
            self.skipTest("Set BTPS_BLUETOOTHD to the verified iOS 17.0 device binary")
        data = pathlib.Path(path).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), "e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6")
        source = (ROOT / "tweak/Hooks.xm").read_text()
        block = source.split("IDLE_CHECK_GATES[] = {", 1)[1].split("};", 1)[0]
        gates = re.findall(r"\{(0x[0-9a-f]+)u,\{([^}]+)\}\}", block)
        self.assertEqual(len(gates), 7)
        for offset, words in gates:
            expected = tuple(int(w.rstrip("u"), 16) for w in words.split(","))
            self.assertEqual(struct.unpack_from("<4I", data, int(offset, 16)), expected, offset)
        # Verify this is a full function, not the adjacent 8-byte predicate.
        self.assertIn("IDLE_CHECK_ENTRY 0x00376034ul", source)

    def test_daemon_lease_expires_and_stop_invalidates_it(self):
        # Include the complete daemon source to compile the production lease API
        # and STOP logic. Its worker is never launched in this test.
        self.compile_run(r'''
#include <assert.h>
#include "tweak/AudioRelay.c"
void SpeakaLogNow(const char *fmt,...) {(void)fmt;}
void audio_ring_init(AudioRing *s,uint32_t pid,uint64_t token) {(void)s;(void)pid;(void)token;}
int audio_ring_enqueue(AudioRing *s,const AudioPacket *p) {(void)s;(void)p;return 0;}
int audio_ring_pop(AudioRingQueue *q,void *slots,size_t stride,unsigned n,void *v,size_t bytes) {
    (void)q;(void)slots;(void)stride;(void)n;(void)v;(void)bytes;return 0;
}
void audio_ring_event(AudioRing *s,int h,AudioRingEvent e) {(void)s;(void)h;(void)e;}
int main(void) {
    AudioRing shared={0};g_shared=&shared;
    shared.generation=3;g_health_generation=3;g_health_deadline_ms=monotonic_ms()+10000;
    assert(AudioRelaySinkHealthy());
    g_health_deadline_ms=monotonic_ms()-1;assert(!AudioRelaySinkHealthy());
    g_health_deadline_ms=monotonic_ms()+10000;
    AudioRelaySetActive(0);assert(!(shared.generation&1) && !g_health_deadline_ms);
    assert(!AudioRelaySinkHealthy());
    /* Even a late publication from the worker's previous iteration is invalid. */
    g_health_deadline_ms=monotonic_ms()+10000;assert(!AudioRelaySinkHealthy());
    AudioRelaySetActive(1);assert(!AudioRelaySinkHealthy());
    g_health_deadline_ms=monotonic_ms()+10000;assert(!AudioRelaySinkHealthy());
    g_health_generation=shared.generation;assert(AudioRelaySinkHealthy());
    g_shared=NULL;assert(!AudioRelaySinkHealthy());
    return 0;
}
''')

    def test_native_signaling_and_media_are_not_suppressed(self):
        source = (ROOT / "tweak/Hooks.xm").read_text()
        tx = source.split("static uint64_t hook_transmit_signal(", 1)[1].split("static void install_signal_tx", 1)[0]
        self.assertNotIn("AudioRelaySinkHealthy", tx)
        self.assertIn("g_signal_tx_orig(cid, packet, length", tx)
        helper = (ROOT / "audiohelper/AudioHelper.m").read_text()
        self.assertIn("report_health(o,s,generation)", helper)
        self.assertIn("kAudioQueueProperty_IsRunning", helper.split("static void report_health(", 1)[1])
        daemon = (ROOT / "tweak/AudioRelay.c").read_text()
        forward = daemon.split("void AudioRelayForward(", 1)[1].split("static void drain_logs", 1)[0]
        for term in ("clock_gettime", "monotonic_ms", "AudioRelaySinkHealthy", "pthread_", "SpeakaLogLine"):
            self.assertNotIn(term, forward)


# ---- what the log records about the audio ----
# v8.9.3 diagnostics: a log export must cover the whole session and say where the audio went.
#
# The 21:06 export covered only the last 7.5 s of a 3-minute session because 94% of its 480 lines were
# per-packet success lines, and it could not show which output device the helper used.

def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


class RelayDiagnostics(unittest.TestCase):
    def compile_run(self, source):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        flags = ["-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT)]
        env = dict(os.environ)
        if os.environ.get("BTPS_SANITIZE"):
            flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            env.update(ASAN_OPTIONS="detect_leaks=0", UBSAN_OPTIONS="halt_on_error=1")
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "t.c"
            exe = pathlib.Path(td) / "t"
            src.write_text(source)
            built = subprocess.run([compiler, *flags, str(src), "-o", str(exe), "-pthread"],
                                   text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(exe)], text=True, capture_output=True, timeout=20, env=env)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_daemon_summarizes_packets_and_keeps_errors_visible(self):
        # Compiles the real daemon source with a fake event queue and a capturing logger.
        self.compile_run(r'''
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "tweak/AudioRelay.c"

static char lines[64][400];
static int nlines;
void SpeakaLogNow(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    if (nlines < 64) vsnprintf(lines[nlines++], sizeof lines[0], fmt, ap);
    va_end(ap);
}
void audio_ring_init(AudioRing *s, uint32_t pid, uint64_t token) { (void)s; (void)pid; (void)token; }
int audio_ring_enqueue(AudioRing *s, const AudioPacket *p) { (void)s; (void)p; return 0; }
void audio_ring_event(AudioRing *s, int h, AudioRingEvent e) { (void)s; (void)h; (void)e; }

static AudioRing shared;
static AudioRingEvent fwd[200], dec[200];
static unsigned nfwd, ndec, ifwd, idec;
int audio_ring_pop(AudioRingQueue *q, void *slots, size_t stride, unsigned n, void *v, size_t bytes) {
    (void)slots; (void)stride; (void)n;
    if (q == &shared.forward_log && ifwd < nfwd) { memcpy(v, &fwd[ifwd++], bytes); return 1; }
    if (q == &shared.decode_log && idec < ndec) { memcpy(v, &dec[idec++], bytes); return 1; }
    return 0;
}
static int count(const char *needle) {
    int c = 0;
    for (int i = 0; i < nlines; i++) if (strstr(lines[i], needle)) c++;
    return c;
}
static const char *find(const char *needle) {
    for (int i = 0; i < nlines; i++) if (strstr(lines[i], needle)) return lines[i];
    return "";
}

int main(void) {
    SinkHealth health = {0};
    for (int i = 0; i < 100; i++) fwd[nfwd++] = (AudioRingEvent){RING_EVENT_FORWARD, 953, 1, 0};
    for (int i = 0; i < 5; i++)   fwd[nfwd++] = (AudioRingEvent){RING_EVENT_FORWARD, 953, 1, -1};   /* after SUSPEND */
    for (int i = 0; i < 12; i++)  fwd[nfwd++] = (AudioRingEvent){RING_EVENT_FORWARD, 4000, 1, -5};  /* abnormal */
    for (int i = 0; i < 100; i++) dec[ndec++] = (AudioRingEvent){RING_EVENT_DECODE, 953, 8, 0};
    dec[ndec++] = (AudioRingEvent){RING_EVENT_DECODE, 500, 0, 1};       /* fragment pending: normal */
    dec[ndec++] = (AudioRingEvent){RING_EVENT_DECODE, 953, 0, -3};      /* malformed: abnormal */
    dec[ndec++] = (AudioRingEvent){RING_EVENT_PCM, 9730, 92416, 92266};
    dec[ndec++] = (AudioRingEvent){RING_EVENT_RENDER, 8320, 48512, 111};

    relay_flush_tally(1000, 0);                  /* opens the window */
    drain_logs(&shared, &health, 1000);
    relay_flush_tally(1500, 0);
    assert(count("V893_RX") == 0);              /* still inside the window */
    relay_flush_tally(3000, 0);
    assert(count("V893_RX") == 1);
    assert(count("result=0") == 0);             /* no per-packet success lines */
    assert(count("V88_FORWARD") + count("V88_DECODE") == 8);   /* abnormal ones, capped per window */
    assert(strstr(find("V893_RX"), "forwarded=117 ok=100 inactive=5 other=12"));
    assert(strstr(find("V893_RX"), "decoded=102 ok=100 partial=1 bad=1 frames=800"));
    assert(strstr(find("V893_RX"), "held_back=5"));             /* 13 abnormal, 8 shown */
    assert(count("V881_PCM samples=92416 nonzero=92266 peak=9730 | RENDER callbacks=111 pcm_frames=48512 "
                 "silence_frames=8320") == 1);

    nlines = 0;
    relay_flush_tally(6000, 0);
    assert(nlines == 0);                        /* an empty window writes nothing */
    fwd[nfwd++] = (AudioRingEvent){RING_EVENT_FORWARD, 953, 1, 0};
    drain_logs(&shared, &health, 6100);
    relay_flush_tally(6200, 1);                  /* forced, e.g. right before a stream state change */
    assert(count("V893_RX") == 1 && strstr(find("V893_RX"), "forwarded=1 ok=1 "));
    return 0;
}
''')

    def test_worker_summarizes_before_logging_stream_changes(self):
        worker = read("tweak/AudioRelay.c").split("static void *worker(void *arg) {", 1)[1].split("int AudioRelayPrepare(void)", 1)[0]
        flush = worker.index("relay_flush_tally(now,generation!=last_generation);")
        self.assertLess(worker.index("drain_logs(s,&health,now);"), flush)
        self.assertLess(flush, worker.index('"V88_STREAM active=%u generation=%llu"'))
        drain = read("tweak/AudioRelay.c").split("static void drain_logs(", 1)[1].split("static int attach_helper(", 1)[0]
        self.assertNotIn('"V88_FORWARD bytes=%u handle=%u result=%d",e.bytes,e.frames,e.result);\n    }', drain)
        self.assertIn("if(relay_detail_allowed()) SpeakaLogNow(\"V88_FORWARD", drain)
        self.assertIn("if(relay_detail_allowed()) SpeakaLogNow(\"V88_DECODE", drain)

    def test_settings_export_includes_helper_status_and_log(self):
        launchd = plistlib.loads((ROOT / "layout/Library/LaunchDaemons/com.parv.speaka.audio.plist").read_bytes())
        path = re.search(r'#define SPEAKA_HELPER_LOG_FILE\s+"([^"]+)"', read("shared/SpeakaShared.h")).group(1)
        self.assertEqual(path, launchd["StandardOutPath"])
        reader = read("settings/DebugLog.m")
        full = reader.split("- (NSString *)fullText", 1)[1]
        self.assertLess(full.index("[self appendHelperSection:out];"), full.index("== WHAT THIS SCREEN COULD READ =="))
        section = reader.split("- (void)appendHelperSection:(NSMutableString *)out", 1)[1].split("- (NSString *)fullText", 1)[0]
        for needle in ("CFSTR(SPEAKA_KEY_HELPER_STATUS)", "CFSTR(SPEAKA_KEY_OUTPUT_MODE)", "CFSTR(SPEAKA_KEY_NEVER_OUTPUTS)",
                       "ReadFileTail(@SPEAKA_HELPER_LOG_FILE", '@"V881_PCM"', 'h[@"volume"]'):
            self.assertIn(needle, section)
        tail = reader.split("static NSArray<NSString *> *ReadFileTail(", 1)[1].split("static NSString *HelperText(", 1)[0]
        self.assertIn("fseeko(f, start, SEEK_SET)", tail)          # reads the end, never the whole growing file
        self.assertIn("removeObjectAtIndex:0", tail)               # drops the partial first line

    def test_helper_lines_are_timestamped(self):
        helper = read("audiohelper/AudioHelper.m")
        self.assertNotIn("fprintf(stdout,", helper)
        self.assertNotIn("fprintf(stderr,", helper)
        self.assertLess(helper.index("static void hlog(FILE *stream,const char *fmt,...) {"), helper.index("#define PCM_CAPACITY"))
        self.assertIn("__attribute__((format(printf,2,3)))", helper)
        self.assertIn("localtime_r(&secs,&tm)", helper)
        self.assertGreaterEqual(helper.count("hlog(stdout,") + helper.count("hlog(stderr,"), 13)
        self.assertIn('@"volume":@(session.outputVolume)', helper)


# ---- the helper's playback callback ----
# Run the production PCM callback with a fake AudioQueue; routing needs iOS.

SOURCE = ROOT / "audiohelper/AudioHelper.m"


class HelperPlayback(unittest.TestCase):
    def test_render_silence_stop_race_and_pcm_levels(self):
        source = SOURCE.read_text()
        # Compile the actual C-compatible callback and level meter, not a copy.
        render = source.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        levels = source.split("typedef struct { uint32_t samples,nonzero,peak; } PCMLevels;", 1)[1].split("static void report_progress", 1)[0]
        harness = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
typedef int32_t OSStatus;
typedef void *AudioQueueRef;
typedef struct { void *mAudioData; uint32_t mAudioDataByteSize; } AudioQueueBuffer;
typedef AudioQueueBuffer *AudioQueueBufferRef;
static OSStatus AudioQueueEnqueueBuffer(AudioQueueRef,AudioQueueBufferRef,unsigned,const void *);
'''
        harness += "#define PCM_CAPACITY" + render
        harness += "typedef struct { uint32_t samples,nonzero,peak; } PCMLevels;" + levels
        harness += r'''
static unsigned enqueues;
static OSStatus enqueue_error;
static int stop_on_enqueue;
static OSStatus AudioQueueEnqueueBuffer(AudioQueueRef queue,AudioQueueBufferRef b,
                                      unsigned packets,const void *descriptions) {
    assert(b->mAudioDataByteSize==AUDIO_FRAMES*4u);
    assert(packets==0 && descriptions==NULL);
    enqueues++;
    if(stop_on_enqueue) __atomic_store_n(&((Output *)queue)->stopping,1,__ATOMIC_RELEASE);
    return enqueue_error;
}
static void init_output(Output *o) {
    memset(o,0,sizeof *o);
    assert(pthread_mutex_init(&o->lock,NULL)==0);
    enqueues=0;enqueue_error=0;stop_on_enqueue=0;
}
int main(void) {
    Output o;int16_t data[AUDIO_FRAMES*2];
    AudioQueueBuffer buffer={data,0};
    const int16_t expected[6]={INT16_MIN,0,123,-123,0,INT16_MAX};

    /* Stop suppresses both enqueue and PCM consumption. Then resume with a
     * wraparound read and a short buffer, preserving the exact PCM samples. */
    init_output(&o);o.stopping=1;o.read=PCM_CAPACITY-1;o.count=3;
    for(unsigned i=0;i<3;i++) {
        unsigned slot=(o.read+i)%PCM_CAPACITY;
        o.pcm[slot*2]=expected[i*2];o.pcm[slot*2+1]=expected[i*2+1];
    }
    for(unsigned i=0;i<AUDIO_FRAMES*2;i++) data[i]=42;
    render(&o,&o,&buffer);
    assert(enqueues==0 && o.count==3 && o.render_callbacks==0 && data[0]==42);
    o.stopping=0;render(&o,&o,&buffer);
    assert(enqueues==1 && o.count==0 && o.read==2);
    assert(o.render_tick==3);
    assert(memcmp(data,expected,sizeof expected)==0);
    for(unsigned i=6;i<AUDIO_FRAMES*2;i++) assert(data[i]==0);
    assert(o.render_callbacks==1 && o.submitted_frames==3 && o.submitted_nonzero==4);
    assert(o.silence_frames==AUDIO_FRAMES-3 && o.error==0);
    pthread_mutex_destroy(&o.lock);

    /* A busy decoder lock must produce silence without waiting or consuming. */
    init_output(&o);o.count=2;o.pcm[0]=999;
    assert(pthread_mutex_lock(&o.lock)==0);
    render(&o,&o,&buffer);
    assert(pthread_mutex_unlock(&o.lock)==0);
    assert(enqueues==1 && o.count==2 && o.read==0);
    assert(o.submitted_frames==0 && o.submitted_nonzero==0 && o.silence_frames==AUDIO_FRAMES);
    assert(o.render_tick==0);
    for(unsigned i=0;i<AUDIO_FRAMES*2;i++) assert(data[i]==0);
    pthread_mutex_destroy(&o.lock);

    /* A live enqueue error is reported; an error caused by an overlapping stop
     * is not carried into the next stream. Failed enqueue is not counted. */
    init_output(&o);enqueue_error=-66632;
    render(&o,&o,&buffer);
    assert(enqueues==1 && o.error==-66632 && o.silence_frames==0);
    assert(o.render_tick==0);
    pthread_mutex_destroy(&o.lock);
    init_output(&o);enqueue_error=-66632;stop_on_enqueue=1;
    render(&o,&o,&buffer);
    assert(enqueues==1 && o.stopping==1 && o.error==0 && o.silence_frames==0);
    assert(o.render_tick==0);
    render(&o,&o,&buffer);assert(enqueues==1);
    pthread_mutex_destroy(&o.lock);

    PCMLevels level={0};
    measure_pcm(&level,expected,3);
    assert(level.samples==6 && level.nonzero==4 && level.peak==32768);
    const int16_t zero[4]={0};measure_pcm(&level,zero,2);
    assert(level.samples==10 && level.nonzero==4 && level.peak==32768);
    level=(PCMLevels){0};measure_pcm(&level,zero,2);
    assert(level.samples==4 && level.nonzero==0 && level.peak==0);
    puts("v8.8.1 PCM render: wrap, underflow, busy lock, stop race and levels passed");
    return 0;
}
'''
        cc = shutil.which("cc")
        self.assertIsNotNone(cc)
        flags = ["-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread"]
        env = dict(os.environ)
        if os.environ.get("BTPS_SANITIZE"):
            flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            env["ASAN_OPTIONS"] = "detect_leaks=0"
            env["UBSAN_OPTIONS"] = "halt_on_error=1"
        with tempfile.TemporaryDirectory() as td:
            c = pathlib.Path(td) / "render.c"
            exe = pathlib.Path(td) / "render"
            c.write_text(harness)
            built = subprocess.run([cc, *flags, str(c), "-o", str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10, env=env)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            print(ran.stdout, end="")

    def test_jitter_buffer_primes_before_playing_and_reprimes_on_empty(self):
        # Part 1 (v9.9.1): the device log shows Windows ramping its A2DP transmit rate over ~25 s, sending ~24 frames/s
        # when 47 are needed; the old render callback started on the first frame and spliced silence between the sparse
        # real frames (audible stutter). The callback now waits for PCM_PRIME frames, then plays, and re-primes if the
        # buffer ever empties - so the gap is one clean short wait, not continuous choppiness. Compiles the real
        # callback, not a copy.
        source = SOURCE.read_text()
        render = source.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        harness = r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
typedef int32_t OSStatus;
typedef void *AudioQueueRef;
typedef struct { void *mAudioData; uint32_t mAudioDataByteSize; } AudioQueueBuffer;
typedef AudioQueueBuffer *AudioQueueBufferRef;
static OSStatus AudioQueueEnqueueBuffer(AudioQueueRef,AudioQueueBufferRef,unsigned,const void *);
#define PCM_CAPACITY""" + render + r"""
static OSStatus AudioQueueEnqueueBuffer(AudioQueueRef q,AudioQueueBufferRef b,unsigned p,const void *d) {
    (void)q;(void)p;(void)d;(void)b;return 0;
}
/* Fill n stereo frames of value v into the ring at the write end. */
static void fill(Output *o,unsigned n,int16_t v) {
    for(unsigned i=0;i<n;i++){ unsigned slot=(o->read+o->count)%PCM_CAPACITY; o->pcm[slot*2]=v;o->pcm[slot*2+1]=v;o->count++; }
}
int main(void) {
    Output o; int16_t data[AUDIO_FRAMES*2]; AudioQueueBuffer buffer={data,0};
    memset(&o,0,sizeof o); assert(pthread_mutex_init(&o.lock,NULL)==0);
    o.priming=1;                                   /* start_output sets this */

    /* Below the prime threshold: the callback emits silence and consumes nothing. */
    fill(&o,100,1000);
    for(unsigned i=0;i<AUDIO_FRAMES*2;i++) data[i]=7;
    render(&o,&o,&buffer);
    assert(o.priming==1 && o.count==100);          /* nothing drained */
    for(unsigned i=0;i<AUDIO_FRAMES*2;i++) assert(data[i]==0);   /* silence out */
    assert(o.submitted_frames==0 && o.silence_frames==AUDIO_FRAMES);

    /* Reaching the threshold ends priming; now it plays. */
    fill(&o,PCM_PRIME,2000);                        /* 100 + PCM_PRIME buffered */
    render(&o,&o,&buffer);
    assert(o.priming==0 && o.count==100+PCM_PRIME-AUDIO_FRAMES);
    assert(o.submitted_frames==AUDIO_FRAMES && data[0]==1000);   /* oldest frame first, in order */

    /* Drain it dry: the callback plays what's left, then the NEXT empty call re-primes. */
    unsigned guard=0;
    while(o.count>0){ render(&o,&o,&buffer); assert(++guard<10000); }
    uint64_t reprimes_before=o.reprimes;
    render(&o,&o,&buffer);                          /* empty now */
    assert(o.priming==1 && o.reprimes==reprimes_before+1);

    /* While re-priming, a trickle below the threshold stays silent (the real starvation case). */
    fill(&o,50,3000);
    render(&o,&o,&buffer);
    assert(o.priming==1 && o.count==50);
    /* And it recovers once enough is buffered again. */
    fill(&o,PCM_PRIME,3000);
    render(&o,&o,&buffer);
    assert(o.priming==0 && o.submitted_frames>0);

    pthread_mutex_destroy(&o.lock);
    puts("jitter buffer: primes, plays in order, re-primes on empty, holds through a trickle");
    return 0;
}
"""
        cc = shutil.which("cc"); self.assertIsNotNone(cc)
        flags = ["-std=gnu11","-O2","-Wall","-Wextra","-Werror","-pthread"]
        env = dict(os.environ)
        if os.environ.get("BTPS_SANITIZE"):
            flags += ["-fsanitize=address,undefined","-fno-omit-frame-pointer"]
            env["ASAN_OPTIONS"]="detect_leaks=0"; env["UBSAN_OPTIONS"]="halt_on_error=1"
        with tempfile.TemporaryDirectory() as td:
            c = pathlib.Path(td)/"jb.c"; exe = pathlib.Path(td)/"jb"; c.write_text(harness)
            built = subprocess.run([cc,*flags,str(c),"-o",str(exe)],capture_output=True,text=True,timeout=30)
            self.assertEqual(built.returncode,0,built.stdout+built.stderr)
            ran = subprocess.run([str(exe)],capture_output=True,text=True,timeout=10,env=env)
            self.assertEqual(ran.returncode,0,ran.stdout+ran.stderr)
            print(ran.stdout,end="")

    def test_startup_starvation_measured_packet_pattern(self):
        """The v9.8.89 startup (11:04:28-11:05:08), packet by packet: the arrival rates measured in each 2 s window of the
        device log (13-27 packets/s for 26 s, then ~47/s), one 1024-frame AAC frame per packet, contiguous - through the
        real render callback, the real PCM writers and the real input monitor. v9.9.1's behaviour (no input gate)
        must reproduce the device's fill/play/run-dry cycles (26 re-primes measured); v9.9.2 must hold the output
        instead of bursting during the starved window, play continuously once the input is at real time, and keep a
        healthy start's latency low."""
        source = SOURCE.read_text()
        render = source.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        writers = source.split("static void hold_pcm(", 1)[1].split("static void keep_output(", 1)[0]
        harness = r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
typedef int32_t OSStatus;
typedef void *AudioQueueRef;
typedef struct { void *mAudioData; uint32_t mAudioDataByteSize; } AudioQueueBuffer;
typedef AudioQueueBuffer *AudioQueueBufferRef;
static OSStatus AudioQueueEnqueueBuffer(AudioQueueRef q,AudioQueueBufferRef b,unsigned p,const void *d){(void)q;(void)b;(void)p;(void)d;return 0;}
#define PCM_CAPACITY""" + render + "static void hold_pcm(" + writers + r"""
#include "OutputPolicy.h"
/* Packets per second in each 2 s window of the device log, 11:04:30 to 11:05:08. */
static const double MEASURED[] = {21.5,25.8,23.5,24.8,13.9,13.4,13.5,24.5,26.2,26.4,26.5,26.3,26.9,
                                  44.9,46.4,47.1,46.5,47.2,46.9,47.0};
typedef struct { unsigned reprimes_slow, onsets_slow, underrun_after; long first_audio_after_ms, first_audio_ms;
                 uint64_t gated, silence_slow, audio_slow; } Result;
static Result run(int gate, const double *rates, unsigned windows, double healthy_from_s) {
    static Output o; memset(&o,0,sizeof o); pthread_mutex_init(&o.lock,NULL); o.priming=1;
    int16_t pcm[1024*2]; for (int i=0;i<2048;i++) pcm[i]=1000;
    int16_t data[AUDIO_FRAMES*2]; AudioQueueBuffer buf={data,0};
    input_rate_t in; input_rate_reset(&in,0,1);
    Result r={0,0,0,-1,-1,0,0,0};
    uint64_t total_us=(uint64_t)windows*2000000u, next_render=0, next_pkt=0; unsigned seed=12345; double sched=0;
    int was_audible=0; double healthy_us=healthy_from_s*1e6;
    for (uint64_t t=0;t<total_us;t+=1000) {                               /* 1 ms steps */
        unsigned w=(unsigned)(t/2000000u); double rate=rates[w<windows?w:windows-1];
        if (t>=next_pkt) {                                                  /* a packet arrives: decode, measure, write */
            int c=input_rate_add(&in,1024,48000,t/1000);
            if (gate && c) { if (c<0) o.priming=o.count<PCM_PRIME; o.input_starved=c>0; }
            if (gate && o.input_starved) hold_pcm(&o,pcm,1024); else write_pcm(&o,pcm,1024);
            /* The next packet is scheduled from this one's scheduled time (not from the 1 ms step it fired on),
             * so the long-run rate is exactly the measured one; the jitter is around that schedule. */
            seed=seed*1103515245u+12345u; long jitter=(long)((seed>>16)%7)-3;   /* +-3 ms */
            sched+=1e6/rate; next_pkt=(uint64_t)(sched+jitter*1000.0>0?sched+jitter*1000.0:0);
        }
        if (t%5000==0) {                                                    /* the main loop closes windows on time */
            int c=input_rate_add(&in,0,48000,t/1000);
            if (gate && c) { if (c<0) o.priming=o.count<PCM_PRIME; o.input_starved=c>0; }
        }
        if (t>=next_render) {                                               /* 512 frames at 48 kHz */
            uint64_t before=o.submitted_frames, und=o.silence_underrun; unsigned rep=(unsigned)o.reprimes;
            render(&o,&o,&buf);
            int audible=o.submitted_frames>before;
            int slow=t<(uint64_t)healthy_us;
            if (slow) { if (audible && !was_audible) r.onsets_slow++; r.reprimes_slow+=(unsigned)o.reprimes-rep;
                        if (audible) r.audio_slow+=AUDIO_FRAMES; else r.silence_slow+=AUDIO_FRAMES; }
            if (audible && r.first_audio_ms<0) r.first_audio_ms=(long)(t/1000);
            if (!slow && audible && r.first_audio_after_ms<0) r.first_audio_after_ms=(long)((t-(uint64_t)healthy_us)/1000);
            if (!slow && t>(uint64_t)healthy_us+2000000u) r.underrun_after+=(unsigned)(o.silence_underrun-und);
            was_audible=audible;
            next_render+=10667;
        }
    }
    r.gated=o.silence_gated;
    pthread_mutex_destroy(&o.lock);
    return r;
}
int main(void) {
    /* The measured startup: starved for the first 26 s (13 windows), near real time after. */
    Result old=run(0,MEASURED,20,26.0), now=run(1,MEASURED,20,26.0);   /* from 26 s: 44.9/s = 96% of real time */
    printf("v9.9.1 (no gate): reprimes in the starved window=%u, audible bursts=%u\n",old.reprimes_slow,old.onsets_slow);
    printf("v9.9.2 (gate):    reprimes in the starved window=%u, audible bursts=%u, gated=%llu frames,"
           " audio %ld ms after the input reached real time, underruns after=%u\n",now.reprimes_slow,now.onsets_slow,
           (unsigned long long)now.gated,now.first_audio_after_ms,now.underrun_after);
    assert(old.reprimes_slow>=15 && old.reprimes_slow<=40);   /* the model reproduces the device's ~26 cycles */
    assert(old.onsets_slow>=15);
    assert(now.onsets_slow==0);    /* v9.9.4: the 400 ms cold verdict holds before the first sound - no 1 s blip */
    assert(now.gated>(uint64_t)48000*20);                       /* most of the starved window is a clean hold */
    assert(now.first_audio_after_ms>=0 && now.first_audio_after_ms<=1500);   /* plays soon after real time returns */
    assert(now.underrun_after==0);                              /* and then continuously */
    /* A healthy stream (a resume within a connection: 47/s from the first packet) must not be gated or delayed. */
    static const double HEALTHY[]={47.0,46.9,47.1,46.8,47.0};
    Result h=run(1,HEALTHY,5,0.0);
    printf("healthy start: first audio at %ld ms, gated=%llu frames, underruns=%u\n",h.first_audio_ms,
           (unsigned long long)h.gated,h.underrun_after);
    assert(h.first_audio_ms>=0 && h.first_audio_ms<=350);       /* the 0.25 s lead, not a measurement delay */
    assert(h.gated==0 && h.underrun_after==0);
    return 0;
}
"""
        cc = shutil.which("cc"); self.assertIsNotNone(cc)
        flags = ["-std=gnu11","-O2","-Wall","-Wextra","-Werror","-pthread","-I"+str(ROOT/"audiohelper"),"-I"+str(ROOT)]
        with tempfile.TemporaryDirectory() as td:
            c = pathlib.Path(td)/"startup.c"; exe = pathlib.Path(td)/"startup"; c.write_text(harness)
            built = subprocess.run([cc,*flags,str(c),"-o",str(exe)],capture_output=True,text=True,timeout=60)
            self.assertEqual(built.returncode,0,built.stdout+built.stderr)
            ran = subprocess.run([str(exe)],capture_output=True,text=True,timeout=60)
            self.assertEqual(ran.returncode,0,ran.stdout+ran.stderr)
            print(ran.stdout,end="")

    def test_gated_output_keeps_the_stream_lease_end_to_end(self):
        """The v9.8.90 failure, end to end: START, then input at ~44% of real time (12:05:55: 21397/48000), then real
        time, then the input stops. Through the real render callback, PCM writers and input monitor, the real
        health_holding decision, and the real lease (tweak/Tweak.h), with the helper reporting every 200 ms as it does.
        v9.8.90's report (holding only when paused) must reproduce the device: the lease lapses ~1.5 s after the gate
        engages. v9.9.3 must keep it while packets are received and decoded, keep it through recovery, and still let it
        lapse within the normal timeout once the input stops."""
        source = SOURCE.read_text()
        render = source.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        writers = source.split("static void hold_pcm(", 1)[1].split("static void keep_output(", 1)[0]
        harness = r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
typedef int32_t OSStatus;
typedef void *AudioQueueRef;
typedef struct { void *mAudioData; uint32_t mAudioDataByteSize; } AudioQueueBuffer;
typedef AudioQueueBuffer *AudioQueueBufferRef;
static OSStatus AudioQueueEnqueueBuffer(AudioQueueRef q,AudioQueueBufferRef b,unsigned p,const void *d){(void)q;(void)b;(void)p;(void)d;return 0;}
#define PCM_CAPACITY""" + render + "static void hold_pcm(" + writers + r"""
#include "OutputPolicy.h"
#include "tweak/Tweak.h"
typedef struct { long gated_at, first_unready_while_receiving, unready_after_stop; unsigned unready_reports_recovery;
                 uint64_t silence_gated; } R;
/* Phases: 0-1.5 s real time (the lease is established), 1.5-12 s starved at 44% of real time, 12-22 s real time,
 * 22-25 s nothing arrives (stream still active). */
static double rate_at(double s) { return s < 1.5 ? 46.875 : s < 12.0 ? 0.44 * 46.875 : s < 22.0 ? 46.875 : 0.0; }
static R run(int fixed) {
    static Output o; memset(&o,0,sizeof o); pthread_mutex_init(&o.lock,NULL); o.priming=1;
    int16_t pcm[2048]; for (int i=0;i<2048;i++) pcm[i]=1000;
    int16_t data[AUDIO_FRAMES*2]; AudioQueueBuffer buf={data,0};
    input_rate_t in; input_rate_reset(&in,0,1);
    SinkHealth h; sink_health_reset(&h,3,0,0);
    uint64_t media=0, beat=0; double sched=0; uint64_t next_pkt=0, next_render=0;
    R r={-1,-1,-1,0,0};
    for (uint64_t t=0; t<25000000; t+=1000) {
        double sec=t/1e6, rate=rate_at(sec);
        if (rate>0 && t>=next_pkt) {                       /* a packet: the tweak counts media, the helper decodes it */
            media++;
            o.decoded_frames+=1024;
            int c=input_rate_add(&in,1024,48000,t/1000);
            if (c) { if (c<0) o.priming=o.count<PCM_PRIME; o.input_starved=c>0; if (c>0 && r.gated_at<0) r.gated_at=(long)(t/1000); }
            if (o.input_starved) hold_pcm(&o,pcm,1024); else write_pcm(&o,pcm,1024);
            sched+=1e6/rate; next_pkt=(uint64_t)sched;
        }
        if (rate==0) next_pkt=t+1000, sched=(double)t;
        if (t%5000==0) {                                   /* the helper's loop: heartbeat, window close */
            beat++;
            int c=input_rate_add(&in,0,48000,t/1000);
            if (c) { if (c<0) o.priming=o.count<PCM_PRIME; o.input_starved=c>0; if (c>0 && r.gated_at<0) r.gated_at=(long)(t/1000); }
        }
        if (t>=next_render) { render(&o,&o,&buf); next_render+=10667; }
        if (t%200000==0 && t>0) {                          /* report_health every 200 ms -> the relay's lease */
            int holding = fixed ? health_holding(1,1,(int)o.paused,(int)o.input_starved) : (int)o.paused;
            uint32_t counter = holding ? o.decoded_frames : (uint32_t)o.render_tick;
            sink_health_sample(&h,media,beat,t/1000);
            sink_health_render(&h,3,counter,holding?2u:1u,t/1000);
            int ready=sink_health_ready(&h,1,t/1000);
            if (!ready && sec>1.6 && sec<12.0 && r.first_unready_while_receiving<0) r.first_unready_while_receiving=(long)(t/1000);
            if (!ready && sec>=12.5 && sec<22.0) r.unready_reports_recovery++;
            if (!ready && sec>=22.0 && r.unready_after_stop<0) r.unready_after_stop=(long)(t/1000)-22000;
        }
    }
    r.silence_gated=o.silence_gated;
    pthread_mutex_destroy(&o.lock);
    return r;
}
int main(void) {
    R old=run(0), now=run(1);
    printf("v9.8.90: gate at %ld ms, lease lapsed at %ld ms (%ld ms after the gate)\n", old.gated_at,
           old.first_unready_while_receiving, old.first_unready_while_receiving-old.gated_at);
    printf("v9.9.3:  gate at %ld ms, lapsed while receiving: %s, unready reports during recovery: %u,"
           " lapsed %ld ms after the input stopped, gated silence %llu frames\n", now.gated_at,
           now.first_unready_while_receiving<0?"never":"YES", now.unready_reports_recovery, now.unready_after_stop,
           (unsigned long long)now.silence_gated);
    assert(old.gated_at>0 && now.gated_at>0);                          /* the monitor sees ~44% as starved */
    long lag = old.first_unready_while_receiving - old.gated_at;
    assert(old.first_unready_while_receiving>0 && lag>=1000 && lag<=2200);   /* reproduces 12:05:55.870 -> 57.443 */
    assert(now.first_unready_while_receiving<0);                       /* A: healthy while receiving and decoding */
    assert(now.silence_gated>48000u*5u);                               /* ... with the output genuinely held */
    assert(now.unready_reports_recovery==0);                           /* C: no false timeout through recovery */
    assert(now.unready_after_stop>=0 && now.unready_after_stop<=1900); /* B: the input stopping still lapses it */
    return 0;
}
"""
        cc = shutil.which("cc"); self.assertIsNotNone(cc)
        flags = ["-std=gnu11","-O2","-Wall","-Wextra","-Werror","-pthread","-I"+str(ROOT/"audiohelper"),"-I"+str(ROOT)]
        with tempfile.TemporaryDirectory() as td:
            c = pathlib.Path(td)/"lease.c"; exe = pathlib.Path(td)/"lease"; c.write_text(harness)
            built = subprocess.run([cc,*flags,str(c),"-o",str(exe)],capture_output=True,text=True,timeout=60)
            self.assertEqual(built.returncode,0,built.stdout+built.stderr)
            ran = subprocess.run([str(exe)],capture_output=True,text=True,timeout=60)
            self.assertEqual(ran.returncode,0,ran.stdout+ran.stderr)
            print(ran.stdout,end="")

    def test_warm_pause_resume_latency_and_continuity(self):
        """Priority 2 (v9.9.4): Windows pause/resume on an established connection. Measured at 14:52:14 in v9.8.91: the
        first packet 54 ms after START, four silent 16-byte frames (~85 ms) first, packet gaps up to 38 ms. v9.8.91
        primed 0.25 s on every resume and played the silence; a warm resume primes 64 ms, drops the leading digital
        silence, and doubles its lead after an underrun. Ten cycles through the real render callback, PCM writers,
        keep_output and input monitor, with the measured gaps; one cycle carries a 70 ms gap."""
        source = SOURCE.read_text()
        render = source.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        writers = source.split("static void hold_pcm(", 1)[1].split("static void keep_output(", 1)[0]
        keep = source.split("static void keep_output(", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        harness = r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
typedef int32_t OSStatus;
typedef void *AudioQueueRef;
typedef struct { void *mAudioData; uint32_t mAudioDataByteSize; } AudioQueueBuffer;
typedef AudioQueueBuffer *AudioQueueBufferRef;
static OSStatus AudioQueueEnqueueBuffer(AudioQueueRef q,AudioQueueBufferRef b,unsigned p,const void *d){(void)q;(void)b;(void)p;(void)d;return 0;}
#define hlog(f,...) ((void)0)
#define PCM_CAPACITY""" + render + "static void hold_pcm(" + writers + "static void keep_output(" + keep + r"""
#include "OutputPolicy.h"
static const int GAPS[] = {18,17,27,28,19,19,19,32,24,0,38,7,25,31};   /* ms, v9.8.91 14:52:14 and 14:52:32 */
typedef struct { long latency_ms[10]; unsigned stale, underruns_healthy, underruns_jitter, gated, bumped; } R;
static R run(int v994) {
    static Output o; memset(&o,0,sizeof o); pthread_mutex_init(&o.lock,NULL); o.priming=1;
    int16_t pcm[2048], data[AUDIO_FRAMES*2]; AudioQueueBuffer buf={data,0};
    input_rate_t in; R r; memset(&r,0,sizeof r);
    uint64_t t=0, next_render=0; unsigned g=0;
    for (unsigned cycle=0; cycle<10; cycle++) {
        /* START: the helper's stream change, cold-or-warm (the connection is warm: it has streamed). */
        keep_output(&o,0); input_rate_reset(&in,t/1000,1);
        if (v994) { input_rate_warm(&in); pthread_mutex_lock(&o.lock); o.prime_target=PCM_PRIME_WARM; o.trim_silence=1; pthread_mutex_unlock(&o.lock); }
        o.input_starved=0;
        int16_t value=(int16_t)(100*(cycle+1));
        uint64_t first_audio_pkt=0, end=t+3000000u, next_pkt=t+54000u; unsigned pkt=0; long heard=-1;
        double sched=(double)next_pkt;
        uint64_t und_before=o.silence_underrun, reprimes_before=o.reprimes; unsigned jitter_cycle=cycle==6;
        while (t<end) {
            if (t>=next_pkt) {
                int silent = pkt<4;                                    /* Windows' four silent frames */
                for (int i=0;i<2048;i++) pcm[i]=silent?0:value;
                if (!silent && !first_audio_pkt) first_audio_pkt=t;
                int c=input_rate_add(&in,1024,48000,t/1000);
                if (c>0) r.gated++;
                int keepit=1;
                if (v994 && o.trim_silence) { int all=1; for (int i=0;i<2048 && all;i++) all=pcm[i]==0; if (all) keepit=0; else o.trim_silence=0; }
                if (keepit) write_pcm(&o,pcm,1024);
                /* The measured gaps (clumps of two included), scaled to a real-time mean: they average 21.71 ms,
                 * and a live stream averages 21.33 ms (1024 frames at 48 kHz). */
                int gap = (jitter_cycle && pkt==60) ? 70 : GAPS[(g++)%14];
                sched += gap * 1000.0 * (21333.3 / 21714.3);
                next_pkt = (uint64_t)sched;
                pkt++;
            }
            if (t>=next_render) {
                render(&o,&o,&buf);
                if (heard<0 && first_audio_pkt) for (unsigned i=0;i<AUDIO_FRAMES*2;i++) if (data[i]) {
                    heard=(long)((t-first_audio_pkt)/1000); if (data[i]!=value) r.stale++; break; }
                next_render+=10667;
            }
            t+=1000;
        }
        r.latency_ms[cycle]=heard;
        uint64_t und=o.silence_underrun-und_before;              /* frames of underrun silence */
        uint64_t events=o.reprimes-reprimes_before;             /* times the lead ran dry */
        if (jitter_cycle) r.underruns_jitter=(unsigned)events; else r.underruns_healthy+=(unsigned)und;
        if (v994 && jitter_cycle && o.prime_target>PCM_PRIME_WARM) r.bumped=1;
        /* Pause: SUSPEND - the stream change empties the ring; 1.5 s without packets, the output keeps running. */
        keep_output(&o,0); input_rate_reset(&in,t/1000,0); o.input_starved=0;
        for (uint64_t stop=t+1500000u; t<stop; t+=1000) if (t>=next_render) { render(&o,&o,&buf); next_render+=10667; }
    }
    pthread_mutex_destroy(&o.lock);
    return r;
}
static long median(const long *v) {
    long s[10]; memcpy(s,v,sizeof s);
    for (int i=0;i<10;i++)
        for (int j=i+1;j<10;j++)
            if (s[j]<s[i]) { long x=s[i]; s[i]=s[j]; s[j]=x; }
    return s[5];
}
int main(void) {
    R old=run(0), now=run(1);
    printf("v9.8.91 resume: median %ld ms from the first audio packet to sound; underruns %u; stale %u\n",
           median(old.latency_ms), old.underruns_healthy, old.stale);
    printf("v9.9.4  resume: median %ld ms; underrun frames (healthy cycles) %u; 70 ms gap cycle: %u underrun(s), lead bumped %u;"
           " stale %u; gated %u\n", median(now.latency_ms), now.underruns_healthy, now.underruns_jitter, now.bumped,
           now.stale, now.gated);
    for (int i=0;i<10;i++) assert(now.latency_ms[i]>=0 && old.latency_ms[i]>=0);
    assert(median(old.latency_ms) >= 150);        /* v9.8.91: the full 0.25 s lead (behind the silence) on every resume */
    assert(median(now.latency_ms) <= 100);        /* v9.9.4: tens of milliseconds */
    assert(now.stale == 0 && old.stale == 0);     /* the first sound after a resume is that resume's audio */
    assert(now.underruns_healthy == 0);           /* the measured jitter never drains the 64 ms lead */
    assert(now.underruns_jitter <= 1 && now.bumped);   /* a 70 ms gap costs at most one underrun, then the lead grows */
    assert(now.gated == 0);                       /* a healthy resume is never mistaken for starvation */
    return 0;
}
"""
        cc = shutil.which("cc"); self.assertIsNotNone(cc)
        # -Wno-unused-parameter: the hlog stub drops keep_output's generation argument; -Wno-unused-function: the
        # extracted writers include hold_pcm, which a healthy resume never needs.
        flags = ["-std=gnu11","-O2","-Wall","-Wextra","-Werror","-Wno-unused-parameter","-Wno-unused-function","-pthread","-I"+str(ROOT/"audiohelper"),"-I"+str(ROOT)]
        with tempfile.TemporaryDirectory() as td:
            c = pathlib.Path(td)/"resume.c"; exe = pathlib.Path(td)/"resume"; c.write_text(harness)
            built = subprocess.run([cc,*flags,str(c),"-o",str(exe)],capture_output=True,text=True,timeout=60)
            self.assertEqual(built.returncode,0,built.stdout+built.stderr)
            ran = subprocess.run([str(exe)],capture_output=True,text=True,timeout=60)
            self.assertEqual(ran.returncode,0,ran.stdout+ran.stderr)
            print(ran.stdout,end="")
        # The model's stream change is the helper's: same lead, same trim, same warm marking.
        helper = source
        loop = between(helper, "for(;;) {", "[[NSNotificationCenter defaultCenter] removeObserver:observer];")
        generation = between(loop, "if(current!=generation) {", "\n            }\n")
        for line in ("if(warm) input_rate_warm(&input);", "o->prime_target=warm?PCM_PRIME_WARM:PCM_PRIME;o->trim_silence=warm&&(current&1u);"):
            self.assertIn(line, generation)

    def test_speaker_route_and_teardown_guards(self):
        source = SOURCE.read_text()
        # The speaker path is unchanged since v8.8.1. v8.9.4 adds the only other PlayAndRecord configuration, wired
        # headphones mode, with no options; neither can reach Bluetooth or AirPlay, and nothing ever records.
        configure = source.split("static int configure_speaker(", 1)[1].split("static int configure_external(", 1)[0]
        self.assertIn("AVAudioSessionCategoryPlayAndRecord", configure)
        self.assertIn("output_session_options(1,(int)g_mix.mixing)", configure)   # v9.9.4: DefaultToSpeaker via the policy
        self.assertIn("#define SESSION_OPT_DEFAULT_TO_SPEAKER 0x8u", read("audiohelper/OutputPolicy.h"))
        self.assertIn("overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker", configure)
        self.assertIn("!speaker_selected(session)", configure)
        self.assertEqual(source.count("AVAudioSessionCategoryPlayAndRecord"), 2)
        wired = source.split("static int configure_wired(", 1)[1].split("static int configure_output(", 1)[0]
        self.assertIn("output_session_options(0,(int)g_mix.mixing)", wired)      # v9.9.4: mixing, no Bluetooth option
        for option in ("AllowBluetooth", "AllowBluetoothA2DP", "AllowAirPlay"):
            self.assertNotIn("AVAudioSessionCategoryOption" + option, source)
        for api in ("AudioQueueNewInput", "requestRecordPermission", "setPreferredInput", "inputNode", "AVAudioRecorder"):
            self.assertNotIn(api, source)
        # v8.9: the speaker remains the default branch of the output dispatcher.
        dispatch = source.split("static int configure_output(", 1)[1].split("static int start_output(", 1)[0]
        self.assertIn("g_output_mode==OUTPUT_MODE_SPEAKER?configure_speaker(rate,s)", dispatch)
        self.assertIn("static int g_output_mode=OUTPUT_MODE_SPEAKER;", source)
        start = source.split("static int start_output(", 1)[1].split("} PCMLevels;", 1)[0]
        self.assertLess(start.index("configure_output(rate,s)"), start.index("AudioQueueNewOutput("))
        self.assertLess(start.index("route_verdict(session,NULL)"), start.index("AudioQueueStart("))
        stop = source.split("static void stop_output(", 1)[1].split("static int speaker_selected(", 1)[0]
        self.assertLess(stop.index("&o->stopping,1"), stop.index("AudioQueueStop("))
        self.assertLess(stop.index("AudioQueueDispose("), stop.index("&o->error,0"))


# ---- headphones mode and route changes ----
# v8.9.4: headphones mode is wired/USB only, and the helper can see route changes and interruptions.
#
# The 21:39 log showed every headphones-mode start playing to `BluetoothA2DPOutput LAPTOP-UGIVBIA5`: iOS lists the
# sending PC as an A2DP output and, because it connected last, routes the Playback category to it. Leaving that route
# made the phone send SUSPEND, after which the PC closed the stream. No route change or interruption was ever handled
# in any session, although earbuds were replugged during playback.

HELPER = ROOT / "audiohelper/AudioHelper.m"


def between(text, start, end):
    return text.split(start, 1)[1].split(end, 1)[0]


class WiredOutput(unittest.TestCase):
    def compile_run(self, source):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        flags = ["-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT),
                 "-I" + str(ROOT / "audiohelper")]
        env = dict(os.environ)
        if os.environ.get("BTPS_SANITIZE"):
            flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            env.update(ASAN_OPTIONS="detect_leaks=0", UBSAN_OPTIONS="halt_on_error=1")
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "t.c"
            exe = pathlib.Path(td) / "t"
            src.write_text(source)
            built = subprocess.run([compiler, *flags, str(src), "-o", str(exe), "-pthread"],
                                   text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(exe)], text=True, capture_output=True, timeout=20, env=env)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_policy_wired_mode_never_accepts_bluetooth(self):
        self.compile_run(r'''
#include <assert.h>
#include <string.h>
#include "OutputPolicy.h"
static int check(int mode, output_kind_t kind, const char *name, const output_never_t *never, size_t n) {
    output_port_t port = {kind, "AA:BB:CC:DD:EE:FF-tacl", name};
    size_t bad = 99;
    return output_route_check(mode, &port, 1, never, n, &bad);
}
int main(void) {
    const output_never_t pc = {"", "laptop-ugivbia5"};          /* matched case-insensitively by name */
    /* Wired / USB only: the PC (or any Bluetooth device) is refused even when nothing is never-listed. */
    assert(check(OUTPUT_MODE_HEADPHONES, OUTPUT_PORT_HEADPHONES, "Headphones", NULL, 0) == OUTPUT_ROUTE_OK);
    assert(check(OUTPUT_MODE_HEADPHONES, OUTPUT_PORT_BLUETOOTH, "LAPTOP-UGIVBIA5", NULL, 0) == OUTPUT_ROUTE_NOT_HEADPHONES);
    assert(check(OUTPUT_MODE_HEADPHONES, OUTPUT_PORT_BLUETOOTH, "AirPods", NULL, 0) == OUTPUT_ROUTE_NOT_HEADPHONES);
    assert(check(OUTPUT_MODE_HEADPHONES, OUTPUT_PORT_RECEIVER, "Receiver", NULL, 0) == OUTPUT_ROUTE_NOT_HEADPHONES);
    assert(check(OUTPUT_MODE_HEADPHONES, OUTPUT_PORT_SPEAKER, "Speaker", NULL, 0) == OUTPUT_ROUTE_NOT_HEADPHONES);
    /* Any device still allows Bluetooth earbuds, and the never-play list still blocks the PC. */
    assert(check(OUTPUT_MODE_ANY, OUTPUT_PORT_BLUETOOTH, "AirPods", &pc, 1) == OUTPUT_ROUTE_OK);
    assert(check(OUTPUT_MODE_ANY, OUTPUT_PORT_BLUETOOTH, "LAPTOP-UGIVBIA5", &pc, 1) == OUTPUT_ROUTE_NEVER_LISTED);
    /* Speaker mode is unchanged. */
    assert(check(OUTPUT_MODE_SPEAKER, OUTPUT_PORT_SPEAKER, "Speaker", NULL, 0) == OUTPUT_ROUTE_OK);
    assert(check(OUTPUT_MODE_SPEAKER, OUTPUT_PORT_BLUETOOTH, "LAPTOP-UGIVBIA5", NULL, 0) == OUTPUT_ROUTE_NOT_SPEAKER);
    assert(strstr(output_route_reason(OUTPUT_ROUTE_NOT_HEADPHONES), "wired"));
    /* The stored value is unchanged, so an existing "headphones" choice becomes wired mode. */
    assert(output_mode_from_string("headphones") == OUTPUT_MODE_HEADPHONES);
    return 0;
}
''')

    def test_helper_classifies_bluetooth_separately(self):
        kinds = between(read("audiohelper/AudioHelper.m"), "static output_kind_t port_kind(", "static NSString *text_field(")
        wired = kinds.split("return OUTPUT_PORT_HEADPHONES;", 1)[0]
        self.assertIn("AVAudioSessionPortHeadphones", wired)
        self.assertIn("AVAudioSessionPortUSBAudio", wired)
        self.assertNotIn("Bluetooth", wired.split("AVAudioSessionPortBuiltInReceiver", 1)[1])
        bluetooth = kinds.split("return OUTPUT_PORT_HEADPHONES;", 1)[1].split("return OUTPUT_PORT_BLUETOOTH;", 1)[0]
        for port in ("BluetoothA2DP", "BluetoothLE", "BluetoothHFP"):
            self.assertIn("AVAudioSessionPort" + port, bluetooth)

    def test_wired_mode_cannot_reach_bluetooth_and_beats_a_later_pc(self):
        helper = read("audiohelper/AudioHelper.m")
        wired = between(helper, "static int configure_wired(", "static int configure_output(")
        # v9.9.4: options come from the policy (mixing, never Bluetooth); PlayAndRecord without them cannot reach the PC.
        category = wired.index("setCategory:AVAudioSessionCategoryPlayAndRecord mode:AVAudioSessionModeDefault\n"
                               "                         options:(AVAudioSessionCategoryOptions)output_session_options(0,(int)g_mix.mixing)")
        override = wired.index("overrideOutputAudioPort:AVAudioSessionPortOverrideNone")
        precheck = wired.index("if(verdict && builtin_only(session))")
        activate = wired.index("setActive:YES")
        verify = wired.index("for(unsigned i=0;i<20 && route_verdict(session,NULL);i++)")
        self.assertLess(category, override)
        self.assertLess(override, precheck)
        self.assertLess(precheck, activate)
        self.assertLess(activate, verify)
        self.assertNotIn("DefaultToSpeaker", wired)            # never falls back to the loudspeaker
        self.assertIn("AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation", wired.split("note_refusal(session,\"configured\"", 1)[0])
        only = between(helper, "static int builtin_only(", "static int configure_wired(")
        self.assertIn("if(!outputs.count) return 0;", only)  # an empty candidate is not conclusive
        self.assertIn("kind!=OUTPUT_PORT_SPEAKER && kind!=OUTPUT_PORT_RECEIVER", only)
        dispatch = between(helper, "static int configure_output(", "static int start_output(")
        self.assertLess(dispatch.index("if(g_output_mode==OUTPUT_MODE_HEADPHONES) return configure_wired(rate,s);"),
                        dispatch.index("configure_external(rate,s)"))
        external = between(helper, "static int configure_external(", "static int builtin_only(")
        self.assertIn("AVAudioSessionCategoryPlayback", external)  # "any device" keeps following iOS

    def test_route_changes_and_interruptions_can_be_delivered(self):
        helper = read("audiohelper/AudioHelper.m")
        loop = between(helper, "__block int interruption=0,route_changed=0",
                       "[[NSNotificationCenter defaultCenter] removeObserver:observer];")
        drain = loop.index("CFRunLoopRunInMode(kCFRunLoopDefaultMode,0,true)")
        self.assertLess(drain, loop.index("usleep(5000);"))
        self.assertGreater(drain, loop.index("for(;;) {"))
        registration = helper.split("__block int interruption=0,route_changed=0", 1)[1].split("for(;;) {", 1)[0]
        # handlers run on the posting thread, whichever it is. v9.1 adds the third: media services were reset.
        self.assertEqual(registration.count("queue:nil"), 3)
        self.assertNotIn("dispatch_get_main_queue", helper)   # nothing else is waiting to run on the main thread

    def test_an_interruption_pauses_in_place_and_resumes_by_the_rules(self):
        # v9.9.2: the queue is paused, not disposed; the PCM and decoder are kept; resuming is decided only by the
        # interruption rules (ended, the other audio stopping, or the takeover), and resumes the same queue.
        helper = read("audiohelper/AudioHelper.m")
        loop = helper.split("for(;;) {", 1)[1].split("[[NSNotificationCenter defaultCenter] removeObserver:observer];", 1)[0]
        began = loop.split("if(changed==1) {", 1)[1].split("} else {", 1)[0]
        self.assertIn('pause_output(o,s,"interruption_began");', began)
        self.assertNotIn("stop_output(", began)                                 # v9.9.1 disposed the queue here
        self.assertLess(began.index('note_session(0,"interrupted")'), began.index("own_interrupted(&own,now);"))
        ended = loop.split("} else {", 1)[1].split("\n            }\n", 1)[0]
        self.assertIn("own_interruption_cleared(&own,now);", ended)
        # The other app's audio stopping clears it: quiet for 0.5 s, at least 0.5 s after it began.
        self.assertIn("[[AVAudioSession sharedInstance] isOtherAudioPlaying]", loop)
        self.assertIn("now-other_quiet_since>=500u && now-own.interrupted_ms>=500u", loop)
        # Resuming reuses the paused queue; a rebuild only when it can't be resumed or the rate changed.
        pause = between(helper, "static void pause_output(", "\n}\n")
        self.assertNotIn("AudioQueueDispose", pause)
        resume = between(helper, "static int resume_output(", "\n}\n")
        self.assertIn("AudioQueueStart(o->queue,NULL)", resume)
        self.assertNotIn("AudioQueueNewOutput", resume)
        self.assertIn("if(started==-4 || started==-3) {", loop)                 # activation refused (-1): keep paused
        # A paused queue is not a running output anywhere in the loop.
        for site in ("if(o->queue && !o->paused) {\n                    log_route(", "if(o->queue && !o->paused && o->rate!=rate) {",
                     "else if(o->queue && !o->paused && !own.running) own_started(&own,now);",
                     "if((!o->queue || o->paused) && own_may_start(&own,now)) {"):
            self.assertIn(site, loop)
        self.assertIn("if(!(generation&1u) || packet.generation!=generation) continue;", loop)   # packets keep decoding
        # The comment says what is actually kept (v9.9.1's said "the buffered PCM" while the ring was cleared).
        self.assertIn("What is kept: the", began)

    def test_interruption_policy_against_the_v9_8_89_timeline(self):
        # 11:05:31.050 an app takes the audio and keeps playing. v9.9.1 waited 8.8 s; v9.9.2 takes it back at 3 s, never
        # lets a START or route change resume early, resumes at once when the app stops, and backs off a fight.
        out = OutputOwnership.compile_run(self, r"""
#include <assert.h>
#include "OutputPolicy.h"
#include "shared/AudioRing.h"
int main(void) {
    own_state_t h; own_init(&h); own_started(&h,0);
    own_interrupted(&h,10000);                                 /* the app takes the audio */
    own_stream(&h,AUDIO_STREAM_STREAMING,10500);               /* a START mid-interruption ... */
    own_now(&h,10600);                                         /* ... or a route change */
    assert(!own_may_start(&h,10600));                          /* neither resumes it (v9.9.1: both did) */
    assert(!own_may_start(&h,10000+OWN_TAKEOVER_MS-1) && own_may_start(&h,10000+OWN_TAKEOVER_MS));
    own_resumed(&h,13000);                                     /* takeover at 3 s, not 8.8 s */
    /* The app takes it straight back, twice: each takeover waits twice as long. */
    own_interrupted(&h,14000); assert(h.fights==1 && !own_may_start(&h,14000+2*OWN_TAKEOVER_MS-1));
    own_resumed(&h,20000);
    own_interrupted(&h,21000); assert(h.fights==2 && !own_may_start(&h,21000+4*OWN_TAKEOVER_MS-1));
    assert(own_may_start(&h,21000+4*OWN_TAKEOVER_MS));
    /* The app stops: resume at once, and the fight count resets after a quiet spell. */
    own_interruption_cleared(&h,22000); assert(own_may_start(&h,22000));
    own_resumed(&h,22000);
    own_interrupted(&h,60000); assert(h.fights==0);
    /* A call: activation fails, the interruption stays, attempts back off. */
    own_interruption_cleared(&h,61000); own_start_failed(&h,61000,0);
    assert(h.interrupted && !own_may_start(&h,61999) && own_may_start(&h,62000));
    /* A refused route (headphones gone) ends the interruption, so plugging them back in resumes. */
    own_start_failed(&h,62000,1); assert(!h.interrupted && !own_may_start(&h,999999));
    own_now(&h,70000); assert(own_may_start(&h,70000));
    return 0;
}
""")
        self.assertEqual(out, "")

    def test_health_reasons_reach_the_daemon_log(self):
        health = between(read("audiohelper/AudioHelper.m"), "static void report_health(", "static int read_exact(")
        for bit, cause in (("0x1u", "generation&1u"), ("0x4u", "!running"), ("0x8u", "&o->stopping"),
                           ("0x10u", "&o->error"), ("0x20u", "route_verdict(")):
            line = next(l for l in health.splitlines() if "why|=" + bit in l)
            self.assertIn(cause, line)
        # Compiled: a negative result is "not playing" with reasons, 1 is playing.
        self.compile_run(r'''
#include <assert.h>
#include <stdarg.h>
#include "tweak/AudioRelay.c"
void SpeakaLogNow(const char *fmt, ...) { (void)fmt; }
void audio_ring_init(AudioRing *s, uint32_t pid, uint64_t token) { (void)s; (void)pid; (void)token; }
int audio_ring_enqueue(AudioRing *s, const AudioPacket *p) { (void)s; (void)p; return 0; }
void audio_ring_event(AudioRing *s, int h, AudioRingEvent e) { (void)s; (void)h; (void)e; }
static AudioRing shared;
static AudioRingEvent queued[4]; static unsigned nq, iq;
int audio_ring_pop(AudioRingQueue *q, void *slots, size_t stride, unsigned n, void *v, size_t bytes) {
    (void)slots; (void)stride; (void)n;
    if (q == &shared.decode_log && iq < nq) { memcpy(v, &queued[iq++], bytes); return 1; }
    return 0;
}
int main(void) {
    SinkHealth health = {0};
    health.generation = 7;                                       /* an active stream generation */
    queued[nq++] = (AudioRingEvent){RING_EVENT_HEALTH, 7, 100, -0x20};
    drain_logs(&shared, &health, 1000);
    assert(health.playing == 0 && g_relay_health_why == 0x20u);
    queued[nq++] = (AudioRingEvent){RING_EVENT_HEALTH, 7, 101, 1};
    drain_logs(&shared, &health, 1100);
    assert(health.playing == 1 && g_relay_health_why == 0u);
    return 0;
}
''')
        daemon = read("tweak/AudioRelay.c")
        self.assertIn("why=0x%x", daemon)

    def test_settings_and_version(self):
        # v9.7: the output is chosen in the app (Settings no longer has Play audio on).
        client = read("app/Speaka/Model/SPTweakClient.m")
        modes = client.split("+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes", 1)[1].split("];", 1)[0]
        self.assertEqual(re.findall(r'@"key": @"([^"]+)"', modes), ["speaker", "headphones", "any"])
        self.assertEqual(re.findall(r'@"title": @"([^"]+)"', modes)[1], "Wired headphones or earbuds")
        self.assertEqual(re.findall(r'@"shortTitle": @"([^"]+)"', modes)[1], "Wired headphones")
        # v8.9.4 or any later build (v9.0 renamed everything to Speaka).
        for pattern, text in ((r'#define SESSION_BUILD_TAG "v(\d+)\.(\d+)\.(\d+)-one-tap-output-select"', read("tweak/Hooks.xm")),
                              (r"V889_HELPER build=v(\d+)\.(\d+)\.(\d+)-output-select", read("audiohelper/AudioHelper.m"))):
            version = re.search(pattern, text)
            self.assertIsNotNone(version, pattern)
            self.assertGreaterEqual(tuple(map(int, version.groups())), (8, 9, 4))


# ---- owning the output for the whole Speaka session ----
# v9.9: the v9.8.86 device test. iOS treats the sending PC as a Bluetooth output, so Apple's A2DP code suspended the
# PC's stream whenever the helper activated its session and started it whenever the helper let go. v9.1 let go after
# 8 s without media, so every pause longer than that looped (13 phone STARTs and 13 phone SUSPENDs in two minutes).
# The helper now owns the output from its connection until the session ends. Policy: audiohelper/OutputPolicy.h.

class OutputOwnership(unittest.TestCase):
    def compile_run(self, source, cpp=False):
        compiler = shutil.which("c++" if cpp else "cc")
        self.assertIsNotNone(compiler)
        flags = ["-std=c++17" if cpp else "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                 "-I" + str(ROOT), "-I" + str(ROOT / "audiohelper")]
        env = dict(os.environ)
        if os.environ.get("BTPS_SANITIZE"):
            flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            env.update(ASAN_OPTIONS="detect_leaks=0", UBSAN_OPTIONS="halt_on_error=1")
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / ("t.cpp" if cpp else "t.c")
            exe = pathlib.Path(td) / "t"
            src.write_text(source)
            built = subprocess.run([compiler, *flags, str(src), "-o", str(exe), "-pthread"],
                                   text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            return ran.stdout

    def test_policy(self):
        self.compile_run(r'''
#include <assert.h>
#include "OutputPolicy.h"
#include "shared/AudioRing.h"
int main(void) {
    own_state_t h; own_init(&h);
    assert(own_may_start(&h, 0));                      /* connected: start at once, before any packet */
    own_started(&h, 0);
    assert(!own_may_start(&h, 0));
    own_stream(&h, AUDIO_STREAM_STREAMING, 1000);
    own_stream(&h, AUDIO_STREAM_PAUSED, 10000);        /* Windows paused ... */
    for (uint64_t t = 10000; t < 10000 + 3600000; t += 5000)
        assert(h.running && !own_should_release(&h, t));   /* ... for an hour: never let go for lack of media */
    /* Another app takes the output: take it back shortly. */
    own_taken(&h, 20000);
    assert(!own_may_start(&h, 20000 + OWN_RETAKE_MS - 1) && own_may_start(&h, 20000 + OWN_RETAKE_MS));
    own_started(&h, 20300);
    /* Failures back off 1 s, 2 s, 3 s, 4 s, 4 s ... */
    uint64_t t = 30000;
    unsigned want[] = {1000, 2000, 3000, 4000, 4000};
    for (unsigned i = 0; i < 5; i++) {
        own_start_failed(&h, t, 0);
        assert(!own_may_start(&h, t + want[i] - 1) && own_may_start(&h, t + want[i]));
        t += want[i];
    }
    own_now(&h, t);                                     /* an interruption ended: at once, backoff forgotten */
    assert(own_may_start(&h, t) && h.failures == 0);
    /* Headphones mode with none plugged in waits for a device change instead of toggling the session. */
    own_start_failed(&h, t, 1);
    assert(!own_may_start(&h, t + 3600000));
    own_now(&h, t + 5000);
    assert(own_may_start(&h, t + 5000));
    own_started(&h, t + 5000);
    /* The PC closes its stream: kept for a reopen, let go after the grace, owned again on a new configuration. */
    uint64_t c = t + 10000;
    own_stream(&h, AUDIO_STREAM_CLOSED, c);
    assert(!own_should_release(&h, c + OWN_CLOSED_RELEASE_MS - 1) && own_should_release(&h, c + OWN_CLOSED_RELEASE_MS));
    own_stream(&h, AUDIO_STREAM_CONFIGURED, c + 5000);  /* reopened within the grace: kept */
    assert(!own_should_release(&h, c + 10 * OWN_CLOSED_RELEASE_MS));
    own_stream(&h, AUDIO_STREAM_CLOSED, c + 6000);
    own_released(&h);
    assert(!own_may_start(&h, c + 100000));             /* released: nothing restarts by itself */
    own_stream(&h, AUDIO_STREAM_CONFIGURED, c + 200000);
    assert(own_may_start(&h, c + 200000));              /* the PC configured the speaker again: own it again */
    return 0;
}
''')

    def test_pause_resume_model_v91_versus_v99(self):
        """A model of the mechanism the v9.8.86 log shows, not of iOS itself: activating the helper's session while the
        PC streams makes the phone SUSPEND the stream ~130 ms later; letting go while the stream is open (paused) makes
        the phone START it ~130 ms later. The PC connects at 0 s, plays from 1 s, pauses at 10 s, resumes at 40 s.
        v9.1 (8 s hold, 3 s rearm, audible-only starts) loops; v9.9 (owned from the connection) never toggles."""
        out = self.compile_run(r'''
#include <assert.h>
#include <stdio.h>
#include "OutputPolicy.h"
#include "shared/AudioRing.h"
enum { OPEN_PAUSED, STREAMING };
typedef struct {
    int v99, stream, session;
    uint64_t t, phone_suspend_at, phone_start_at, media_ms, rearm_ms, resumed_at, first_audio_after_resume;
    unsigned phone_toggles;
    own_state_t own;
} W;
static void activate(W *w) {
    w->session = 1;
    if (w->stream == STREAMING) w->phone_suspend_at = w->t + 130;   /* the route moved to the speaker */
}
static void release_session(W *w) {
    w->session = 0;
    if (w->stream == OPEN_PAUSED) w->phone_start_at = w->t + 130;   /* the route moved back to the "PC output" */
}
static int run(int v99) {
    W w = {0}; w.v99 = v99; w.stream = OPEN_PAUSED; own_init(&w.own);
    for (w.t = 0; w.t < 60000; w.t += 10) {
        /* The PC. */
        if (w.t == 1000 && w.stream == OPEN_PAUSED) w.stream = STREAMING;
        if (w.t == 10000 && w.stream == STREAMING) w.stream = OPEN_PAUSED;     /* Windows paused */
        if (w.t == 40000) { w.resumed_at = w.t; if (w.stream == OPEN_PAUSED) w.stream = STREAMING; }
        /* Apple's A2DP code, reacting to the helper's session. */
        if (w.phone_suspend_at && w.t >= w.phone_suspend_at) {
            w.phone_suspend_at = 0;
            if (w.stream == STREAMING) { w.stream = OPEN_PAUSED; if (w.t > 10000) w.phone_toggles++; }
        }
        if (w.phone_start_at && w.t >= w.phone_start_at) {
            w.phone_start_at = 0;
            if (w.stream == OPEN_PAUSED) { w.stream = STREAMING; if (w.t > 10000) w.phone_toggles++; }
        }
        int media = w.stream == STREAMING;
        /* The helper. */
        if (v99) {
            if (!w.session && own_may_start(&w.own, w.t)) { activate(&w); own_started(&w.own, w.t); }
        } else {
            if (media) {
                if (!w.session && (!w.rearm_ms || w.t >= w.rearm_ms)) activate(&w);   /* audible media, rearm over */
                if (w.session) w.media_ms = w.t;
            }
            if (w.session && w.t - w.media_ms >= 8000) { release_session(&w); w.rearm_ms = w.t + 3000; }
        }
        if (w.resumed_at && !w.first_audio_after_resume && media && w.session) w.first_audio_after_resume = w.t;
    }
    printf("%s phone_toggles_after_the_pause=%u audio_after_resume_ms=%lld\\n", v99 ? "v9.9" : "v9.1", w.phone_toggles,
           w.first_audio_after_resume ? (long long)(w.first_audio_after_resume - w.resumed_at) : -1ll);
    return (int)w.phone_toggles * 1000000 + (w.first_audio_after_resume ? (int)(w.first_audio_after_resume - w.resumed_at) : 999999);
}
int main(void) {
    int old = run(0), now = run(1);
    assert(old / 1000000 >= 2);                 /* v9.1: Apple's A2DP code keeps toggling the paused stream */
    assert(now / 1000000 == 0);                 /* v9.9: not once */
    assert(now % 1000000 <= 500);               /* v9.9: audio within half a second of the PC resuming */
    return 0;
}
''')
        self.assertIn("v9.9 phone_toggles_after_the_pause=0", out)

    def test_helper_wiring(self):
        helper = read("audiohelper/AudioHelper.m")
        self.assertIn('#include "OutputPolicy.h"', helper)
        loop = between(helper, "for(;;) {", "[[NSNotificationCenter defaultCenter] removeObserver:observer];")
        # Owned from the connection: the start is the loop's own, before the packet drain, not a packet's.
        owner = loop.index("if((!o->queue || o->paused) && own_may_start(&own,now)) {")
        self.assertLess(owner, loop.index("for(unsigned i=0;i<AUDIO_RING_SLOTS && audio_ring_dequeue(s,&packet);i++) {"))
        self.assertIn("} else started=start_output(o,rate_hint,s);", loop)
        packets = loop.split("for(unsigned i=0;i<AUDIO_RING_SLOTS && audio_ring_dequeue(s,&packet);i++) {", 1)[1]
        self.assertEqual(packets.count("start_output("), 1)                    # only the sample-rate rebuild
        self.assertIn('stop_output(o,s,"sample_rate_changed",0);', packets)   # a new rate keeps the session
        self.assertIn("else if(keep && o->queue) write_pcm(o,pcm,samples);", packets)
        # Never released for lack of media; a pause keeps everything; a close lets go after the grace.
        # The v9.1 hold policy is gone (hold_pcm, v9.9.2's held-output writer, is unrelated).
        for gone in ("hold_expired", "hold_may_start", "hold_started", "hold_released", "hold_media", "HOLD_",
                     "media_idle", "V91_HOLD_EXPIRED", "rearm", "silent_stream"):
            self.assertNotIn(gone, loop)
        generation = between(loop, "if(current!=generation) {", "\n            }\n")
        self.assertIn("if(own.running) keep_output(o,current);", generation)
        self.assertNotIn("stop_output(", generation)
        self.assertIn("own_stream(&own,state,now);", loop)
        self.assertIn('stop_output(o,s,"stream_closed",1);codec_decoder_reset(&decoder);own_released(&own);', loop)
        # Every release in the loop tells the policy what happened.
        releases = list(re.finditer(r"stop_output\(o,s,[^;]*,1\);", loop))
        self.assertGreaterEqual(len(releases), 6)          # settings, media reset, route, error, close, rate
        for match in releases:
            self.assertRegex(loop[match.end():match.end() + 200], r"own_(restart|start_failed|released)\(", match.group(0))
        # v9.9.4: the session mixes (OutputPolicy.h) - never ducking others, never a Bluetooth route option - and keeps
        # the strongest public priority before every activation.
        for option in ("DuckOthers", "InterruptSpokenAudioAndMixWithOthers", "AllowBluetoothA2DP", "OptionAllowBluetooth"):
            self.assertNotIn(option, helper)
        self.assertEqual(helper.count("output_session_options("), 3)                # every configure_* uses the policy
        self.assertEqual(helper.count("prefer_uninterrupted(session);"), 4)   # every activation: 3 configure_* + resume
        self.assertIn("setPrefersNoInterruptionsFromSystemAlerts:YES", helper)
        self.assertEqual(helper.count("ok=[session setActive:YES error:&error];if(ok) note_session(1,"), 3)
        # Unchanged v9.1 invariants: speaker mode re-applies its override; a stop releases only when asked.
        self.assertIn("g_output_mode==OUTPUT_MODE_SPEAKER && !reassert_speaker(o,o->rate,s)", loop)
        stop = between(helper, "static void stop_output(", "static int speaker_selected(")
        self.assertLess(stop.index("if(!release) return;"), stop.index("setActive:NO"))
        start = between(helper, "static int start_output(", "static int reassert_speaker(")
        self.assertIn("int configured=reuse?0:configure_output(rate,s);", start)
        self.assertIn("AVAudioSessionMediaServicesWereResetNotification", helper)
        # The tweak connects the helper once the PC configures the speaker, before START.
        relay = read("tweak/AudioRelay.c")
        self.assertIn("if(fd<0 && ((generation&1u) || __atomic_load_n(&g_relay_warm,__ATOMIC_ACQUIRE)) && !retry) {", relay)
        hooks = read("tweak/Hooks.xm")
        config = between(hooks, "static uint64_t hook_setconfigind(", "\n}\n")
        self.assertIn("AudioRelaySetStreamState(AUDIO_STREAM_CONFIGURED);", config)

    def test_health_states_for_pause_interruption_gate_and_shutdown(self):
        # v9.9.3, test D: each lifecycle state keeps its own health behaviour. Holding needs an active stream and a
        # queue; a Windows pause (stream inactive) is never holding and resets the input monitor, so a pause is not
        # starvation; an interruption still pauses in place; shutdown still releases the session.
        self.compile_run(r"""
#include <assert.h>
#include "OutputPolicy.h"
int main(void) {
    assert(health_holding(1,1,0,1) == 1);     /* gated on a starved input, stream active: holding */
    assert(health_holding(1,1,1,0) == 1);     /* paused by an interruption, stream active: holding */
    assert(health_holding(1,1,0,0) == 0);     /* playing normally: the render-based report applies */
    assert(health_holding(0,1,1,1) == 0);     /* Windows paused the stream: never holding, whatever the flags */
    assert(health_holding(1,0,0,1) == 0);     /* no queue: nothing is held */
    return 0;
}
""")
        helper = read("audiohelper/AudioHelper.m")
        health = between(helper, "static void report_health(Output *o,AudioRing *s,uint64_t generation) {", "\n}\n")
        self.assertIn("if(health_holding((generation&1u)!=0,o->queue!=NULL,(int)o->paused,", health)
        self.assertIn("(int)__atomic_load_n(&o->input_starved,__ATOMIC_RELAXED))) {", health)
        self.assertIn("__atomic_load_n(&o->decoded_frames,__ATOMIC_RELAXED),2});", health)
        self.assertIn("(uint32_t)__atomic_load_n(&o->render_tick,__ATOMIC_RELAXED),why?-(int32_t)why:1});", health)
        relay = read("tweak/AudioRelay.c")
        self.assertIn("sink_health_render(health,e.bytes,e.frames,e.result==1?1u:e.result==2?2u:0u,now);", relay)
        self.assertIn("playing=%u holding=%u media_age_ms=", relay)                # the next device log says which
        loop = between(helper, "for(;;) {", "[[NSNotificationCenter defaultCenter] removeObserver:observer];")
        generation = between(loop, "if(current!=generation) {", "\n            }\n")
        # A pause (or any new stream) restarts the measurement and lifts the gate: a pause is not starvation.
        self.assertIn("input_rate_reset(&input,now,(int)(current&1u));__atomic_store_n(&o->input_starved,0,__ATOMIC_RELAXED);",
                      generation)
        began = loop.split("if(changed==1) {", 1)[1].split("} else {", 1)[0]
        self.assertIn('pause_output(o,s,"interruption_began");', began)          # interruptions: unchanged
        self.assertIn('stop_output(o,s,"ipc_closed",1);', helper)                 # shutdown: releases the session

    def test_mixing_session_policy_and_fallback(self):
        # Priority 3 (v9.9.4): TikTok's non-mixing session interrupted Speaka's (v9.8.91, 14:52:27 and 14:52:32) and, while
        # it played, iOS sent its audio to the PC - Apple's A2DP code started the PC's stream at 14:52:32.736 and suspended
        # it as TikTok stopped (14:52:38.536). Speaka's session now mixes in every mode, so an ordinary app's audio plays
        # alongside instead of taking it; it never carries a Bluetooth option, so its own output can't reach the PC; and if
        # another app's audio still pulls the shared route away, it falls back to exclusive playback once, never toggling.
        self.compile_run(r"""
#include <assert.h>
#include "OutputPolicy.h"
int main(void) {
    for (int speaker = 0; speaker <= 1; speaker++)
        for (int mixing = 0; mixing <= 1; mixing++) {
            unsigned o = output_session_options(speaker, mixing);
            assert(!(o & SESSION_OPT_ALLOW_BLUETOOTH) && !(o & SESSION_OPT_ALLOW_BLUETOOTH_A2DP));   /* never the PC */
            assert(((o & SESSION_OPT_MIX_WITH_OTHERS) != 0) == (mixing != 0));
            assert(((o & SESSION_OPT_DEFAULT_TO_SPEAKER) != 0) == (speaker != 0));
        }
    assert(SESSION_OPT_MIX_WITH_OTHERS == 0x1u && SESSION_OPT_DEFAULT_TO_SPEAKER == 0x8u);   /* the SDK's values */
    assert(SESSION_OPT_ALLOW_BLUETOOTH == 0x4u && SESSION_OPT_ALLOW_BLUETOOTH_A2DP == 0x20u);
    mix_state_t m; mix_init(&m);
    assert(m.mixing == 1);                       /* a helper session starts mixing */
    assert(mix_route_lost(&m) == 1 && m.mixing == 0 && m.fallbacks == 1);   /* the route was taken: exclusive */
    assert(mix_route_lost(&m) == 0 && m.fallbacks == 1);                    /* once: never back and forth */
    mix_init(&m); assert(m.mixing == 1 && m.fallbacks == 0);                /* the next helper session mixes again */
    return 0;
}
""")
        helper = read("audiohelper/AudioHelper.m")
        for name, speaker in (("configure_speaker", 1), ("configure_external", 0), ("configure_wired", 0)):
            body = between(helper, "static int %s(" % name, "\n}\n")
            self.assertIn("output_session_options(%d,(int)g_mix.mixing)" % speaker, body, name)
        loop = between(helper, "for(;;) {", "[[NSNotificationCenter defaultCenter] removeObserver:observer];")
        route = loop.split("if(o->queue && !o->paused) {\n                    log_route(", 1)[1].split("} else if(device || !own_echo) {", 1)[0]
        # The fallback comes after re-asserting the speaker and before giving the output up; it rebuilds in place.
        self.assertLess(route.index("reassert_speaker(o,o->rate,s)"), route.index("mix_route_lost(&g_mix)"))
        self.assertLess(route.index("mix_route_lost(&g_mix)"), route.index('stop_output(o,s,"route_changed",1)'))
        fallback = route.split("mix_route_lost(&g_mix)", 1)[1].split("} else {", 1)[0]
        self.assertIn('stop_output(o,s,"mix_fallback",0);', fallback)        # the session stays active
        self.assertIn("own_restart(&own,helper_ms());", fallback)
        setup = between(helper, "    int own_logged=-1;\n", "    for(;;) {")
        self.assertIn("mix_init(&g_mix);", setup)
        self.assertIn('@"mixing":@(g_mix.mixing)', helper)                   # the app and the log can tell
        # System interruptions (calls, alarms) are still handled by the v9.9.2 pause/resume path.
        began = loop.split("if(changed==1) {", 1)[1].split("} else {", 1)[0]
        self.assertIn('pause_output(o,s,"interruption_began");', began)

    def test_control_log_names_the_initiator(self):
        tweak = read("tweak/Hooks.xm")
        note = between(tweak, "static void note_signal(", "/* v8.8.2: the verified native idle check")
        self.compile_run(r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <stdarg.h>
#include <stdio.h>
#include <pthread.h>
#include "tweak/StreamControl.c"
static unsigned g_relay_ready=1,g_sink_accepted=1;
static volatile uint64_t g_pc_signal_cid=0x108;
static StreamControl g_stream_control;
static pthread_mutex_t g_stream_control_lock=PTHREAD_MUTEX_INITIALIZER;
static char line[256];
void AudioRelaySetActive(int) {}
void AudioRelaySetStreamState(uint32_t) {}
void SpeakaLogNow(const char *fmt,...) {va_list ap;va_start(ap,fmt);vsnprintf(line,sizeof line,fmt,ap);va_end(ap);}
void SpeakaSetStatus(const char *,const char *,...) {}   /* v9.6: diag.v95.peer, checked in test_v89 */
static uint64_t speaka_since_launch_ms(void) {return 1234;}
static void note_signal(''' + note + r'''
static void expect(unsigned direction,const uint8_t *p,size_t n,const char *tail) {
    line[0]=0;note_signal(direction,0x108,p,n);
    assert(strstr(line,tail));
}
int main() {
    memset(&g_stream_control,0,sizeof g_stream_control);
    g_stream_control.local_seid=7;g_stream_control.remote_seid=52;
    /* Exact PDUs from the 23:33 log: the phone suspends (TX 80 09 d0, accepted RX 82 09) and starts (TX 90 07 d0,
     * accepted RX 92 07); at 23:33:44 the PC suspends (RX 60 09 1c, accepted TX 62 09). */
    const uint8_t phone_suspend[3]={0x80,0x09,0xd0},phone_suspend_ok[2]={0x82,0x09};
    const uint8_t phone_start[3]={0x90,0x07,0xd0},phone_start_ok[2]={0x92,0x07};
    const uint8_t pc_suspend[3]={0x60,0x09,0x1c},pc_suspend_ok[2]={0x62,0x09};
    const uint8_t pc_start[3]={0x30,0x07,0x1c},pc_start_ok[2]={0x32,0x07};
    expect(1,phone_suspend,3,"active=0 by=phone");
    expect(0,phone_suspend_ok,2,"active=0 by=phone");
    line[0]=0;note_signal(1,0x108,phone_start,3);assert(!line[0]);   /* a START command alone changes nothing */
    /* v9.9.4: who started it and how long after this bluetoothd launched - the startup throttle's two facts. */
    expect(0,phone_start_ok,2,"msg=2 sig=7 active=1 by=phone since_launch_ms=1234");
    expect(0,pc_suspend,3,"active=0 by=pc");
    expect(1,pc_suspend_ok,2,"active=0 by=pc");
    line[0]=0;note_signal(0,0x108,pc_start,3);assert(!line[0]);
    expect(1,pc_start_ok,2,"msg=2 sig=7 active=1 by=pc");
    return 0;
}
''', cpp=True)


if __name__ == "__main__":
    unittest.main()
