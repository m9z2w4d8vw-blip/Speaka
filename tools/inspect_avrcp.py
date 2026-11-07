#!/usr/bin/env python3
"""Read-only Mach-O AVRCP evidence collector (LIEF + Capstone).

No binary is loaded/executed, written, patched or re-signed. Function boundaries
come from LC_FUNCTION_STARTS. Labels below are descriptive analyst names unless
explicitly identified as Objective-C method metadata. ADRP/ADD and LDR reference
annotations use bounded straight-line propagation, not a complete dataflow proof.
"""

import lief, capstone, bisect, re, struct


class MachO:
    def __init__(self, path):
        self.path = path
        self.raw = open(path, "rb").read()
        self.b = lief.MachO.parse(path).at(0)
        self.base = self.b.imagebase
        self.cs = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
        self.cs.skipdata = True
        self.starts = [self.base + x for x in self.b.function_starts.functions]
        self.secs = list(self.b.sections)
        self.strings = {}
        for s in self.secs:
            if s.name in [
                "__cstring",
                "__oslogstring",
                "__objc_methname",
                "__objc_classname",
                "__objc_methtype",
            ]:
                for m in re.finditer(rb"[^\0]+", bytes(s.content)):
                    self.strings[s.virtual_address + m.start()] = m[0].decode(
                        errors="replace"
                    )
        self.ptrs = {
            r.address: r.target for r in self.b.relocations if hasattr(r, "target")
        }
        self.bindings = {r.address: r.symbol.name for r in self.b.bindings}
        self.cfstrings = {}
        for s in self.secs:
            if s.name == "__cfstring":
                for a in range(s.virtual_address, s.virtual_address + s.size, 32):
                    if self.string(self.ptr(a + 16)):
                        self.cfstrings[a] = self.string(self.ptr(a + 16))
        self.labels = {}
        self.methods = {}
        for s in self.secs:
            if s.name == "__objc_methlist":
                p = s.virtual_address
                end = p + s.size
                while p + 8 <= end:
                    flags, count = struct.unpack("<II", self.read(p, 8))
                    size = flags & 0xFFFF
                    if not size or count > 10000:
                        break
                    for i in range(count):
                        a = p + 8 + i * size
                        if flags & 0x80000000:
                            n, t, f = struct.unpack("<iii", self.read(a, 12))
                            sel = a + n
                            if not flags & 0x40000000:
                                sel = self.ptr(sel)
                            imp = a + 8 + f
                            typ = a + 4 + t
                        else:
                            sel, typ, imp = (
                                self.ptr(a),
                                self.ptr(a + 8),
                                self.ptr(a + 16),
                            )
                        self.methods[imp] = (self.string(sel), self.string(typ))
                        self.labels[imp] = self.string(sel)
                    p = (p + 8 + count * size + 7) & ~7
        for s in self.secs:
            if s.name in ["__auth_stubs", "__stubs", "__objc_stubs"]:
                stride = 0x20 if s.name == "__objc_stubs" else 0x10
                for p in range(s.virtual_address, s.virtual_address + s.size, stride):
                    refs = self.refs(list(self.cs.disasm_lite(self.read(p, stride), p)))
                    names = []
                    for a, v in refs:
                        if v in self.bindings:
                            names.append(self.bindings[v])
                        elif self.string(self.ptr(v)):
                            names.append("objc:" + self.string(self.ptr(v)))
                    if names:
                        self.labels[p] = " / ".join(dict.fromkeys(names))
        s = self.b.get_section("__text")
        self.ins = list(self.cs.disasm_lite(bytes(s.content), s.virtual_address))
        self.references = self.refs(self.ins)
        self.calls = {}
        for a, size, m, op in self.ins:
            if m in ["bl", "b"] and op.startswith("#0x"):
                self.calls.setdefault(int(op[1:], 16), []).append(a)

    def read(self, a, n):
        for s in self.secs:
            if (
                s.offset
                and s.virtual_address <= a
                and a + n <= s.virtual_address + s.size
            ):
                return self.raw[
                    s.offset
                    + a
                    - s.virtual_address : s.offset
                    + a
                    - s.virtual_address
                    + n
                ]
        return b""

    def ptr(self, a):
        if a in self.ptrs:
            return self.ptrs[a]
        data = self.read(a, 8)
        return struct.unpack("<Q", data)[0] if len(data) == 8 else 0

    def string(self, a):
        return self.strings.get(a, "")

    def label(self, a):
        if a in self.labels:
            return self.labels[a]
        if a in self.cfstrings:
            return "CF:" + repr(self.cfstrings[a])
        if a in self.bindings:
            return self.bindings[a]
        if a in self.strings:
            return repr(self.strings[a])
        ptr = self.ptr(a)
        if ptr in self.strings:
            return "->" + repr(self.strings[ptr])
        # CFString record
        ptr = self.ptr(a + 16)
        if ptr in self.strings and self.read(a + 8, 4) == b"\xc8\x07\0\0":
            return "CF:" + repr(self.strings[ptr])
        return ""

    def func(self, a):
        i = bisect.bisect_right(self.starts, a) - 1
        return self.starts[i] if i >= 0 else a

    def refs(self, ins):
        regs = {}
        out = []
        lastend = 0
        for a, size, m, op in ins:
            if a != lastend:
                regs = {}
            lastend = a + size
            parts = op.split(", ")
            val = None
            ref = None
            if m in ["adrp", "adr"] and len(parts) == 2:
                val = int(parts[1][1:], 16)
                ref = val if m == "adr" else None
            elif (
                m == "add"
                and len(parts) >= 3
                and parts[1] in regs
                and parts[2].startswith("#")
            ):
                val = regs[parts[1]][0] + int(parts[2][1:], 0)
                ref = val
            elif m.startswith("ldr") and len(parts) == 2 and parts[1].startswith("#0x"):
                ref = int(parts[1][1:], 16)
            elif m.startswith("ldr") and len(parts) >= 2:
                match = re.search(r"\[(x\d+)(?:, #(0x[\da-f]+|\d+))?\]", op)
                if match and match[1] in regs:
                    ref = regs[match[1]][0] + int(match[2] or "0", 0)
            if ref is not None:
                out.append((a, ref))
            # Limited straight-line address propagation; never infer through loads/calls/branches.
            if m in ["bl", "blr", "blraa", "blrab"]:
                regs = {k: v for k, v in regs.items() if int(k[1:]) >= 19}
            elif (
                m in ["b", "br", "ret", "retab"]
                or m.startswith("b.")
                or m in ["cbz", "cbnz", "tbz", "tbnz"]
            ):
                regs = {}
            elif (
                parts
                and re.fullmatch(r"[xw]\d+", parts[0])
                and m not in ["cmp", "tst", "str", "strb", "strh", "stp"]
            ):
                reg = "x" + parts[0][1:]
                regs.pop(reg, None)
                if val is not None:
                    regs[reg] = (val, a)
            regs = {k: v for k, v in regs.items() if a - v[1] <= 80}
        return out

    def show(self, start, end=None):
        if end is None:
            start = self.func(start)
            i = bisect.bisect_right(self.starts, start)
            end = self.starts[i] if i < len(self.starts) else start + 512
        ins = list(self.cs.disasm_lite(self.read(start, end - start), start))
        refs = dict(self.refs(ins))
        print(f"FUNCTION {start:#x} {self.label(start)} end {end:#x}")
        for a, size, m, op in ins:
            target = (
                int(op[1:], 16)
                if m in ["bl", "b"] and op.startswith("#0x")
                else refs.get(a)
            )
            comment = self.label(target) if target is not None else ""
            if target is not None and not comment:
                comment = f"ref {target:#x}"
            print(f"{a:011x}: {self.read(a,4).hex()} {m:9s} {op:46s} {comment}")

    def search(self, pattern):
        rx = re.compile(pattern, re.I)
        for a, v in self.references:
            lab = self.label(v)
            if lab and rx.search(lab):
                print(f"{a:#x} function {self.func(a):#x} -> {v:#x} {lab}")


