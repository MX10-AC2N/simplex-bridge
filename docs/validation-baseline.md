# Baseline de validation — passerelle WebSocket simplex-bridge

**Statut : CERTIFIÉE** — résultat figé, non re-run automatiquement.
**Date du run :** 2026-11-23
**Run de référence (immuable) :** https://github.com/MX10-AC2N/simplex-bridge/actions/runs/45246557568/job/137944719536

| Paramètre | Valeur |
|---|---|
| Résultat | **12 PASS / 1 WARN / 0 FAIL** |
| CLI SimpleX | 7.1.4.0 |
| simplexmq | 7.1.0.18 |
| `websockets` (Python) | 17.4 (épinglé) |

## Résultats détaillés

| Test | Ce que ça certifie pour HA | Statut |
|---|---|---|
| T01 | Connexion WebSocket (`binary_sensor.simplex_chat_connection`) | ✅ |
| T02 | Enveloppe protocole `{"corrId","cmd"}` mode string | ✅ |
| T03 | Allé-retour RPC corrélé | ✅ |
| T04 | Corrélation corrId entrelacée (réponses en désordre) | ✅ |
| T05 | Résilience JSON malformé | ✅ |
| T06 | Connexions concurrentes | ✅ |
| T07 | 5 cycles de reconnexion (backoff HA) | ✅ |
| T10 | `list_contacts` (capteur contacts) | ✅ |
| T11 | `create_invite` — lien propre | ✅ |
| T12 | Appairage interactif + ping (WARN = réponse arrivée pendant T08, prévu) | ⚠️ |
| T08 | Stabilité 60 s sous trafic réel | ✅ |
| T09 | Flux entrant : video/image/file/text + itemId, alias, direction, itemText | ✅ |
| T13 | Replay mapping HA (wrapper v7 déballé, item_id = chatItem.itemId) | ✅ |

## Preuves du flux entrant (messages réels du run)

- `contenu=video itemId=17`, `contenu=text itemId=18 'Alors ?'`,
  `contenu=image itemId=19`, `contenu=file itemId=20`, `contenu=text itemId=21 'Cool'`
- 3 × `rcvFileDescrReady` (fichiers reçus préparés par le démon)
- 7 items système filtrés : `chatBanner`, `rcvChatFeature`, `rcvDirectE2EEInfo`

## Réserves

- Chaque run CI crée une identité de bot NEUVE (volume détruit par `down -v`) ;
  la production garde le volume persistant `simplex-data`.
- Le WARN T12 est un artefact de fenêtrage (réponse après 90 s), pas un défaut.
- T13 valide la réplique du mapping HA contre la sérialisation CLI, pas le code
  HA installé — la validation finale se fait côté intégration.