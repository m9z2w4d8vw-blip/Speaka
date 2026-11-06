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

    def test_speaker_route_and_teardown_guards(self):
        source = SOURCE.read_text()
        # The speaker path is unchanged since v8.8.1. v8.9.4 adds the only other PlayAndRecord configuration, wired
        # headphones mode, with no options; neither can reach Bluetooth or AirPlay, and nothing ever records.
        configure = source.split("static int configure_speaker(", 1)[1].split("static int configure_external(", 1)[0]
        self.assertIn("AVAudioSessionCategoryPlayAndRecord", configure)
        self.assertIn("AVAudioSessionCategoryOptionDefaultToSpeaker", configure)
        self.assertIn("overrideOutputAudioPort:AVAudioSessionPortOverrideSpeaker", configure)
        self.assertIn("!speaker_selected(session)", configure)
        self.assertEqual(source.count("AVAudioSessionCategoryPlayAndRecord"), 2)
        wired = source.split("static int configure_wired(", 1)[1].split("static int configure_output(", 1)[0]
        self.assertIn("setCategory:AVAudioSessionCategoryPlayAndRecord mode:AVAudioSessionModeDefault options:0", wired)
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
        category = wired.index("setCategory:AVAudioSessionCategoryPlayAndRecord mode:AVAudioSessionModeDefault options:0")
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
        loop = between(helper, "__block int interruption=0,route_changed=0;",
                       "[[NSNotificationCenter defaultCenter] removeObserver:observer];")
        drain = loop.index("CFRunLoopRunInMode(kCFRunLoopDefaultMode,0,true)")
        self.assertLess(drain, loop.index("usleep(5000);"))
        self.assertGreater(drain, loop.index("for(;;) {"))
        registration = helper.split("__block int interruption=0,route_changed=0;", 1)[1].split("for(;;) {", 1)[0]
        # handlers run on the posting thread, whichever it is. v9.1 adds the third: media services were reset.
        self.assertEqual(registration.count("queue:nil"), 3)
        self.assertNotIn("dispatch_get_main_queue", helper)   # nothing else is waiting to run on the main thread

    def test_an_interruption_is_taken_back(self):
        # v9.9: another app's session interrupting the helper used to stop its output and drop every packet until the
        # PC sent a new START. Now the dead queue is dropped without deactivating (which would tell the other app to
        # carry on), the session is marked inactive so the retake activates afresh, and the output is taken back.
        helper = read("audiohelper/AudioHelper.m")
        loop = helper.split("for(;;) {", 1)[1].split("[[NSNotificationCenter defaultCenter] removeObserver:observer];", 1)[0]
        began = loop.split("if(changed==1) {", 1)[1].split("} else own_now(&own,now);", 1)[0]
        self.assertIn('stop_output(o,s,"interruption_began",0);', began)          # no release: no NotifyOthers
        self.assertLess(began.index('note_session(0,"interrupted")'), began.index("own_taken(&own,now);"))
        self.assertIn("} else own_now(&own,now);", loop)                       # ended: back at once
        self.assertNotIn("interrupted", loop.replace('"interrupted"', "").replace("interruption", ""))
        self.assertIn("if(!(generation&1u) || packet.generation!=generation) continue;", loop)   # packets keep decoding
        self.assertIn('"V882_INTERRUPTION type=%s reason=%ld queue=%d\\n"', loop)

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
        owner = loop.index("if(!o->queue && own_may_start(&own,now)) {")
        self.assertLess(owner, loop.index("for(unsigned i=0;i<AUDIO_RING_SLOTS && audio_ring_dequeue(s,&packet);i++) {"))
        self.assertIn("int started=start_output(o,rate_hint,s);", loop)
        packets = loop.split("for(unsigned i=0;i<AUDIO_RING_SLOTS && audio_ring_dequeue(s,&packet);i++) {", 1)[1]
        self.assertEqual(packets.count("start_output("), 1)                    # only the sample-rate rebuild
        self.assertIn('stop_output(o,s,"sample_rate_changed",0);', packets)   # a new rate keeps the session
        self.assertIn("if(o->queue) write_pcm(o,pcm,samples);", packets)
        # Never released for lack of media; a pause keeps everything; a close lets go after the grace.
        for gone in ("hold_", "HOLD_", "media_idle", "V91_HOLD_EXPIRED", "rearm", "silent_stream"):
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
        # Exclusive, non-mixing playback, and the strongest public priority before every activation.
        for option in ("MixWithOthers", "DuckOthers", "InterruptSpokenAudioAndMixWithOthers"):
            self.assertNotIn(option, helper)
        self.assertEqual(helper.count("prefer_uninterrupted(session);"), 3)
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
    expect(0,phone_start_ok,2,"msg=2 sig=7 active=1 by=phone");
    expect(0,pc_suspend,3,"active=0 by=pc");
    expect(1,pc_suspend_ok,2,"active=0 by=pc");
    line[0]=0;note_signal(0,0x108,pc_start,3);assert(!line[0]);
    expect(1,pc_start_ok,2,"msg=2 sig=7 active=1 by=pc");
    return 0;
}
''', cpp=True)


if __name__ == "__main__":
    unittest.main()
