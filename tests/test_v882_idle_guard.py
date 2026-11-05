"""Exercise freshness and connection scoping; verify exact native code gates."""
import hashlib
import os
import pathlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class V882IdleGuard(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
