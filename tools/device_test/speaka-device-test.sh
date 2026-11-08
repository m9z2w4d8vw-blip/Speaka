#!/bin/sh
# Speaka device test. Run as mobile over SSH:   sh /tmp/speaka-device-test.sh
#
# 1. Launch controls: the installed SpeakaAudioHelper (a binary known to work on this phone), the same binary copied
#    to /tmp, and the two smoke binaries (arm64-only, and arm64 + arm64e) from /tmp. Together they separate
#    "SSH can't launch anything", "/tmp is the problem", "the arm64-only recipe is the problem".
# 2. The AAC self-test inside the installed helper, stage by stage: entry, create, decode1, full.
#
# Every test's stdout, stderr and exit status go to /tmp/speaka-device-test/<name>.{out,err,exit}; summary.txt
# lists them. Nothing here needs root; nothing is installed or changed.
emulate sh 2>/dev/null || true

# The defaults are the phone's paths; the variables exist so the script can be tested off the phone.
OUT=${SPEAKA_OUT:-/tmp/speaka-device-test}
HELPER=${SPEAKA_HELPER:-/var/jb/usr/bin/SpeakaAudioHelper}
CRASH=${SPEAKA_CRASH_DIR:-/var/mobile/Library/Logs/CrashReporter}
TIMEOUT=${SPEAKA_TIMEOUT:-90}
rm -rf "$OUT"
mkdir -p "$OUT" || exit 1
SUMMARY="$OUT/summary.txt"
FAILED=0

say() { echo "$*"; echo "$*" >>"$SUMMARY"; }

# run NAME EXPECTED_EXIT COMMAND...   (portable timeout: no coreutils needed)
run() {
    name=$1; expected=$2; shift 2
    start=$(date +%s)
    "$@" >"$OUT/$name.out" 2>"$OUT/$name.err" </dev/null &
    pid=$!
    waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge "$TIMEOUT" ]; then
            kill -9 "$pid" 2>/dev/null
            echo "TIMEOUT after ${TIMEOUT}s" >>"$OUT/$name.err"
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    wait "$pid"
    code=$?
    end=$(date +%s)
    echo "$code" >"$OUT/$name.exit"
    signal=""
    if [ "$code" -gt 128 ]; then signal=" (signal $((code - 128)))"; fi
    verdict=ok
    if [ "$code" != "$expected" ]; then verdict=UNEXPECTED; FAILED=$((FAILED + 1)); fi
    say "$verdict  $name  exit=$code$signal expected=$expected  seconds=$((end - start))"
    first=$(head -n 1 "$OUT/$name.err" 2>/dev/null)
    if [ -n "$first" ]; then say "      stderr: $first"; fi
    first=$(head -n 1 "$OUT/$name.out" 2>/dev/null)
    if [ -n "$first" ]; then say "      stdout: $first"; fi
}

say "== Speaka device test  $(date)"
say "   $(uname -a)"
say "   user: $(id)"
for f in "$HELPER" /tmp/speaka-smoke-arm64 /tmp/speaka-smoke-fat; do
    if [ -e "$f" ]; then say "   present: $(ls -l "$f")"; else say "   missing: $f"; fi
done
ls -1t "$CRASH" >"$OUT/crash-before.txt" 2>/dev/null

say ""
say "== Launch controls"
if [ -x "$HELPER" ]; then
    run helper-installed 2 "$HELPER"
    cp "$HELPER" /tmp/speaka-helper-copy && chmod 755 /tmp/speaka-helper-copy && run helper-copy-in-tmp 2 /tmp/speaka-helper-copy
    rm -f /tmp/speaka-helper-copy
else
    say "skip: $HELPER not installed"
fi
for s in speaka-smoke-arm64 speaka-smoke-fat; do
    if [ -e "/tmp/$s" ]; then chmod 755 "/tmp/$s"; run "$s" 0 "/tmp/$s"; else say "skip: /tmp/$s not copied"; fi
done

say ""
say "== AAC self-test (the real decoder and AudioToolbox, inside the installed helper)"
if [ -x "$HELPER" ]; then
    run selftest-entry 0 "$HELPER" --aac-selftest --stage=entry
    if grep -q "STAGE entry" "$OUT/selftest-entry.err" 2>/dev/null; then
        run selftest-create 0 "$HELPER" --aac-selftest --stage=create
        run selftest-decode1 0 "$HELPER" --aac-selftest --stage=decode1
        run selftest-full 0 "$HELPER" --aac-selftest
        say "   strategy results:"
        grep "strategy_done" "$OUT/selftest-full.err" 2>/dev/null | while read -r line; do say "     $line"; done
    else
        say "   The installed helper has no --aac-selftest: install the Speaka build from this run first."
    fi
fi

say ""
say "== Crash reports written during this run"
ls -1t "$CRASH" >"$OUT/crash-after.txt" 2>/dev/null
new=0
while read -r report; do
    if ! grep -qx "$report" "$OUT/crash-before.txt" 2>/dev/null; then
        new=1
        say "   new: $report"
        head -c 4000 "$CRASH/$report" >"$OUT/crash-$report.txt" 2>/dev/null || say "     (not readable as mobile)"
    fi
done <"$OUT/crash-after.txt"
if [ "$new" = 0 ]; then say "   none"; fi

say ""
say "== $FAILED unexpected result(s). Files: $OUT"
exit "$FAILED"
