# Architecture notes

Internal notes on how `plugin.py` is put together, for future maintenance. See `README.md` for user-facing documentation.

## Why Gen 3 only

Gen 3 Wall Connectors have a built-in local web server exposing a simple, unauthenticated JSON API (`/api/1/vitals`, `/api/1/lifetime`, `/api/1/wifi_status`, `/api/1/version`). Gen 2 units have no equivalent - community integrations for Gen 2 (e.g. TWCManager) instead emulate a second Wall Connector on the RS485 bus to read/influence charging, a fundamentally different and far more involved integration than local HTTP polling. This plugin only targets Gen 3.

## No control, monitoring only

None of the four local endpoints accept writes - they're GET-only. Starting/stopping a charge or setting a current limit isn't exposed by the Wall Connector's own API at all; that's negotiated between the vehicle and charger over the J1772 pilot signal, and the only way to influence it externally is through Tesla's vehicle-side cloud API (a different integration surface entirely - Indigo's separate "Tesla EV Control" plugin controls the car, not the charger).

## HTTP layer (`_get`)

A single `urllib.request` GET per endpoint, no auth, no external dependencies - matches this user's MyAir plugin's HTTP layer rather than adding `aiohttp`/`requests` for what's a synchronous polling loop anyway (Indigo's `runConcurrentThread` model, not asyncio).

Two defensive workarounds are applied before `json.loads`, both cross-checked against `einarhauks/tesla-wall-connector`'s own `api.py` (which documents the exact same quirks, implying they're real, observed Wall Connector firmware behavior rather than integration-specific bugs):

1. **Bare `nan` tokens.** The Wall Connector occasionally emits `nan` instead of `null` for a missing numeric field (e.g. `prox_v` on some firmware) - not valid JSON. Replaced with `null` via `NAN_RE` before parsing.
2. **Truncated responses.** The Wall Connector occasionally cuts off its own response mid-stream, missing the final `}`. If the first parse fails and the response doesn't already end in `}`, one closing brace is appended and parsing is retried once before giving up.

