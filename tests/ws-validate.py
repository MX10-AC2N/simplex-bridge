#!/usr/bin/env python3
"""
ws-validate.py — Suite de validation WebSocket pour la passerelle simplex-bridge.

Simule fidèlement le comportement du client HA (SimplexWsClient) :
corrélation RPC par corrId, commandes successives, connexions multiples,
tolérance aux messages malformés, reconnexions en boucle.

Usage :
  python3 ws-validate.py --url ws://127.0.0.1:5225
  python3 ws-validate.py --url ws://127.0.0.1:5225 --summary "$GITHUB_STEP_SUMMARY"
  python3 ws-validate.py --url ws://127.0.0.1:5225 --probe-cmds "/help,/contacts,/version"
  python3 ws-validate.py --url ws://127.0.0.1:5225 --listen-events 10

Codes de sortie : 0 = tout passé, 1 = au moins un échec.
"""

import asyncio
import json
import os
import sys
import time
import uuid
import argparse

try:
    import websockets
except ImportError:
    print("ERREUR: 'pip install websockets' requis.", file=sys.stderr)
    sys.exit(2)


class Result:
    def __init__(self, name):
        self.name = name
        self.status = "PASS"   # PASS | WARN | FAIL
        self.details = ""

    def ok(self, details=""):
        self.status, self.details = "PASS", details

    def warn(self, details):
        self.status, self.details = "WARN", details

    def fail(self, details):
        self.status, self.details = "FAIL", details


# ---------------------------------------------------------------- helpers

def new_corr_id():
    return str(uuid.uuid4())[:8]


async def recv_matching(ws, corr_id, timeout=10.0):
    """Attend un message JSON dont le corrId correspond. Retourne (dict|None, brut)."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, ""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return None, ""
        except Exception as e:
            return None, f"<exception réception: {e}>"
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue  # pas du JSON (événement texte brut ?) — on continue d'écouter
        if isinstance(data, dict) and data.get("corrId") == corr_id:
            return data, raw


async def send_probe(ws, corr_id, envelope_mode, cmd):
    """Envoie une commande sous une variante d'enveloppe donnée."""
    if envelope_mode == "string":
        payload = {"corrId": corr_id, "cmd": cmd}
    elif envelope_mode == "object_cmd":
        payload = {"corrId": corr_id, "cmd": {"type": cmd}}
    elif envelope_mode == "bare_json_no_cmd":
        payload = {"corrId": corr_id, "text": cmd}
    else:  # raw text
        payload = cmd
    data = payload if isinstance(payload, str) else json.dumps(payload)
    await ws.send(data)


async def probe_protocol(ws):
    """Découvre quelle combinaison (enveloppe, commande) produit une réponse corrélée.
    Retourne (envelope_mode, cmd, réponse_exemple) ou (None, None, diagnostics)."""
    cmds = [c.strip() for c in ARGS.probe_cmds.split(",") if c.strip()]
    modes = ["string", "object_cmd", "raw text", "bare_json_no_cmd"]
    diagnostics = []
    for mode in modes:
        for cmd in cmds:
            cid = new_corr_id()
            try:
                await send_probe(ws, cid, mode, cmd)
                data, raw = await recv_matching(ws, cid, timeout=ARGS.timeout)
                if data is not None:
                    return mode, cmd, data
                diagnostics.append(f"mode={mode!r} cmd={cmd!r}: aucune réponse corrélée")
            except Exception as e:
                diagnostics.append(f"mode={mode!r} cmd={cmd!r}: {e}")
                # tenter de rétablir la connexion si elle a cassé
                try:
                    await ws.close()
                except Exception:
                    pass
                break
    return None, None, diagnostics


# ---------------------------------------------------------------- tests

async def test_t01_connect(results):
    """T01 — Connexion WebSocket et handshake."""
    try:
        async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
            if not ws.open:
                results["t01"].fail("socket fermée immédiatement après l'ouverture")
                return False
            results["t01"].ok(f"connecté à {ARGS.url}")
            return True
    except Exception as e:
        results["t01"].fail(f"connexion impossible : {e}")
        return False