IDENTITIES = {
    "BTAudioHALPlugin": "5194ccb2c0168bb7a5b70ee039024b194a5497b66dc5fc8fa72e21cb8ede1f68",
    "bluetoothd": "e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6",
    "BTAvrcp": "4bd97270734de51a461fd787aac6b76b574df656c5c234817b8bbd8bdb1bfa61",
}
# Each value is (function entry offset, descriptive identification, excerpt ranges).
# An empty range list means dump the complete function.
TARGETS = {
    "bluetoothd": [
        (
            0x54F5C0,
            "Register AVRCP target/controller callbacks",
            [(0x54F65C, 0x54F674)],
        ),
        (
            0xE1A98,
            "Target registration / callback structure copy",
            [(0xE1B34, 0xE1BD0)],
        ),
        (
            0xE0FF4,
            "Target vendor command parser and dispatcher (includes inlined UnmarshalCommand)",
            [
                (0xE0FF4, 0xE1108),
                (0xE127C, 0xE12C8),
                (0xE1310, 0xE1398),
                (0xE142C, 0xE14B4),
                (0xE15D0, 0xE15EC),
                (0xE1618, 0xE1638),
                (0xE188C, 0xE1894),
            ],
        ),
        (0x551CF4, "Target GetCapabilities", [(0x551E24, 0x551E7C)]),
        (
            0x552CF8,
            "Target RegisterNotification",
            [(0x552D78, 0x552D94), (0x552E90, 0x552ED4)],
        ),
        (0x55B940, "Target notification callback thunk", []),
        (
            0xDECAC,
            "Simple metadata response builder",
            [(0xDECAC, 0xDED18), (0xDED58, 0xDED94)],
        ),
        (0xE28E4, "Target AV/C response entry", []),
        (
            0xDD580,
            "Common AV/C serializer and AVCTP send",
            [(0xDD5A8, 0xDD5E8), (0xDD61C, 0xDD6E4), (0xDD6EC, 0xDD738)],
        ),
        (0x93FE0, "Controller SetAbsoluteVolume command builder", []),
        (
            0x559FCC,
            "Controller scalar-to-wire-volume setter",
            [(0x55A1A0, 0x55A1B0), (0x55A24C, 0x55A270)],
        ),
        (0xDDC64, "Controller RegisterNotification command builder", []),
        (
            0xDDEF8,
            "OI_AVRCPController_UnmarshalMetaResponse (assertion identification)",
            [(0xDDF78, 0xDDF94), (0xDE038, 0xDE070)],
        ),
        (
            0x5593E4,
            "Controller metadata response handler",
            [
                (0x559584, 0x559600),
                (0x559884, 0x5598AC),
                (0x559BF8, 0x559C20),
                (0x559CFC, 0x559D14),
            ],
        ),
        (
            0x37EC98,
            "Remote-volume bridge toward native audio device",
            [(0x37ED34, 0x37ED74)],
        ),
        (
            0x380FE8,
            "Audio volumeDidUpdate schedules controller send",
            [(0x380FE8, 0x381080)],
        ),
        (0x3810DC, "Scheduled audio-volume send block", []),
        (0x26F1B8, "Native audio volume-property wrapper", []),
        (0x26FCA4, "Native float property conversion and notification", []),
        (0x55B2F4, "Connection teardown callback thunk (ABI candidate)", []),
        (
            0x550EB0,
            "Connection teardown removes registered per-peer state",
            [(0x550F7C, 0x5510EC)],
        ),
        (0xA042C, "Native stack mutex lock", []),
        (0xA0528, "Native stack mutex unlock", []),
        (0x247C10, "Stack lock RAII constructor", []),
        (0x247C48, "Stack lock RAII destructor", []),
        (0x4108F8, "BD_ADDR formatter establishes byte order", [(0x410928, 0x410960)]),
    ],
    "BTAudioHALPlugin": [
        (0xFD34, "Send scalar-volume property via XPC", []),
        (0xF808, "Audio XPC envelope and xpc_connection_send_message", []),
        (
            0x6550,
            "Native audio property dispatcher",
            [(0x6550, 0x65D4), (0x685C, 0x68D0), (0x69A0, 0x6A10)],
        ),
        (0x2FF70, "A2DP incoming native device volume", []),
        (0x2D7D8, "A2DP outgoing volume update", []),
    ],
    "BTAvrcp": [
        (0x71C8, "handleMsg: Objective-C method / XPC dispatcher", []),
        (0x5F14, "handleRegisterForChangesMsg:replyBlock: Objective-C method", []),
    ],
}


