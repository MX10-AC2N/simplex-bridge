#!/usr/bin/env python3
"""
ws-validate.py — Suite de validation WebSocket pour la passerelle simplex-bridge.

Simule fidèlement le comportement du client HA (SimplexWsClient) :
corrélation RPC par corrId, commandes successives, connexions multiples,
tolérance aux messages malformés, reconnexions en boucle,
ET les 4 services métier utilisés par l'intégration Home Assistant :
list contacts, create_invite, accept_invite, send_message,
plus la validation du flux d'événements entrants.

Appairage interactif (UN SEUL RUN, aucune fenêtre à synchroniser) :
  1. lancez le workflow (ou : python3 ws-validate.py --url ws://127.0.0.1:5225) ;
  2. dès que T12 démarre, le lien d'invitation du bot est affiché sur UNE
     ligne « INVITE LINK: … » dans les logs et dans le Summary du run ;
  3. ouvrez ce lien dans l'app SimpleX de votre téléphone
     (Add contact → paste link) pendant que le run attend ;
  4. le bot détecte le contact, envoie « ping de validation » (vous le recevez) ;
  5. RÉPONDEZ depuis votre téléphone PENDANT LE COMPTE À REBOURS (90 s) :
     chaque message reçu (texte, photo, vidéo, fichier) est affiché EN DIRECT
     dans le log avec sa direction CLI (directRcv) et son type de contenu,
     et T09 valide les champs CLI que HA consomme (itemId, chatDir, msgContent).

IMPORTANT : répondez pendant le run. Un message envoyé APRÈS la fin du run
part vers un bot dont le volume vient d'être détruit (down -v) : il n'arrivera
nulle part. Les messages reçus pendant le run, eux, sont prouvés dans le log.

NB : le live-tail des logs GitHub Actions duplique parfois les lignes pendant
le streaming — c'est un artefact d'affichage, pas une sortie réelle du script.

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


# ---------------------------------------------------------------- état global

PROBE = {"mode": None, "cmd": None, "last_invite": None, "contacts": []}
EVENTS = []          # frames reçus sur la connexion d'écoute dédiée (toute la suite)
LISTENER_TASK = None
INVITE_PUBLISHED = False


# ---------------------------------------------------------------- helpers

def new_corr_id():
    return str(uuid.uuid4())[:8]


# Le lien d'invitation est suivi de caractères JSON dans la réponse brute :
# on s'arrête au premier guillemet, espace, antislash ou accolade.
LINK_RE = re.compile(r"https?://[^\s\"\\}]+", re.I)


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


def find_keys(o, acc=None):
    """Collecte récursivement les clés présentes dans un objet JSON."""
    if acc is None:
        acc = set()
    if isinstance(o, dict):
        for k, v in o.items():
            acc.add(k)
            find_keys(v, acc)
    elif isinstance(o, list):
        for v in o:
            find_keys(v, acc)
    return acc


# ------------------------------------------------- analyse des événements CLI

KEY_PATH_RE = re.compile(r"content|msg|text|file|image|video|caption|audio", re.I)


def collect_key_paths(o, prefix="$", acc=None):
    """Cartographie récursive : chemin (breadcrumb) de chaque clé dont le nom
    ressemble à du contenu de message, avec aperçu de la valeur. Sert à
    localiser où la version du CLI place réellement le corps du message."""
    if acc is None:
        acc = []
    if isinstance(o, dict):
        for k, v in o.items():
            path = f"{prefix}.{k}"
            if KEY_PATH_RE.search(str(k)) and not isinstance(v, (dict, list)):
                preview = str(v)[:60]
                acc.append(f"{path} = {preview!r}")
            collect_key_paths(v, path, acc)
    elif isinstance(o, list):
        for idx, v in enumerate(o[:5]):  # limiter la profondeur des listes
            collect_key_paths(v, f"{prefix}[{idx}]", acc)
    return acc


def find_first_value(o, key):
    """Première valeur trouvée pour une clé donnée, en profondeur d'abord."""
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            r = find_first_value(v, key)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_first_value(v, key)
            if r is not None:
                return r
    return None