async def test_t02_probe(results):
    """T02 — Sonde du protocole : trouve l'enveloppe et une commande qui répondent."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        mode, cmd, outcome = await probe_protocol(ws)
        if mode is None:
            details = "\n".join(outcome) if isinstance(outcome, list) else str(outcome)
            results["t02"].fail(
                "aucune enveloppe/commande n'a produit de réponse corrélée.\n"
                f"Diagnostics :\n{details}\n"
                "-> vérifier manuellement avec websocat et ajuster --probe-cmds."
            )
            return False
        PROBE["mode"], PROBE["cmd"] = mode, cmd
        results["t02"].ok(
            f"protocole détecté : mode={mode!r}, cmd={cmd!r}\n"
            f"exemple de réponse : {json.dumps(outcome)[:300]}"
        )
        return True


async def roundtrip(ws, label=""):
    """Envoi + réception corrélée avec la combinaison détectée."""
    cid = new_corr_id()
    await send_probe(ws, cid, PROBE["mode"], PROBE["cmd"])
    data, raw = await recv_matching(ws, cid, timeout=ARGS.timeout)
    return data is not None, data, raw


async def test_t03_roundtrip(results):
    """T03 — Aller-retour RPC basique."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        ok, data, raw = await roundtrip(ws)
        if ok:
            results["t03"].ok(f"réponse corrélée reçue : {raw[:200]}")
        else:
            results["t03"].fail("pas de réponse corrélée pour une commande valide")


