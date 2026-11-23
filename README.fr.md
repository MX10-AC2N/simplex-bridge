# SimpleX Bridge — MX10-AC2N

[English](README.md) | **Français**

Un démon bot SimpleX Chat dockerisé exposant l'API WebSocket du CLI SimpleX.

Ce dépôt est un fork maintenu de
[libre-7/simplex-bridge](https://github.com/libre-7/simplex-bridge), étendu avec
une suite de validation orientée Home Assistant et un démarrage durci.

Les images Docker sont publiées sur GitHub Container Registry :

`ghcr.io/mx10-ac2n/simplex-bridge`

[![WebSocket Validation](https://github.com/MX10-AC2N/simplex-bridge/actions/workflows/ws-validation.yml/badge.svg)](https://github.com/MX10-AC2N/simplex-bridge/actions/workflows/ws-validation.yml)

## Fonctionnalités

- Démon SimpleX Chat en mode WebSocket.
- Création automatique du profil bot et de l'adresse de contact (idempotente —
  sans risque au redémarrage).
- Base de données et identité du bot persistantes.
- Exécution du démon sans privilèges via PUID/PGID configurables.
- Correction des permissions DNS Docker au démarrage, vérifiée sous l'identité
  réelle du démon.
- Pièces jointes reçues dans un volume dédié, rendues lisibles par tous pour
  que Home Assistant puisse le monter en lecture seule.
- Durcissement sécurité Docker (capabilities retirées, no-new-privileges).
- Images multi-architectures (AMD64 et ARM64).
- Suite de validation WebSocket automatisée (T01–T13) certifiant tout ce dont
  l'intégration Home Assistant dépend.

## Démarrage rapide

Cloner le dépôt :

```bash
git clone https://github.com/MX10-AC2N/simplex-bridge.git
cd simplex-bridge
```

Construire et démarrer :

```bash
docker compose up -d --build
```

Voir les logs :

```bash
docker compose logs -f simplex-bridge
```

Récupérer l'adresse de contact du bot :

```bash
docker compose exec simplex-bridge cat /data/bot_address.txt
```

Le volume Docker nommé `simplex-data` stocke la base SimpleX et l'identité du
bot. Les pièces jointes reçues sont stockées à part dans `simplex-inbox` ; la
outbox partagée optionnelle est `simplex-outbox`. Sauvegardez `simplex-data`
régulièrement.

## Utiliser l'image publiée

Pour utiliser l'image publiée par ce fork au lieu de construire localement :

```bash
docker compose pull
docker compose up -d
```

Image :

```text
ghcr.io/mx10-ac2n/simplex-bridge:latest
```

En production, préférez un tag de commit ou de version précis plutôt que
`latest`.

## Configuration

| Variable | Défaut | Description |
|---|---|---|
| `SIMPLEX_DISPLAY_NAME` | SimpleX Bridge | Nom affiché du bot |
| `SIMPLEX_AUTO_ACCEPT` | true | Acceptation automatique des contacts |
| `SIMPLEX_FILES_ENABLED` | true | Activer les transferts de fichiers |
| `SIMPLEX_MARK_READ` | true | Marquer les messages comme lus |
| `SIMPLEX_TOR` | false | Activer l'option Tor de SimpleX |
| `SIMPLEX_STARTUP_TIMEOUT` | 15 | Timeout de démarrage WebSocket (secondes) |
| `SIMPLEX_FILES_DIR` | `/data/received_files` | Dossier utilisé par le CLI pour les pièces jointes reçues (`--files-folder`) |
| `SIMPLEX_DNS_CHECK_HOST` | smp5.simplex.im | Hôte du précontrôle DNS |
| `SIMPLEX_SOCAT_PORT` | non définie | Port d'écoute socat optionnel |
| `SIMPLEX_BOT_COMMANDS` | non définie | Menu de commandes bot (syntaxe SimpleX) |
| `PUID` | 99 | UID de l'utilisateur du démon |
| `PGID` | 100 | GID du groupe du démon |
| `TZ` | UTC | Fuseau horaire du conteneur |

PUID doit être non nul. Le démon n'est volontairement pas exécuté en root.

### Volumes de pièces jointes partagés avec Home Assistant

Le fichier Compose donne aux volumes de pièces jointes des noms Docker stables :
`simplex-inbox` et `simplex-outbox`. Un projet Compose Home Assistant séparé
peut monter ces mêmes volumes en `external: true`. Les fichiers qui arrivent
dans l'inbox sont créés lisibles par le groupe et par tous (`umask 0022`), ce
qui permet à l'intégration de les servir sans problème de permissions.
Gardez `simplex-data` privé à ce conteneur passerelle : il contient la base
SimpleX et les clés d'identité.

## Correctif des permissions DNS

### Problème

Docker génère `/etc/resolv.conf` et `/etc/hosts` au démarrage du conteneur.

Sur certains systèmes, ces fichiers peuvent avoir des permissions restrictives :

```text
-rw-r----- root root /etc/resolv.conf
-rw-r----- root root /etc/hosts
```

Le démon SimpleX tourne sous un utilisateur sans privilèges.

Si cet utilisateur ne peut pas lire ces fichiers, la résolution DNS échoue
même quand l'hôte Docker lui-même résout les mêmes domaines.

### Solution

L'entrypoint corrige ces permissions au démarrage :

```bash
chmod 0644 /etc/resolv.conf
chmod 0644 /etc/hosts
```

Il vérifie ensuite que l'utilisateur du démon peut lire :

- `/etc/resolv.conf`
- `/etc/hosts`
- `/etc/nsswitch.conf`

Enfin, il teste la résolution DNS avec le même UID/GID que le démon :

```bash
gosu "$PUID:$PGID" getent ahostsv4 smp5.simplex.im
```

Le test DNS est retenté trois fois.

Si la résolution échoue toujours, le démarrage s'arrête avec les diagnostics.

Cette correction est faite à l'exécution car Docker crée ou monte ces fichiers
au démarrage du conteneur : changer leurs permissions dans le Dockerfile ne
résoudrait pas le problème.

### Configuration DNS

Le nom d'hôte utilisé par le précontrôle DNS est modifiable :

```yaml
environment:
  SIMPLEX_DNS_CHECK_HOST: "smp5.simplex.im"
```

Le défaut est `smp5.simplex.im`.

### Diagnostic DNS

Consulter les logs :

```bash
docker compose logs --tail=150 simplex-bridge
```

Vérifier les permissions des résolveurs :

```bash
docker compose exec simplex-bridge ls -l \
  /etc/resolv.conf \
  /etc/hosts \
  /etc/nsswitch.conf
```

Tester le DNS sous l'identité du démon :

```bash
docker compose exec simplex-bridge \
  gosu 99:100 getent ahostsv4 smp5.simplex.im
```

Si cela échoue, inspectez le réseau Docker, le forwarding DNS et les règles du
pare-feu de l'hôte.

## Réseau et sécurité

L'API WebSocket écoute sur :

```text
127.0.0.1:5225
```

L'API ne fournit pas d'authentification par défaut.

Le réseau hôte est recommandé quand les clients (Home Assistant, Hermes Agent)
tournent sur la même machine.

Pour l'[intégration Home Assistant MX10-AC2N](https://github.com/MX10-AC2N/SimpleX-Chat_HA)
sur le même hôte Linux, configurez **à la fois** `homeassistant` et
`simplex-bridge` avec `network_mode: host`. Dans l'intégration, utilisez
`ws://127.0.0.1:5225` (c'est aussi son URL par défaut). Aucun mapping de port
ni proxy SOCAT nécessaires pour cette topologie.

Le port TCP 5225 de l'hôte doit être libre avant le démarrage de la passerelle.
L'entrypoint échoue franchement si un autre processus écoute déjà sur ce port,
plutôt que de confondre un listener sans rapport avec le démon SimpleX.

Le conteneur retire toutes les capabilities Linux sauf :

- CHOWN
- SETUID
- SETGID

Elles sont requises par l'entrypoint root pour préparer la propriété des
données et abandonner les privilèges.

La protection suivante reste activée :

```yaml
security_opt:
  - no-new-privileges:true
```

### Avertissement SOCAT

Si `SIMPLEX_SOCAT_PORT` est activé, socat peut exposer un listener sans
authentification sur le réseau.

N'activez cette fonctionnalité que sur des réseaux de confiance.

N'exposez jamais l'API WebSocket directement sur Internet.

### Commandes bot

Vous pouvez exposer un menu de commandes aux utilisateurs :

```yaml
SIMPLEX_BOT_COMMANDS: "'Help':/help,'Status':/status,'Info':/info"
```

Syntaxe complète (menus imbriqués possibles) :

```text
'Label':/keyword,'Menu':{'Sub':/sub <param>}
```

Voir la documentation officielle :
https://github.com/simplex-chat/simplex-chat/blob/stable/bots/README.md

## Suite de validation WebSocket

`tests/ws-validate.py` (lancée par
[`.github/workflows/ws-validation.yml`](.github/workflows/ws-validation.yml))
valide le contrat complet de la passerelle pour l'intégration Home Assistant :

| Interaction HA | Test(s) |
|---|---|
| `binary_sensor.simplex_chat_connection` + backoff de reconnexion | T01, T07 |
| Protocole RPC `{"corrId","cmd"}` (mode string), corrélation | T02, T03, T04 |
| Résilience aux frames malformées, connexions concurrentes | T05, T06 |
| `list_contacts` / `create_invite` / `accept_invite` / `send_message` | T10, T11, T12 |
| Stabilité longue durée sous trafic réel | T08 |
| Flux entrant : alias, direction, item_id, texte (image/vidéo/fichier inclus) | T09 |
| Replay du mapping chat-item HA (garde-fou régression sur la sérialisation CLI) | T13 |

Lancer localement :

```bash
python3 tests/ws-validate.py --url ws://127.0.0.1:5225
```

### Appairage interactif (un seul run)

Le workflow est un `workflow_dispatch` avec paramètres ajustables (attente de
contact, fenêtre de réponse). Pendant le run :

1. T12 publie un lien d'invitation frais dans les logs et dans le Summary du
   run (ligne `INVITE LINK: …`) ;
2. ouvrez ce lien dans l'app SimpleX de votre téléphone pendant que le run
   attend ;
3. le bot envoie un ping de validation au nouveau contact ;
4. répondez depuis votre téléphone pendant la fenêtre de réponse (défaut 90 s,
   compte à rebours affiché). La détection continue pendant T08, et T09
   évalue la totalité du run — une réponse un peu tardive reste comptée.

Chaque run CI crée une identité de bot NEUVE (le volume est détruit par
`down -v`) : répondez toujours dans le fil qui vient de recevoir le ping —
pas dans un ancien fil « SimpleX Bridge » d'un run précédent.

### Baseline certifiée

**Baseline : [12 PASS / 1 WARN / 0 FAIL](docs/validation-baseline.md)** — run
du 2026-11-23, SimpleX CLI 7.1.4.0 / simplexmq 7.1.0.18, `websockets` 17.4.

Run immuable de référence :
https://github.com/MX10-AC2N/simplex-bridge/actions/runs/45246557568

Le badge reflète le **dernier** run (il passe rouge si une évolution future
casse le contrat) ; la baseline figée vit dans
[`docs/validation-baseline.md`](docs/validation-baseline.md).

## Autres intégrations (héritage du fork)

Le fork amont embarque un assistant d'intégration Hermes Agent :

```bash
bash install-websockets.sh
```

Cet assistant installe la dépendance WebSocket et vérifie l'environnement
Hermes Agent (`SIMPLEX_WS_URL`, `SIMPLEX_ALLOW_ALL_USERS`,
`SIMPLEX_HOME_CHANNEL`). Les deux conteneurs doivent partager l'espace réseau
de l'hôte.

## Construction locale

Construire directement :

```bash
docker build -t simplex-bridge:local .
```

Ou avec Docker Compose :

```bash
docker compose up -d --build
```

## Tests

Lancer les tests de régression :

```bash
bash tests/run-all.sh
```

La suite vérifie :

- Syntaxe shell.
- Compilation Python.
- YAML Docker Compose et workflows.
- Templates XML.
- Précontrôle DNS.
- Détection de port.
- Comportement de l'installeur.
- Setup premier démarrage.
- Cohérence de la documentation.

Le contrat WebSocket lui-même est validé par la suite dédiée — voir
[suite de validation WebSocket](#suite-de-validation-websocket).

## Publication des images

Les images sont publiées automatiquement sur :

```text
ghcr.io/mx10-ac2n/simplex-bridge
```

## Licence

Le code source du projet est sous licence GPL-3.0.
Voir le fichier LICENSE.

Les composants tiers conservent leurs licences respectives.