def extract_msg_content(chat_item):
    """Retourne le dict de contenu d'un chatItem quel que soit l'emplacement.
    Formes connues : chatItem.content.msgContent (v7 documentée),
    chatItem.msgContent (plate). Sinon : recherche de toute clé dont le nom
    contient 'msgContent' ou 'content' (variantes de sérialisation)."""
    content = chat_item.get("content")
    if isinstance(content, dict):
        mc = content.get("msgContent")
        if isinstance(mc, dict):
            return mc
        # forme abrégée possible : content = {"type": "...", ...} directement
        if "type" in content:
            return content
    mc = chat_item.get("msgContent")
    if isinstance(mc, dict):
        return mc
    for key in ("msgContent", "content", "msg"):
        mc = find_first_value(chat_item, key)
        if isinstance(mc, dict) and mc.get("type"):
            return mc
    return {}


def parse_event(raw):
    """Décode une frame d'événement CLI :
    retourne {"type": ..., "items": [résumés de chatItem], "raw_keys": set,
    "key_paths": [...]} ou None si la frame n'est pas un événement exploitable.
    Chaque résumé contient direction (chatDir.type), kind (type de contenu),
    item_id, contact (alias depuis chatInfo) et text_preview — les primitives
    que HA dérive pour alias/chat_ref/text/direction/item_id."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    resp = obj.get("resp") if isinstance(obj, dict) else None
    if not isinstance(resp, dict):
        return None
    out = {"type": resp.get("type"), "items": [], "raw_keys": set(),
           "key_paths": []}
    find_keys(resp, out["raw_keys"])
    out["key_paths"] = collect_key_paths(resp)

    if out["type"] == "newChatItems":
        items = resp.get("chatItems") or []
        if not isinstance(items, list):
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            # alias du pair : chatInfo.contact.localDisplayName (= "alias" HA)
            contact = "?"
            chat_info = it.get("chatInfo")
            if isinstance(chat_info, dict):
                c = chat_info.get("contact")
                if isinstance(c, dict):
                    contact = str(c.get("localDisplayName") or
                                  (c.get("profile") or {}).get("displayName") or "?")
            # CLI v7 : chatItems[].chatItem.{meta.chatDir, content.msgContent}
            chat_item = it.get("chatItem") if isinstance(it.get("chatItem"), dict) else it
            meta = chat_item.get("meta") if isinstance(chat_item.get("meta"), dict) else {}
            chat_dir = meta.get("chatDir") if isinstance(meta.get("chatDir"), dict) else \
                       (chat_item.get("chatDir") if isinstance(chat_item.get("chatDir"), dict) else {})
            direction = chat_dir.get("type") or "?"
            mc = extract_msg_content(chat_item)
            kind = mc.get("type") or "?"
            text = mc.get("text") or mc.get("fileName") or mc.get("filePath") or ""
            item_id = meta.get("itemId") or chat_item.get("itemId") or "?"
            out["items"].append({
                "direction": str(direction),
                "kind": str(kind),
                "item_id": item_id,
                "contact": contact,
                "text_preview": str(text)[:80],
            })
    return out


def event_incoming_items(raw):
    """Résumés des chatItems RECUS (directRcv) d'une frame newChatItems."""
    ev = parse_event(raw)
    if not ev or ev["type"] != "newChatItems":
        return []
    return [i for i in ev["items"] if i["direction"].lower().startswith("directrcv")]


def summarize_event_types(events):
    types = {}
    for raw in events:
        ev = parse_event(raw)
        t = ev["type"] if ev else "<non-json>"
        types[t] = types.get(t, 0) + 1
    return ", ".join(f"{k}×{v}" for k, v in sorted(types.items()))


def publish_invite_link(link):
    """Publie le lien d'invitation une seule fois : une ligne compacte dans les
    logs + bloc dans le Summary. NB : le live-tail GitHub peut dupliquer les
    lignes à l'affichage — le log archivé montre la sortie réelle."""
    global INVITE_PUBLISHED
    if INVITE_PUBLISHED:
        return
    INVITE_PUBLISHED = True
    print("\nINVITE LINK — ouvrez ce lien dans l'app SimpleX de votre téléphone "
          "MAINTENANT (Add contact → paste link), le run attend :\n"
          f"INVITE LINK: {link}\n", flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    "## 🔗 Appairage interactif (T12)\n\n"
                    "Ouvrez ce lien dans l'app SimpleX de votre téléphone "
                    "**pendant que le run tourne**, puis répondez au ping pendant "
                    "le compte à rebours :\n\n```\n"
                    f"{link}\n```\n"
                )
        except OSError:
            pass


