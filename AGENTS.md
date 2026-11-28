# AGENTS.md — Roadmap & Specifications for `simplex-bridge` & `SimpleX-Chat_HA`

> **Purpose**: Actionable engineering instructions for AI coding agents (Claude Code, Codex, Cursor, Copilot, etc.) working across `MX10-AC2N/simplex-bridge` and `MX10-AC2N/SimpleX-Chat_HA`.
> **Baseline**: Audited against `simplex-bridge` (v1.5.1 / SimpleX CLI 7.1.4.0) and `SimpleX-Chat_HA` (HA 2025.8+).

---

## 1. System Architecture & Cross-Repo Contract

The solution is split into two decoupled repositories communicating over a local WebSocket (`ws://127.0.0.1:5225` by default) and shared Docker volumes:

1. **`simplex-bridge`** (Daemon / Gateway):
   - Runs `simplex-chat -p 5225` as a non-root user (`PUID` / `PGID`, strictly non-zero).
   - Manages persistent identity & SQLite DB in private volume `simplex-data`.
   - Stores incoming attachments in `simplex-inbox` (`/data/received_files`, `umask 0022` so HA can read them) and outgoing staging files in `simplex-outbox`.
   - Enforces DNS preflight (`getent ahostsv4`), port readiness checks, and container hardening (`no-new-privileges:true`, minimal capabilities `CHOWN`, `SETUID`, `SETGID`).
   - Validates the WebSocket contract via `tests/ws-validate.py` (`T01`–`T13`) and `tests/run-all.sh`.

2. **`SimpleX-Chat_HA`** (Home Assistant Custom Component + Lovelace Frontend):
   - Connects to `simplex-bridge` via `custom_components/simplex/websocket_client.py` using correlated RPC commands (`{"corrId": "...", "cmd": "..."}`) and asynchronous event streaming.
   - Maps contacts/groups to HA Devices, `notify.*` entities, sensors, binary sensors, bus events, services, and Assist/LLM tools (`llm_api.py`).
   - Serves inline media from read-only mounted `simplex-inbox` via `http.py` and renders custom cards (`custom_components/simplex/cards/`) + dashboards (`custom_components/simplex/dashboards/`).

### Invariants Agents MUST Preserve
- **Never** expose the unauthenticated WebSocket API publicly or remove the `127.0.0.1` default binding.
- **Never** mount `simplex-data` into Home Assistant; only `simplex-inbox` (`:ro`) and `simplex-outbox` may be shared.
- **Never** break the RPC correlation contract (`corrId` + `cmd` string mode) tested by `T01`–`T13` in `simplex-bridge/tests/ws-validate.py`.
- **Always** initialize/re-subscribe to the SimpleX CLI asynchronous event stream (`/_start`) upon WebSocket connection and reconnection.

---

## 2. Priority Task Backlog for AI Agents

Execute tasks in priority order (`P0` → `P2`). Run the relevant test suite after every task.

---

### P0 — Cleanup & Repository Hygiene (`SimpleX-Chat_HA`)

#### Task P0.1: Remove stray artifact in frontend source
- **Target**: `frontend/messenger/src/1.txt`
- **Action**: Delete `frontend/messenger/src/1.txt` (1-byte leftover file) and ensure `.gitignore` prevents temporary artifacts.

#### Task P0.2: Synchronize TypeScript frontend build with HA card assets
- **Target**: `frontend/messenger/` and `custom_components/simplex/cards/simplex-messenger-card/`
- **Problem**: `frontend/messenger/src/simplex-messenger-card.ts` (with `glass-theme.ts` and `types.ts`) is built via Vite, while Home Assistant loads `custom_components/simplex/cards/simplex-messenger-card/simplex-messenger-card.js`. Any edit to `.ts` files risks drifting from the committed `.js` bundle.
- **Actions**:
  1. Inspect `frontend/messenger/vite.config.ts` and `frontend/messenger/package.json` to ensure `npm run build` outputs directly to `custom_components/simplex/cards/simplex-messenger-card/simplex-messenger-card.js` (or add a post-build copy step).
  2. Add a CI check in `.github/workflows/ci.yml` that builds `frontend/messenger` and verifies `git diff --exit-code custom_components/simplex/cards/` so `.ts` and `.js` never drift.
  3. Document the exact build workflow in `docs/development.md`.

---

### P1 — Media & Event Mapping Hardening + Replay Tests (`SimpleX-Chat_HA` & `simplex-bridge`)

#### Task P1.1: Enforce pure, defensive `chatItem` parsing & path-traversal protection
- **Target**: `custom_components/simplex/mapping.py` and `custom_components/simplex/http.py`
- **Requirements**:
  1. Keep `mapping.py` pure (no direct network calls or side effects) so payloads can be replayed deterministically in unit tests.
  2. Normalize all message variants into a consistent internal representation covering:
     - `media_type`: `text`, `image`, `audio` (including CLI `voice` items), `video`, `file`, `unknown`
     - `direction`: incoming (`in`) vs outgoing (`out`)
     - `chat_type`: direct contact (`contactId`) vs group (`groupId`)
     - `file_status`: `ready` (file exists on disk), `pending` (metadata received before `rcvFileComplete` / `chatItemUpdated`), `unavailable` (files disabled or folder unmounted), `invalid_path` (security rejection)
  3. **Path Traversal Guard**: When resolving attachment filenames against `inbox_dir`, resolve canonical paths (`Path.resolve()`) and verify the target strictly resides inside `inbox_dir.resolve()`. Reject `../` or absolute path escapes with `invalid_path` and log a warning.
  4. Handle two-phase media delivery gracefully: when `newChatItems` arrives before the file finishes downloading, mark `file_status = "pending"` and update the entity/UI cleanly when `chatItemUpdated` / `rcvFileComplete` fires.

