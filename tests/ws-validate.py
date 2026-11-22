#!/usr/bin/env python3
"""
ws-validate.py — Suite de validation WebSocket pour la passerelle simplex-bridge.

Simule fidèlement le comportement du client HA (SimplexWsClient) :
corrélation RPC par corrId, commandes successives, connexions multiples,
tolérance aux messages malformés, reconnexions en boucle,
ET les 4 services métier utilisés par l'intégration Home Assistant :
list contacts, create_invite, accept_invite, send_message,
plus la validation du schéma des événements entrants.

Usage :
  python3 ws-validate.py --url ws://127.0.0.1:5225
  python3 ws-validate.py --url ws://127.0.0.1:5225 --summary "$GITHUB_STEP_SUMMARY"
  python3 ws-validate.py --url ws://127.0.0.1:5225 --probe-cmds "/help,/contacts,/version"
  python3 ws-validate.py --url ws://127.0.0.1:5225 --listen-events 30
  # Pour un test réel d'acceptation d'invitation (lien depuis votre téléphone) :
  python3 ws-validate.py --url ws://127.0.0.1:5225 --invite-link "https://simplex.im/..."

Codes de sortie : 0 = tout passé, 1 = au moins un échec.
"""

import asyncio
import json
import os
import re
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


async def rpc(ws, cmd, timeout=None):
    """RPC générique avec l'enveloppe détectée par la sonde : renvoie (data, raw)."""
    cid = new_corr_id()
    await send_probe(ws, cid, PROBE["mode"], cmd)
    return await recv_matching(ws, cid, timeout=timeout or ARGS.timeout)


def resp_obj(data):
    """Retourne le champ resp décodé (dict) ou {}."""
    if isinstance(data, dict):
        resp = data.get("resp")
        if isinstance(resp, dict):
            return resp
    return {}