def collect(directory, output):
    import hashlib, json, uuid
    from pathlib import Path
    from contextlib import redirect_stdout

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "tools": {"lief": lief.__version__, "capstone": capstone.__version__},
        "binaries": {},
    }
    for name, digest in IDENTITIES.items():
        path = Path(directory) / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(
                f"{name}: SHA-256 mismatch; refusing build-specific conclusions"
            )
        b = MachO(str(path))
        record = {
            "sha256": actual,
            "size": len(b.raw),
            "uuid": str(uuid.UUID(bytes=bytes(b.b.uuid.uuid))),
            "image_base": hex(b.base),
            "functions": [],
        }
        manifest["binaries"][name] = record
        with (output / (name + ".txt")).open("w") as f, redirect_stdout(f):
            print(name, "SHA-256", actual, "UUID", record["uuid"])
            print(
                "All addresses are unslid VAs. Labels are descriptive; no live hook validation.\n"
            )
            for offset, label, ranges in TARGETS[name]:
                entry = b.base + offset
                assert entry in b.starts, (
                    name,
                    label,
                    "not an LC_FUNCTION_STARTS entry",
                )
                end = b.starts[b.starts.index(entry) + 1]
                record["functions"].append(
                    {
                        "label": label,
                        "entry": hex(entry),
                        "end": hex(end),
                        "first_32_bytes": b.read(entry, 32).hex(),
                        "sha256": hashlib.sha256(
                            b.read(entry, end - entry)
                        ).hexdigest(),
                    }
                )
                print(
                    "\nIDENTIFICATION:",
                    label,
                    "LC_FUNCTION_STARTS:",
                    hex(entry),
                    "..",
                    hex(end),
                )
                print(
                    "Direct branch/call sites:",
                    ", ".join(hex(a) for a in b.calls.get(entry, [])),
                )
                print(
                    "Chained-fixup pointers:",
                    ", ".join(hex(a) for a, v in b.ptrs.items() if v == entry),
                )
                for start, stop in ranges or [(offset, end - b.base)]:
                    b.show(b.base + start, b.base + stop)
            if name == "bluetoothd":
                assert b.read(b.base + 0x779498, 7) == bytes([1, 2, 8, 9, 10, 11, 12])
                assert b.ptr(b.base + 0x9369F0) == b.base + 0x936D78
                assert b.ptr(b.base + 0x936DD0) == b.base + 0x55B940
                assert b.ptr(b.base + 0x936E38) == 0
                print(
                    "\nNative events at image+0x779498:",
                    b.read(b.base + 0x779498, 7).hex(" "),
                )
                print("Target descriptor image+0x9369e0:")
                for a in range(b.base + 0x9369E0, b.base + 0x936A08, 8):
                    print(hex(a), "->", hex(b.ptr(a)))
                print(
                    "Target metadata callback table (chained fixups decoded by LIEF):"
                )
                for i in range(25):
                    print(
                        hex(i * 8),
                        hex(b.base + 0x936D78 + i * 8),
                        "->",
                        hex(b.ptr(b.base + 0x936D78 + i * 8)),
                    )
            if name == "BTAvrcp":
                print("\nObjective-C method-list metadata (selector, type encoding):")
                for a, (sel, typ) in sorted(b.methods.items()):
                    print(hex(a), sel, typ)
        print(
            "Analysed",
            name,
            record["uuid"],
            len(record["functions"]),
            "function entries",
        )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "directory", help="read-only directory containing the three reference binaries"
    )
    p.add_argument(
        "--output-dir", required=True, help="separate directory for textual evidence"
    )
    a = p.parse_args()
    from pathlib import Path

    if Path(a.output_dir).resolve() == Path(a.directory).resolve():
        p.error("output must not be the reference directory")
    collect(a.directory, a.output_dir)
