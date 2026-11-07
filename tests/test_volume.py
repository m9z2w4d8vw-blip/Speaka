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
        self.assertLess(install.index("if(!mode) return;"), install.index("MSHookFunction("))
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
        self.assertIn('hlog(stdout,"V106_VOLUME gain=%u/127 (%u%%) output=%s\\n"', helper)

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
        self.assertIn("uint64_t result=original_avrcp_connect(manager,device);", adapter)
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
        self.assertIn("sp_volume_mailbox_set(mailbox,1,state.value);engaged=1;", receive)