async def test_t04_interleaved_correlation(results):
    """T04 — Corrélation corrId : 3 requêtes entrelacées, réponses dans le désordre attendues."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        cids = [new_corr_id() for _ in range(3)]
        for cid in cids:
            await send_probe(ws, cid, PROBE["mode"], PROBE["cmd"])
        received = []
        deadline = time.monotonic() + ARGS.timeout * 3
        pending = set(cids)
        while pending and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            rid = data.get("corrId") if isinstance(data, dict) else None
            if rid in pending:
                pending.discard(rid)
                received.append(rid)
        if not pending:
            results["t04"].ok(f"les 3 corrIds ({', '.join(cids)}) ont reçu leur réponse distincte")
        else:
            results["t04"].fail(f"corrIds sans réponse : {', '.join(pending)} ({len(received)}/3 reçues)")


async def test_t05_malformed_resilience(results):
    """T05 — Messages malformés : la passerelle doit survivre et continuer à répondre."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        for junk in ("not json at all {{{", '{"corrId": "x"', '[]'):
            try:
                await ws.send(junk)
            except Exception as e:
                results["t05"].warn(f"connexion interrompue par du JSON malformé : {e}")
                return
        ok, _, _ = await roundtrip(ws)
        if ok:
            results["t05"].ok("la passerelle ignore les entrées malformées et répond toujours")
        else:
            results["t05"].fail("après des messages malformés, plus de réponse à une commande valide")


async def test_t06_concurrent_connections(results):
    """T06 — Deux connexions simultanées, chacune reçoit ses propres réponses."""
    async def do_session(ws, cid):
        await send_probe(ws, cid, PROBE["mode"], PROBE["cmd"])
        data, _ = await recv_matching(ws, cid, timeout=ARGS.timeout)
        return data is not None

    try:
        async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as w1, \
                   websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as w2:
            ok1, ok2 = await asyncio.gather(
                do_session(w1, new_corr_id()),
                do_session(w2, new_corr_id()),
            )
        if ok1 and ok2:
            results["t06"].ok("2 connexions concurrentes, RPC corrélés OK")
        else:
            results["t06"].fail(f"session1={'OK' if ok1 else 'KO'}, session2={'OK' if ok2 else 'KO'}")
    except Exception as e:
        results["t06"].fail(f"impossible d'ouvrir 2 connexions : {e}")


async def test_t07_reconnect_loop(results):
    """T07 — Simule le backoff HA : 5 cycles connexion/commande/déconnexion."""
    failures = []
    for i in range(1, 6):
        try:
            async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
                ok, _, _ = await roundtrip(ws)
                if not ok:
                    failures.append(f"cycle {i}: pas de réponse")
        except Exception as e:
            failures.append(f"cycle {i}: {e}")
        await asyncio.sleep(1)
    if failures:
        results["t07"].fail(f"{len(failures)}/5 échecs : {'; '.join(failures)}")
    else:
        results["t07"].ok("5 cycles reconnexion + RPC réussis")


async def test_t08_stability_long_run(results):
    """T08 — Stabilité : connexion maintenue, un RPC toutes les 5 s pendant --duration s."""
    duration = max(30, ARGS.duration)
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        start = time.monotonic()
        total, misses = 0, 0
        while time.monotonic() - start < duration:
            total += 1
            ok, _, _ = await roundtrip(ws)
            if not ok:
                misses += 1
            await asyncio.sleep(5)
        if misses == 0:
            results["t08"].ok(f"{total} RPC sur {int(duration)} s sans perte")
        elif misses <= max(1, total // 10):
            results["t08"].warn(f"{misses}/{total} RPC sans réponse (toléré)")
        else:
            results["t08"].fail(f"{misses}/{total} RPC sans réponse sur {int(duration)} s")


async def test_t09_listen_events(results):
    """T09 — Écoute passive des événements entrants (--listen-events N secondes).
    Utile avec un humain qui envoie un message au bot depuis un client SimpleX."""
    if ARGS.listen_events <= 0:
        results["t09"].warn("ignoré (passer --listen-events N et envoyer un message au bot)")
        return
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        # demander un abonnement si le protocole le permet
        for sub in ('{"corrId": "' + new_corr_id() + '", "cmd": "/subscribe"}',):
            try:
                await ws.send(sub)
            except Exception:
                pass
        events = []
        deadline = time.monotonic() + ARGS.listen_events
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            events.append(raw[:300])
        if events:
            results["t09"].ok(f"{len(events)} messages/event(s) reçus :\n" + "\n---\n".join(events[:5]))
        else:
            results["t09"].warn(
                f"aucun événement en {ARGS.listen_events} s. "
                "Envoyer un message au bot pendant ce test pour valider le flux entrant."
            )


# ---------------------------------------------------------------- rapport

def write_summary(results, args):
    lines = ["## Validation passerelle WebSocket simplex-bridge", ""]
    icons = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    ok = sum(1 for r in results.values() if r.status == "PASS")
    warn = sum(1 for r in results.values() if r.status == "WARN")
    fail = sum(1 for r in results.values() if r.status == "FAIL")
    lines.append(f"**{ok} PASS / {warn} WARN / {fail} FAIL**\n")
    for r in results.values():
        lines.append(f"{icons[r.status]} **{r.name}** — {r.status}")
        if r.details:
            for line in r.details.splitlines():
                lines.append(f"  > {line}")
        lines.append("")
    text = "\n".join(lines)
    print(text)
    summary_path = args.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            print(f"(impossible d'écrire le résumé CI : {e})", file=sys.stderr)
    for r in results.values():
        if r.status == "FAIL":
            print(f"::error::{r.name} — {r.details}", file=sys.stderr)
        elif r.status == "WARN":
            print(f"::warning::{r.name} — {r.details}", file=sys.stderr)


# ---------------------------------------------------------------- main

PROBE = {"mode": None, "cmd": None}

TESTS = {
    "t01": "T01 Connexion WebSocket",
    "t02": "T02 Sonde protocole (enveloppe corrId)",
    "t03": "T03 Allé-retour RPC",
    "t04": "T04 Corrélation corrId entrelacée",
    "t05": "T05 Résilience JSON malformé",
    "t06": "T06 Connexions concurrentes",
    "t07": "T07 Boucle de reconnexion (5 cycles)",
    "t08": "T08 Stabilité longue durée",
    "t09": "T09 Flux entrant (events)",
}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("SIMPLEX_WS_URL", "ws://127.0.0.1:5225"))
    parser.add_argument("--timeout", type=float, default=10.0, help="timeout RPC en secondes")
    parser.add_argument("--duration", type=int, default=60, help="durée du test de stabilité (s)")
    parser.add_argument("--probe-cmds", default="/version,/help,/contacts,/show version,chats",
                        help="commandes candidates pour la sonde de protocole, séparées par virgules")
    parser.add_argument("--listen-events", type=int, default=0,
                        help="secondes d'écoute passive des événements entrants (0 = ignoré)")
    parser.add_argument("--skip", default="", help="tests à ignorer, ex: t08,t09")
    parser.add_argument("--summary", default=None, help="fichier pour le résumé markdown (CI)")
    global ARGS
    ARGS = parser.parse_args()

    skipped = {s.strip() for s in ARGS.skip.split(",") if s.strip()}
    results = {k: Result(v) for k, v in TESTS.items() if k not in skipped}
    skipped_results = {k: Result(TESTS[k]) for k in skipped}

    impl = {
        "t01": test_t01_connect,
        "t02": test_t02_probe,
        "t03": test_t03_roundtrip,
        "t04": test_t04_interleaved_correlation,
        "t05": test_t05_malformed_resilience,
        "t06": test_t06_concurrent_connections,
        "t07": test_t07_reconnect_loop,
        "t08": test_t08_stability_long_run,
        "t09": test_t09_listen_events,
    }

    for key, fn in impl.items():
        if key in skipped_results:
            skipped_results[key].warn("ignoré via --skip")
            continue
        print(f">>> {TESTS[key]} …")
        try:
            await fn(results)
        except Exception as e:
            results[key].fail(f"exception inattendue : {e}")

    write_summary({**results, **skipped_results}, ARGS)
    sys.exit(1 if any(r.status == "FAIL" for r in results.values()) else 0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