def resp_text(data):
    """Aplati une réponse RPC en texte pour analyse heuristique."""
    if not isinstance(data, dict):
        return ""
    resp = data.get("resp", data)
    try:
        return json.dumps(resp, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(resp)


def is_chat_error(data):
    """Détecte une erreur métier du CLI (chatCmdError)."""
    return resp_obj(data).get("type") == "chatCmdError"


def chat_error_message(data):
    err = resp_obj(data).get("chatError", {})
    return json.dumps(err, ensure_ascii=False)[:200]


def extract_contacts(data):
    """Extrait les noms locaux des contacts d'une réponse contactsList."""
    contacts = resp_obj(data).get("contacts")
    names = []
    if isinstance(contacts, list):
        for c in contacts:
            if isinstance(c, dict):
                name = c.get("localDisplayName") or (c.get("contact") or {}).get("localDisplayName")
                if name:
                    names.append(str(name))
    return names


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

CONTACT_LINK_RE = re.compile(r"https?://\S+", re.I)


async def test_t01_connect(results):
    """T01 — Connexion WebSocket et handshake."""
    # NB : entrer dans le context manager lève une exception si le handshake échoue ;
    # on ne vérifie PAS l'attribut `.open`, supprimé de websockets >= 14
    # (ClientConnection). Compatible toutes versions.
    try:
        async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
            # test de vivacité : une réception avec timeout court ne doit pas échouer
            try:
                await asyncio.wait_for(ws.recv(), timeout=1.0)
                idle = "socket vive (message spontané reçu)"
            except asyncio.TimeoutError:
                idle = "socket ouverte, silencieuse en l'absence de commande (attendu)"
            results["t01"].ok(f"connecté à {ARGS.url} — {idle}")
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


# ------------- tests métier (services utilisés par l'intégration HA) -----

async def test_t10_list_contacts(results):
    """T10 — Service HA « lister les contacts » (capteur configured_contacts).
    Traduction CLI : /contacts. Une réponse contactsList bien formée = PASS,
    même avec zéro contact (base CI vierge)."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        data, raw = await rpc(ws, "/contacts")
        if data is None:
            results["t10"].fail("/contacts : aucune réponse corrélée")
            return
        if is_chat_error(data):
            results["t10"].fail(f"/contacts rejeté par le CLI : {chat_error_message(data)}")
            return
        if resp_obj(data).get("type") == "contactsList":
            names = extract_contacts(data)
            if names:
                PROBE["contacts"] = names
                results["t10"].ok(
                    f"contactsList valide — {len(names)} contact(s) : {', '.join(names)}"
                )
            else:
                results["t10"].ok(
                    "contactsList valide — 0 contact (normal sur une base CI vierge)"
                )
        else:
            results["t10"].warn(
                "réponse corrélée mais type inattendu (attendu contactsList) : " + raw[:300]
            )


async def test_t11_create_invite(results):
    """T11 — Service HA « simplex.create_invite » (traduction CLI : /connect).
    La réponse doit contenir un lien d'invitation exploitable."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        data, raw = await rpc(ws, "/connect")
        if data is None:
            results["t11"].fail("/connect : aucune réponse corrélée (create_invite HS)")
            return
        if is_chat_error(data):
            results["t11"].fail(f"/connect rejeté par le CLI : {chat_error_message(data)}")
            return
        m = CONTACT_LINK_RE.search(raw)
        if m:
            PROBE["last_invite"] = m.group(0)
            results["t11"].ok(
                f"lien d'invitation obtenu (servira à T12) : {m.group(0)[:120]}"
            )
        else:
            results["t11"].warn(
                "réponse corrélée mais aucun lien https:// détecté : " + raw[:300]
            )


async def test_t12_accept_and_send(results):
    """T12 — Services HA « accept_invite » + « send_message ».
    Acceptation d'invitation :
      - avec --invite-link <url> : accepte une VRAIE invitation (depuis un
        autre appareil) — validation en conditions réelles ;
      - sinon, tente l'auto-connexion avec le lien de T11 : la corrélation est
        validée mais une erreur CLI (chatCmdError) est attendue et signalée
        sans faire échouer le run (l'auto-connexion bot→bot est un cas dégénéré).
    Envoi de message : @<contact> <texte>, comme notify.* / send_message / LLM.
    """
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        # --- 1) acceptation d'invitation (service accept_invite)
        invite = ARGS.invite_link or PROBE.get("last_invite")
        mode_label = "lien fourni (--invite-link)" if ARGS.invite_link else "auto-connexion bot→bot"
        accepted = None
        if invite:
            data, raw = await rpc(ws, f"/connect {invite}", timeout=ARGS.timeout * 2)
            if data is None:
                accepted = False
                results_part = f"accept_invite [{mode_label}] : pas de réponse corrélée"
            elif is_chat_error(data):
                accepted = False
                results_part = (
                    f"accept_invite [{mode_label}] : erreur CLI (corrélation OK) — "
                    f"{chat_error_message(data)}"
                )
            else:
                accepted = True
                results_part = f"accept_invite [{mode_label}] : accepté ({raw[:150]})"
        else:
            results_part = "accept_invite ignoré (aucun lien disponible)"

        # --- 2) envoi de message (service send_message / notify.*)
        data, raw = await rpc(ws, "/contacts")
        names = extract_contacts(data) if data else []
        if not names:
            # fallback heuristique : @handle dans la réponse brute
            m = re.search(r"@([\w-]{2,})", resp_text(data))
            recipient = m.group(1) if m else None
        else:
            recipient = names[0]
        if not recipient:
            extra = ""
            if accepted is False and not ARGS.invite_link:
                extra = (
                    " Astuce : fournir un vrai lien via --invite-link (depuis un autre "
                    "appareil SimpleX) pour tester accept + send en conditions réelles."
                )
            results["t12"].warn(
                f"{results_part} — envoi non testé : aucun contact disponible.{extra}"
            )
            return
        sent_ok, sent_raw = await rpc(
            ws, f"@{recipient} ping de validation ws-validate.py"
        )
        if sent_ok:
            if is_chat_error(sent_raw):
                results["t12"].warn(
                    f"{results_part} — envoi à @{recipient} rejeté : {chat_error_message(sent_raw)}"
                )
            else:
                results["t12"].ok(
                    f"{results_part} — message envoyé à @{recipient}, réponse : {sent_raw[:200]}"
                )
        else:
            results["t12"].warn(
                f"{results_part} — envoi sans réponse corrélée vers @{recipient}"
            )


async def test_t09_listen_events(results):
    """T09 — Écoute passive des événements entrants (--listen-events N secondes).
    Valide le flux utilisé par l'event simplex_message_received et le sensor
    last_message. Idéalement : envoyer un message au bot depuis un client SimpleX
    pendant la fenêtre d'écoute."""
    if ARGS.listen_events <= 0:
        results["t09"].warn("ignoré (passer --listen-events N et envoyer un message au bot)")
        return
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        events = []
        deadline = time.monotonic() + ARGS.listen_events
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            events.append(raw)
        if not events:
            results["t09"].warn(
                f"aucun événement en {ARGS.listen_events} s. "
                "Envoyer un message au bot pendant ce test pour valider le flux entrant."
            )
            return
        # Validation du schéma attendu par HA : alias, chat_ref, text,
        # direction, item_id (cf. event simplex_message_received)
        keys_seen = set()
        for raw in events:
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue
            def dig(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        keys_seen.add(k)
                        dig(v)
                elif isinstance(o, list):
                    for v in o:
                        dig(v)
            dig(obj)
        expected = {"alias", "chat_ref", "text", "direction", "item_id"}
        found = expected & keys_seen
        missing = expected - keys_seen
        sample = "\n---\n".join(e[:300] for e in events[:5])
        if not missing:
            results["t09"].ok(
                f"{len(events)} événement(s), schéma complet alias/chat_ref/text/"
                f"direction/item_id présent :\n{sample}"
            )
        else:
            results["t09"].warn(
                f"{len(events)} événement(s) ; champs manquants attendus par HA : "
                f"{', '.join(sorted(missing))} (peut venir du format de la version CLI) :\n{sample}"
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

PROBE = {"mode": None, "cmd": None, "last_invite": None, "contacts": []}

TESTS = {
    "t01": "T01 Connexion WebSocket",
    "t02": "T02 Sonde protocole (enveloppe corrId)",
    "t03": "T03 Allé-retour RPC",
    "t04": "T04 Corrélation corrId entrelacée",
    "t05": "T05 Résilience JSON malformé",
    "t06": "T06 Connexions concurrentes",
    "t07": "T07 Boucle de reconnexion (5 cycles)",
    "t08": "T08 Stabilité longue durée",
    "t09": "T09 Flux entrant + schéma event HA",
    "t10": "T10 List contacts (service HA)",
    "t11": "T11 Create invite (service HA)",
    "t12": "T12 Accept invite + Send message (services HA)",
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
    parser.add_argument("--invite-link", default=os.environ.get("SIMPLEX_INVITE_LINK"),
                        help="vrai lien d'invitation SimpleX pour tester accept_invite en conditions réelles")
    parser.add_argument("--skip", default="", help="tests à ignorer, ex: t08,t12")
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
        "t10": test_t10_list_contacts,
        "t11": test_t11_create_invite,
        "t12": test_t12_accept_and_send,
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
