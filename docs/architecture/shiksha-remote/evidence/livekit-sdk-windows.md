# RC-003 measurement 1 — the LiveKit Rust SDK on Windows

**Status: answered.** The LiveKit Rust SDK builds, links, runs and joins a real
room from Windows. Three platform-specific obstacles stood in the way; all three
are solved, and each is recorded below because each failed in a way that
argued for the wrong fix.

## The question

RC-003 asks whether the companion can be built on the platform it has to run on.
If the Rust SDK could not link on Windows, the whole native-companion design
would need revisiting, and it would be far better to learn that in Phase 0 than
in Phase 5. So the question is deliberately narrow: does it build, and does it
reach a real server?

## The answer

```
url      : wss://devshikshacom-3gibokr2.livekit.cloud
token    : 376 chars
CONNECTED in 1305 ms
  room      : rc003-spike-probe
  local sid : rc003-spike
  remotes   : 0
disconnected cleanly
```

A native Windows binary joined a real room on our LiveKit Cloud project and left
cleanly. That closes the question RC-003 asked: the companion can be built on
the platform it has to run on, and the SFU we already use in production will
talk to it.

Worth recording the intermediate result too, because it is the one that made the
last obstacle findable. Before TLS was enabled, a deliberately invalid token
produced:

```
CONNECT FAILED: engine: signal failure: client error: 401 Unauthorized - invalid API key
```

A 401 is a *success* for a connectivity probe. It came from LiveKit Cloud, not
from our process, so every layer beneath authentication had already worked: the
binary linked against the prebuilt WebRTC and started, the WebRTC runtime
initialised, rustls negotiated TLS, the WebSocket upgraded, the signalling
client framed a join, and the server parsed our JWT and rejected its signature.
Staging the probe that way turns "it doesn't connect" into a single remaining
unknown instead of a search across the whole stack.

**1305 ms is session setup, not steady-state latency**, and the distinction
matters for the budget. It covers DNS, the TLS handshake, the WebSocket upgrade,
the join round-trip and engine startup, measured cold from a residential link in
India to the LiveKit Cloud region. It does not belong in the input-to-pixel
budget that RC-003's measurement 3 is about. It *is* the user-visible pause
between a teacher approving a control request and the first frame appearing, so
it belongs in the RC-011 consent UI as something that needs a progress state
rather than an unexplained freeze — roughly a second and a third, on a good
connection, before anything can possibly appear.

Build cost, for planning: a cold release build is roughly 4–5 minutes (it
downloads and links an 85 MB prebuilt `webrtc.lib`); an incremental rebuild
after a feature change is ~51 s. The resulting binary is 29 MB.

## Three Windows-specific findings

All three cost real time, none is documented where you would look first, and
each one fails in a way that argues for the wrong fix. They are recorded here so
RC-012 and RC-028 do not pay for them twice.

### 1. The prebuilt WebRTC requires the static CRT

The default build dies at the link step — not the compile step — with hundreds
of lines of:

```
error LNK2038: mismatch detected for 'RuntimeLibrary':
  value 'MT_StaticRelease' doesn't match value 'MD_DynamicRelease'
  in libwebrtc_sys-....rlib
error LNK2005: ... already defined in msvcprt.lib(MSVCP140.dll)
...
fatal error LNK1169: one or more multiply defined symbols found
```

Every crate compiles. Only the final link fails, which reads as a code problem
when it is a one-flag problem. The fix is `-C target-feature=+crt-static`, now
in `.cargo/config.toml`.

### 2. That error message points the wrong way

The message names `MT_StaticRelease` **first**, which invites the reading that
Rust is the static side and should be moved to dynamic. It is the other way
round: Rust defaults to the *dynamic* CRT on `*-pc-windows-msvc`, and the `cc`
crate follows whatever Rust picked when it compiles `webrtc-sys`'s C++ glue. The
static side is the vendored `webrtc.lib`.

Setting `-crt-static` therefore merely restates the default and fails
**byte-identically** — same objects, same error count. That is the trap: an
identical failure reads as "my flag didn't apply", so the natural next move is
to fight the build system rather than to flip the sign.

The library settles it directly, which is why this is a measurement and not an
opinion:

```
$ grep -a -o -E 'DEFAULTLIB:(libcmt|libcpmt|msvcrt|msvcprt)' webrtc.lib \
    | sort | uniq -c
   3156 DEFAULTLIB:libcmt
   1654 DEFAULTLIB:libcpmt
```

4,810 directives naming the static CRT; not one naming the dynamic one.

**Consequence for RC-028, and it is the good direction.** With the CRT linked
statically the companion carries its own runtime and does **not** depend on the
Microsoft Visual C++ redistributable being present on the target machine. That
removes an install-time failure mode on a teacher's laptop rather than adding
one — a missing `MSVCP140.dll` produces an OS-level error dialog before any of
our code runs, so it could not be reported, logged, or self-diagnosed by the
companion. Verified on the linked binary rather than assumed:

```
MSVCP140.dll 0   VCRUNTIME140.dll 0   ucrtbase.dll 0      (none imported)
KERNEL32.dll 2   ADVAPI32.dll 1       bcrypt.dll 1        (sanity: method works)
```

### 3. The SDK ships with no TLS support enabled

`livekit` 0.12 has `default = ["native"]`, and `native` does **not** include TLS.
Four TLS features exist (`native-tls`, `native-tls-vendored`,
`rustls-tls-native-roots`, `rustls-tls-webpki-roots`) and none is on by default.

This compiles and links perfectly and then fails at runtime, at connect time:

```
CONNECT FAILED: engine: signal failure: transport connection error:
  URL error: TLS support not compiled in
```

The message never mentions Cargo features, so it reads as a certificate or
network problem — the most likely wrong turn is to start debugging the school's
proxy. Any `wss://` URL fails this way.

We selected **`rustls-tls-native-roots`**: pure-Rust TLS, so no OpenSSL
vendoring and no further CRT or linkage surprises, while still reading the
**operating system trust store**. The trust store matters for the actual
deployment: schools and similar institutions frequently run a TLS-inspecting
proxy whose root is installed in the OS store, and `webpki-roots` — which embeds
a fixed Mozilla root list — would reject exactly those networks while working
perfectly on our machines. That is a failure that would only appear at a pilot
site, and only for some sites.

## What is still open

- **A native screen publish into a live room.** The join is proven; publishing a
  captured display into it is not. That is the step that exercises the capture
  path from `sendinput-fidelity.md` against the SFU.
- **Measurement 3, the full event-to-inject path.** Capture at 16.6 ms and
  injection at 0.73 ms p95 are both small; the browser and SFU hops, where the
  latency budget will actually be spent, remain unmeasured.
- **Multi-monitor and fractional DPI.** Still needs no software at all, only a
  second display placed to the LEFT of primary so the virtual-desktop origin
  goes negative.

Token handling, unchanged and worth keeping: the LiveKit API secret lives only
in the dev VM's `.env` and is never copied to a developer machine. Probe tokens
are minted server-side with a 20-minute TTL and a single room in their grant.

## Spike status

`rc003-livekit` is a throwaway probe, like `rc003-spike`. Phase 0's rollback is
"delete isolated spike; keep documents/tests", so the code does not ship — these
findings and the two config lines they justify are the deliverable.
