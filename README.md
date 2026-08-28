# Tesla Wall Connector

An [Indigo Domotics](https://www.indigodomo.com/) plugin for monitoring a 3rd-generation Tesla Wall Connector over its local network API - live charging status, power/current/voltage, temperatures, and lifetime counters, with no cloud account or internet connection required.

> This is an unofficial, community-built plugin. It is not affiliated with or endorsed by Tesla. It is **monitoring only** - the Wall Connector's local API exposes no way to start/stop a charge or set a current limit. Controlling charging is only possible through Tesla's own vehicle-side app/API (a completely different integration - see Indigo's separate "Tesla EV Control" plugin, which controls the *car*, not the charger).

## Requirements

- A **3rd-generation** Tesla Wall Connector (Gen 2 units have no local API and aren't supported by this plugin - see Architecture).
- The Wall Connector on the same local network as your Indigo server. Its local API is unauthenticated and unencrypted, served on port 80 - fine on a private home LAN, not something to expose to the internet.

## Devices

### Tesla Wall Connector (`wallConnector`)

| Config field | Description |
|---|---|
| Wall Connector IP Address | Local IP or hostname of the Wall Connector |
| Poll Interval | Dropdown: 10s (not recommended)/15s/30s (default)/1m/5m |
| North American split-phase supply | Check only for North American 120/240V split-phase wiring; leave unchecked for a single 230V phase or 3-phase (e.g. Australia/UK/EU) |

| State | Description |
|---|---|
| `vehicleConnected` | Whether a vehicle is currently plugged in |
| `charging` | `contactorClosed` AND `vehicleConnected` - power is actively being delivered right now |
| `contactorClosed` | Whether the internal contactor is closed (delivering/attempting power) |
| `power` | Instantaneous power (W) |
| `vehicleCurrent` | Current being drawn by the vehicle (A) |
| `gridVoltage` | Measured grid voltage (V) |
| `gridFrequency` | Measured grid frequency (Hz) |
| `sessionEnergy` | Energy delivered so far this session (kWh) |
| `sessionTime` | Duration of the current session (e.g. `1h 27m`) |
| `handleTemp` / `pcbaTemp` / `mcuTemp` | Internal temperatures (°C) |
| `currentAlerts` | Any active alerts, comma-separated, or `None` |
| `notReadyReasons` | Why the EVSE isn't ready to charge, if applicable, or `None` |
| `evseState` | Raw numeric EVSE state code - not decoded into a label, since its meaning isn't documented anywhere (see Architecture) |
| `lifetimeEnergy` | Total energy delivered over the unit's lifetime (kWh) |
| `chargeStarts` | Number of charging sessions started |
| `contactorCycles` | Number of times the contactor has cycled |
| `alertCount` | Lifetime alert count |
| `uptime` | How long the Wall Connector has been running since its last reboot |
| `wifiConnected` / `internetConnected` | Wifi and internet connectivity |
| `wifiSignalStrength` | Wifi signal strength |
| `firmwareVersion` / `serialNumber` | Unit identification |

`lifetime`/`wifi_status`/`version` are polled far less often than the live telemetry (every 5 minutes and every hour respectively, regardless of the configured Poll Interval) - see Architecture for why.

## Live dashboard

A live web dashboard is served directly by the plugin at:

```
http://<your-indigo-host>:8176/message/com.coolcaper.teslawallconnector/dashboard
```

Shows current status (Charging/Connected/Not Connected), power, session energy/time, grid voltage/frequency, temperatures, alerts, and lifetime/connectivity stats - self-refreshes every 5 seconds, no login required (same LAN-only trust model as the Wall Connector's own local API). If you have more than one Wall Connector device configured, a picker lets you switch between them.

## Credits

Endpoint/field shapes cross-checked against [einarhauks/tesla-wall-connector](https://github.com/einarhauks/tesla-wall-connector) (MIT-licensed, actively maintained, used to back Home Assistant's own integration).