async def listener_loop():
    """Connexion d'écoute dédiée : capture tous les événements entrants
    pendant toute la durée de la suite."""
    try:
        async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
            while True:
                raw = await ws.recv()
                EVENTS.append(raw)
    except (asyncio.CancelledError, Exception):
        return


def start_listener():
    global LISTENER_TASK
    if LISTENER_TASK is None or LISTENER_TASK.done():
        LISTENER_TASK = asyncio.get_event_loop().create_task(listener_loop())


async def stop_listener():
    global LISTENER_TASK
    if LISTENER_TASK is not None:
        LISTENER_TASK.cancel()
        try:
            await LISTENER_TASK
        except (asyncio.CancelledError, Exception):
            pass
        LISTENER_TASK = None


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
                try:
                    await ws.close()
                except Exception:
                    pass
                break
    return None, None, diagnostics


# ---------------------------------------------------------------- tests session

async def test_t01_connect(results):
    """T01 — Connexion WebSocket et handshake."""
    # NB : entrer dans le context manager lève une exception si le handshake échoue ;
    # on ne vérifie PAS l'attribut `.open`, supprimé de websockets >= 14
    # (ClientConnection). Compatible toutes versions.
    try:
        async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
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


# ------------- tests métier (services utilisés par l'intégration HA) -----

async def test_t10_list_contacts(results):
    """T10 — Service HA « lister les contacts » (capteur configured_contacts).
    Une réponse contactsList bien formée = PASS, même avec zéro contact."""
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
            PROBE["contacts"] = names
            if names:
                results["t10"].ok(
                    f"contactsList valide — {len(names)} contact(s) : {', '.join(names)}"
                )
            else:
                results["t10"].ok(
                    "contactsList valide — 0 contact (l'appairage interactif de T12 en créera un)"
                )
        else:
            results["t10"].warn(
                "réponse corrélée mais type inattendu (attendu contactsList) : " + raw[:300]
            )


