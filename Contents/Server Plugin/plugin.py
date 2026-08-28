try:
    import indigo
except ImportError:
    pass

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Optional

# Field/endpoint shapes cross-checked against einarhauks/tesla-wall-connector
# (MIT-licensed, actively maintained, used to back Home Assistant's own
# integration) - https://github.com/einarhauks/tesla-wall-connector - rather
# than guessing at the JSON shape from scratch. All four endpoints below are
# GET-only: Gen 3 Wall Connectors expose no local write/control API. Starting/
# stopping a charge or setting current is only possible via Tesla's
# vehicle-side cloud API (a completely different integration surface - see
# the separate "Tesla EV Control" plugin) - this plugin is monitoring-only.
#
# The Wall Connector's local API is documented (by that project, and by
# evcc-io/evcc issue #28525) to:
#  - occasionally emit invalid JSON containing bare `nan` tokens instead of
#    `null`
#  - occasionally truncate its response, missing the final `}`
#  - be slow to start responding (can take several seconds)
#  - after PROLONGED polling (hours), stop responding to /api/1/vitals
#    entirely until the unit itself is power-cycled - a firmware/hardware
#    limitation with no known software fix. This plugin can only detect and
#    report that as an error, not work around it.
NAN_RE = re.compile(r":\s*\bnan\b", re.IGNORECASE)
HTTP_TIMEOUT = 10  # seconds - the Wall Connector can be slow to start responding

# runConcurrentThread ticks every 5 seconds, so anything faster wouldn't
# actually poll any sooner - just enforced as a floor. Matches the AlphaESS
# Modbus plugin's same per-device pollInterval pattern.
MIN_POLL_INTERVAL = 10
DEFAULT_POLL_INTERVAL = 30

# lifetime/wifi_status change far less often than vitals, and version/
# firmware essentially never changes between restarts - polling them every
# cycle would just add unnecessary requests against an API already known to
# degrade under sustained load (see above), so they're fetched on much
# slower cadences instead.
SLOW_POLL_INTERVAL = 300      # seconds (5 min) - lifetime/wifi_status
VERSION_POLL_INTERVAL = 3600  # seconds (1 hour) - version/firmware

# Every state the wallConnector device carries, in display order - handed to
# the dashboard page as one flat object per device (this plugin has no child
# device types, unlike AlphaESS Modbus's Inverter/Solar/Battery/Grid split).
DASHBOARD_STATE_KEYS = [
    "vehicleConnected", "charging", "contactorClosed", "power", "vehicleCurrent",
    "gridVoltage", "gridFrequency", "sessionEnergy", "sessionTime",
    "handleTemp", "pcbaTemp", "mcuTemp", "currentAlerts", "notReadyReasons", "evseState",
    "lifetimeEnergy", "chargeStarts", "contactorCycles", "alertCount", "uptime",
    "wifiConnected", "internetConnected", "wifiSignalStrength",
    "firmwareVersion", "serialNumber",
]

