#!/usr/bin/env python3
"""F-01 / F-07 regression — drives the REAL setup block from entrypoint.sh
against a fake WebSocket daemon that speaks simplex-chat's documented
envelope (apps/simplex-chat/Server.hs, Library/Commands.hs).

The setup code is extracted from entrypoint.sh at runtime, so this cannot
drift from what ships.

What this proves:

  F-01 (non-convergence after a crash): /ad (APICreateMyAddress) is NOT
  idempotent — it fails with chatCmdError/duplicateContactLink when an
  address already exists (Library/Commands.hs:2487-2491). The fix queries
  /show_address (ShowMyAddress) first and only calls /ad when the store
  reports userContactLinkNotFound, mirroring upstream's own bot init
  (Bot.hs:55-63). "address already exists" is exactly the post-crash state:
  a container that created its address, then died before writing
  .setup-complete, must converge on restart instead of looping forever.
    -> scenario 2 proves /ad is never called when the address already
       exists.
    -> scenario 3 proves a duplicateContactLink race (another concurrent
       attempt) is recovered via a /show_address retry, not treated as
       fatal.

  F-07 (false positive on bot command config): chatCmdError must not be
  reported as success. The previous code set confirmed=True on
  chatCmdError too, so a single run could print both "WARNING: bot
  commands may have failed" and "Bot commands configured" for the same
  rejected menu.
    -> scenario 4 proves a rejected bot-command menu is reported as an
       error, not confirmed, and ends the run with code 1 (so
       .setup-complete is not written and the operator-fixable config
       error is retried on the next restart rather than silently kept).

  General regressions kept from the previous version of this test:
    -> the userId passed to /_address_settings is the REAL one, not a
       literal 1;
    -> every confirmation is correlated on corrId, so an unrelated event
       sharing a response TYPE (but not the corrId) does not count as
       success;
    -> when no userId can be determined, auto-accept is skipped with a
       warning rather than applied to a guessed profile.
"""
import asyncio
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRYPOINT = os.path.join(HERE, "..", "entrypoint.sh")

PASS = []
FAIL = []


def ck(desc, cond, detail=""):
    if cond:
        PASS.append(desc)
        print(f"  ✓ {desc}")
    else:
        FAIL.append(desc)
        print(f"  ✗ {desc}" + (f"  [{detail}]" if detail else ""))


def extract_setup():
    """Pull the python heredoc body out of entrypoint.sh."""
    src = open(ENTRYPOINT).read()
    m = re.search(r"<<'PYEOF' > \"\$SETUP_LOG\" 2>&1\n(.*?)\nPYEOF", src, re.S)
    if not m:
        print("FATAL: could not extract the setup heredoc from entrypoint.sh")
        sys.exit(2)
    return m.group(1)


def run_scenario(name, replies, env=None):
    """Run the extracted setup against a fake daemon replaying `replies`.

    Each entry in `replies` is sent back, in order, one per command the
    extracted code sends — regardless of the command's own corrId — so the
    scenario's reply list must author the SAME corrId the real code is
    expected to send at that step (s1=/user, s2=/show_address,
    s2b=/ad, s2c=/show_address retry, s3=/_address_settings, s4=/set bot
    commands). A mismatch here would mean the scenario no longer describes
    what the code actually does, not a false pass.

    Returns (stdout, exit_code, [cmd, ...] actually sent).
    """
    received = []
    reply_idx = [0]

    def fake_ws(url, **kw):
        """Stand in for websockets.connect(...).

        websockets.connect returns an awaitable async-context-manager, and
        the setup code uses it as `async with websockets.connect(...) as
        ws`, so the replacement exposes __aenter__/__aexit__ directly (it
        is NOT a coroutine function).

        Two SEPARATE queues model the socket directions: the daemon must
        read what the client sent and write replies the client then reads.
        Sharing one queue lets each side consume the other's traffic.
        """

        class FakeWS:
            def __init__(self):
                self.to_daemon = asyncio.Queue()  # client -> daemon
                self.to_client = asyncio.Queue()  # daemon -> client
                self._task = None

            async def send(self, data):
                await self.to_daemon.put(data)

            async def recv(self):
                # The client wraps recv() in asyncio.wait_for(...), so this
                # must yield; a fake that blocks forever defeats wait_for.
                return await asyncio.wait_for(self.to_client.get(), timeout=5)

            async def __aenter__(self):
                self._task = asyncio.ensure_future(handler(self))
                return self

            async def __aexit__(self, *exc):
                if self._task:
                    self._task.cancel()
                return False

        return FakeWS()

    async def handler(ws):
        """Fake daemon: answer each command as the client sends it, in the
        scripted order — one reply per received command."""
        try:
            while True:
                raw = await ws.to_daemon.get()
                msg = json.loads(raw)
                received.append(msg)
                if len(replies) > reply_idx[0]:
                    await ws.to_client.put(json.dumps(replies[reply_idx[0]]))
                    reply_idx[0] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    # Neutralise the on-disk write; we only care about the logic.
    tmpdata = tempfile.mkdtemp()
    src = extract_setup().replace(
        "/data/bot_address.txt", os.path.join(tmpdata, "addr.txt")
    )

    # Inject a stub module named `websockets` so the extracted setup code's
    # `import websockets` succeeds. The real library is NOT required: the
    # code under test only calls websockets.connect(...), which is replaced
    # with the fake below. Depending on the real package would make this
    # suite fail on any runner that doesn't have it installed.
    stub_mod = types.ModuleType("websockets")
    stub_mod.connect = fake_ws
    stub_mod.__version__ = "stub"
    prev_mod = sys.modules.get("websockets", "<absent>")
    sys.modules["websockets"] = stub_mod

    env = env or {}
    prev_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    buf = io.StringIO()
    code = 0
    g = {
        "__name__": "__main__",
        "asyncio": asyncio,
        "json": json,
        "os": os,
        "sys": sys,
        "websockets": stub_mod,
    }
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(src, "setup", "exec"), g)
    except SystemExit as e:
        code = e.code or 0
    finally:
        if prev_mod == "<absent>":
            sys.modules.pop("websockets", None)
        else:
            sys.modules["websockets"] = prev_mod
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    out = buf.getvalue()

    print(f"\n--- {name} ---")
    for line in out.strip().splitlines():
        print(f"    {line}")

    cmds = [m["cmd"] for m in received if "cmd" in m]
    return out, code, cmds