async def test_t11_create_invite(results):
    """T11 — Service HA « simplex.create_invite » (traduction CLI : /connect).
    La réponse doit contenir un lien d'invitation PROPRE (sans succoter de JSON)."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        data, raw = await rpc(ws, "/connect")
        if data is None:
            results["t11"].fail("/connect : aucune réponse corrélée (create_invite HS)")
            return
        if is_chat_error(data):
            results["t11"].fail(f"/connect rejeté par le CLI : {chat_error_message(data)}")
            return
        m = LINK_RE.search(raw)
        if m:
            link = m.group(0)
            PROBE["last_invite"] = link
            clean = not any(ch in link for ch in '"\\}]{')
            results["t11"].ok(
                f"create_invite OK — lien propre : {link[:100]}" if clean
                else f"create_invite OK mais lien suspect (caractères JSON inclus) : {link[:100]}"
            )
        else:
            results["t11"].warn(
                "réponse corrélée mais aucun lien https:// détecté : " + raw[:300]
            )


async def wait_for_contact(ws, wait_s):
    """Sonde /contacts régulièrement jusqu'à ce qu'un contact apparaisse
    (la connexion via lien d'invitation est asynchrone : confirmation via SMP).
    Retourne le nom du premier contact ou None."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        data, _ = await rpc(ws, "/contacts")
        names = extract_contacts(data) if data else []
        if names:
            return names[0]
        print(f"   … attente appairage téléphone ↔ bot "
              f"({int(deadline - time.monotonic())} s restantes)", flush=True)
        await asyncio.sleep(3)
    return None


async def test_t12_pairing_and_send(results):
    """T12 — Services HA « accept_invite » + « send_message », avec appairage
    interactif en un seul run :
      1. si --invite-link est fourni, le bot accepte ce lien réel ;
      2. sinon le bot génère son propre lien (ligne « INVITE LINK: … », une fois) ;
      3. contact établi → envoie « ping de validation » (vous le recevez) ;
      4. COMPTE À REBOURS affiché toutes les 10 s pendant l'attente de votre
         message retour : chaque message reçu (texte/image/vidéo/fichier)
         est affiché EN DIRECT dans le log."""
    async with websockets.connect(ARGS.url, open_timeout=ARGS.timeout) as ws:
        recipient = None
        notes = []

        # --- établissement du contact (si pas déjà présent dans la base)
        data, _ = await rpc(ws, "/contacts")
        names = extract_contacts(data) if data else []
        if names:
            recipient = names[0]
            notes.append(f"contact déjà présent : {recipient}")
        elif ARGS.invite_link:
            data, raw = await rpc(ws, f"/connect {ARGS.invite_link}", timeout=ARGS.timeout * 2)
            if data is None:
                notes.append("accept_invite : pas de réponse corrélée")
            elif is_chat_error(data):
                notes.append(f"accept_invite : erreur CLI (corrélation OK) — {chat_error_message(data)}")
            else:
                notes.append("accept_invite : commande acceptée")
                recipient = await wait_for_contact(ws, ARGS.contact_wait)
        else:
            data, raw = await rpc(ws, "/connect", timeout=ARGS.timeout * 2)
            if data is None or is_chat_error(data):
                notes.append(
                    "génération du lien d'invitation impossible : "
                    + (chat_error_message(data) if data else "pas de réponse corrélée")
                )
            else:
                m = LINK_RE.search(raw)
                if m:
                    publish_invite_link(m.group(0))
                    notes.append("lien d'invitation publié (logs + Summary)")
                    recipient = await wait_for_contact(ws, ARGS.contact_wait)
                else:
                    notes.append("aucun lien dans la réponse /connect : " + raw[:200])

        if not recipient:
            notes.append(
                f"aucun contact établi en {ARGS.contact_wait} s — le lien doit être "
                "ouvert dans l'app SimpleX PENDANT le run (app ouverte, en ligne)."
            )
            results["t12"].warn(" ; ".join(notes))
            return

        # --- envoi du ping (service send_message / notify.* / LLM)
        sent_ok, sent_data = await rpc(
            ws, f"@{recipient} ping de validation ws-validate.py"
        )
        if not sent_ok:
            results["t12"].warn(
                " ; ".join(notes) + f" — ping sans réponse corrélée vers @{recipient}"
            )
            return
        if is_chat_error(sent_data):
            results["t12"].warn(
                " ; ".join(notes)
                + f" — ping rejeté par le CLI : {chat_error_message(sent_data)}"
            )
            return
        notes.append(f"ping envoyé à @{recipient} (vérifiez la réception sur le téléphone)")

        # --- attente du message retour avec compte à rebours VISIBLE
        window = ARGS.incoming_wait
        deadline = time.monotonic() + window
        print(f"   >>> RÉPONDEZ MAINTENANT depuis votre téléphone ! "
              f"(fenêtre de {window} s — décompte affiché toutes les 10 s)",
              flush=True)
        got_reply = False
        seen_keys = set()
        received_notes = []
        last_mark = None
        while time.monotonic() < deadline:
            for raw in EVENTS:
                for i in event_incoming_items(raw):
                    key = (i["item_id"], i["direction"], i["text_preview"], i["kind"])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        received_notes.append((i["item_id"], i["contact"], i["kind"], i["text_preview"]))
                        print(f"   📥 message reçu du téléphone ! de=@{i['contact']} "
                              f"direction={i['direction']} contenu={i['kind']} "
                              f"itemId={i['item_id']} extrait={i['text_preview']!r}", flush=True)
                        got_reply = True
            if got_reply:
                break
            remaining = int(deadline - time.monotonic())
            mark = (remaining // 10) * 10
            if mark != last_mark and remaining > 0:
                print(f"   ⏳ {remaining} s pour répondre au ping depuis le téléphone…",
                      flush=True)
                last_mark = mark
            await asyncio.sleep(1)
        if got_reply:
            notes.append("message(s) retour reçu(s) du téléphone (directRcv) : "
                         + "; ".join(f"de=@{c} [{k}] {p!r}" for _, c, k, p in received_notes))
            results["t12"].ok(" ; ".join(notes))
        else:
            notes.append(
                f"aucun message retour pendant la fenêtre de {window} s — répondez au ping "
                "dès l'affichage « RÉPONDEZ MAINTENANT » (le décompte s'affiche). "
                "ATTENTION : un message envoyé après la fin du run va vers un bot dont "
                "le volume a été détruit — il ne peut pas être reçu."
            )
            results["t12"].warn(" ; ".join(notes))


def dump_event_diagnostics(raw):
    """Diagnostics complets d'une frame : cartographie des chemins de clés
    suspectes (contenu du message) + JSON intégral. Sort à la fois dans le
    log (stdout) et dans le Summary CI, sans troncature de la structure."""
    ev = parse_event(raw)
    paths = (ev or {}).get("key_paths") or []
    lines = ["   🔍 CARTOGRAPHIE DES CLÉS DE CONTENU (chemin = valeur) :"]
    for p in paths[:40]:
        lines.append(f"      {p}")
    if not paths:
        lines.append("      (aucune clé ressemblant à un contenu de message)")
    full = raw if len(raw) <= 6000 else raw[:6000] + " …[tronqué]"
    lines.append("   🔍 JSON INTÉGRAL de la frame :")
    lines.append(f"      {full}")
    text = "\n".join(lines)
    print(text, flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("### 🔍 Diagnostics structure CLI (T09)\n\n```\n"
                        + text.replace("   ", "") + "\n```\n")
        except OSError:
            pass


async def test_t09_eval_events(results):
    """T09 — Évaluation du flux entrant capturé par le listener pendant toute
    la suite. Vérifie les primitives CLI que HA consomme :
      - événements de cycle de contact (contactConnecting/contactConnected) ;
      - newChatItems avec chatItem.meta.itemId, chatDir, contenu du message ;
      - au moins un message REÇU (chatDir directRcv) = event
        simplex_message_received + sensor last_message garantis côté HA.
    En cas de doute sur l'emplacement du contenu, dump une cartographie
    complète des clés + le JSON intégral de la frame."""
    if not EVENTS:
        results["t09"].warn(
            "aucun événement reçu pendant toute la suite — soit aucun message "
            "n'a circulé, soit le flux entrant est cassé (à investiguer)"
        )
        return
    type_summary = summarize_event_types(EVENTS)
    item_lines = []
    directions_seen = set()
    kinds_seen = set()
    fields_seen = set()
    contacts_seen = set()
    first_newchatitems_raw = None
    dumped = False
    for raw in EVENTS:
        ev = parse_event(raw)
        if not ev:
            continue
        fields_seen |= ev["raw_keys"]
        if ev["type"] == "newChatItems":
            if first_newchatitems_raw is None:
                first_newchatitems_raw = raw
            for i in ev["items"]:
                directions_seen.add(i["direction"])
                kinds_seen.add(i["kind"])
                contacts_seen.add(i["contact"])
                item_lines.append(
                    f"chatItem: de=@{i['contact']} direction={i['direction']} "
                    f"contenu={i['kind']} itemId={i['item_id']} "
                    f"extrait={i['text_preview']!r}"
                )

    has_chat_items = "newChatItems" in type_summary
    has_incoming = any(d.lower().startswith("directrcv") for d in directions_seen)
    item_fields = {"itemId", "chatDir", "msgContent"} & fields_seen
    sample = "\n".join(item_lines[:10]) or "(aucun chatItem dans les événements capturés)"

    if has_chat_items and has_incoming and len(item_fields) == 3:
        results["t09"].ok(
            f"{len(EVENTS)} événements [{type_summary}] — message(s) ENTRANT validés "
            f"(directRcv), contacts vus : {', '.join('@' + c for c in sorted(contacts_seen))}, "
            f"contenus vus : {', '.join(sorted(kinds_seen))}. Champs CLI itemId/chatDir/"
            f"msgContent présents (base des alias/chat_ref/text/direction/item_id de HA) :\n"
            f"{sample}"
        )
    elif has_chat_items and has_incoming:
        # directRcv validé mais emplacement du contenu inconnu : diagnostics complets
        if not dumped and first_newchatitems_raw:
            print("\n>>> T09 : msgContent non localisé — dump diagnostique complet :\n",
                  flush=True)
            dump_event_diagnostics(first_newchatitems_raw)
            dumped = True
        results["t09"].warn(
            f"{len(EVENTS)} événements [{type_summary}] — messages ENTRANTS (directRcv) "
            f"capturés, itemId/chatDir/alias(contact) validés, mais l'emplacement du "
            f"contenu du message n'est pas celui attendu (champs ciblés vus : "
            f"{', '.join(sorted(item_fields))}). La cartographie des clés + le JSON "
            f"intégral ci-dessus montrent où il se trouve réellement — à mapper côté HA "
            f"si nécessaire :\n{sample}"
        )
    elif has_chat_items and len(item_fields) == 3:
        results["t09"].warn(
            f"{len(EVENTS)} événements [{type_summary}] — newChatItems et champs CLI "
            f"présents ({', '.join(sorted(item_fields))}) ; directions vues : "
            f"{', '.join(sorted(directions_seen))} ; mais aucun directRcv : répondez au "
            "ping pendant le compte à rebours de T12 pour valider le sens incoming :\n"
            + sample
        )
    else:
        results["t09"].warn(
            f"{len(EVENTS)} événements [{type_summary}] — cycle de connexion capturé "
            "mais aucun chatItem exploitable (normale si personne n'a écrit au bot "
            "pendant le run) :\n" + sample
        )


async def test_t08_stability_long_run(results):
    """T08 — Stabilité : connexion maintenue, un RPC toutes les 5 s pendant --duration s.
    Passé en dernier pour ne pas retarder l'appairage interactif."""
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

TESTS = {
    "t01": "T01 Connexion WebSocket",
    "t02": "T02 Sonde protocole (enveloppe corrId)",
    "t03": "T03 Allé-retour RPC",
    "t04": "T04 Corrélation corrId entrelacée",
    "t05": "T05 Résilience JSON malformé",
    "t06": "T06 Connexions concurrentes",
    "t07": "T07 Boucle de reconnexion (5 cycles)",
    "t10": "T10 List contacts (service HA)",
    "t11": "T11 Create invite (service HA)",
    "t12": "T12 Appairage interactif + ping + attente réponse",
    "t09": "T09 Flux entrant (capturé sur tout le run) + champs CLI pour HA",
    "t08": "T08 Stabilité longue durée",
}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("SIMPLEX_WS_URL", "ws://127.0.0.1:5225"))
    parser.add_argument("--timeout", type=float, default=10.0, help="timeout RPC en secondes")
    parser.add_argument("--duration", type=int, default=60, help="durée du test de stabilité (s)")
    parser.add_argument("--probe-cmds", default="/version,/help,/contacts,/show version,chats",
                        help="commandes candidates pour la sonde de protocole, séparées par virgules")
    parser.add_argument("--invite-link", default=os.environ.get("SIMPLEX_INVITE_LINK"),
                        help="lien d'invitation créé sur votre téléphone (optionnel : sans lui, "
                             "le bot génère et publie son propre lien pour l'appairage interactif)")
    parser.add_argument("--contact-wait", type=int, default=90,
                        help="secondes max d'attente de l'appairage après publication du lien (défaut 90)")
    parser.add_argument("--incoming-wait", type=int, default=90,
                        help="secondes d'attente du message retour après le ping (défaut 90)")
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
        "t10": test_t10_list_contacts,
        "t11": test_t11_create_invite,
        "t12": test_t12_pairing_and_send,
        "t09": test_t09_eval_events,
        "t08": test_t08_stability_long_run,
    }

    try:
        for key, fn in impl.items():
            if key in skipped_results:
                skipped_results[key].warn("ignoré via --skip")
                continue
            if key == "t03" and PROBE["mode"]:
                start_listener()
            print(f">>> {TESTS[key]} …", flush=True)
            try:
                await fn(results)
            except Exception as e:
                results[key].fail(f"exception inattendue : {e}")
    finally:
        await stop_listener()

    write_summary({**results, **skipped_results}, ARGS)
    sys.exit(1 if any(r.status == "FAIL" for r in results.values()) else 0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