#### Task P1.2: Expand `cli_v7_replay_frames.json` and replay unit tests
- **Target**: `tests/data/cli_v7_replay_frames.json` and `tests/test_simplex.py`
- **Actions**:
  1. Ensure `tests/data/cli_v7_replay_frames.json` includes real anonymized SimpleX CLI v7.1+ JSON frames for:
     - Incoming & outgoing plain text (including Unicode/emojis and quoted replies)
     - Image with caption and without caption
     - Voice note (`voice`) and audio attachment (`audio`)
     - Video attachment (`.mp4`)
     - Generic file attachment
     - Two-step pending → ready file update (`chatItemUpdated`)
     - Group message (`groupInfo`) vs direct contact (`contact`)
     - Malicious path traversal filename (`../../etc/passwd`)
  2. Add parameterized replay assertions in `tests/test_simplex.py` validating `item_id`, `chat_id`, `sender_alias`, `direction`, `media_type`, `text`, `file_name`, and `file_status` for every fixture.

#### Task P1.3: Document & resolve the `1 WARN` in `simplex-bridge` baseline
- **Target**: `simplex-bridge/docs/validation-baseline.md` and `simplex-bridge/tests/ws-validate.py`
- **Actions**:
  1. Inspect the `1 WARN` recorded in the certified baseline (`12 PASS / 1 WARN / 0 FAIL`).
  2. Either fix the underlying timing/serialization edge case in `entrypoint.sh` / `ws-validate.py` so the suite achieves `13 PASS / 0 WARN / 0 FAIL`, or explicitly document the exact trigger and acceptance rationale in `docs/validation-baseline.md`.

---

### P1 — Architectural Refactoring & Diagnostics (`SimpleX-Chat_HA`)

#### Task P1.4: Decompose `__init__.py` (40 KB) and `websocket_client.py` (36.5 KB)
- **Target**: `custom_components/simplex/__init__.py`, `custom_components/simplex/websocket_client.py`
- **Problem**: Large monolithic modules make lifecycle testing (`tests/test_lifecycle.py`) and maintenance harder.
- **Actions**:
  1. Extract service registration and handlers from `__init__.py` into a dedicated `custom_components/simplex/services.py` module, keeping `__init__.py` focused on `async_setup_entry`, `async_unload_entry`, and coordinator wiring.
  2. Separate `websocket_client.py` into clean layers:
     - Transport & reconnection loop (exponential backoff, `/_start` subscription on connect, `corrId` pending future table with per-request timeouts and cleanup on disconnect).
     - High-level RPC command helpers (`list_contacts`, `create_invite`, `accept_invite`, `send_message`, `send_file`).
  3. Ensure `tests/test_lifecycle.py` covers clean teardown (canceling pending `corrId` futures and background tasks without leaked tasks on integration reload/unload).

#### Task P1.5: Add diagnostic sensors/attributes for attachments & bridge health
- **Target**: `custom_components/simplex/binary_sensor.py` and `custom_components/simplex/sensor.py`
- **Actions**:
  1. Expose clear diagnostic attributes on the bridge connection entity: WebSocket URL, reconnect count, last error timestamp/reason, and inbox mount status (`readable`, `missing`, or `empty`).
  2. Surface a clear warning/repair or diagnostic state if `simplex-inbox` is inaccessible when media messages arrive.

---

### P2 — Security Hardening, Dashboards & Documentation Consistency

#### Task P2.1: Startup warning for `SIMPLEX_SOCAT_PORT` (`simplex-bridge`)
- **Target**: `simplex-bridge/entrypoint.sh`
- **Action**: Emit a prominent security warning in container startup logs whenever `SIMPLEX_SOCAT_PORT` is non-empty, reminding operators that the WebSocket API has no authentication.

#### Task P2.2: Dashboard dependency reduction & FR/EN parity (`SimpleX-Chat_HA`)
- **Target**: `custom_components/simplex/dashboards/` and `docs/dashboards.md`
- **Actions**:
  1. Ensure there is a zero-third-party-dependency default dashboard (using only built-in HA cards + `custom:simplex-*` cards bundled by the integration) so users do not hit `Custom element: mushroom-...` errors out of the box.
  2. Audit FR/EN parity across `dashboards/` (currently `simplex_tablet_fr.yaml`, `simplex_android_home_fr.yaml`, and `simplex_android_look_fr.yaml` lack `_en.yaml` counterparts) — either provide `_en.yaml` equivalents or document them as FR-specific templates in `docs/dashboards.md` and `tests/test_dashboards.py`.
  3. Standardize French README naming across both repos (`README.fr.md` vs `README_fr.md`) with a symlink or updated cross-links so no bookmarks break.

---

## 3. Verification Checklist Before Finishing Any PR

### In `simplex-bridge`
```bash
bash tests/run-all.sh
python3 tests/check-docs.py
# When a local daemon is running:
python3 tests/ws-validate.py --url ws://127.0.0.1:5225
```

### In `SimpleX-Chat_HA`
```bash
pytest tests/
npm --prefix frontend/messenger ci && npm --prefix frontend/messenger run build
git diff --exit-code custom_components/simplex/cards/
```