def main():
    body = extract_setup()
    ck("setup block extracted from entrypoint.sh", "activeUser" in body)
    ck(
        "no hardcoded '/_address_settings 1 ' remains",
        "/_address_settings 1 " not in body,
    )
    ck(
        "address settings confirmation is corrId-correlated, not guessed",
        "ws, 's3', f'/_address_settings" in body,
    )
    ck(
        "/show_address is tried before any address is created (F-01)",
        "await send_and_await(ws, 's2', '/show_address')" in body,
    )
    ck(
        "/ad is only sent after a userContactLinkNotFound store error (F-01)",
        "store_error_type(show_resp) == 'userContactLinkNotFound'" in body,
    )
    ck(
        "chatCmdError is never mapped to confirmed=True for bot commands (F-07)",
        "bot_commands_confirmed = True\n                print(f'[setup] Bot commands configured"
        in body,
    )

    link = "simplex:/contact#/?v=2-7&smp=smp%3A%2F%2Ffake"
    link_retry = "simplex:/contact#/?v=2-7&smp=smp%3A%2F%2Ffake-retry"

    # ------------------------------------------------------------------
    # 1. Normal first-run case: no address yet -> /show_address reports
    #    userContactLinkNotFound -> /ad creates it.
    # ------------------------------------------------------------------
    out, code, cmds = run_scenario(
        "1. fresh install: create address",
        [
            {"corrId": "s1", "resp": {"type": "activeUser",
                                       "user": {"userId": 42, "activeUser": True}}},
            {"corrId": "s2", "resp": {"type": "chatCmdError",
                                       "chatError": {"type": "errorStore",
                                                     "storeError": {"type": "userContactLinkNotFound"}}}},
            {"corrId": "s2b", "resp": {"type": "userContactLinkCreated",
                                       "user": {"userId": 42},
                                       "connLinkContact": {"connFullLink": link}}},
            {"corrId": "s3", "resp": {"type": "userContactLinkUpdated",
                                      "user": {"userId": 42}}},
        ],
    )
    ck("1: sent /user, /show_address then /ad, in order",
       cmds == ["/user", "/show_address", "/ad",
                f"/_address_settings 42 {json.dumps({'businessAddress': False, 'autoAccept': {'acceptIncognito': False}})}"],
       f"cmds={cmds}")
    ck("1: exits 0 (address saved)", code == 0, f"code={code}")
    ck("1: reports auto-accept enabled", "Auto-accept enabled" in out)

    # ------------------------------------------------------------------
    # 2. THE F-01 REGRESSION: address already exists, as after a crash
    #    between address creation and the .setup-complete marker being
    #    written on a previous attempt. /show_address must find it and
    #    /ad must NEVER be called — the old code called /ad
    #    unconditionally here, got chatCmdError/duplicateContactLink, and
    #    failed the run forever.
    # ------------------------------------------------------------------
    out, code, cmds = run_scenario(
        "2. F-01: address already exists (post-crash restart)",
        [
            {"corrId": "s1", "resp": {"type": "activeUser",
                                       "user": {"userId": 7, "activeUser": True}}},
            {"corrId": "s2", "resp": {"type": "userContactLink",
                                       "user": {"userId": 7},
                                       "contactLink": {"connLinkContact": {"connFullLink": link}}}},
            {"corrId": "s3", "resp": {"type": "userContactLinkUpdated",
                                      "user": {"userId": 7}}},
        ],
    )
    ck("2: /ad was NEVER called", "/ad" not in cmds, f"cmds={cmds}")
    ck("2: converges with exit 0", code == 0, f"code={code}")
    ck("2: bot address recovered from /show_address", f"{link[:80]}" in out or "Bot address" in out)

    # ------------------------------------------------------------------
    # 3. Race: /show_address says not found, but /ad then reports
    #    duplicateContactLink (another concurrent attempt won). The run
    #    must recover via a /show_address retry instead of failing.
    # ------------------------------------------------------------------
    out, code, cmds = run_scenario(
        "3. duplicateContactLink race recovers via retry",
        [
            {"corrId": "s1", "resp": {"type": "activeUser",
                                       "user": {"userId": 9, "activeUser": True}}},
            {"corrId": "s2", "resp": {"type": "chatCmdError",
                                       "chatError": {"type": "errorStore",
                                                     "storeError": {"type": "userContactLinkNotFound"}}}},
            {"corrId": "s2b", "resp": {"type": "chatCmdError",
                                       "chatError": {"type": "errorStore",
                                                     "storeError": {"type": "duplicateContactLink"}}}},
            {"corrId": "s2c", "resp": {"type": "userContactLink",
                                       "user": {"userId": 9},
                                       "contactLink": {"connLinkContact": {"connFullLink": link_retry}}}},
            {"corrId": "s3", "resp": {"type": "userContactLinkUpdated",
                                      "user": {"userId": 9}}},
        ],
    )
    ck("3: sent /user, /show_address, /ad, /show_address (retry)",
       cmds[:4] == ["/user", "/show_address", "/ad", "/show_address"], f"cmds={cmds}")
    ck("3: exits 0 after recovering via retry", code == 0, f"code={code}")

    # ------------------------------------------------------------------
    # 4. THE F-07 REGRESSION: the daemon rejects SIMPLEX_BOT_COMMANDS
    #    (bad grammar per botCommandsP). The run must report this as an
    #    error — never as "Bot commands configured" — and must exit
    #    non-zero so .setup-complete is not written.
    # ------------------------------------------------------------------
    out, code, cmds = run_scenario(
        "4. F-07: bot commands rejected by daemon",
        [
            {"corrId": "s1", "resp": {"type": "activeUser",
                                       "user": {"userId": 5, "activeUser": True}}},
            {"corrId": "s2", "resp": {"type": "userContactLink",
                                       "user": {"userId": 5},
                                       "contactLink": {"connLinkContact": {"connFullLink": link}}}},
            {"corrId": "s3", "resp": {"type": "userContactLinkUpdated",
                                      "user": {"userId": 5}}},
            {"corrId": "s4", "resp": {"type": "chatCmdError",
                                       "chatError": {"type": "errorChat",
                                                     "errorType": {"type": "commandError"}}}},
        ],
        env={"SIMPLEX_BOT_COMMANDS": "'Help':/help,'Status':/status"},
    )
    ck("4: did NOT claim bot commands were configured",
       "Bot commands configured" not in out, detail=out)
    ck("4: reported the rejection as an error", "ERROR: bot commands rejected" in out)
    ck("4: exits non-zero so setup retries next restart", code == 1, f"code={code}")

    # ------------------------------------------------------------------
    # 5. No userId available at all (e.g. a fresh multi-profile daemon
    #    where /user is unhelpful and the creation event carries no user
    #    either) — must not guess, must warn, must still save the address.
    # ------------------------------------------------------------------
    out, code, cmds = run_scenario(
        "5. no userId available: auto-accept skipped, not guessed",
        [
            {"corrId": "s1", "resp": {"type": "cmdOk"}},
            {"corrId": "s2", "resp": {"type": "userContactLink",
                                       "contactLink": {"connLinkContact": {"connFullLink": link}}}},
        ],
    )
    ck("5: did not send /_address_settings (no id to target)",
       not any(c.startswith("/_address_settings") for c in cmds), f"cmds={cmds}")
    ck("5: warned instead of guessing a userId",
       "could not determine userId" in out)
    ck("5: still saved the address and exited 0", code == 0, f"code={code}")

    # ------------------------------------------------------------------
    # 6. Decoy: an unrelated event carrying the SAME response type as the
    #    expected confirmation, but the WRONG corrId. Must not count.
    # ------------------------------------------------------------------
    out, code, cmds = run_scenario(
        "6. wrong-corrId decoy is not accepted as confirmation",
        [
            {"corrId": "s1", "resp": {"type": "activeUser",
                                       "user": {"userId": 3, "activeUser": True}}},
            {"corrId": "s2", "resp": {"type": "userContactLink",
                                       "user": {"userId": 3},
                                       "contactLink": {"connLinkContact": {"connFullLink": link}}}},
            {"corrId": "s99", "resp": {"type": "userContactLinkUpdated",
                                       "user": {"userId": 3}}},
        ],
    )
    ck("6: did NOT falsely report auto-accept enabled",
       "Auto-accept enabled" not in out and "WARNING" in out)
    ck("6: still exits 0 (address itself was found)", code == 0, f"code={code}")

    print(f"\n=== RESULT: {len(PASS)} passed, {len(FAIL)} failed ===")
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
