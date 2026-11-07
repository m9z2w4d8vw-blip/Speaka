"""Start and Stop: the request the app writes, and the tweak validating it and restarting bluetoothd.

Sections, in order:
  - Start and Stop, and the output choice
"""
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---- Start and Stop, and the output choice ----
# v8.9: one-tap Start/Stop and audio output selection.
#
# The pure-C parts (request validation, output-route policy, channel-scoped stream control) are compiled and run on
# the host. The CoreFoundation and Objective-C glue cannot run here, so it is checked statically for the ordering and
# fail-closed properties the design depends on. Behaviour on the phone still needs a device test.

def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def shared_define(name):
    match = re.search(r'#define\s+%s\s+"([^"]+)"' % name, read("shared/SpeakaShared.h"))
    assert match, name
    return match.group(1)


class StartStop(unittest.TestCase):
    def compile_run(self, source, cpp=False, extra=()):
        compiler = shutil.which("c++" if cpp else "cc")
        self.assertIsNotNone(compiler)
        flags = ["-std=c++17" if cpp else "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                 "-I" + str(ROOT), "-I" + str(ROOT / "audiohelper"), *extra]
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
            ran = subprocess.run([str(exe)], text=True, capture_output=True, timeout=20, env=env)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    # ---------------------------------------------------------------- session request (daemon side, pure C)
    def test_session_request_validation(self):
        self.compile_run(r'''
#include <assert.h>
#include "tweak/Tweak.h"
int main(void) {
    assert(session_parse_action("start") == SESSION_ACTION_START);
    assert(session_parse_action("stop") == SESSION_ACTION_STOP);
    assert(session_parse_action("START") == SESSION_ACTION_NONE);
    assert(session_parse_action("restart") == SESSION_ACTION_NONE);
    assert(session_parse_action("") == SESSION_ACTION_NONE);
    assert(session_parse_action(NULL) == SESSION_ACTION_NONE);
    assert(session_nonce_valid("2F1E6A0C-9B1D-4C55-8A7E-0D3B5C6F7A81"));    /* NSUUID string from Settings */
    assert(session_nonce_valid("deadbeef"));
    assert(!session_nonce_valid("deadbee"));
    assert(!session_nonce_valid("0123456789abcdef0123456789abcdef012345678"));
    assert(!session_nonce_valid("2F1E6A0C 9B1D"));
    assert(!session_nonce_valid("nonce=1234;x"));
    assert(!session_nonce_valid(NULL));
    assert(session_request_fresh(1000.0, 1000.0));
    assert(session_request_fresh(1000.0, 1030.0));
    assert(!session_request_fresh(1000.0, 1030.5));
    assert(session_request_fresh(1004.0, 1000.0));
    assert(!session_request_fresh(1006.0, 1000.0));
    assert(!session_request_fresh(0.0, 10.0));
    assert(!session_request_fresh(-5.0, 10.0));
    return 0;
}
''')

    def test_session_entry_points_link_from_cpp(self):
        cc = shutil.which("cc")
        cxx = shutil.which("c++")
        self.assertTrue(cc and cxx)
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            (td / "provider.c").write_text('#include "tweak/Tweak.h"\n'
                                           'void SpeakaSessionStart(void) {}\n'
                                           'void SpeakaSessionReport(int a, int b) {(void)a;(void)b;}\n')
            (td / "consumer.cpp").write_text('#include "tweak/Tweak.h"\n'
                                             'int main() { SpeakaSessionStart(); SpeakaSessionReport(1, 0); return 0; }\n')
            subprocess.run([cc, "-std=c11", "-Wall", "-Werror", "-I", str(ROOT), "-c", str(td / "provider.c"),
                            "-o", str(td / "provider.o")], check=True)
            subprocess.run([cxx, "-std=c++17", "-Wall", "-Werror", "-I", str(ROOT), str(td / "consumer.cpp"),
                            str(td / "provider.o"), "-o", str(td / "link")], check=True)
            subprocess.run([str(td / "link")], check=True)

    def test_daemon_consumes_validates_and_verifies_before_restarting(self):
        code = read("tweak/Session.c")
        body = code.split("*/", 1)[1]          # the header comment may mention what NOT to do
        for forbidden in ("system(", "popen(", "posix_spawn", "execv", "execl", "fork(", "launchctl", "MSHookFunction"):
            self.assertNotIn(forbidden, body)
        self.assertIn("notify_register_dispatch(SPEAKA_NOTIFY_SESSION", body)
        self.assertIn("DISPATCH_QUEUE_SERIAL", body)
        take = body.split("static int session_take_record(", 1)[1].split("static int session_read_bool(", 1)[0]
        self.assertLess(take.index("CFPreferencesSetAppValue(key, NULL, SESSION_DOMAIN)"), take.index("return ok ? 1 : -2"))
        self.assertIn("return -1;", take)
        handle = body.split("static void session_handle_request(void)", 1)[1].split("void SpeakaSessionStart(void)", 1)[0]
        consume = handle.index("session_take_record(CFSTR(SPEAKA_KEY_SESSION_REQUEST)")
        refuse = handle.index("if (why) {")
        write = handle.index("if (!session_write_switches(")
        kill = handle.index("kill(getpid(), SIGKILL)")
        self.assertLess(consume, refuse)
        self.assertLess(refuse, write)
        self.assertLess(write, kill)
        self.assertIn("session_request_fresh(when, session_now())", handle)
        self.assertIn("session_nonce_valid(nonce)", handle)
        switches = body.split("static int session_write_switches(", 1)[1].split("static void session_handle_request(", 1)[0]
        for key in ("SPEAKA_KEY_SEVEN_SLOTS_ONCE", "SPEAKA_KEY_SINK_ONCE", "SPEAKA_KEY_EXPERIMENTS", "SPEAKA_KEY_SESSION_PENDING"):
            self.assertIn("CFSTR(%s)" % key, switches)
        self.assertIn("session_read_bool(CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE), &seven) || seven != start", switches)
        self.assertIn("session_read_bool(CFSTR(SPEAKA_KEY_SINK_ONCE), &decisive) || decisive != start", switches)
        report = body.split("void SpeakaSessionReport(", 1)[1]
        self.assertIn("session_take_record(CFSTR(SPEAKA_KEY_SESSION_PENDING)", report)
        self.assertIn("nonce=%s pid=%d result=%s", report)

    def test_restart_is_self_sigkill_like_killall(self):
        # bluetoothd runs as mobile, so system-domain launchctl is unavailable to it; signalling itself needs no
        # privilege and launchd relaunches it exactly as after `killall -9 bluetoothd`.
        self.assertIn("The daemon runs as user `mobile`", read("shared/SpeakaShared.h"))
        body = read("tweak/Session.c").split("*/", 1)[1]
        self.assertEqual(body.count("kill(getpid(), SIGKILL)"), 1)
        self.assertIn("#include <signal.h>", body)
        handle = body.split("static void session_handle_request(void)", 1)[1].split("void SpeakaSessionStart(void)", 1)[0]
        self.assertLess(handle.index("g_session_restarting = 1;"), handle.index("kill(getpid(), SIGKILL)"))
        self.assertLess(handle.index("SpeakaLogNow("), handle.index("kill(getpid(), SIGKILL)"))  # breadcrumb hits disk first

    def test_daemon_wiring_and_key_literals(self):
        tweak = read("tweak/Hooks.xm")
        self.assertIn('#include "tweak/Tweak.h"', tweak)
        ctor = tweak.split("%ctor {", 1)[1]
        self.assertIn("SpeakaLogInit();\n    SpeakaSessionStart();", ctor)
        self.assertLess(ctor.index("SpeakaSessionStart();"), ctor.index("find_main_image(&index, &mh)"))
        final = ctor.rindex("SpeakaSessionReport(g_sink_armed, sink_receive_ready());")
        self.assertLess(ctor.index("install_native_registration_hook((uintptr_t)mh, uuid_ok);"), final)
        self.assertNotIn("BTPSDaemonProbeStart", tweak)                     # v9.8: the v7.4/v7.5 probes are gone
        early = ctor.split("if (!find_main_image(&index, &mh)) {", 1)[1].split("return;", 1)[0]
        self.assertIn("SpeakaSessionReport(0, 0);", early)
        self.assertIn("tweak/Session.c", read("Makefile").split("Speaka_FILES", 1)[1].split("\n", 1)[0])
        # Settings, the tweak and the helper must agree on the literal the existing one-shot code consumes.
        self.assertEqual(shared_define("SPEAKA_KEY_SINK_ONCE"), "SpeakaV82DecisiveOnce")
        self.assertIn('CFSTR("SpeakaV82DecisiveOnce")', tweak)
        self.assertIn("CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE)", tweak)

    # ---------------------------------------------------------------- channel-scoped stream control (tweak)
    def test_stream_control_ignores_other_devices_once_pc_channel_is_known(self):
        tweak = read("tweak/Hooks.xm")
        note = tweak.split("static void note_signal(", 1)[1].split("/* v8.8.2: the verified native idle check", 1)[0]
        self.assertLess(note.index("if (sink_cid && cid!=sink_cid) return;"), note.index("stream_control_signal("))
        rx = tweak.split("static uint64_t hook_receive_signal(", 1)[1].split("static void install_signal_rx", 1)[0]
        self.assertIn("(control[1]&63u)==3u && sink_seid(control[2]>>2)", rx)   # v9.9.11: either registered Sink
        self.assertLess(rx.index("__atomic_store_n(&g_pc_signal_cid,cid,__ATOMIC_RELEASE)"),
                        rx.index("note_signal(0,cid,control,(size_t)length)"))
        self.compile_run(r'''
#include <assert.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
#include "tweak/StreamControl.c"
static unsigned g_relay_ready=1,g_sink_accepted=1;
/* v9.6: the PC's side of the stream, as the app reads it (diag.v95.peer). */
static char peer[32]="";
void SpeakaSetStatus(const char *key,const char *fmt,...) {
    if(strcmp(key,"diag.v95.peer")) return;
    va_list ap;va_start(ap,fmt);vsnprintf(peer,sizeof peer,fmt,ap);va_end(ap);
}
static volatile uint64_t g_pc_signal_cid=0;
static StreamControl g_stream_control;
static pthread_mutex_t g_stream_control_lock=PTHREAD_MUTEX_INITIALIZER;
static int last_active=-9,changes=0;
void AudioRelaySetActive(int active) {last_active=active;changes++;}
static unsigned last_state=0;
void SpeakaVolumeDisconnect(void) {}
void AudioRelaySetStreamState(uint32_t state) {last_state=state;}   /* v9.9: what the helper is told */
void SpeakaLogNow(const char *,...) {}
static uint64_t speaka_since_launch_ms(void) {return 0;}
static void note_signal(''' + note + r'''
int main() {
    memset(&g_stream_control,0,sizeof g_stream_control);
    g_stream_control.local_seid=7;g_stream_control.remote_seid=52;
    /* PC on CID 0x108; earbuds on CID 0x140 whose endpoint happens to also be SEID 52. */
    const uint8_t start_pc[3]={0x30,0x07,0x1c},accept_start_pc[2]={0x32,0x07};
    note_signal(0,0x108,start_pc,3);assert(changes==0);
    note_signal(1,0x108,accept_start_pc,2);assert(changes==1 && last_active==1);
    assert(!strcmp(peer,"streaming"));
    assert(last_state==AUDIO_STREAM_STREAMING);
    /* Channel not yet known (old behaviour): an earbud SUSPEND with a colliding SEID would stop forwarding. */
    const uint8_t suspend_buds[3]={0x50,0x09,0xd0};
    note_signal(1,0x140,suspend_buds,3);assert(changes==2 && last_active==0);
    assert(last_state==AUDIO_STREAM_PAUSED);     /* v9.9: a SUSPEND is a pause: the stream stays open */
    /* Channel known: earbud traffic is ignored; the PC still starts and stops the stream. */
    g_pc_signal_cid=0x108;changes=0;last_active=-9;strcpy(peer,"streaming");
    note_signal(1,0x140,suspend_buds,3);assert(changes==0);
    assert(!strcmp(peer,"streaming"));                    /* earbud traffic doesn't touch the PC's status */
    const uint8_t start_buds[3]={0x60,0x07,0xd0},accept_buds[2]={0x62,0x07};
    note_signal(1,0x140,start_buds,3);note_signal(0,0x140,accept_buds,2);assert(changes==0);
    const uint8_t start_pc2[3]={0x40,0x07,0x1c},accept_pc2[2]={0x42,0x07};
    note_signal(0,0x108,start_pc2,3);note_signal(1,0x108,accept_pc2,2);
    assert(changes==1 && last_active==1);
    const uint8_t suspend_pc[3]={0x70,0x09,0x1c};
    note_signal(0,0x108,suspend_pc,3);assert(changes==2 && last_active==0);
    assert(!strcmp(peer,"paused"));                       /* a pause: the PC is still connected */
    const uint8_t close_pc[3]={0x80,0x08,0x1c};
    note_signal(0,0x108,close_pc,3);assert(!strcmp(peer,"closed"));
    const uint8_t start_pc3[3]={0x90,0x07,0x1c},accept_pc3[2]={0x92,0x07};
    note_signal(0,0x108,start_pc3,3);note_signal(1,0x108,accept_pc3,2);
    assert(!strcmp(peer,"streaming"));
    const uint8_t abort_pc[3]={0xa0,0x0a,0x1c};
    note_signal(0,0x108,abort_pc,3);assert(!strcmp(peer,"closed"));
    g_sink_accepted=0;changes=0;
    note_signal(0,0x108,suspend_pc,3);assert(changes==0);
    return 0;
}
''', cpp=True)

    # ---------------------------------------------------------------- output policy (helper, pure C)
    def test_output_route_policy(self):
        self.compile_run(r'''
#include <assert.h>
#include <stdio.h>
#include "OutputPolicy.h"
int main(void) {
    const output_port_t speaker[]={{OUTPUT_PORT_SPEAKER,"Speaker","Speaker"}};
    const output_port_t receiver[]={{OUTPUT_PORT_RECEIVER,"Built-In Receiver","Receiver"}};
    const output_port_t airpods[]={{OUTPUT_PORT_HEADPHONES,"AA:BB:CC:DD:EE:01-tacl","AirPods Pro"}};
    const output_port_t wired[]={{OUTPUT_PORT_HEADPHONES,"Wired Headphones","Headphones"}};
    const output_port_t pc[]={{OUTPUT_PORT_HEADPHONES,"11:22:33:44:55:66-tacl","LAPTOP-UGIVBIA5"}};
    const output_port_t airplay[]={{OUTPUT_PORT_OTHER,"airplay-1","Living Room"}};
    const output_port_t mixed[]={{OUTPUT_PORT_HEADPHONES,"h","Headphones"},{OUTPUT_PORT_OTHER,"hdmi","HDMI"}};
    const output_port_t speaker_plus[]={{OUTPUT_PORT_SPEAKER,"Speaker","Speaker"},{OUTPUT_PORT_OTHER,"a","TV"}};
    const output_port_t unnamed[]={{OUTPUT_PORT_HEADPHONES,"",""}};
    const output_never_t never_uid[]={{"11:22:33:44:55:66-TACL",""}};
    const output_never_t never_name[]={{"","laptop-ugivbia5"}};
    const output_never_t never_builtin[]={{"Speaker","Speaker"},{"Built-In Receiver","Receiver"}};
    const output_never_t blank[]={{"",""}},nulls[]={{NULL,NULL}};
    size_t bad=99;

    assert(output_mode_from_string("speaker")==OUTPUT_MODE_SPEAKER);
    assert(output_mode_from_string("headphones")==OUTPUT_MODE_HEADPHONES);
    assert(output_mode_from_string("any")==OUTPUT_MODE_ANY);
    assert(output_mode_from_string(NULL)==OUTPUT_MODE_SPEAKER);
    assert(output_mode_from_string("Headphones")==OUTPUT_MODE_SPEAKER);
    assert(output_mode_from_string("bogus")==OUTPUT_MODE_SPEAKER);
    for(int m=0;m<3;m++) assert(output_mode_from_string(output_mode_name(m))==m);

    /* speaker: exactly the built-in speaker, as in v8.8.1 */
    assert(output_route_check(OUTPUT_MODE_SPEAKER,speaker,1,NULL,0,&bad)==OUTPUT_ROUTE_OK);
    assert(output_route_check(OUTPUT_MODE_SPEAKER,speaker,1,never_builtin,2,&bad)==OUTPUT_ROUTE_OK);
    assert(output_route_check(OUTPUT_MODE_SPEAKER,airpods,1,NULL,0,&bad)==OUTPUT_ROUTE_NOT_SPEAKER);
    assert(output_route_check(OUTPUT_MODE_SPEAKER,receiver,1,NULL,0,&bad)==OUTPUT_ROUTE_NOT_SPEAKER);
    assert(output_route_check(OUTPUT_MODE_SPEAKER,speaker_plus,2,NULL,0,&bad)==OUTPUT_ROUTE_NOT_SPEAKER);
    assert(output_route_check(OUTPUT_MODE_SPEAKER,speaker,0,NULL,0,&bad)==OUTPUT_ROUTE_NO_OUTPUT);
    assert(output_route_check(OUTPUT_MODE_SPEAKER,NULL,1,NULL,0,&bad)==OUTPUT_ROUTE_NO_OUTPUT);
    assert(output_route_check(7,airpods,1,NULL,0,&bad)==OUTPUT_ROUTE_NOT_SPEAKER); /* unknown mode */

    /* headphones: wired or Bluetooth only; waits on the speaker; refuses the never-listed PC */
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,airpods,1,never_uid,1,&bad)==OUTPUT_ROUTE_OK);
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,wired,1,NULL,0,&bad)==OUTPUT_ROUTE_OK);
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,speaker,1,NULL,0,&bad)==OUTPUT_ROUTE_NOT_HEADPHONES);
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,receiver,1,NULL,0,&bad)==OUTPUT_ROUTE_NOT_HEADPHONES);
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,airplay,1,NULL,0,&bad)==OUTPUT_ROUTE_NOT_HEADPHONES);
    bad=99;assert(output_route_check(OUTPUT_MODE_HEADPHONES,mixed,2,NULL,0,&bad)==OUTPUT_ROUTE_NOT_HEADPHONES && bad==1);
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,pc,1,NULL,0,&bad)==OUTPUT_ROUTE_OK); /* not marked yet */
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,pc,1,never_uid,1,&bad)==OUTPUT_ROUTE_NEVER_LISTED);
    assert(output_route_check(OUTPUT_MODE_HEADPHONES,pc,1,never_name,1,&bad)==OUTPUT_ROUTE_NEVER_LISTED);

    /* any: everything except the never-play list; the iPhone's own outputs can never be excluded */
    assert(output_route_check(OUTPUT_MODE_ANY,speaker,1,never_builtin,2,&bad)==OUTPUT_ROUTE_OK);
    assert(output_route_check(OUTPUT_MODE_ANY,receiver,1,never_builtin,2,&bad)==OUTPUT_ROUTE_OK);
    assert(output_route_check(OUTPUT_MODE_ANY,airplay,1,never_uid,1,&bad)==OUTPUT_ROUTE_OK);
    assert(output_route_check(OUTPUT_MODE_ANY,mixed,2,NULL,0,&bad)==OUTPUT_ROUTE_OK);
    bad=99;assert(output_route_check(OUTPUT_MODE_ANY,pc,1,never_name,1,&bad)==OUTPUT_ROUTE_NEVER_LISTED && bad==0);
    assert(output_route_check(OUTPUT_MODE_ANY,unnamed,1,blank,1,&bad)==OUTPUT_ROUTE_OK);  /* blanks never match */
    assert(output_route_check(OUTPUT_MODE_ANY,airpods,1,nulls,1,&bad)==OUTPUT_ROUTE_OK);
    for(int r=0;r<=4;r++) assert(output_route_reason(r)[0]);
    puts("v8.9 output policy passed");
    return 0;
}
''')

    def test_helper_output_wiring(self):
        helper = read("audiohelper/AudioHelper.m")
        external = helper.split("static int configure_external(", 1)[1].split("static int builtin_only(", 1)[0]
        self.assertIn("AVAudioSessionCategoryPlayback", external)
        self.assertNotIn("PlayAndRecord", external)
        self.assertLess(external.index("route_verdict(session,&device)"), external.index("setActive:YES"))
        self.assertIn("return -2;", external)
        kinds = helper.split("static output_kind_t port_kind(", 1)[1].split("static NSString *text_field(", 1)[0]
        for port in ("BuiltInSpeaker", "BuiltInReceiver", "Headphones", "USBAudio", "BluetoothA2DP",
                     "BluetoothLE", "BluetoothHFP"):
            self.assertIn("AVAudioSessionPort" + port, kinds)
        load = helper.split("static void load_output_settings(void)", 1)[1].split("static int route_verdict(", 1)[0]
        self.assertIn("CFSTR(SPEAKA_KEY_OUTPUT_MODE)", load)
        self.assertIn("CFSTR(SPEAKA_KEY_NEVER_OUTPUTS)", load)
        self.assertIn("notify_register_check(SPEAKA_NOTIFY_OUTPUT,&g_output_notify)", helper)
        self.assertIn("notify_check(g_output_notify,&settings_changed)", helper)
        status = helper.split("static void publish_status(NSString *state,NSString *reason,BOOL force) {", 1)[1]
        status = status.split("static void log_route(", 1)[0]
        self.assertLess(status.index("dispatch_async(g_status_queue"), status.index("CFSTR(SPEAKA_KEY_HELPER_STATUS)"))
        health = helper.split("static void report_health(", 1)[1].split("static int read_exact(", 1)[0]
        # v8.9.4: an unacceptable route is reported as reason bit 0x20 (any reason means "not playing").
        self.assertIn("if(route_verdict([AVAudioSession sharedInstance],NULL)) why|=0x20u;", health)
        self.assertIn("why?-(int32_t)why:1", health)
        # The render callback and level meter that test_v881 compiles must not depend on the new code.
        render = helper.split("#define PCM_CAPACITY", 1)[1].split("static void stop_output", 1)[0]
        for term in ("route_verdict", "publish_status", "g_output_mode", "NSString"):
            self.assertNotIn(term, render)
        policy = read("audiohelper/OutputPolicy.h")
        for mode in ("speaker", "headphones", "any"):
            self.assertIn('"%s"' % mode, policy)

    # ---------------------------------------------------------------- Settings page
    def test_settings_page_wiring(self):
        plist = plistlib.loads((ROOT / "settings/Resources/Root.plist").read_bytes())
        items = plist["items"]
        controller = read("settings/SettingsPage.m")
        # Every selector the plist names must exist, or tapping the row crashes Settings.
        for item in items:
            for field in ("get", "action"):
                if field in item:
                    selector = item[field]
                    self.assertRegex(controller, r"- \((?:id|void)\)%s\(id\)specifier" % re.escape(selector), selector)
        # v9.7: Start, Stop, the session and the audio output are the app's now, through the same protocol
        # (app/Speaka/Model/SPTweakClient.m). The page's code behind them stays, unused.
        actions = {item.get("action") for item in items}
        self.assertFalse({"startSink:", "stopSink:", "chooseOutput:"} & actions)
        self.assertNotIn("session", {item.get("id") for item in items})
        self.assertEqual([item for item in items if item.get("cell") == "PSLinkListCell"], [])
        client = read("app/Speaka/Model/SPTweakClient.m")
        for name in ("@SPEAKA_KEY_SESSION_REQUEST", "SPEAKA_NOTIFY_SESSION", "@SPEAKA_KEY_OUTPUT_MODE",
                     "SPEAKA_NOTIFY_OUTPUT"):
            self.assertIn(name, client)
        modes = client.split("+ (NSArray<NSDictionary<NSString *, NSString *> *> *)outputModes", 1)[1].split("];", 1)[0]
        self.assertEqual(re.findall(r'@"key": @"([^"]+)"', modes), ["speaker", "headphones", "any"])
        # v9.8: the page only reads. It never writes a request or arms a switch, and has no switches to arm.
        self.assertEqual([item for item in items if item.get("cell") == "PSSwitchCell"], [])
        for name in ("SPEAKA_KEY_SESSION_REQUEST", "SPEAKA_NOTIFY_SESSION", "notify_post", "CFPreferencesSetAppValue",
                     "SPEAKA_KEY_SEVEN_SLOTS_ONCE", "SPEAKA_KEY_SINK_ONCE", "SpeakaV81SevenOnce", "SpeakaV82DecisiveOnce"):
            self.assertNotIn(name, controller)
        self.assertNotIn("AVKit", read("settings/Makefile"))

    def test_the_app_never_advises_toggling_bluetooth(self):
        # Turning Bluetooth off and on does not restart bluetoothd and drops a live Sink: never recommend it.
        # (v9.8: this was a check on the Settings page's Start; the app is the only place with Start now.)
        texts = "".join(read(f) for f in ("app/Speaka/SpeakaModel.swift", "app/Speaka/SessionSteps.swift",
                                          "app/Speaka/Model/SPTweakClient.m"))
        for advice in ("turn Bluetooth off and on", "Turn Bluetooth off and on", "toggle Bluetooth", "Airplane Mode off/on",
                       "auto-restart unavailable"):
            self.assertNotIn(advice, texts)
        self.assertIn('"Bluetooth was turned off and on, which removed the speaker. Tap Start."', texts)

    def test_sink_loss_after_bluetooth_toggle_is_reported(self):
        tweak = read("tweak/Hooks.xm")
        hook = tweak.split("static uint64_t hook_native_registration(", 1)[1] \
            .split("static void install_native_registration_hook(", 1)[0]
        self.compile_run(r'''
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "shared/AudioRing.h"
typedef uint64_t (*source_registration_fn_t)(uint64_t);
static source_registration_fn_t g_source_reg_orig;
static int g_sink_armed = 1;
static volatile unsigned g_native_source_success = 6, g_audio_table_extended = 0, g_sink_accepted = 0;
static volatile unsigned g_native_loops = 0;
static pthread_mutex_t g_stream_control_lock = PTHREAD_MUTEX_INITIALIZER;
static int post_source_calls = 0, set_active_calls = 0, last_active = -9;
static char last_sep[200];
static void SpeakaLogNow(const char *, ...) {}
static void SpeakaSetStatus(const char *key, const char *fmt, ...) {
    if (!strcmp(key, "diag.v82.sep")) snprintf(last_sep, sizeof last_sep, "%s", fmt);
}
static void AudioRelaySetActive(int active) { set_active_calls++; last_active = active; }
static unsigned last_stream_state = 0;
static unsigned volume_disconnects;
static void SpeakaVolumeDisconnect(void) { ++volume_disconnects; }
static void AudioRelaySetStreamState(uint32_t state) { last_stream_state = state; }
static void register_sink_after_sources(uint64_t) {   /* first power-up installs the Sink, later ones cannot */
    post_source_calls++;
    if (post_source_calls == 1) { g_audio_table_extended = 1; g_sink_accepted = 1; }
}
static uint64_t native_loop(uint64_t) { return 0; }
static uint64_t hook_native_registration(''' + hook + r'''
int main() {
    g_source_reg_orig = native_loop;
    hook_native_registration(0x1000);                 /* bluetoothd starts: Sink installed */
    assert(post_source_calls == 1 && g_sink_accepted == 1 && set_active_calls == 0 && !last_sep[0]);
    hook_native_registration(0x2000);                 /* Bluetooth off/on inside the same process */
    assert(g_sink_accepted == 0 && set_active_calls == 1 && last_active == 0);
    assert(strncmp(last_sep, "LOST", 4) == 0 && strstr(last_sep, "tap Start"));
    assert(last_stream_state == AUDIO_STREAM_CLOSED);  /* v9.9: the speaker's stream is gone; the helper may let go */
    assert(volume_disconnects == 1);
    /* Not armed: nothing is reported or changed. */
    g_sink_armed = 0; last_sep[0] = 0; set_active_calls = 0;
    hook_native_registration(0x3000);
    assert(set_active_calls == 0 && !last_sep[0]);
    assert(volume_disconnects == 1);
    return 0;
}
''', cpp=True)

    def test_postinst_restarts_bluetoothd_to_load_the_new_tweak(self):
        post = read("layout/DEBIAN/postinst")
        restart = [line for line in post.splitlines() if "killall -9 bluetoothd" in line]
        self.assertEqual(len(restart), 1)
        self.assertTrue(restart[0].rstrip().endswith("|| true"), restart[0])   # can never fail the install
        self.assertLess(post.index("launchctl bootstrap system"), post.index("killall -9 bluetoothd"))
        self.assertLess(post.index("killall -9 bluetoothd"), post.index("\nfi\nexit 0"))  # inside the configure branch
        subprocess.run(["sh", "-n", str(ROOT / "layout/DEBIAN/postinst")], check=True)


if __name__ == "__main__":
    unittest.main()
