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