# Self-contained dashboard page - inline CSS/JS, no CDN dependency, dark-mode
# aware via prefers-color-scheme. Served by the `dashboard` action below;
# polls `dashboard_data` client-side every 5s. Layout/CSS tokens/JS
# conventions (tile/detail-card grid, .dot categorical indicators, banner for
# errors, textContent-only rendering) match this user's AlphaESS Modbus
# plugin's dashboard for visual consistency across their Indigo plugins -
# simplified here since there's only one device type/no children to fan out
# to. Status colors (green=charging, blue=connected-not-charging, muted=not
# connected) are a state indicator, not a categorical identity channel, so
# reusing hues from the AlphaESS palette here doesn't create the "two
# different things share a color" confusion that palette was designed to
# avoid - these two dashboards are never viewed side by side.
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tesla Wall Connector</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page-plane: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --border: rgba(11,11,11,0.10);
    --status-charging: #1baf7a;
    --status-connected: #2a78d6;
    --status-critical: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --page-plane: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --border: rgba(255,255,255,0.10);
      --status-charging: #199e70;
      --status-connected: #3987e5;
      --status-critical: #e66767;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page-plane);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 16px 48px;
  }
  .page { max-width: 880px; margin: 0 auto; }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0; }
  .meta { color: var(--text-muted); font-size: 13px; }
  .headerMeta { color: var(--text-muted); font-size: 13px; margin-top: 4px; }
  select {
    font: inherit; color: var(--text-primary); background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px;
  }
  .banner {
    display: none; align-items: center; gap: 8px; margin-bottom: 16px;
    padding: 10px 14px; border-radius: 10px; font-size: 14px;
    color: var(--status-critical); border: 1px solid var(--status-critical);
    background: color-mix(in srgb, var(--status-critical) 10%, transparent);
  }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }
  .tile {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px;
  }
  .tile-label { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 13px; margin-bottom: 10px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .dot-charging { background: var(--status-charging); }
  .dot-connected { background: var(--status-connected); }
  .dot-idle { background: var(--text-muted); }
  .value { font-size: 28px; font-weight: 600; line-height: 1.1; }
  .sub { color: var(--text-muted); font-size: 13px; margin-top: 6px; }
  .details {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px; margin-top: 12px;
  }
  .detail-card {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px;
  }
  .detail-card h2 { font-size: 14px; font-weight: 600; margin: 0 0 10px; }
  .rows { display: flex; flex-direction: column; }
  .row {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 7px 0; border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  .row:last-child { border-bottom: none; }
  .row-label { color: var(--text-secondary); }
  .row-value { font-weight: 500; text-align: right; }
  .empty-note { color: var(--text-muted); font-size: 13px; }
  footer { margin-top: 24px; color: var(--text-muted); font-size: 12px; }
</style>
</head>
<body>
<div class="page">
  <header>
    <div>
      <h1 id="deviceName">Tesla Wall Connector</h1>
      <div class="headerMeta" id="wcMeta"></div>
    </div>
    <div style="display:flex; align-items:center; gap:10px;">
      <select id="deviceSelect" style="display:none;"></select>
      <span class="meta" id="lastUpdated">-</span>
    </div>
  </header>

  <div class="banner" id="banner">&#9888; <span id="bannerText"></span></div>

  <div class="grid">
    <div class="tile">
      <div class="tile-label"><span class="dot" id="statusDot"></span>Status</div>
      <div class="value" id="statusValue">-</div>
      <div class="sub" id="statusSub">-</div>
    </div>
    <div class="tile">
      <div class="tile-label">Power</div>
      <div class="value" id="powerValue">-</div>
      <div class="sub" id="currentSub">-</div>
    </div>
    <div class="tile">
      <div class="tile-label">Session Energy</div>
      <div class="value" id="sessionEnergyValue">-</div>
      <div class="sub" id="sessionTimeSub">-</div>
    </div>
  </div>

  <div class="details">
    <div class="detail-card">
      <h2>Charging</h2>
      <div class="rows" id="chargingDetail"></div>
    </div>
    <div class="detail-card">
      <h2>Wall Connector</h2>
      <div class="rows" id="deviceDetail"></div>
    </div>
    <div class="detail-card">
      <h2>Lifetime &amp; connectivity</h2>
      <div class="rows" id="lifetimeDetail"></div>
    </div>
  </div>

  <footer>Auto-refreshes every 5 seconds.</footer>
</div>
<script>
(function () {
  var POLL_MS = 5000;
  var selectedDeviceId = null;
  var deviceSelect = document.getElementById("deviceSelect");

  function fmt(value, decimals, unit) {
    if (value === null || value === undefined || value === "") return "-";
    return Number(value).toFixed(decimals) + (unit ? " " + unit : "");
  }

  function formatPower(watts) {
    if (watts === null || watts === undefined) return "-";
    var abs = Math.abs(watts);
    if (abs < 1000) return Math.round(watts) + " W";
    return (watts / 1000).toFixed(2) + " kW";
  }

  function row(label, valueText) {
    var r = document.createElement("div");
    r.className = "row";
    var l = document.createElement("span");
    l.className = "row-label";
    l.textContent = label;
    var v = document.createElement("span");
    v.className = "row-value";
    v.textContent = valueText;
    r.appendChild(l);
    r.appendChild(v);
    return r;
  }

  function setBanner(message) {
    var banner = document.getElementById("banner");
    if (message) {
      document.getElementById("bannerText").textContent = message;
      banner.style.display = "flex";
    } else {
      banner.style.display = "none";
    }
  }

  function render(data) {
    document.getElementById("deviceName").textContent = data.deviceName || "Tesla Wall Connector";
    document.getElementById("lastUpdated").textContent = "Updated " + new Date().toLocaleTimeString();

    var s = data.states || {};
    var metaParts = [];
    if (s.firmwareVersion) metaParts.push("Firmware " + s.firmwareVersion);
    if (s.serialNumber) metaParts.push(s.serialNumber);
    if (s.uptime) metaParts.push("Up " + s.uptime);
    document.getElementById("wcMeta").textContent = metaParts.join(" · ");

    var statusDot = document.getElementById("statusDot");
    var statusValue = document.getElementById("statusValue");
    var statusSub = document.getElementById("statusSub");
    if (s.charging) {
      statusDot.className = "dot dot-charging";
      statusValue.textContent = "Charging";
    } else if (s.vehicleConnected) {
      statusDot.className = "dot dot-connected";
      statusValue.textContent = "Connected";
    } else {
      statusDot.className = "dot dot-idle";
      statusValue.textContent = "Not Connected";
    }
    statusSub.textContent = (s.currentAlerts && s.currentAlerts !== "None") ? ("⚠ " + s.currentAlerts) : "No active alerts";

    document.getElementById("powerValue").textContent = formatPower(s.power);
    document.getElementById("currentSub").textContent = s.vehicleCurrent !== undefined ? fmt(s.vehicleCurrent, 1, "A") : "-";

    document.getElementById("sessionEnergyValue").textContent = s.sessionEnergy !== undefined ? fmt(s.sessionEnergy, 2, "kWh") : "-";
    document.getElementById("sessionTimeSub").textContent = s.sessionTime ? ("Session " + s.sessionTime) : "-";

    var chargingDetail = document.getElementById("chargingDetail");
    chargingDetail.replaceChildren();
    chargingDetail.appendChild(row("Contactor", s.contactorClosed ? "Closed" : "Open"));
    chargingDetail.appendChild(row("Grid voltage", fmt(s.gridVoltage, 1, "V")));
    chargingDetail.appendChild(row("Grid frequency", fmt(s.gridFrequency, 2, "Hz")));
    chargingDetail.appendChild(row("Alerts", s.currentAlerts || "-"));
    chargingDetail.appendChild(row("Not-ready reasons", s.notReadyReasons || "-"));

    var deviceDetail = document.getElementById("deviceDetail");
    deviceDetail.replaceChildren();
    deviceDetail.appendChild(row("Handle temp", fmt(s.handleTemp, 1, "°C")));
    deviceDetail.appendChild(row("PCBA temp", fmt(s.pcbaTemp, 1, "°C")));
    deviceDetail.appendChild(row("MCU temp", fmt(s.mcuTemp, 1, "°C")));
    deviceDetail.appendChild(row("EVSE state code", s.evseState !== undefined ? String(s.evseState) : "-"));
    deviceDetail.appendChild(row("Firmware", s.firmwareVersion || "-"));
    deviceDetail.appendChild(row("Serial number", s.serialNumber || "-"));

    var lifetimeDetail = document.getElementById("lifetimeDetail");
    lifetimeDetail.replaceChildren();
    lifetimeDetail.appendChild(row("Lifetime energy", fmt(s.lifetimeEnergy, 2, "kWh")));
    lifetimeDetail.appendChild(row("Charge starts", s.chargeStarts !== undefined ? String(s.chargeStarts) : "-"));
    lifetimeDetail.appendChild(row("Contactor cycles", s.contactorCycles !== undefined ? String(s.contactorCycles) : "-"));
    lifetimeDetail.appendChild(row("Alert count", s.alertCount !== undefined ? String(s.alertCount) : "-"));
    lifetimeDetail.appendChild(row("Uptime", s.uptime || "-"));
    lifetimeDetail.appendChild(row("Wifi", s.wifiConnected ? ("Connected · " + fmt(s.wifiSignalStrength, 0, "")) : "Not connected"));
    lifetimeDetail.appendChild(row("Internet", s.internetConnected ? "Connected" : "Not connected"));

    if (data.errorState) {
      setBanner("Device reporting an error: " + data.errorState);
    } else {
      setBanner(null);
    }

    if (data.devices && data.devices.length > 1) {
      deviceSelect.style.display = "inline-block";
      if (deviceSelect.options.length !== data.devices.length) {
        deviceSelect.innerHTML = "";
        data.devices.forEach(function (d) {
          var opt = document.createElement("option");
          opt.value = d.id;
          opt.textContent = d.name;
          deviceSelect.appendChild(opt);
        });
      }
      deviceSelect.value = data.deviceId;
    }
    selectedDeviceId = data.deviceId;
  }

  function poll() {
    var url = "dashboard_data" + (selectedDeviceId ? ("?deviceId=" + selectedDeviceId) : "");
    fetch(url).then(function (res) { return res.json(); }).then(function (data) {
      if (!data.ok) {
        setBanner(data.error || "No Tesla Wall Connector device found");
        return;
      }
      render(data);
    }).catch(function () {
      setBanner("Could not reach the Tesla Wall Connector plugin");
    });
  }

  deviceSelect.addEventListener("change", function () {
    selectedDeviceId = deviceSelect.value;
    poll();
  });

  poll();
  setInterval(poll, POLL_MS);
})();
</script>
</body>
</html>
"""


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId: str, pluginDisplayName: str, pluginVersion: str, pluginPrefs: indigo.Dict) -> None:
        """Initialize the plugin instance and set the debug logging level.

        Args:
            pluginId (str): This plugin's bundle identifier.
            pluginDisplayName (str): The plugin's display name.
            pluginVersion (str): The plugin's version string.
            pluginPrefs (indigo.Dict): Saved plugin preferences.
        """
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = self.pluginPrefs.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self._next_poll_at: dict = {}
        self._next_slow_poll_at: dict = {}
        self._next_version_poll_at: dict = {}

    def startup(self) -> None:
        """Called once when the plugin starts."""
        self.logger.debug("startup called")

    def shutdown(self) -> None:
        """Called once when the plugin is shutting down."""
        self.logger.debug("shutdown called")

    def closedPrefsConfigUi(self, valuesDict: indigo.Dict, userCancelled: bool) -> None:
        """Re-apply the debug logging level live when plugin preferences are saved.

        Args:
            valuesDict (indigo.Dict): The dialog's saved field values.
            userCancelled (bool): True if the dialog was cancelled instead of saved.
        """
        if not userCancelled:
            self.debug = valuesDict.get("showDebugInfo", False)
            self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)

    def runConcurrentThread(self) -> None:
        """Indigo's polling loop entry point.

        Ticks every 5 seconds and polls each enabled Wall Connector device
        once its own configured pollInterval has elapsed, so devices can be
        polled at different rates - same pattern as the AlphaESS Modbus
        plugin's runConcurrentThread.
        """
        try:
            while True:
                now = time.time()
                for dev in indigo.devices.iter("self.wallConnector"):
                    if not dev.enabled or not dev.configured:
                        continue
                    if now < self._next_poll_at.get(dev.id, 0):
                        continue
                    try:
                        interval = max(MIN_POLL_INTERVAL, int(dev.pluginProps.get("pollInterval", DEFAULT_POLL_INTERVAL)))
                    except (TypeError, ValueError):
                        # validateDeviceConfigUi rejects bad values going forward, but a
                        # device saved before that check existed could still have one on
                        # disk - falling back here keeps that one device's bad value from
                        # taking down polling for every other configured Wall Connector.
                        interval = DEFAULT_POLL_INTERVAL
                    self._next_poll_at[dev.id] = now + interval
                    try:
                        self._poll_wall_connector(dev)
                    except Exception:
                        self.logger.exception(f"Error polling {dev.name}")
                self.sleep(5)
        except self.StopThread:
            pass

    def deviceStartComm(self, dev: indigo.Device) -> None:
        """Poll a Wall Connector device immediately once it's enabled/configured.

        Args:
            dev (indigo.Device): The device being started.
        """
        self.logger.debug(f"deviceStartComm: {dev.name} (type: {dev.deviceTypeId})")
        if dev.configured:
            # dev.configured is False for the moment between "New Device" and
            # Save being clicked (pluginProps are still empty then) - polling
            # during that window just produces a misleading "no IP" error.
            try:
                self._poll_wall_connector(dev)
            except Exception:
                self.logger.exception(f"Error polling {dev.name}")

    def deviceStopComm(self, dev: indigo.Device) -> None:
        """Clear this device's poll scheduling when it's disabled/deleted.

        Args:
            dev (indigo.Device): The device being stopped.
        """
        self.logger.debug(f"deviceStopComm: {dev.name} (type: {dev.deviceTypeId})")
        self._next_poll_at.pop(dev.id, None)
        self._next_slow_poll_at.pop(dev.id, None)
        self._next_version_poll_at.pop(dev.id, None)

    def validateDeviceConfigUi(self, valuesDict: indigo.Dict, typeId: str, devId: int) -> tuple:
        """Validate the New/Edit Device dialog before it's allowed to save.

        Args:
            valuesDict (indigo.Dict): The dialog's current field values.
            typeId (str): The device type being configured.
            devId (int): The device's ID (0 for a device being newly created).

        Returns:
            tuple: ``(True, valuesDict)`` if valid, or
                ``(False, valuesDict, errorsDict)`` with per-field error messages
                if not.
        """
        errors_dict = indigo.Dict()
        if typeId == "wallConnector":
            address = valuesDict.get("address", "").strip()
            if not address:
                errors_dict["address"] = "Wall Connector IP address is required."
            elif " " in address:
                errors_dict["address"] = "IP address must not contain spaces."
            poll_interval = valuesDict.get("pollInterval", "").strip()
            try:
                if int(poll_interval) < MIN_POLL_INTERVAL:
                    errors_dict["pollInterval"] = f"Poll interval must be at least {MIN_POLL_INTERVAL} seconds."
            except ValueError:
                errors_dict["pollInterval"] = "Poll interval must be a whole number of seconds."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def _get(self, address: str, endpoint: str) -> Optional[dict]:
        """Fetch and parse one of the Wall Connector's local API endpoints.

        Applies the same defensive JSON workarounds as
        einarhauks/tesla-wall-connector (see the module comment above): the
        Wall Connector occasionally emits a bare `nan` token instead of
        `null`, and occasionally truncates its response, missing the final
        `}` - both are repaired before parsing rather than treated as a
        fatal error.

        Args:
            address (str): The Wall Connector's local IP address or hostname.
            endpoint (str): One of "vitals", "lifetime", "wifi_status", "version".

        Returns:
            Optional[dict]: The parsed JSON response, or None on failure
                (already logged).
        """
        url = f"http://{address}/api/1/{endpoint}"
        self.logger.debug(f"GET {url}")
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            self.logger.warning(f"Could not reach Wall Connector at {address}: {e}")
            return None
        except Exception:
            self.logger.exception(f"Unexpected error fetching {endpoint} from Wall Connector at {address}")
            return None

        raw = NAN_RE.sub(":null ", raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if raw.rstrip()[-1:] != "}":
                try:
                    data = json.loads(raw + "}")
                except json.JSONDecodeError:
                    self.logger.warning(
                        f"Could not parse {endpoint} response from {address} - even after appending a closing brace"
                    )
                    return None
            else:
                self.logger.warning(f"Could not parse {endpoint} response from {address}")
                return None
        self.logger.debug(f"{endpoint} response from {address}: {data}")
        return data

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration(seconds) -> str:
        """Format a seconds count as "Xh Ym" (or just "Ym" under an hour).

        Args:
            seconds: Duration in seconds (int/float).

        Returns:
            str: Human-readable duration, or "" if seconds is missing/invalid.
        """
        try:
            total_minutes = int(seconds) // 60
        except (TypeError, ValueError):
            return ""
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours}h {minutes}m" if hours else f"{minutes}m"

    def _poll_wall_connector(self, dev: indigo.Device) -> None:
        """Poll one Wall Connector device: fetch, parse, and write its current state.

        Always fetches `vitals` (the live telemetry). `lifetime`/`wifi_status`
        and `version` are fetched on much slower cadences (SLOW_POLL_INTERVAL/
        VERSION_POLL_INTERVAL) since they change far less often and this API is
        documented to degrade under sustained polling load - see the module
        comment above.

        Args:
            dev (indigo.Device): The Wall Connector device to poll.
        """
        address = dev.pluginProps.get("address", "")
        if not address:
            self.logger.error(f"No IP address specified for {dev.name}")
            dev.setErrorStateOnServer("No IP address configured")
            return

        vitals = self._get(address, "vitals")
        if vitals is None:
            dev.setErrorStateOnServer("Could not reach Wall Connector")
            return

        try:
            split_phase = bool(dev.pluginProps.get("splitPhase", False))
            if split_phase:
                # North American split-phase wiring - grid_v already reflects
                # the combined line-to-line voltage with a single current
                # sensor, per einarhauks/tesla-wall-connector's total_power_w.
                power = round(vitals["grid_v"] * vitals["vehicle_current_a"], 1)
            else:
                # Single-phase and 3-phase installs both use this sum: an
                # unused phase (B/C on single-phase) simply reads 0V/0A and
                # contributes nothing.
                power = round(
                    (vitals["voltageA_v"] * vitals["currentA_a"])
                    + (vitals["voltageB_v"] * vitals["currentB_a"])
                    + (vitals["voltageC_v"] * vitals["currentC_a"]),
                    1,
                )
            contactor_closed = bool(vitals["contactor_closed"])
            vehicle_connected = bool(vitals["vehicle_connected"])
            alerts = vitals.get("current_alerts") or []
            not_ready = vitals.get("evse_not_ready_reasons") or []
            states = [
                {"key": "vehicleConnected", "value": vehicle_connected},
                # A vehicle can be connected without actively drawing current
                # (charge complete, paused, scheduled) - contactor_closed is
                # what actually indicates power is being delivered right now.
                {"key": "charging", "value": contactor_closed and vehicle_connected},
                {"key": "contactorClosed", "value": contactor_closed},
                {"key": "power", "value": int(power)},
                {"key": "vehicleCurrent", "value": vitals["vehicle_current_a"], "decimalPlaces": 1},
                {"key": "gridVoltage", "value": vitals["grid_v"], "decimalPlaces": 1},
                {"key": "gridFrequency", "value": vitals["grid_hz"], "decimalPlaces": 2},
                {"key": "sessionEnergy", "value": round(vitals["session_energy_wh"] / 1000, 2), "decimalPlaces": 2},
                {"key": "sessionTime", "value": self._format_duration(vitals["session_s"])},
                {"key": "handleTemp", "value": vitals["handle_temp_c"], "decimalPlaces": 1},
                {"key": "pcbaTemp", "value": vitals["pcba_temp_c"], "decimalPlaces": 1},
                {"key": "mcuTemp", "value": vitals["mcu_temp_c"], "decimalPlaces": 1},
                {"key": "currentAlerts", "value": ", ".join(alerts) if alerts else "None"},
                {"key": "notReadyReasons", "value": ", ".join(str(r) for r in not_ready) if not_ready else "None"},
                # evse_state's numeric meaning isn't documented anywhere
                # (Tesla, the reference library, and community threads all
                # leave it undecoded past a handful of observed values) -
                # exposed as a raw diagnostic value rather than guessed at,
                # same posture as this user's AlphaESS Modbus plugin leaving
                # its own undocumented battery_status register undecoded.
                {"key": "evseState", "value": int(vitals["evse_state"])},
            ]
        except (KeyError, TypeError) as e:
            self.logger.error(f"Unexpected vitals response shape from {dev.name} at {address}: {e}")
            dev.setErrorStateOnServer("Unexpected response from Wall Connector")
            return

        now = time.time()
        if now >= self._next_slow_poll_at.get(dev.id, 0):
            self._next_slow_poll_at[dev.id] = now + SLOW_POLL_INTERVAL
            states.extend(self._poll_slow(dev, address))
        if now >= self._next_version_poll_at.get(dev.id, 0):
            self._next_version_poll_at[dev.id] = now + VERSION_POLL_INTERVAL
            states.extend(self._poll_version(dev, address))

        dev.updateStatesOnServer(states)
        dev.setErrorStateOnServer(None)

    def _poll_slow(self, dev: indigo.Device, address: str) -> list:
        """Fetch lifetime + wifi_status - called on SLOW_POLL_INTERVAL, not every poll.

        Args:
            dev (indigo.Device): The Wall Connector device (for logging only).
            address (str): The Wall Connector's local IP address or hostname.

        Returns:
            list: State-update dicts to merge into the main poll's batch.
                Either fetch failing just logs a warning (via _get) and
                contributes no states for that endpoint - not treated as a
                poll-wide failure, since vitals (the data that matters most)
                already succeeded by the time this is called.
        """
        states = []
        lifetime = self._get(address, "lifetime")
        if lifetime is not None:
            try:
                states.extend([
                    {"key": "lifetimeEnergy", "value": round(lifetime["energy_wh"] / 1000, 2), "decimalPlaces": 2},
                    {"key": "chargeStarts", "value": int(lifetime["charge_starts"])},
                    {"key": "contactorCycles", "value": int(lifetime["contactor_cycles"])},
                    {"key": "alertCount", "value": int(lifetime["alert_count"])},
                    {"key": "uptime", "value": self._format_duration(lifetime["uptime_s"])},
                ])
            except (KeyError, TypeError) as e:
                self.logger.warning(f"Unexpected lifetime response shape from {dev.name} at {address}: {e}")

        wifi = self._get(address, "wifi_status")
        if wifi is not None:
            try:
                states.extend([
                    {"key": "wifiConnected", "value": bool(wifi["wifi_connected"])},
                    {"key": "internetConnected", "value": bool(wifi["internet"])},
                    {"key": "wifiSignalStrength", "value": int(wifi["wifi_signal_strength"])},
                ])
            except (KeyError, TypeError) as e:
                self.logger.warning(f"Unexpected wifi_status response shape from {dev.name} at {address}: {e}")
        return states

    def _poll_version(self, dev: indigo.Device, address: str) -> list:
        """Fetch firmware/serial info - called on VERSION_POLL_INTERVAL, essentially static.

        Args:
            dev (indigo.Device): The Wall Connector device (for logging only).
            address (str): The Wall Connector's local IP address or hostname.

        Returns:
            list: State-update dicts to merge into the main poll's batch, or
                an empty list if the fetch failed.
        """
        version = self._get(address, "version")
        if version is None:
            return []
        try:
            return [
                {"key": "firmwareVersion", "value": version["firmware_version"]},
                {"key": "serialNumber", "value": version["serial_number"]},
            ]
        except (KeyError, TypeError) as e:
            self.logger.warning(f"Unexpected version response shape from {dev.name} at {address}: {e}")
            return []

    # ------------------------------------------------------------------
    # Dashboard (HTTP responder)
    # ------------------------------------------------------------------

    # NOTE: dashboard/dashboard_data are reachable via Indigo's HTTP
    # responder (registered in Actions.xml with uiPath="hidden") at
    # http://<host>:8176/message/com.coolcaper.teslawallconnector/<id>.
    # Their signatures MUST stay untyped (no type hints, no return
    # annotation) - Indigo's HTTP-dispatch bridge fails on this specific
    # path with an opaque "RuntimeError: unable to convert python exception"
    # raised before the method body ever runs if the signature carries type
    # hints. Discovered and documented the hard way on this user's AlphaESS
    # Modbus plugin; every device-scoped Actions.xml callback elsewhere in
    # this codebase keeps its type hints fine, so it's specific to this
    # no-device HTTP-dispatch path.

    def dashboard(self, action, dev=None, caller_waiting_for_result=None):
        """Serve the live Tesla Wall Connector dashboard page.

        Reachable at ``http://<this-mac's-ip>:8176/message/<pluginId>/dashboard``
        via Indigo's built-in plugin HTTP responder. The page itself is static;
        it polls ``dashboard_data`` client-side for live values.

        Args:
            action (indigo.Dict): The inbound HTTP request wrapper Indigo provides.
            dev: Unused - required by Indigo's HTTP responder calling convention.
            caller_waiting_for_result: Unused - required by Indigo's HTTP responder calling convention.

        Returns:
            indigo.Dict: An HTTP reply dict (status/content/headers).
        """
        try:
            reply = indigo.Dict()
            reply["status"] = 200
            reply["content"] = DASHBOARD_HTML
            reply["headers"] = {"Content-Type": "text/html; charset=utf-8"}
            return reply
        except Exception:
            # Indigo's own exception-marshalling can itself fail ("unable to
            # convert python exception"), hiding the real cause - log it here
            # ourselves rather than relying on that bridge.
            self.logger.exception("Error serving dashboard")
            reply = indigo.Dict()
            reply["status"] = 500
            reply["content"] = "Internal error - see plugin log"
            reply["headers"] = {"Content-Type": "text/plain"}
            return reply

    def dashboard_data(self, action, dev=None, caller_waiting_for_result=None):
        """Serve the current Wall Connector states as JSON, for the dashboard page to poll.

        Args:
            action (indigo.Dict): The inbound HTTP request wrapper Indigo provides;
                its ``props["url_query_args"]`` may contain a ``deviceId`` to pick
                a specific Wall Connector when more than one is configured.
            dev: Unused - required by Indigo's HTTP responder calling convention.
            caller_waiting_for_result: Unused - required by Indigo's HTTP responder calling convention.

        Returns:
            indigo.Dict: An HTTP reply dict wrapping a JSON body.
        """
        try:
            props = dict(action.props) if action is not None else {}
            query = props.get("url_query_args", {}) or {}
            requested_id = query.get("deviceId")

            devices = list(indigo.devices.iter("self.wallConnector"))
            device_list = [{"id": d.id, "name": d.name} for d in devices]

            target = None
            if requested_id:
                target = next((d for d in devices if str(d.id) == str(requested_id)), None)
            if target is None:
                target = next((d for d in devices if d.enabled and d.configured), None)

            reply = indigo.Dict()
            reply["status"] = 200
            reply["headers"] = {"Content-Type": "application/json"}

            if target is None:
                reply["content"] = json.dumps({"ok": False, "error": "No configured Tesla Wall Connector device found", "devices": device_list})
                return reply

            payload = {
                "ok": True,
                "deviceId": target.id,
                "deviceName": target.name,
                "devices": device_list,
                "errorState": target.errorState or None,
                "states": {k: target.states.get(k) for k in DASHBOARD_STATE_KEYS},
            }
            reply["content"] = json.dumps(payload)
            return reply
        except Exception:
            self.logger.exception("Error serving dashboard_data")
            reply = indigo.Dict()
            reply["status"] = 500
            reply["content"] = json.dumps({"ok": False, "error": "Internal error - see plugin log"})
            reply["headers"] = {"Content-Type": "application/json"}
            return reply