A single `HTTP_TIMEOUT = 10` (vs. e.g. MyAir's 5s) accounts for the Wall Connector being documented to sometimes take several seconds to start responding at all.

## Known, unfixable degradation under sustained polling

`evcc-io/evcc` issue #28525 documents `/api/1/vitals` becoming completely unresponsive after hours of continuous polling, resolved only by physically power-cycling the Wall Connector (restarting the polling client doesn't help) - a firmware/hardware limitation, not a bug in any particular client. This plugin can't fix that; it can only avoid making it worse and report it clearly as a device error when it happens (`dev.setErrorStateOnServer("Could not reach Wall Connector")`). Two design choices follow directly from this:

- **Poll Interval defaults to 30s, with a 10s floor** (`MIN_POLL_INTERVAL`) rather than allowing arbitrarily fast polling - the Devices.xml field's own description calls this out to the user directly.
- **`lifetime`/`wifi_status`/`version` are fetched on much slower, independent cadences** (`SLOW_POLL_INTERVAL` = 5 min, `VERSION_POLL_INTERVAL` = 1 hour) rather than every poll cycle - `_poll_wall_connector` only calls `_poll_slow`/`_poll_version` once their own timer (tracked per-device in `self._next_slow_poll_at`/`self._next_version_poll_at`) has elapsed, regardless of how fast the user has set the main Poll Interval. These three endpoints' data barely changes between polls anyway (lifetime counters, wifi signal, firmware version), so this is close to free - it directly cuts the plugin's own contribution to total request volume against an API known to degrade under load.

A failed `lifetime`/`wifi_status`/`version` fetch (`_poll_slow`/`_poll_version`) is *not* treated as a poll-wide failure - only a missing `vitals` response sets the device's error state. `vitals` is the data that actually matters moment-to-moment; losing a periodic firmware-version refresh shouldn't turn the whole device red.

## `evse_state` deliberately left undecoded

Neither Tesla, `einarhauks/tesla-wall-connector`, nor community threads (checked: a GitHub home-assistant.io issue, several forum threads) document the full meaning of `evse_state`'s numeric codes - only a handful of observed values (1 = standby, 11 = charging, 4/9 = plugged in but not charging) are informally known, with no confirmed complete mapping. Exposed as a raw `Integer` state (`evseState`) rather than guessed at, matching this user's AlphaESS Modbus plugin's same posture toward its own undocumented `battery_status` register - `charging` (computed from the two *documented* booleans `contactor_closed` + `vehicle_connected`) is the reliable, decoded status state; `evseState` is a diagnostic extra for anyone who wants to dig further.

## Power calculation (`_poll_wall_connector`)

Mirrors `einarhauks/tesla-wall-connector`'s `Vitals.total_power_w` exactly: split-phase installs (`splitPhase` config field) use `grid_v * vehicle_current_a` (a single combined-voltage/single-current-sensor reading, correct for North American 120/240V two-leg wiring); everything else sums `voltage * current` across phases A/B/C. That sum is correct for both single-phase and true 3-phase installs without needing to special-case them separately - an unused phase (B/C on a single-phase supply) simply reads 0V/0A and contributes nothing to the sum.

## Fields deliberately not exposed

`vitals` also includes several low-level hardware diagnostic signals - `relay_coil_v`/`relay_k1_v`/`relay_k2_v`, `pilot_high_v`/`pilot_low_v`, `prox_v`, `input_thermopile_uv`. These are meaningful for engineers debugging a fault condition, not for home-automation triggers/control-page display, so they're left out of `Devices.xml` entirely (same "expose what's genuinely useful, not everything available" filtering this user's AlphaESS Modbus plugin applied when it went from 1025 documented registers down to ~30). Could be added later if a real troubleshooting need comes up.

## Dashboard (`dashboard`/`dashboard_data`)

Same HTTP-responder mechanism as this user's AlphaESS Modbus plugin: registered in `Actions.xml` as `<Action id="..." uiPath="hidden"><CallbackMethod>...</CallbackMethod></Action>`, reachable at `http://<host>:8176/message/com.coolcaper.teslawallconnector/<id>`, per Indigo's official `Example HTTP Responder` SDK sample. **Both callback signatures must stay untyped** (`def dashboard(self, action, dev=None, caller_waiting_for_result=None):`, no type hints or return annotation) - this is the same Indigo HTTP-dispatch bug documented in AlphaESS Modbus's `ARCHITECTURE.md` (type hints on this specific no-device dispatch path cause an opaque `RuntimeError: unable to convert python exception` raised *before* the method body runs), applied here from the start rather than rediscovered.

`dashboard` returns the static `DASHBOARD_HTML` page (inline CSS/JS, no CDN dependency, dark-mode aware via `prefers-color-scheme`). `dashboard_data` returns the current device's states as JSON for the page's 5-second client-side poll; supports a `?deviceId=` query param and returns a `devices` list (same as AlphaESS's dashboard) so the page can render a picker if more than one Wall Connector is configured. Unlike AlphaESS's dashboard - which fans out across four separate child devices (Inverter/Solar/Battery/Grid) - this plugin has only one device type carrying every state directly, so the payload is a single flat `states` object (`DASHBOARD_STATE_KEYS`, kept in sync with every `<State>` in `Devices.xml`) rather than AlphaESS's per-child-device nesting. All dynamic values are written into the page via `.textContent`/`createElement`/`replaceChildren`, never `.innerHTML`, so a device renamed to contain HTML/script content can't inject into the dashboard - same defensive posture as AlphaESS's.

Status color (green=charging, blue=connected-not-charging, gray=not connected) reuses hue choices from AlphaESS's dashboard palette, but as a state indicator rather than a categorical-identity channel (there's no risk of confusing "this dashboard's green" with "that dashboard's green" since they're never viewed side by side) - not bound by that palette's identity-channel consistency rules.
