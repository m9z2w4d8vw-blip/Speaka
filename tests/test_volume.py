"""Run the portable volume layer against wire vectors and a real shared mapping.
No host test claims to verify a private iOS hook or Windows interoperability.
"""

import os
import hashlib
import re
import struct
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VolumeRuntime(unittest.TestCase):
    def test_native_entry_fingerprints_match_reference_function_starts(self):
        path = ROOT / "docs/reference-binaries/ios17-iphone13/bluetoothd"
        if not path.exists():
            self.skipTest(
                "requires the exact reference bluetoothd; does not test hooks live"
            )
        data = path.read_bytes()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            "e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6",
        )
        segments = []
        cursor = 32
        starts_data = b""
        for _ in range(struct.unpack_from("<I", data, 16)[0]):
            cmd, size = struct.unpack_from("<II", data, cursor)
            if cmd == 0x19:
                segments.append(struct.unpack_from("<QQQQ", data, cursor + 24))
            if cmd == 0x26:
                offset, length = struct.unpack_from("<II", data, cursor + 8)
                starts_data = data[offset : offset + length]
            cursor += size
        base = next(vm for vm, _, off, size in segments if off == 0 and size)
        starts, offset, delta, shift = set(), 0, 0, 0
        for byte in starts_data:
            delta |= (byte & 127) << shift
            if byte & 128:
                shift += 7
            else:
                if not delta:
                    break
                offset += delta
                starts.add(offset)
                delta = shift = 0
        self.assertTrue(starts)
        source = (ROOT / "tweak/VolumeControl.m").read_text()
        block = source.split("checks[]={", 1)[1].split("};", 1)[0]
        gates = re.findall(r"\{(0x[0-9a-f]+),\{([^}]+)\}\}", block)
        # v9.9.8: dispatcher, response, lock, unlock, teardown, the Target's SDP registration (+0xe1a98);
        # v9.9.9: the Target command callback, AVCTP's incoming connection, and iOS's own AVRCP connect.
        self.assertEqual(len(gates), 9)
        for offset in ("0xe1a98", "0xe1d9c", "0xccb08", "0x54f8c4"):
            self.assertIn(offset, [g[0] for g in gates])
        for offset, words in gates:
            address = base + int(offset, 16)
            self.assertIn(int(offset, 16), starts)
            vm, _, fileoff, _ = next(
                s for s in segments if s[0] <= address < s[0] + s[3]
            )
            actual = struct.unpack_from("<4I", data, fileoff + address - vm)
            self.assertEqual(
                actual, tuple(int(word, 16) for word in words.split(",")), offset
            )

    def test_volume_protocol_pcm_and_ipc(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = str(Path(tmp) / "volume-test")
            flags = [
                "-std=gnu11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-O2",
                "-I",
                str(ROOT),
            ]
            if os.environ.get("BTPS_SANITIZE"):
                flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            subprocess.run(
                [
                    shutil.which("cc"),
                    *flags,
                    str(ROOT / "tests/volume_runtime.c"),
                    str(ROOT / "shared/VolumeControl.c"),
                    str(ROOT / "shared/VolumeMailbox.c"),
                    "-o",
                    exe,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            env = dict(
                os.environ,
                ASAN_OPTIONS="detect_leaks=0",
                UBSAN_OPTIONS="halt_on_error=1",
            )
            result = subprocess.run(
                [exe, tmp], capture_output=True, text=True, timeout=30, env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stdout, end="")


class VolumeIntegration(unittest.TestCase):
    """v9.9.8: Astra's AVRCP volume engine integrated with v9.8.96's verified peer identity (the stream map), an
    experimental Category-2 SDP advertisement, and diagnostics. The engine itself is VolumeRuntime's subject."""

    def read(self, path):
        return (ROOT / path).read_text()

    def test_binding_follows_the_verified_a2dp_device(self):
        # The adapter's sequence (tweak/VolumeControl.m), driven through the real engine: install binds the last verified
        # Speaka PC; Windows' early requests are answered; configuration rebinds with a fresh epoch; other devices stay
        # native; a remote SetAbsoluteVolume is never echoed.
        harness = r"""
#include <assert.h>
#include <string.h>
#include "shared/VolumeControl.h"
static unsigned sends, applied = 999; static uint8_t last_code, last_pdu, last_payload[8];
static int apply_fn(void *c, uint8_t v) { (void)c; applied = v; return 1; }
static int send_fn(void *c, const uint8_t peer[6], uint8_t label, uint8_t code, const uint8_t *p, size_t n) {
    (void)c; (void)peer; (void)label; sends++; last_code = code; last_pdu = p[3];
    memset(last_payload, 0, sizeof last_payload); memcpy(last_payload, p + 7, n > 15 ? 8 : n - 7); return 1;
}
static const SPVolumeIO io = { apply_fn, send_fn, NULL };
static const uint8_t events[] = {1, 2, 8, 9, 10, 11, 12};
static int cmd(SPVolume *s, const uint8_t *peer, uint64_t epoch, uint8_t label, uint8_t ctype, const uint8_t *p, size_t n) {
    return sp_volume_command(s, &io, peer, epoch, label, ctype, p, n, events, sizeof events);
}
int main(void) {
    const uint8_t laptop[6] = {0x24, 0xB2, 0xB9, 0xA1, 0xA4, 0x68}, other[6] = {0x11, 0x22, 0x33, 0x44, 0x55, 0x66};
    const uint8_t caps[] = {0, 0x19, 0x58, 0x10, 0, 0, 1, 3};
    const uint8_t reg[] = {0, 0x19, 0x58, 0x31, 0, 0, 5, 0x0d, 0, 0, 0, 0};
    const uint8_t set64[] = {0, 0x19, 0x58, 0x50, 0, 0, 1, 64}, set_mute[] = {0, 0x19, 0x58, 0x50, 0, 0, 1, 0};
    const uint8_t set_max[] = {0, 0x19, 0x58, 0x50, 0, 0, 1, 0xff};   /* reserved bit 7 set: 127 */
    SPVolume s; uint64_t epoch = 1;
    /* Install: the persisted last Speaka PC is bound before any connection. */
    sp_volume_bind(&s, laptop, epoch, 127);
    /* Windows asks for capabilities before A2DP configures: answered, with 0x0d appended to the native seven. */
    assert(cmd(&s, laptop, epoch, 1, SP_AVRCP_STATUS, caps, sizeof caps) == 1);
    assert(last_code == SP_AVRCP_STABLE && last_pdu == 0x10 && last_payload[1] == 8);
    assert(cmd(&s, laptop, epoch, 2, SP_AVRCP_NOTIFY, reg, sizeof reg) == 1 && last_code == SP_AVRCP_INTERIM);
    assert(s.subscribed && s.label == 2);
    /* A2DP configures: a fresh epoch, the same verified device - no stale subscription or label survives. */
    sp_volume_bind(&s, laptop, ++epoch, 127);
    assert(!s.subscribed);
    assert(cmd(&s, laptop, epoch - 1, 3, SP_AVRCP_CONTROL, set64, sizeof set64) == 0);   /* the old epoch: native */
    assert(cmd(&s, laptop, epoch, 4, SP_AVRCP_NOTIFY, reg, sizeof reg) == 1 && s.subscribed);
    /* Windows sets 64: applied, ACCEPTED, and the subscription is NOT completed (no CHANGED echo). */
    unsigned before = sends;
    assert(cmd(&s, laptop, epoch, 5, SP_AVRCP_CONTROL, set64, sizeof set64) == 1);
    assert(applied == 64 && last_code == SP_AVRCP_ACCEPTED && sends == before + 1 && s.subscribed);
    assert(cmd(&s, laptop, epoch, 6, SP_AVRCP_CONTROL, set_mute, sizeof set_mute) == 1 && applied == 0);
    assert(cmd(&s, laptop, epoch, 7, SP_AVRCP_CONTROL, set_max, sizeof set_max) == 1 && applied == 127);
    /* Another device is never this layer's: delegated, nothing applied, nothing sent. */
    before = sends; applied = 999;
    assert(cmd(&s, other, epoch, 8, SP_AVRCP_CONTROL, set64, sizeof set64) == 0 && applied == 999 && sends == before);
    /* A different PC configures: it is bound; the laptop now goes to native code. */
    sp_volume_bind(&s, other, ++epoch, 127);
    assert(cmd(&s, laptop, epoch, 9, SP_AVRCP_CONTROL, set64, sizeof set64) == 0);
    assert(cmd(&s, other, epoch, 10, SP_AVRCP_CONTROL, set64, sizeof set64) == 1 && applied == 64);
    /* The stream closes: reset to unity, the last PC bound again under a new epoch. */
    sp_volume_bind(&s, other, ++epoch, 127);
    assert(s.value == 127 && !s.subscribed);
    return 0;
}
"""
        cc = shutil.which("cc") or shutil.which("gcc")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "bind.c"; exe = Path(td) / "bind"; src.write_text(harness)
            build = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT), str(src),
                                    str(ROOT / "shared/VolumeControl.c"), "-o", str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_the_adapter_binds_the_identity_not_a_learned_path(self):
        adapter = self.read("tweak/VolumeControl.m")
        for gone in ("SPEAKA_KEY_PEER_PATH", "peer_read_path", "peer_path_t", "read_path("):
            self.assertNotIn(gone, adapter)
        configure = adapter.split("void SpeakaVolumeConfigure(const uint8_t *peer) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if(peer) {memcpy(last_peer,peer,6);have_last_peer=1;}", configure)
        self.assertIn('reset_locked(peer?"configured":"configured_unidentified");', configure)
        reset = adapter.split("static void reset_locked(const char *why) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("++epoch;sp_volume_reset(&state);sp_volume_mailbox_set(mailbox,0,127);", reset)
        # v9.9.9: rebound at the level that PC last set, and at unity until Windows engages.
        self.assertIn("if(mode==2 && have_last_peer) sp_volume_bind(&state,last_peer,epoch,(uint8_t)last_level);", reset)
        self.assertIn("sp_volume_mailbox_set(mailbox,0,127);engaged=0;", reset)
        # Off mode installs nothing.
        install = adapter.split("void SpeakaVolumeInstall(uintptr_t base,int exact_uuid) {", 1)[1]
        self.assertLess(install.index("if(!mode) {SpeakaLogNow(\"AVRCP_VOLUME off (SpeakaVolumeSync=off)\");return;}"), install.index("MSHookFunction("))
        # Early requests don't wait for the helper; the gain waits in the mailbox for it.
        receive = adapter.split("static uint32_t receive(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if(mode==2)\n                consumed=sp_volume_command(", receive)
        self.assertIn("match=%d", receive)
        # The identity drives it: PeerIdentity binds the stream's device; Hooks.xm no longer passes the manager.
        peer = self.read("tweak/PeerIdentity.c")
        self.assertLess(peer.index('SpeakaSetStatus("diag.v105.peer_name", "%s", id.name);'),
                        peer.index("SpeakaVolumeConfigure(id.address);"))
        self.assertIn("SpeakaVolumeConfigure(NULL);", peer)
        self.assertNotIn("SpeakaVolumeConfigure(a0", self.read("tweak/Hooks.xm"))

    def test_category2_is_advertised_only_in_experimental_mode(self):
        adapter = self.read("tweak/VolumeControl.m")
        hook = adapter.split("static uint64_t register_target(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("uint32_t advertised=mode==2?(features|0x02u):features;", hook)
        self.assertIn("return original_register_target(descriptor,advertised,a2,a3);", hook)
        install = adapter.split("void SpeakaVolumeInstall(uintptr_t base,int exact_uuid) {", 1)[1]
        self.assertLess(install.index("(void *)(base+0xe1a98)"), install.index("(void *)(base+0xe0ff4)"))
        # The tweak installs the adapter from its constructor - before bluetoothd's AVRCP registration runs.
        hooks = self.read("tweak/Hooks.xm")
        ctor = hooks[hooks.index("%ctor {"):]
        self.assertIn("SpeakaVolumeInstall((uintptr_t)mh, uuid_ok);", ctor)

    def test_gain_is_applied_once_after_decode_for_every_codec(self):
        helper = self.read("audiohelper/AudioHelper.m")
        self.assertEqual(helper.count("sp_volume_pcm("), 1)
        render = helper.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        self.assertLess(render.index("o->read=(o->read+take)%PCM_CAPACITY;o->count-=take;"), render.index("sp_volume_pcm(out,take*2u,gain);"))
        self.assertLess(render.index("sp_volume_pcm(out,take*2u,gain);"), render.index("AudioQueueEnqueueBuffer(queue,buffer,0,NULL)"))
        writers = helper.split("static void hold_pcm(", 1)[1].split("static void keep_output(", 1)[0]
        self.assertNotIn("sp_volume", writers)                 # the ring holds decoded, un-attenuated PCM
        self.assertIn('hlog(stdout,"V106_VOLUME gain=%u/127 (%u%%) output=%s domain=%s\\n"', helper)

    def test_observation_hooks_pass_everything_through(self):
        # v9.9.9: the earliest points of the incoming Target path. They log and call the original, unconditionally,
        # in observe and experimental mode alike - they can't drop, alter or answer anything.
        adapter = self.read("tweak/VolumeControl.m")
        for name, call in (("static void target_command(", "    original_target_command(address,label,incoming);\n}"),
                           ("static void avctp_incoming(", "    original_avctp_incoming(context,address,channel);\n}"),
                           ("static uint64_t avrcp_connect(", "    return result;\n}")):
            body = adapter.split(name, 1)[1].split("\n}\n", 1)[0] + "\n}"
            self.assertTrue(body.rstrip().endswith(call.split("\n")[-1]), name)
            self.assertIn(call.split("\n")[0].strip(), body)
            self.assertNotIn("consumed", body)
            self.assertNotIn("return 0;", body)
        self.assertIn("if(original_avrcp_connect) result=original_avrcp_connect(manager,device);", adapter)   # v9.9.10: guarded
        install = adapter.split("void SpeakaVolumeInstall(uintptr_t base,int exact_uuid) {", 1)[1]
        hooks = install.index("(void *)(base+0xe1d9c)")
        self.assertLess(install.index("if(!match)"), hooks)                       # gated by the build checks
        self.assertLess(hooks, install.index("if(mode==2) {\n        MSHookFunction((void *)(base+0x55b2f4)"))
        self.assertIn("if(n<=64u || n%100u==0u) {", adapter)                      # bounded logging

    def test_initial_volume_is_the_pcs_last_level_never_maximum(self):
        harness = r"""
#include <assert.h>
#include <string.h>
#include "shared/VolumeControl.h"
static uint8_t interim_value = 255;
static int apply_fn(void *c, uint8_t v) { (void)c; (void)v; return 1; }
static int send_fn(void *c, const uint8_t peer[6], uint8_t label, uint8_t code, const uint8_t *p, size_t n) {
    (void)c; (void)peer; (void)label;
    if (code == SP_AVRCP_INTERIM && n >= 9) interim_value = p[8];   /* 00 19 58 31 00 00 02 0d <value> */
    return 1;
}
static const SPVolumeIO io = { apply_fn, send_fn, NULL };
int main(void) {
    const uint8_t laptop[6] = {0x24, 0xB2, 0xB9, 0xA1, 0xA4, 0x68};
    const uint8_t reg[] = {0, 0x19, 0x58, 0x31, 0, 0, 5, 0x0d, 0, 0, 0, 0};
    SPVolume s;
    /* The laptop last set 40/127: on reconnecting it is told 40 when it registers - no jump if it adopts it. */
    sp_volume_bind(&s, laptop, 7, 40);
    assert(sp_volume_command(&s, &io, laptop, 7, 3, SP_AVRCP_NOTIFY, reg, sizeof reg, NULL, 0) == 1);
    assert(interim_value == 40);
    /* A PC never seen: half, not maximum. */
    sp_volume_bind(&s, laptop, 8, 64);
    assert(sp_volume_command(&s, &io, laptop, 8, 4, SP_AVRCP_NOTIFY, reg, sizeof reg, NULL, 0) == 1);
    assert(interim_value == 64);
    return 0;
}
"""
        cc = shutil.which("cc") or shutil.which("gcc")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "level.c"; exe = Path(td) / "level"; src.write_text(harness)
            build = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT), str(src),
                                    str(ROOT / "shared/VolumeControl.c"), "-o", str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stderr)
        adapter = self.read("tweak/VolumeControl.m")
        self.assertIn("#define SP_VOLUME_FIRST_DEFAULT 64u", adapter)
        self.assertIn("if(changed) last_level=SP_VOLUME_FIRST_DEFAULT;", adapter)         # another PC: unknown level
        apply = adapter.split("static int apply(void *unused,uint8_t value) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("engaged=1;", apply)
        self.assertIn("dispatch_async_f(queue,NULL,persist_level);", apply)                 # off the receive thread
        receive = adapter.split("static uint32_t receive(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if(consumed && p[3]==0x31 && state.subscribed && !engaged && mailbox) {", receive)
        self.assertIn("sp_volume_mailbox_set_from(mailbox,1,0,state.value);engaged=1;", receive)   # v9.9.11: Speaka's, not Windows'



class VolumeDomain(unittest.TestCase):
    """v9.9.10 (device test v9.8.98): Windows' SetAbsoluteVolume reached Speaka's gain, but the phone's buttons and HUD
    show iOS's output volume - two volumes, never mirrored, and no CHANGED was ever sent. iOS's output volume becomes the
    one volume (mediaremoted's AVSystemController setActiveCategoryVolumeTo:), Speaka's gain stays at unity, and the
    user's iOS changes go to Windows. The pre-gain PCM peak stayed ~19,000 while the slider swept 18..123 (09:12:05-11):
    Windows sends full scale once engaged, so the sink is the only attenuation either way."""

    def read(self, path):
        return (ROOT / path).read_text()

    def test_domain_policy_never_echoes_and_falls_back_when_ineffective(self):
        harness = r"""
#include <assert.h>
#include <stdio.h>
#include "shared/VolumeControl.h"
int main(void) {
    SPVolumeDomain d; sp_domain_init(&d, 1);
    /* The connection: the first iOS reading is the phone's own level, reported as the initial value. */
    assert(sp_domain_observed(&d, 76, 1000) == 2);
    /* Already there: no set. */
    assert(sp_domain_remote(&d, 77, 76, 1100) == -1);
    /* Windows drags its slider: a level every 15 ms for 300 ms; iOS reads back the set before last (it lags). */
    unsigned sets = 0; int last_set = -1, reports = 0;
    uint64_t t = 2000;
    for (int i = 0; i <= 20; i++, t += 15) {
        int level = 20 + i * 5, ios = last_set >= 0 ? last_set : 76;
        int now_set = sp_domain_remote(&d, level, ios, t);
        if (now_set < 0) now_set = sp_domain_due(&d, ios, t);
        if (now_set >= 0) { sp_domain_set(&d, now_set, t); last_set = now_set; sets++; }
        reports += sp_domain_observed(&d, ios, t) == 1;
    }
    for (; t < 2300 + 1000; t += 10) {                       /* settle: the coalesced newest level, then readbacks */
        int now_set = sp_domain_due(&d, last_set, t);
        if (now_set >= 0) { sp_domain_set(&d, now_set, t); last_set = now_set; sets++; }
        reports += sp_domain_observed(&d, last_set, t) == 1;
        (void)sp_domain_verify(&d, last_set, t);
    }
    printf("drag: 21 levels in 300 ms -> %u sets, last %d, reports to Windows %d\n", sets, last_set, reports);
    assert(sets <= 8 && last_set == 120 && reports == 0);   /* coalesced, newest wins, never an echo */
    assert(d.ios == 1);
    /* The user presses volume-down two seconds later: that is reported. */
    assert(sp_domain_observed(&d, 112, t + 2000) == 1);
    /* An ineffective setter: three sets that iOS ignores -> back to Speaka's gain, then nothing more is set or reported. */
    SPVolumeDomain e; sp_domain_init(&e, 1); assert(sp_domain_observed(&e, 60, 100) == 2);
    uint64_t u = 1000; int fell = 0;
    for (int i = 0; i < 3; i++, u += 1000) {
        int level = sp_domain_remote(&e, 90 + i, 60, u); assert(level >= 0);
        sp_domain_set(&e, level, u);
        fell |= sp_domain_verify(&e, 60, u + SP_DOMAIN_VERIFY_MS);
    }
    assert(fell && e.ios == 0);
    assert(sp_domain_remote(&e, 100, 60, u + 5000) == -1 && sp_domain_observed(&e, 50, u + 9000) == 0);
    /* A superseded set is not a failure: only a settled read-back counts. */
    SPVolumeDomain f; sp_domain_init(&f, 1); (void)sp_domain_observed(&f, 60, 100);
    sp_domain_set(&f, 90, 1000); (void)sp_domain_remote(&f, 95, 60, 1010);
    assert(sp_domain_verify(&f, 60, 1000 + SP_DOMAIN_VERIFY_MS) == 0 && f.failures == 0);
    /* The Speaka-gain domain never reports iOS changes. */
    SPVolumeDomain g; sp_domain_init(&g, 0); (void)sp_domain_observed(&g, 60, 100);
    assert(sp_domain_observed(&g, 70, 5000) == 0 && sp_domain_remote(&g, 30, 60, 5000) == -1);
    return 0;
}
"""
        cc = shutil.which("cc") or shutil.which("gcc")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "domain.c"; exe = Path(td) / "domain"; src.write_text(harness)
            build = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT), str(src),
                                    str(ROOT / "shared/VolumeControl.c"), "-o", str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            print(ran.stdout, end="")

    def test_helper_sets_ios_volume_and_reports_the_users_changes(self):
        helper = self.read("audiohelper/AudioHelper.m")
        render = helper.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        self.assertIn("unsigned gain=__atomic_load_n(&o->ios_domain,__ATOMIC_RELAXED)?127u:sp_volume_mailbox_gain(o->volume);", render)
        self.assertLess(render.index("unsigned pre=0,post;"), render.index("sp_volume_pcm(out,take*2u,gain);"))
        setter = helper.split("static id ios_volume_controller(void) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('dlopen("/System/Library/PrivateFrameworks/Celestial.framework/Celestial",RTLD_LAZY);', setter)
        self.assertIn('NSClassFromString(@"AVSystemController")', setter)
        self.assertIn("if([c respondsToSelector:set]) controller=c;", setter)          # missing: the gain domain
        self.assertIn('((BOOL (*)(id,SEL,float))objc_msgSend)(c,NSSelectorFromString(@"setActiveCategoryVolumeTo:"),(float)level/127.0f)', helper)
        loop = helper.split("int windows=sp_volume_mailbox_from_windows(o->volume)?(int)gain:-1;", 1)[1].split("if(o->queue && !o->paused && now-level_reported>=2000u) {", 1)[0]
        self.assertIn("if(change==1) report_local_volume(ios);", loop)                 # the user's change only
        self.assertIn("__atomic_store_n(&o->ios_domain,0u,__ATOMIC_RELAXED);", loop)    # ineffective: back to gain
        self.assertIn("else if(windows<0) report_local_volume(ios);", loop)            # initial: the phone's level
        self.assertIn('hlog(stdout,"V107_LEVEL submitted_peak pre=%u post=%u', helper)
        self.assertIn('strcmp(argv[1],"--volume-domain")==0', helper)
        self.assertIn('#define SPEAKA_KEY_VOLUME_DOMAIN "SpeakaAVRCPVolumeDomain"', self.read("shared/SpeakaShared.h"))

    def test_every_native_original_is_guarded(self):
        # v9.9.10: a hook whose trampoline is missing degrades - it never calls a null original.
        adapter = self.read("tweak/VolumeControl.m")
        lines = adapter.splitlines()
        for i, line in enumerate(lines):
            for name in ("original_receive", "original_disconnect", "original_register_target",
                         "original_target_command", "original_avctp_incoming", "original_avrcp_connect"):
                if name + "(" in line and "MSHookFunction" not in line:
                    context = line + lines[i - 1]
                    self.assertTrue(("if(" + name) in context or ("if(!" + name) in context,
                                    "unguarded call at line %d: %s" % (i + 1, line.strip()))
        self.assertIn('SpeakaLogNow("AVRCP_VOLUME FAILED CLOSED: %s has no original; degraded, not called",which);', adapter)


class VolumeStandard(unittest.TestCase):
    """v9.9.11: volume sync is a standard feature (live-verified both ways on iOS 17.0 + Windows 11, v9.8.99): on without
    any setting, the experiment's key migrated away, `--volume-mode off|observe` for recovery and diagnostics. And the
    v9.8.99 log's one jump - the phone at 127 set to a saved 51 when Windows registered - is gone: only a level Windows
    chose (SetAbsoluteVolume) moves iOS's volume."""

    def read(self, path):
        return (ROOT / path).read_text()

    def test_on_by_default_and_migrated(self):
        adapter = self.read("tweak/VolumeControl.m")
        install = adapter.split("void SpeakaVolumeInstall(uintptr_t base,int exact_uuid) {", 1)[1]
        self.assertLess(install.index("mode=2;"), install.index("CFSTR(SPEAKA_KEY_VOLUME_SYNC)"))   # absent: on
        self.assertIn('if(CFEqual(v,CFSTR("off"))) mode=0;', install)
        self.assertIn('if(CFEqual(v,CFSTR("observe"))) mode=1;', install)
        legacy = install.split("CFPropertyListRef legacy=", 1)[1].split("    mode=2;", 1)[0]
        self.assertIn("CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_VOLUME_MODE),NULL,CFSTR(SPEAKA_PREFS_DOMAIN));", legacy)
        self.assertNotIn('CFSTR("experimental")', install)          # the experiment's values never decide it
        self.assertNotIn("STATIC EVIDENCE ONLY", adapter)
        self.assertIn('#define SPEAKA_KEY_VOLUME_SYNC "SpeakaVolumeSync"', self.read("shared/SpeakaShared.h"))
        helper = self.read("audiohelper/AudioHelper.m")
        cli = helper.split('if(argc==3 && strcmp(argv[1],"--volume-mode")==0) {', 1)[1].split("\n        }\n", 1)[0]
        self.assertIn('strcmp(argv[2],"experimental")?argv[2]:"on"', cli)     # the old name still means on
        self.assertIn("CFSTR(SPEAKA_KEY_VOLUME_SYNC),mode", cli)
        local = helper.split('if(argc==3 && strcmp(argv[1],"--volume")==0) {', 1)[1].split("\n        }\n", 1)[0]
        self.assertIn('int enabled=!mode || (CFGetTypeID(mode)==CFStringGetTypeID() && CFEqual(mode,CFSTR("on")));', local)

    def test_only_windows_choices_move_ios_volume(self):
        harness = r"""
#include <assert.h>
#include "shared/VolumeMailbox.h"
int main(void) {
    SPVolumeMailbox m = {0};
    sp_volume_mailbox_set_from(&m, 1, 1, 51);                 /* SetAbsoluteVolume from Windows */
    assert(sp_volume_mailbox_enabled(&m) && sp_volume_mailbox_from_windows(&m) && sp_volume_mailbox_gain(&m) == 51);
    sp_volume_mailbox_set_from(&m, 1, 0, 51);                 /* Speaka's INTERIM at registration, or the phone's own */
    assert(sp_volume_mailbox_enabled(&m) && !sp_volume_mailbox_from_windows(&m) && sp_volume_mailbox_gain(&m) == 51);
    sp_volume_mailbox_set(&m, 1, 90);                         /* the older call: never Windows' choice */
    assert(!sp_volume_mailbox_from_windows(&m) && sp_volume_mailbox_gain(&m) == 90);
    sp_volume_mailbox_set_from(&m, 0, 1, 40);                 /* disabled: nothing, unity */
    assert(!sp_volume_mailbox_enabled(&m) && !sp_volume_mailbox_from_windows(&m) && sp_volume_mailbox_gain(&m) == 127);
    sp_volume_mailbox_set_from(&m, 1, 1, 0);                  /* mute is a level */
    assert(sp_volume_mailbox_from_windows(&m) && sp_volume_mailbox_gain(&m) == 0);
    return 0;
}
"""
        cc = shutil.which("cc") or shutil.which("gcc")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "provenance.c"; exe = Path(td) / "provenance"; src.write_text(harness)
            build = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT), str(src),
                                    str(ROOT / "shared/VolumeMailbox.c"), "-o", str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            self.assertEqual(subprocess.run([str(exe)], timeout=30).returncode, 0)
        adapter = self.read("tweak/VolumeControl.m")
        self.assertIn("sp_volume_mailbox_set_from(mailbox,1,!applying_local,value);", adapter)
        tick = adapter.split("        applying_local=1;\n", 1)[1].split("\n", 2)
        self.assertEqual(tick[0].strip(), "int ok=sp_volume_local(&state,&io,(unsigned)value);")
        self.assertEqual(tick[1].strip(), "applying_local=0;")
        helper = self.read("audiohelper/AudioHelper.m")
        self.assertIn("int windows=sp_volume_mailbox_from_windows(o->volume)?(int)gain:-1;", helper)
        # At connection, with no level Windows chose: the phone's own level is reported, never changed.
        self.assertIn("else if(windows<0) report_local_volume(ios);", helper)

    def test_mute_unmute_and_the_users_press_after(self):
        harness = r"""
#include <assert.h>
#include "shared/VolumeControl.h"
int main(void) {
    SPVolumeDomain d; sp_domain_init(&d, 1);
    assert(sp_domain_observed(&d, 70, 1000) == 2);
    int l = sp_domain_remote(&d, 0, 70, 2000); assert(l == 0); sp_domain_set(&d, 0, 2000);      /* Windows mutes */
    assert(sp_domain_observed(&d, 0, 2200) == 0);                                                /* its echo */
    assert(sp_domain_verify(&d, 0, 2400) == 0 && d.ios);
    l = sp_domain_remote(&d, 64, 0, 4000); assert(l == 64); sp_domain_set(&d, 64, 4000);        /* and unmutes */
    assert(sp_domain_observed(&d, 64, 4200) == 0 && sp_domain_verify(&d, 64, 4400) == 0);
    assert(sp_domain_observed(&d, 72, 6500) == 1);                                               /* the user's press */
    /* A reconnection: a new domain, the phone's level first. */
    SPVolumeDomain r; sp_domain_init(&r, 1);
    assert(sp_domain_observed(&r, 72, 100) == 2);
    return 0;
}
"""
        cc = shutil.which("cc") or shutil.which("gcc")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "mute.c"; exe = Path(td) / "mute"; src.write_text(harness)
            build = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + str(ROOT), str(src),
                                    str(ROOT / "shared/VolumeControl.c"), "-o", str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            self.assertEqual(subprocess.run([str(exe)], timeout=30).returncode, 0)

