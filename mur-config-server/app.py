"""
Flask web application for managing ESP32 Murmura devices.
Uses device-manager scripts for efficient network scanning.
"""
import os
import json
import time
import threading
import logging
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import requests
from network_wrapper import NetworkConfig, DeviceScannerWrapper, DeviceRegistry

# ============================================================================
# SERVER CONFIGURATION
# ============================================================================
# Default port for the web server (chosen to avoid common port conflicts)
# Override by setting the MUR_CONFIG_SERVER_PORT environment variable
DEFAULT_PORT = 8765
SERVER_PORT = int(os.environ.get('MUR_CONFIG_SERVER_PORT', DEFAULT_PORT))

# The port Murs connect to on the Mur Gateway (its device port). Must match
# MUR_GATEWAY_DEFAULT_PORT in main/http_server.h. Note the gateway's status HTTP
# server is this + 1, which is what /api/triggers queries - a different thing.
MUR_GATEWAY_DEFAULT_PORT = 4000

# Tracks per device. Must match MAX_TRACKS in main/murmura.h.
MAX_TRACKS = 3
# ============================================================================

# Configure logging with detailed output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('murmura_server')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'murmura-server-2025'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Initialize components
network_config = NetworkConfig()
registry = DeviceRegistry()

# ---------------------------------------------------------------------------
# Per-device serialization
#
# The ESP32 has a tiny resource budget; opening a second TCP connection to a
# device while another is in flight causes drops and contention. Every code
# path that talks to device X must hold _device_locks[X]. Background probe
# uses non-blocking acquire (skip the slot if busy); user-initiated paths
# block briefly with a timeout.
# ---------------------------------------------------------------------------
_device_locks_meta_lock = threading.Lock()
_device_locks = {}

class DeviceBusy(Exception):
    pass

def _ensure_device_lock(device_id):
    lock = _device_locks.get(device_id)
    if lock is None:
        with _device_locks_meta_lock:
            lock = _device_locks.get(device_id)
            if lock is None:
                lock = threading.Lock()
                _device_locks[device_id] = lock
    return lock

@contextmanager
def device_lock(device_id, *, blocking=True, timeout=3.0):
    lock = _ensure_device_lock(device_id)
    if blocking:
        acquired = lock.acquire(timeout=timeout)
    else:
        acquired = lock.acquire(blocking=False)
    if not acquired:
        raise DeviceBusy(device_id)
    try:
        yield
    finally:
        lock.release()

# ---------------------------------------------------------------------------
# Device cache
#
# Last-known UI dict for each device plus freshness/health metadata. The cache
# is the single source of truth for GET /api/devices — failed probes never
# zero out card content, only freshness. Detail-page proxies and write
# actions also feed the cache on success so it stays current without extra
# probes.
# _device_cache[device_id] = {
#   'formatted': dict | None,         # last good UI dict (preserved across failures)
#   'last_ok_at': float (epoch),
#   'last_attempt_at': float (epoch),
#   'consecutive_failures': int,
#   'metadata_age_cycles': int,       # cycles since last /api/device fetch
# }
# ---------------------------------------------------------------------------
_device_cache_lock = threading.Lock()
_device_cache = {}

# Sliding window of recent probe outcomes for adaptive global cycle stretch.
_probe_outcomes = deque(maxlen=30)
_probe_outcomes_lock = threading.Lock()
_global_stretch_factor = 1
_global_stretch_low_streak = 0

# Tunables (some loaded from network_config; these are floors / behaviour).
OFFLINE_FAILURES_THRESHOLD = 5
DEVICE_BACKOFF_THRESHOLD = 3
DEVICE_BACKOFF_MAX_SEC = 60

# Background scanning thread
scan_thread = None
scan_active = False

# Probe loop
_probe_loop_thread = None
_probe_loop_active = False
_rr_index = 0

def background_scan():
    """Background thread for continuous scanning."""
    global scan_active
    while scan_active:
        logger.info("Starting background scan cycle")
        
        # Create scanner with progress callback
        def progress_callback(current, total, percent):
            socketio.emit('scan_progress', {
                'current': current,
                'total': total,
                'percent': percent
            })
        
        scanner = DeviceScannerWrapper(network_config, progress_callback)
        devices = scanner.scan_all_networks(progress_callback)
        
        # Update registry
        registry.load_registry()  # Reload from file updated by device_scanner
        
        # Send update to all connected clients
        socketio.emit('devices_update', {
            'devices': devices,
            'timestamp': time.time()
        })
        
        logger.info(f"Background scan complete, found {len(devices)} devices")
        
        # Wait before next scan
        time.sleep(30)

@app.route('/')
def index():
    """Main dashboard page."""
    return render_template('index.html', scene_server_url=_scene_server_url())

@app.route('/device/<device_id>')
def device_detail_page(device_id):
    """Individual device detail page - can be opened in separate tab."""
    # Get device info to pass to template
    device = registry.get_device(device_id)
    if not device:
        # Try to find by IP if not found by ID
        devices = registry.get_device_list()
        for d in devices:
            if d.get('ip_address') == device_id:
                device = d
                break
    
    if device:
        # Format device info
        device_info = {
            'id': device.get('id', device_id),
            'ip': device.get('ip_address', 'unknown'),
            'mac_address': device.get('mac_address', 'Unknown'),
            'ssid': 'Loading...',  # Will be fetched separately via WiFi status
            'status': 'online' if device.get('online', False) else 'offline',
            'uptime': device.get('uptime', 'Unknown'),
            'firmware_version': device.get('firmware_version', 'Unknown')
        }
        return render_template('device_detail.html', device=device_info)
    else:
        return render_template('device_detail.html', device=None, error="Device not found")

@app.route('/api/network/interfaces')
def get_interfaces():
    """Get available network interfaces."""
    interfaces = network_config.get_available_interfaces()
    logger.info(f"Available interfaces: {interfaces}")
    return jsonify({
        'interfaces': interfaces,
        'selected': network_config.config.get('selected_interfaces', []),
        'scan_all': network_config.config.get('scan_all', True)
    })

@app.route('/api/network/config', methods=['GET', 'POST'])
def network_configuration():
    """Get or set network configuration."""
    if request.method == 'GET':
        return jsonify(network_config.config)
    
    elif request.method == 'POST':
        data = request.json
        logger.info(f"Updating network config: {data}")
        
        if 'scan_all' in data:
            network_config.config['scan_all'] = data['scan_all']
        if 'selected_interfaces' in data:
            network_config.config['selected_interfaces'] = data['selected_interfaces']
        if 'selected_networks' in data:
            network_config.config['selected_networks'] = data['selected_networks']
        if 'timeout' in data:
            network_config.config['timeout'] = data['timeout']
        if 'concurrent_limit' in data:
            network_config.config['concurrent_limit'] = data['concurrent_limit']
        if 'probe_timeout' in data:
            network_config.config['probe_timeout'] = data['probe_timeout']
        if 'refresh_interval' in data:
            network_config.config['refresh_interval'] = data['refresh_interval']
        if 'mur_gateway_ip' in data:
            network_config.config['mur_gateway_ip'] = data['mur_gateway_ip']
        if 'mur_gateway_port' in data:
            network_config.config['mur_gateway_port'] = data['mur_gateway_port']
        if 'cycle_target_sec' in data:
            network_config.config['cycle_target_sec'] = float(data['cycle_target_sec'])
        if 'metadata_refetch_every' in data:
            network_config.config['metadata_refetch_every'] = max(1, int(data['metadata_refetch_every']))
        if 'stale_window_sec' in data:
            network_config.config['stale_window_sec'] = float(data['stale_window_sec'])

        network_config.save_config()

        return jsonify({'status': 'success', 'config': network_config.config})

def _fetch_scene(ip_address, probe_timeout):
    """GET /api/scenes. Returns the parsed JSON dict on success, None on failure."""
    try:
        response = requests.get(f"http://{ip_address}/api/scenes", timeout=probe_timeout)
        if response.status_code == 200:
            return response.json()
    except requests.RequestException:
        pass
    return None


def _fetch_metadata(ip_address, probe_timeout):
    """GET /api/device. Returns the parsed JSON dict on success, None on failure."""
    try:
        response = requests.get(f"http://{ip_address}/api/device", timeout=probe_timeout)
        if response.status_code == 200:
            return response.json()
    except requests.RequestException:
        pass
    return None


def _build_formatted(device, scene_data=None, metadata=None, prior=None):
    """Build the UI 'formatted' dict from whatever fresh data we have, falling
    back to `prior` (the previously cached formatted dict) for fields we
    didn't fetch this cycle. This is what preserves card detail through
    failed probes."""
    if prior is not None:
        base = dict(prior)
    else:
        base = {
            'id': device.get('id') or device.get('ip_address') or 'unknown',
            'ip': device.get('ip_address', 'unknown'),
            'status': 'online',
            'playing': False,
            'volume': 0,
            'ssid': device.get('wifi_ssid', 'Unknown'),
            'mac_address': device.get('mac_address', 'Unknown'),
            'firmware_version': device.get('firmware_version', 'Unknown'),
            'last_seen': device.get('last_seen', ''),
            'loops': [],
            'global_volume': 0,
            'active_loops': 0,
            'active_scene': '',
            'scenes': {},
            'mur_gateway_ip': '',
            'mur_gateway_port': 4000,
        }

    # Always keep the IP fresh from the registry (it can change after DHCP).
    base['ip'] = device.get('ip_address', base.get('ip', 'unknown'))

    if metadata is not None:
        base['firmware_version'] = metadata.get('firmware_version', base['firmware_version'])
        base['ssid'] = metadata.get('wifi_ssid', base.get('ssid', 'Unknown'))
        base['mur_gateway_ip'] = metadata.get('mur_gateway_ip', base.get('mur_gateway_ip', ''))
        base['mur_gateway_port'] = metadata.get('mur_gateway_port', base.get('mur_gateway_port', 4000))
        if metadata.get('mac_address'):
            base['mac_address'] = metadata['mac_address']
        if metadata.get('id'):
            base['id'] = metadata['id']

    if scene_data is not None:
        active_scene = scene_data.get('active_scene', '')
        scene = scene_data.get('scenes', {}).get(active_scene, {})
        base['active_scene'] = active_scene
        base['scenes'] = scene_data.get('scenes', {})
        base['global_volume'] = scene.get('global_volume', 0)
        base['volume'] = base['global_volume']

        loops = []
        active_count = 0
        for track in scene.get('tracks', []):
            fp = track.get('file_path', '') or track.get('file', '')
            loop_info = {
                'track': track.get('track', 0),
                'active': track.get('active', False),
                'mode': track.get('mode', 'loop'),
                'playing': track.get('playing', False),
                'volume': track.get('volume', 0),
                'file': fp,
                'filename': fp.split('/')[-1] if fp else 'No file',
                'trigger_name': track.get('trigger_name', ''),
                'trigger_type': track.get('trigger_type', 'On/Off'),
            }
            loops.append(loop_info)
            if loop_info['active']:
                active_count += 1
        base['loops'] = loops
        base['active_loops'] = active_count
        base['playing'] = active_count > 0

    return base


def _feed_cache(device_id, *, scene_data=None, metadata=None):
    """Update the device cache from a successful response on any code path.
    Called from the probe loop AND from detail-page proxies, batch ops, etc.
    so the cache stays fresh without burning extra probe slots."""
    device = registry.get_device(device_id)
    if not device:
        return
    with _device_cache_lock:
        entry = _device_cache.setdefault(device_id, {
            'formatted': None,
            'last_ok_at': 0.0,
            'last_attempt_at': 0.0,
            'consecutive_failures': 0,
            'metadata_age_cycles': 9999,
        })
        prior = entry.get('formatted')
        formatted = _build_formatted(device, scene_data=scene_data, metadata=metadata, prior=prior)
        entry['formatted'] = formatted
        entry['last_ok_at'] = time.time()
        entry['last_attempt_at'] = time.time()
        entry['consecutive_failures'] = 0
        if metadata is not None:
            entry['metadata_age_cycles'] = 0


def _record_cache_failure(device_id):
    """Bump consecutive_failures without zeroing cached card content."""
    with _device_cache_lock:
        entry = _device_cache.setdefault(device_id, {
            'formatted': None,
            'last_ok_at': 0.0,
            'last_attempt_at': 0.0,
            'consecutive_failures': 0,
            'metadata_age_cycles': 9999,
        })
        entry['last_attempt_at'] = time.time()
        entry['consecutive_failures'] += 1


def _record_probe_outcome(success):
    """Sliding-window failure rate drives the global cycle stretch.
    Doubles the effective cycle period when the AP is congested, halves it
    back as conditions recover."""
    global _global_stretch_factor, _global_stretch_low_streak
    with _probe_outcomes_lock:
        _probe_outcomes.append(1 if success else 0)
        if len(_probe_outcomes) >= 10:
            failure_rate = 1.0 - (sum(_probe_outcomes) / len(_probe_outcomes))
            if failure_rate >= 0.5 and _global_stretch_factor < 4:
                _global_stretch_factor *= 2
                _global_stretch_low_streak = 0
                logger.warning(
                    f"[CYCLE STRETCH] Failure rate {failure_rate:.0%}, "
                    f"stretching cycle x{_global_stretch_factor}"
                )
            elif failure_rate < 0.2:
                _global_stretch_low_streak += 1
                if _global_stretch_low_streak >= 2 and _global_stretch_factor > 1:
                    _global_stretch_factor //= 2
                    _global_stretch_low_streak = 0
                    logger.info(
                        f"[CYCLE STRETCH] Recovering, x{_global_stretch_factor}"
                    )
            else:
                _global_stretch_low_streak = 0


def _sleep_remaining(slot_start, slot):
    """Sleep so the next slot begins exactly `slot` seconds after this one
    started, regardless of how long the probe actually took. This is what
    spreads probes evenly across radio time."""
    elapsed = time.monotonic() - slot_start
    remaining = slot - elapsed
    if remaining > 0:
        time.sleep(remaining)


def _probe_loop():
    """Background thread: walks the registry round-robin with fixed-slot
    timing. One probe at a time, evenly spaced. See plan
    `hi-claude-look-around-imperative-lampson.md` for rationale."""
    global _rr_index
    logger.info("[PROBE LOOP] started")
    while _probe_loop_active:
        try:
            registry.load_registry()
            device_list = sorted(
                registry.get_device_list(),
                key=lambda d: d.get('id') or d.get('ip_address') or ''
            )
            n = len(device_list)
            if n == 0:
                time.sleep(2.0)
                continue

            cycle_target = float(network_config.config.get('cycle_target_sec', 30))
            slot = (cycle_target * _global_stretch_factor) / n
            probe_timeout = float(network_config.config.get('probe_timeout', 5))
            # Cap so a hung probe can't eat the next slot.
            effective_probe_timeout = max(0.5, min(probe_timeout, slot - 0.5))
            metadata_every = max(1, int(network_config.config.get('metadata_refetch_every', 10)))

            slot_start = time.monotonic()

            _rr_index = _rr_index % n
            device = device_list[_rr_index]
            _rr_index += 1
            device_id = device.get('id') or device.get('ip_address') or 'unknown'
            ip_address = device.get('ip_address')

            with _device_cache_lock:
                cache_entry = _device_cache.setdefault(device_id, {
                    'formatted': None,
                    'last_ok_at': 0.0,
                    'last_attempt_at': 0.0,
                    'consecutive_failures': 0,
                    'metadata_age_cycles': 9999,  # force first metadata fetch
                })
                cf = cache_entry['consecutive_failures']
                last_attempt_at = cache_entry['last_attempt_at']
                metadata_age = cache_entry['metadata_age_cycles']

            # Per-device backoff: a sick device's slot becomes radio-quiet.
            if cf >= DEVICE_BACKOFF_THRESHOLD:
                backoff = min(DEVICE_BACKOFF_MAX_SEC, 2 ** cf)
                if time.time() - last_attempt_at < backoff:
                    logger.info(
                        f"[SKIP backoff] {device_id} | failures={cf} | backoff={backoff}s"
                    )
                    _sleep_remaining(slot_start, slot)
                    continue

            if not ip_address:
                logger.debug(f"[SKIP no-ip] {device_id}")
                _sleep_remaining(slot_start, slot)
                continue

            # Strict per-device serialization. If something else is talking to
            # this device right now, skip our slot — we don't queue, we don't
            # delay the cycle.
            try:
                with device_lock(device_id, blocking=False):
                    fetch_metadata = metadata_age >= metadata_every
                    t0 = time.time()
                    logger.info(
                        f"[PROBE START] {device_id} @ {ip_address} | "
                        f"slot={slot:.2f}s | timeout={effective_probe_timeout:.2f}s | "
                        f"{'+meta' if fetch_metadata else 'scene-only'}"
                    )

                    scene_data = _fetch_scene(ip_address, effective_probe_timeout)
                    metadata = None
                    if scene_data is not None and fetch_metadata:
                        metadata = _fetch_metadata(ip_address, effective_probe_timeout)

                    elapsed = time.time() - t0

                    if scene_data is not None:
                        prior = cache_entry.get('formatted')
                        formatted = _build_formatted(
                            device, scene_data=scene_data, metadata=metadata, prior=prior
                        )
                        with _device_cache_lock:
                            cache_entry['formatted'] = formatted
                            cache_entry['last_ok_at'] = time.time()
                            cache_entry['last_attempt_at'] = time.time()
                            cache_entry['consecutive_failures'] = 0
                            if fetch_metadata and metadata is not None:
                                cache_entry['metadata_age_cycles'] = 0
                            else:
                                cache_entry['metadata_age_cycles'] += 1
                        # Persist mac/id changes to registry record.
                        identity_changed = False
                        if metadata:
                            if metadata.get('mac_address') and \
                                    metadata['mac_address'] != device.get('mac_address'):
                                device['mac_address'] = metadata['mac_address']
                                identity_changed = True
                            new_id = metadata.get('id')
                            if new_id and new_id != device.get('id'):
                                logger.warning(
                                    f"[ID CHANGE] {device.get('id')} -> {new_id} "
                                    f"@ {ip_address}"
                                )
                                device['id'] = new_id
                                identity_changed = True
                        device['online'] = True
                        registry.update_device(device)
                        # load_registry() is authoritative and runs every slot,
                        # so an identity change kept only in memory would be
                        # reverted seconds later and re-detected forever. Write
                        # it through. Deliberately not saved for the online flag
                        # alone, which churns constantly.
                        if identity_changed:
                            registry.save_registry()
                        logger.info(
                            f"[PROBE OK] {device_id} | {elapsed:.2f}s"
                        )
                        _record_probe_outcome(True)
                    else:
                        with _device_cache_lock:
                            cache_entry['last_attempt_at'] = time.time()
                            cache_entry['consecutive_failures'] += 1
                        device['online'] = False
                        registry.update_device(device)
                        logger.warning(
                            f"[PROBE FAIL] {device_id} | {elapsed:.2f}s | "
                            f"failures={cache_entry['consecutive_failures']}"
                        )
                        _record_probe_outcome(False)
            except DeviceBusy:
                logger.info(f"[SKIP busy] {device_id}")

            _sleep_remaining(slot_start, slot)
        except Exception:
            logger.exception("[PROBE LOOP] iteration crashed")
            time.sleep(1.0)
    logger.info("[PROBE LOOP] stopped")


def _start_probe_loop():
    global _probe_loop_thread, _probe_loop_active
    if _probe_loop_active:
        return
    _probe_loop_active = True
    _probe_loop_thread = threading.Thread(target=_probe_loop, name="probe_loop", daemon=True)
    _probe_loop_thread.start()


@app.route('/api/devices')
def get_devices():
    """Get all registered devices with detailed loop information.

    Reads ONLY from the in-memory cache populated by the background probe
    loop. No live HTTP fan-out per request — the response is instant. Failed
    probes never zero out card content; they only age the cache and adjust
    the status field. See plan
    `hi-claude-look-around-imperative-lampson.md` for rationale.
    """
    registry.load_registry()
    devices = registry.get_device_list()

    stale_window = float(network_config.config.get('stale_window_sec', 60))
    cycle_target = float(network_config.config.get('cycle_target_sec', 30))
    n = max(len(devices), 1)
    slot = (cycle_target * _global_stretch_factor) / n
    # "Online" means we got a fresh probe within roughly the last full cycle
    # (with 1.5x headroom so a single slow/skipped probe doesn't immediately
    # demote a healthy device to "stale"). Tied to the cycle, NOT the slot,
    # so the answer doesn't change as you add devices.
    online_threshold = max(1.5 * cycle_target * _global_stretch_factor, 30.0)

    now = time.time()
    formatted_devices = []
    online_count = 0
    stale_count = 0

    with _device_cache_lock:
        for device in devices:
            device_id = device.get('id') or device.get('ip_address') or 'unknown'
            cache_entry = _device_cache.get(device_id)

            if cache_entry is None or cache_entry.get('formatted') is None:
                # No probe has succeeded yet — return what we know from the
                # registry plus an explicit 'unknown' status.
                formatted = {
                    'id': device_id,
                    'ip': device.get('ip_address', 'unknown'),
                    'status': 'unknown',
                    'mac_address': device.get('mac_address', 'Unknown'),
                    'firmware_version': device.get('firmware_version', 'Unknown'),
                    'ssid': device.get('wifi_ssid', 'Unknown'),
                    'last_seen': device.get('last_seen', ''),
                    'loops': [],
                    'global_volume': 0,
                    'active_loops': 0,
                    'playing': False,
                    'data_age_sec': None,
                    'consecutive_failures': cache_entry['consecutive_failures'] if cache_entry else 0,
                    'source': 'registry',
                }
                formatted_devices.append(formatted)
                continue

            formatted = dict(cache_entry['formatted'])
            age = now - cache_entry['last_ok_at']
            cf = cache_entry['consecutive_failures']

            if age < online_threshold and cf == 0:
                formatted['status'] = 'online'
                formatted['source'] = 'fresh'
                online_count += 1
            elif age < stale_window and cf < OFFLINE_FAILURES_THRESHOLD:
                formatted['status'] = 'stale'
                formatted['source'] = 'cached'
                stale_count += 1
            else:
                formatted['status'] = 'offline'
                formatted['source'] = 'cached'

            formatted['data_age_sec'] = round(age, 1)
            formatted['consecutive_failures'] = cf
            formatted_devices.append(formatted)

    return jsonify({
        'devices': formatted_devices,
        'count': len(formatted_devices),
        'online': online_count + stale_count,  # cards still show as "up" if stale
        'fresh': online_count,
        'stale': stale_count,
        'registry_only': len(formatted_devices) - online_count - stale_count,
        'cycle_target_sec': cycle_target,
        'global_stretch_factor': _global_stretch_factor,
        'effective_slot_sec': round(slot, 2),
    })

@app.route('/api/scan', methods=['POST'])
def start_scan():
    """Start a network scan."""
    logger.info("=== Manual scan requested ===")
    
    def scan_with_progress():
        try:
            def progress_callback(current, total, percent):
                socketio.emit('scan_progress', {
                    'current': current,
                    'total': total,
                    'percent': percent
                })
                
                # Check if scan is complete
                if percent >= 100:
                    # Sleep briefly to ensure last progress update is processed
                    socketio.sleep(0.1)
            
            def network_callback(network, current, total):
                logger.debug(f"Scanning network {current}/{total}: {network}")
                socketio.emit('scanning_network', {
                    'network': network,
                    'current': current,
                    'total': total
                })
            
            scanner = DeviceScannerWrapper(network_config, progress_callback)
            merged_devices = scanner.scan_all_networks(progress_callback, network_callback)

            # Count by online field: True = actually found on network, False = stale registry
            scanned_count = sum(1 for d in merged_devices if d.get('online'))
            registry_count = sum(1 for d in merged_devices if not d.get('online'))

            # Reload registry
            registry.load_registry()

            socketio.emit('scan_complete', {
                'devices': merged_devices,
                'count': len(merged_devices),
                'scanned': scanned_count,
                'registry_only': registry_count,
                'status': 'success'
            })

            logger.info(f"Manual scan complete: {scanned_count} found on network, {registry_count} from registry only")
            
        except Exception as e:
            logger.error(f"Scan failed: {e}")
            socketio.emit('scan_error', {
                'error': str(e),
                'message': 'Network scan failed'
            })
    
    # Start scan in background thread
    thread = threading.Thread(target=scan_with_progress)
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'scanning', 'message': 'Network scan started'})

@app.route('/api/devices/clear', methods=['POST'])
def clear_all_devices():
    """Clear all devices from the registry."""
    logger.info("Clear all devices requested")
    
    try:
        scanner = DeviceScannerWrapper(network_config)
        success = scanner.clear_all_devices()

        if success:
            # Three separate stores hold device state; clearing only the file
            # leaves the other two populated and the stale cards stay on screen.
            removed = len(registry.get_all_devices())
            registry.clear()
            registry.load_registry()
            with _device_cache_lock:
                cached = len(_device_cache)
                _device_cache.clear()
            logger.info(f"Cleared {removed} registry entry(ies) and "
                        f"{cached} cache entry(ies)")

            return jsonify({
                'status': 'success',
                'message': f'All devices cleared ({removed} removed)',
                'removed': removed
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Failed to clear devices'
            }), 500
            
    except Exception as e:
        logger.error(f"Error clearing devices: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

def _format_uptime_seconds(uptime_seconds):
    if not uptime_seconds or uptime_seconds <= 0:
        return 'Just started'
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _status_from_cache(device_id):
    """Return ('online' | 'stale' | 'offline' | 'unknown', age_sec) from cache."""
    entry = _device_cache.get(device_id)
    if entry is None or entry.get('last_ok_at', 0.0) == 0.0:
        return 'unknown', None
    age = time.time() - entry['last_ok_at']
    cf = entry['consecutive_failures']
    stale_window = float(network_config.config.get('stale_window_sec', 60))
    if age < stale_window and cf < OFFLINE_FAILURES_THRESHOLD:
        return ('online' if cf == 0 else 'stale'), age
    return 'offline', age


@app.route('/api/device/<device_id>')
def get_device(device_id):
    """Get information about a specific device.

    Live-fetches /api/device under the per-device lock and feeds the cache.
    Falls back to cached status when the device is busy (probe in flight) or
    the live fetch fails — never returns a hard error to the detail page.
    """
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    ip_address = device.get('ip_address')
    probe_timeout = float(network_config.config.get('probe_timeout', 5))
    metadata = None
    from_cache = False

    try:
        with device_lock(device_id, blocking=True, timeout=3.0):
            if ip_address:
                logger.info(f"[detail] get_device {device_id} @ {ip_address}")
                metadata = _fetch_metadata(ip_address, probe_timeout)
                if metadata is not None:
                    if metadata.get('mac_address'):
                        device['mac_address'] = metadata['mac_address']
                    new_id = metadata.get('id')
                    if new_id and new_id != device.get('id'):
                        logger.warning(f"[ID CHANGE] {device.get('id')} -> {new_id} @ {ip_address}")
                        device['id'] = new_id
                    device.update({k: v for k, v in metadata.items() if k != 'id'})
                    device['online'] = True
                    registry.update_device(device)
                    _feed_cache(device.get('id', device_id), metadata=metadata)
                else:
                    _record_cache_failure(device.get('id', device_id))
                    device['online'] = False
                    registry.update_device(device)
    except DeviceBusy:
        from_cache = True
        logger.info(f"[BUSY] get_device {device_id} -> cache")

    live_device_id = device.get('id', device_id)
    if metadata is not None:
        status = 'online'
        uptime_str = _format_uptime_seconds(metadata.get('uptime_seconds', 0))
    else:
        cache_status, _age = _status_from_cache(live_device_id)
        if cache_status == 'unknown':
            cache_status, _age = _status_from_cache(device_id)
        # 'unknown' means we have no cache entry yet - after a reboot that
        # changed the device's ID, for instance, since the cache is keyed by ID.
        # Reporting that as 'offline' asserts something we have not observed and
        # makes a freshly-seen device look dead. Pass it through and let the
        # client hold its last known state.
        status = cache_status
        uptime_str = 'N/A'

    return jsonify({
        'id': live_device_id,
        'ip': ip_address or 'unknown',
        'status': status,
        'playing': device.get('playing', False),
        'volume': device.get('volume', 0),
        'mac_address': device.get('mac_address', 'Unknown'),
        'firmware_version': device.get('firmware_version', 'Unknown'),
        'last_seen': device.get('last_seen', ''),
        'uptime': uptime_str,
        # Included so the detail page does not need a second (and third) request
        # per poll for them. /api/device/<id>/mur-gateway remains for explicit
        # refreshes; it used to be fetched TWICE per 2 s tick, by loadMurGateway
        # and loadSceneTriggerConfig, which is most of why a booting device got
        # hammered into timing out.
        'mur_gateway_ip': device.get('mur_gateway_ip', ''),
        'mur_gateway_port': device.get('mur_gateway_port', 0),
        'scene_trigger_name': device.get('scene_trigger_name', ''),
        'from_cache': from_cache,
    })


@app.route('/api/device/<device_id>/volume', methods=['POST'])
def set_device_volume(device_id):
    """Set volume for a specific device (via POST /api/scenes with global_volume)."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    volume = (request.json or {}).get('volume', 50)
    ip_address = device.get('ip_address')

    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            logger.info(f"Setting volume to {volume} for device {device_id} via /api/scenes")
            scenes_resp = requests.get(f"http://{ip_address}/api/scenes", timeout=3)
            if scenes_resp.status_code != 200:
                return jsonify({'error': 'Failed to get scenes'}), 500
            active_scene = scenes_resp.json().get('active_scene', 'default')

            response = requests.post(
                f"http://{ip_address}/api/scenes",
                json={active_scene: {'global_volume': volume}},
                timeout=3
            )
            if response.status_code != 200:
                return jsonify({'error': 'Failed to set volume'}), 500

            device['volume'] = volume
            registry.update_device(device)
            # Re-fetch scenes once so the cache reflects the new volume.
            fresh = _fetch_scene(ip_address, 3)
            if fresh is not None:
                _feed_cache(device.get('id', device_id), scene_data=fresh)
            return jsonify({'status': 'success', 'volume': volume})
    except DeviceBusy:
        return jsonify({'error': 'device busy'}), 503
    except requests.RequestException as e:
        logger.error(f"Failed to set volume for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/play', methods=['POST'])
def control_playback(device_id):
    """Control playback on a device via scenes API."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    action = (request.json or {}).get('action', 'toggle')
    ip_address = device.get('ip_address')
    active = (action == 'play')

    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            logger.info(f"Sending {action} (active={active}) to all tracks on {device_id} @ {ip_address}")
            scenes_resp = requests.get(f"http://{ip_address}/api/scenes", timeout=3)
            if scenes_resp.status_code != 200:
                return jsonify({'error': 'Failed to get scenes'}), 500
            scenes_data = scenes_resp.json()
            active_scene = scenes_data.get('active_scene', 'default')
            scene = scenes_data.get('scenes', {}).get(active_scene, {})
            tracks = scene.get('tracks', [])

            track_list = [{'track': t.get('track', 0), 'active': active} for t in tracks]
            response = requests.post(
                f"http://{ip_address}/api/scenes",
                json={active_scene: {'tracks': track_list}},
                timeout=3
            )
            if response.status_code != 200:
                return jsonify({'error': f'Failed to {action} tracks'}), 500

            fresh = _fetch_scene(ip_address, 3)
            if fresh is not None:
                _feed_cache(device.get('id', device_id), scene_data=fresh)
            return jsonify({'status': 'success', 'action': action})
    except DeviceBusy:
        return jsonify({'error': 'device busy'}), 503
    except requests.RequestException as e:
        logger.error(f"Failed to control playback for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/files')
def get_device_files(device_id):
    """Get list of files on a device."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            response = requests.get(f"http://{device.get('ip_address')}/api/files", timeout=5)
            if response.status_code == 200:
                return jsonify(response.json())
            return jsonify({'error': 'Failed to get files'}), 500
    except DeviceBusy:
        return jsonify({'error': 'device busy', 'files': []}), 503
    except requests.RequestException as e:
        logger.error(f"Failed to get files for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/scenes')
def get_device_scenes(device_id):
    """Get scenes configuration for a device.

    Tries a live fetch under the per-device lock; on busy or failure, returns
    the cached scenes payload so the detail page never goes blank.
    """
    device = registry.get_device(device_id)
    if not device:
        logger.error(f"Device not found in registry: {device_id}")
        return jsonify({'active_scene': '', 'scenes': {}, 'from_cache': True})

    ip_address = device.get('ip_address')
    if not ip_address:
        logger.error(f"Device {device_id} has no IP address")
        return jsonify({'active_scene': '', 'scenes': {}, 'from_cache': True})

    try:
        with device_lock(device_id, blocking=True, timeout=3.0):
            response = requests.get(f"http://{ip_address}/api/scenes", timeout=3)
            if response.status_code == 200:
                scene_data = response.json()
                _feed_cache(device.get('id', device_id), scene_data=scene_data)
                payload = dict(scene_data)
                payload['from_cache'] = False
                return jsonify(payload)
            logger.warning(f"Failed to get scenes from {device_id}: HTTP {response.status_code}")
    except DeviceBusy:
        logger.info(f"[BUSY] get_device_scenes {device_id} -> cache")
    except requests.RequestException as e:
        logger.error(f"Failed to get scenes for {device_id}: {e}")
        _record_cache_failure(device.get('id', device_id))

    # Fallback to cached scenes.
    live_id = device.get('id', device_id)
    entry = _device_cache.get(live_id) or _device_cache.get(device_id)
    if entry and entry.get('formatted'):
        formatted = entry['formatted']
        return jsonify({
            'active_scene': formatted.get('active_scene', ''),
            'scenes': formatted.get('scenes', {}),
            'from_cache': True,
        })
    return jsonify({'active_scene': '', 'scenes': {}, 'from_cache': True})


@app.route('/api/device/<device_id>/scenes', methods=['POST'])
def set_device_scenes(device_id):
    """Patch scene configuration for a device (body keys = scene names)."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    data = request.json
    ip_address = device.get('ip_address')

    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            response = requests.post(f"http://{ip_address}/api/scenes", json=data, timeout=5)
            if response.status_code != 200:
                return jsonify({'error': 'Failed to update scenes'}), response.status_code
            body = response.json()
            # Refresh the cache from the post-write state.
            fresh = _fetch_scene(ip_address, 3)
            if fresh is not None:
                _feed_cache(device.get('id', device_id), scene_data=fresh)
            return jsonify(body)
    except DeviceBusy:
        return jsonify({'error': 'device busy'}), 503
    except requests.RequestException as e:
        logger.error(f"Failed to update scenes for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/scene', methods=['POST'])
def device_scene_action(device_id):
    """Scene management actions: create, delete, activate, set_default."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    data = request.json
    ip_address = device.get('ip_address')

    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            response = requests.post(f"http://{ip_address}/api/scene", json=data, timeout=5)
            if response.status_code != 200:
                return jsonify({'error': 'Scene action failed'}), response.status_code
            body = response.json()
            fresh = _fetch_scene(ip_address, 3)
            if fresh is not None:
                _feed_cache(device.get('id', device_id), scene_data=fresh)
            return jsonify(body)
    except DeviceBusy:
        return jsonify({'error': 'device busy'}), 503
    except requests.RequestException as e:
        logger.error(f"Failed scene action for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

def _batch_per_device(device_id, op):
    """Run `op(device, ip_address)` under the per-device lock for one batch item.
    `op` returns a dict to merge into the result. On lock contention or device
    not found, returns a sensible status dict."""
    device = registry.get_device(device_id)
    if not device:
        return {'device_id': device_id, 'status': 'not_found'}
    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            try:
                result = op(device, device.get('ip_address'))
                result.setdefault('device_id', device_id)
                return result
            except requests.RequestException as e:
                logger.error(f"Batch op error on {device_id}: {e}")
                return {'device_id': device_id, 'status': 'error'}
    except DeviceBusy:
        return {'device_id': device_id, 'status': 'busy'}


@app.route('/api/batch/volume', methods=['POST'])
def batch_set_volume():
    """Set global volume for multiple devices via scenes API."""
    data = request.json
    device_ids = data.get('device_ids', [])
    volume = data.get('volume', 50)
    logger.info(f"Batch setting global volume to {volume} for {len(device_ids)} devices")

    def op(device, ip_address):
        scenes_resp = requests.get(f"http://{ip_address}/api/scenes", timeout=3)
        if scenes_resp.status_code != 200:
            return {'status': 'failed'}
        active_scene = scenes_resp.json().get('active_scene', 'default')
        response = requests.post(
            f"http://{ip_address}/api/scenes",
            json={active_scene: {'global_volume': volume}},
            timeout=3
        )
        if response.status_code == 200:
            device['global_volume'] = volume
            device['volume'] = volume
            registry.update_device(device)
            fresh = _fetch_scene(ip_address, 3)
            if fresh is not None:
                _feed_cache(device.get('id', device.get('ip_address')), scene_data=fresh)
            return {'status': 'success'}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})


@app.route('/api/batch/scene/activate', methods=['POST'])
def batch_activate_scene():
    """Activate a scene across multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    scene_name = data.get('scene')
    if not scene_name:
        return jsonify({'error': 'Missing required field: scene'}), 400
    logger.info(f"Batch activating scene '{scene_name}' on {len(device_ids)} devices")

    def op(device, ip_address):
        response = requests.post(
            f"http://{ip_address}/api/scene",
            json={'action': 'activate', 'name': scene_name},
            timeout=3
        )
        if response.status_code == 200:
            resp_data = response.json()
            if resp_data.get('success'):
                fresh = _fetch_scene(ip_address, 3)
                if fresh is not None:
                    _feed_cache(device.get('id', device.get('ip_address')), scene_data=fresh)
                return {'status': 'success'}
            return {'status': 'failed', 'error': resp_data.get('error')}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})


@app.route('/api/batch/scene/create', methods=['POST'])
def batch_create_scene():
    """Create a scene across multiple devices (idempotent — 'already exists' counts as success)."""
    data = request.json
    device_ids = data.get('device_ids', [])
    scene_name = data.get('scene')
    if not scene_name:
        return jsonify({'error': 'Missing required field: scene'}), 400
    logger.info(f"Batch creating scene '{scene_name}' on {len(device_ids)} devices")

    def op(device, ip_address):
        response = requests.post(
            f"http://{ip_address}/api/scene",
            json={'action': 'create', 'name': scene_name},
            timeout=3
        )
        if response.status_code == 200:
            resp_data = response.json()
            if resp_data.get('success') or resp_data.get('error') == 'Scene already exists':
                return {'status': 'success'}
            return {'status': 'failed', 'error': resp_data.get('error')}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})


@app.route('/api/batch/scene-trigger', methods=['POST'])
def batch_set_scene_trigger():
    """Set scene trigger name on multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    scene_trigger_name = data.get('scene_trigger_name', '')
    logger.info(f"Batch setting scene trigger '{scene_trigger_name}' on {len(device_ids)} devices")

    def op(device, ip_address):
        response = requests.post(
            f"http://{ip_address}/api/device",
            json={'scene_trigger_name': scene_trigger_name},
            timeout=3
        )
        if response.status_code == 200:
            resp_data = response.json()
            if resp_data.get('success'):
                return {'status': 'success'}
            return {'status': 'failed', 'error': resp_data.get('error')}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})


@app.route('/api/batch/save-config', methods=['POST'])
def batch_save_config():
    """Save configuration on multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    logger.info(f"Batch saving configuration for {len(device_ids)} devices")

    def op(device, ip_address):
        response = requests.post(f"http://{ip_address}/api/config/save", timeout=5)
        if response.status_code == 200:
            logger.info(f"Configuration saved on {device.get('id', ip_address)}")
            return {'status': 'success'}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})


@app.route('/api/batch/reboot', methods=['POST'])
def batch_reboot():
    """Reboot multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    delay_ms = data.get('delay_ms', 1000)
    logger.info(f"Batch rebooting {len(device_ids)} devices with {delay_ms}ms delay")

    def op(device, ip_address):
        response = requests.post(
            f"http://{ip_address}/api/system/reboot",
            json={'delay_ms': delay_ms},
            timeout=3
        )
        if response.status_code == 200:
            logger.info(f"Reboot initiated on {device.get('id', ip_address)}")
            return {'status': 'success'}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})

# NOTE: /api/device/<id>/track/control and /api/device/<id>/track/volume
# have been removed. Use POST /api/device/<id>/scenes to patch track
# properties within a scene instead.

@app.route('/api/device/<device_id>/mur-gateway', methods=['GET'])
def get_device_mur_gateway(device_id):
    """Get Mur Gateway config for a device (via consolidated /api/device).

    Falls back to cached metadata when the device is busy or unreachable.
    """
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    ip_address = device.get('ip_address')

    try:
        with device_lock(device_id, blocking=True, timeout=3.0):
            response = requests.get(f"http://{ip_address}/api/device", timeout=3)
            if response.status_code == 200:
                data = response.json()
                _feed_cache(device.get('id', device_id), metadata=data)
                return jsonify({
                    'mur_gateway_ip': data.get('mur_gateway_ip', ''),
                    'mur_gateway_port': data.get('mur_gateway_port', 4000),
                    'scene_trigger_name': data.get('scene_trigger_name', ''),
                    'from_cache': False,
                })
    except DeviceBusy:
        logger.info(f"[BUSY] get_device_mur_gateway {device_id} -> cache")
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch mur-gateway for {device_id}: {e}")

    live_id = device.get('id', device_id)
    entry = _device_cache.get(live_id) or _device_cache.get(device_id)
    if entry and entry.get('formatted'):
        formatted = entry['formatted']
        return jsonify({
            'mur_gateway_ip': formatted.get('mur_gateway_ip', ''),
            'mur_gateway_port': formatted.get('mur_gateway_port', 4000),
            'scene_trigger_name': '',
            'from_cache': True,
        })
    return jsonify({'error': 'no data'}), 503


@app.route('/api/device/<device_id>/mur-gateway', methods=['POST'])
def set_device_mur_gateway(device_id):
    """Set Mur Gateway config for a device (via consolidated /api/device)."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    data = request.json or {}
    # Always send a port. `if data.get('mur_gateway_port')` dropped the key when
    # it was missing *or* explicitly 0 (0 is falsy), so the device kept whatever
    # it had. On a device whose gateway config failed to load that is 0, and the
    # firmware only updates the port when the key is present - producing a valid
    # IP on an unconnectable port, reported by nothing.
    raw_port = data.get('mur_gateway_port')
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        port = MUR_GATEWAY_DEFAULT_PORT
    if not 0 < port < 65536:
        return jsonify({'error': f'mur_gateway_port {raw_port!r} is not a valid port'}), 400
    payload = {
        'mur_gateway_ip': data.get('mur_gateway_ip', ''),
        'mur_gateway_port': port,
    }

    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            response = requests.post(
                f"http://{device.get('ip_address')}/api/device",
                json=payload, timeout=3
            )
            if response.status_code == 200:
                # Refresh metadata so the cache reflects the new gateway.
                fresh = _fetch_metadata(device.get('ip_address'), 3)
                if fresh is not None:
                    _feed_cache(device.get('id', device_id), metadata=fresh)
                return jsonify({'status': 'success'})
            return jsonify({'error': f'HTTP {response.status_code}'}), 500
    except DeviceBusy:
        return jsonify({'error': 'device busy'}), 503
    except requests.RequestException as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/device-config', methods=['POST'])
def set_device_config(device_id):
    """Proxy arbitrary fields to a device's POST /api/device endpoint."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            response = requests.post(
                f"http://{device.get('ip_address')}/api/device",
                json=request.json, timeout=3
            )
            body = response.json() if response.content else {}
            if response.status_code == 200:
                fresh = _fetch_metadata(device.get('ip_address'), 3)
                if fresh is not None:
                    _feed_cache(device.get('id', device_id), metadata=fresh)
            return jsonify(body), response.status_code
    except DeviceBusy:
        return jsonify({'error': 'device busy'}), 503
    except requests.RequestException as e:
        logger.error(f"Failed to set device config for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/triggers')
def get_trigger_list():
    """Fetch available trigger names via a device's Mur Gateway.

    The Mur Gateway status HTTP server runs on device_port + 1 (convention).
    """
    gateway_ip = request.args.get('gateway_ip', '').strip()
    gateway_port = int(request.args.get('gateway_port', '4000'))
    if not gateway_ip:
        return jsonify({'trigger_names': [], 'error': 'No gateway_ip provided'}), 200

    status_port = gateway_port + 1
    url = f"http://{gateway_ip}:{status_port}/triggers"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            logger.warning(f"Mur Gateway {gateway_ip} returned HTTP {response.status_code} for /triggers")
            return jsonify({'trigger_names': [], 'error': f'Gateway returned HTTP {response.status_code}'}), 200
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch triggers from {url}: {e}")
        return jsonify({'trigger_names': [], 'error': str(e)}), 200

@app.route('/api/batch/mur-gateway', methods=['POST'])
def batch_set_mur_gateway():
    """Set Mur Gateway IP/port on multiple devices (via consolidated /api/device)."""
    data = request.json
    device_ids = data.get('device_ids', [])
    mur_gateway_ip = data.get('mur_gateway_ip', '')
    raw_port = data.get('mur_gateway_port')

    # Same fix as the single-device route: always send a port, or a device whose
    # stored port is 0 stays unconnectable while looking configured.
    try:
        mur_gateway_port = int(raw_port)
    except (TypeError, ValueError):
        mur_gateway_port = MUR_GATEWAY_DEFAULT_PORT
    if not 0 < mur_gateway_port < 65536:
        return jsonify({'error': f'mur_gateway_port {raw_port!r} is not a valid port'}), 400

    payload = {'mur_gateway_ip': mur_gateway_ip,
               'mur_gateway_port': mur_gateway_port}
    logger.info(f"Batch setting mur gateway to {mur_gateway_ip}:{mur_gateway_port} "
                f"for {len(device_ids)} devices")

    def op(device, ip_address):
        response = requests.post(f"http://{ip_address}/api/device", json=payload, timeout=3)
        if response.status_code == 200:
            fresh = _fetch_metadata(ip_address, 3)
            if fresh is not None:
                _feed_cache(device.get('id', ip_address), metadata=fresh)
            return {'status': 'success'}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})


# NOTE: /api/device/<id>/track/trigger and /api/device/<id>/track/file
# have been removed. Use POST /api/device/<id>/scenes to patch track
# properties within a scene instead.

@app.route('/api/batch/device-volume', methods=['POST'])
def batch_set_device_volume():
    """Set per-device master volume on multiple devices (via /api/device)."""
    data = request.json
    device_ids = data.get('device_ids', [])
    device_volume = data.get('device_volume')

    if device_volume is None:
        return jsonify({'error': 'Missing required field: device_volume'}), 400
    try:
        device_volume = int(device_volume)
    except (TypeError, ValueError):
        return jsonify({'error': 'device_volume must be an integer 0-100'}), 400
    if device_volume < 0 or device_volume > 100:
        return jsonify({'error': 'device_volume must be between 0 and 100'}), 400

    logger.info(f"Batch setting device_volume to {device_volume} for {len(device_ids)} devices")

    def op(device, ip_address):
        response = requests.post(
            f"http://{ip_address}/api/device",
            json={'device_volume': device_volume},
            timeout=3
        )
        if response.status_code == 200:
            device['device_volume'] = device_volume
            registry.update_device(device)
            return {'status': 'success'}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})


@app.route('/api/batch/play', methods=['POST'])
def batch_control_playback():
    """Control playback for multiple devices via scenes API."""
    data = request.json
    device_ids = data.get('device_ids', [])
    action = data.get('action', 'play')
    active = (action in ('play', 'start'))
    logger.info(f"Batch {action} for {len(device_ids)} devices")

    def op(device, ip_address):
        scenes_resp = requests.get(f"http://{ip_address}/api/scenes", timeout=3)
        if scenes_resp.status_code != 200:
            return {'status': 'failed'}
        scenes_data = scenes_resp.json()
        active_scene = scenes_data.get('active_scene', 'default')
        scene = scenes_data.get('scenes', {}).get(active_scene, {})
        track_list = [{'track': t.get('track', 0), 'active': active} for t in scene.get('tracks', [])]
        response = requests.post(
            f"http://{ip_address}/api/scenes",
            json={active_scene: {'tracks': track_list}},
            timeout=3
        )
        if response.status_code == 200:
            fresh = _fetch_scene(ip_address, 3)
            if fresh is not None:
                _feed_cache(device.get('id', ip_address), scene_data=fresh)
            return {'status': 'success'}
        return {'status': 'failed'}

    return jsonify({'results': [_batch_per_device(d, op) for d in device_ids]})

# ===========================================================================
# Ensembles (mur-conductor)
#
# The conductor owns the ensemble timeline and playlists; this server owns the
# UI and all file operations. These routes proxy the conductor's status/admin
# API (so the browser needs no second origin) and implement the two things the
# conductor deliberately does not do: figuring out which files the members of a
# group have in common, and copying files between devices to fill the gaps.
# ===========================================================================

def _conductor_url():
    return (network_config.config.get('conductor_url')
            or 'http://127.0.0.1:4002').rstrip('/')


def _scene_server_url():
    """mur-scene-server base URL.

    The `or` fallback is load-bearing: NetworkConfig.load_config() returns the
    on-disk dict verbatim and does NOT merge the defaults, so an install whose
    network_config.json predates this key would otherwise get None here.
    """
    return (network_config.config.get('scene_server_url')
            or 'http://127.0.0.1:5003').rstrip('/')


@app.route('/ensembles')
def ensembles_page():
    """Ensemble group status, playlist editing, and file sync."""
    return render_template('ensembles.html')


@app.route('/api/conductor/status')
def conductor_status():
    """Proxy the conductor's /status.

    Deliberately does not touch _device_cache or any device lock - everything
    here comes from the conductor's own view (which it derives from the
    gateway), so it costs the devices nothing.
    """
    try:
        response = requests.get(f"{_conductor_url()}/status", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        return jsonify({'error': f'Conductor returned HTTP {response.status_code}',
                        'groups': []}), 200
    except requests.RequestException as e:
        logger.warning(f"Cannot reach conductor at {_conductor_url()}: {e}")
        return jsonify({'error': f'Cannot reach conductor: {e}', 'groups': []}), 200


@app.route('/api/scene-server/scenes')
def scene_server_scenes():
    """Proxy mur-scene-server's scene list.

    This is the fleet-wide list of scene NAMES and which one is active - the
    scene server owns that; per-track content stays on each device. Used by the
    device detail page to check a device's scene trigger against the real scene
    list instead of whatever stale range.values the trigger server advertises.

    Degrades to HTTP 200 with an empty list on any failure, matching the
    conductor routes: a list that cannot populate must never break the page.
    """
    try:
        response = requests.get(f"{_scene_server_url()}/api/scenes", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        return jsonify({'error': f'Scene server returned HTTP {response.status_code}',
                        'scenes': [], 'active_scene': None}), 200
    except requests.RequestException as e:
        logger.warning(f"Cannot reach scene server at {_scene_server_url()}: {e}")
        return jsonify({'error': f'Cannot reach scene server: {e}',
                        'scenes': [], 'active_scene': None}), 200


@app.route('/api/conductor/groups/<group_name>/playlist', methods=['POST'])
def conductor_set_playlist(group_name):
    """Proxy a playlist update to the conductor (applies at the next boundary)."""
    try:
        response = requests.post(
            f"{_conductor_url()}/api/groups/{group_name}/playlist",
            json=request.json or {}, timeout=5)
        return jsonify(response.json()), response.status_code
    except requests.RequestException as e:
        logger.error(f"Failed to set playlist for {group_name}: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/conductor/groups', methods=['POST'])
def conductor_groups_action():
    """Proxy a group create/delete to the conductor: {action, name, ...fields}.

    Action-verb POST rather than DELETE/PUT to match /api/device/<id>/scene and
    the rest of this server, which is GET/POST throughout.
    """
    try:
        response = requests.post(f"{_conductor_url()}/api/groups",
                                 json=request.json or {}, timeout=5)
        return jsonify(response.json()), response.status_code
    except requests.RequestException as e:
        logger.error(f"Failed group action: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/conductor/groups/<group_name>', methods=['POST'])
def conductor_set_group(group_name):
    """Proxy group settings to the conductor.

    Accepts any of name, enabled, trigger_name, scene_name, track,
    expected_device_ids, readiness_timeout_s, prep_lead_ms, loop_playlist. The
    conductor validates the whole prospective group before applying any of it,
    and reports whether a runner restart was needed.
    """
    try:
        response = requests.post(f"{_conductor_url()}/api/groups/{group_name}",
                                 json=request.json or {}, timeout=5)
        return jsonify(response.json()), response.status_code
    except requests.RequestException as e:
        logger.error(f"Failed to update group {group_name}: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/conductor/triggers')
def conductor_triggers():
    """Trigger names the conductor drives, for the group editor's datalist.

    Degrades to 200 with an empty list, like /api/triggers and
    /api/conductor/status: a datalist that cannot populate must never break the
    page. Note these are only the names of groups that already exist, so this
    cannot vet a brand-new group's trigger name - the reserved-name check and
    the per-device readiness check are what catch that.
    """
    try:
        response = requests.get(f"{_conductor_url()}/api/triggers", timeout=5)
        response.raise_for_status()
        return jsonify(response.json())
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Cannot reach conductor for trigger list: {e}")
        return jsonify({'triggers': [], 'error': str(e)}), 200


def _group_status(group_name):
    """One conductor group's full status dict. Raises RuntimeError.

    The readiness check needs scene_name/track/trigger_name as well as the
    member list, so both callers share a single /status fetch.
    """
    try:
        response = requests.get(f"{_conductor_url()}/status", timeout=5)
        response.raise_for_status()
        status = response.json()
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"Cannot reach conductor: {e}")
    for group in status.get('groups', []):
        if group.get('name') == group_name:
            return group
    raise RuntimeError(f"No such conductor group: {group_name}")


def _group_members(group_name):
    """[(device_id, ip)] for a conductor group, from the conductor's status."""
    return [(m['id'], m.get('ip', ''))
            for m in _group_status(group_name).get('members', [])]


def _parse_wav_header(blob, file_size):
    """Duration from a WAV header. Returns None if it isn't parseable WAV.

    Prefers the data chunk's declared size and falls back to the file size,
    because a file written by a streaming encoder can carry a zero or
    placeholder length.
    """
    if len(blob) < 12 or blob[0:4] != b'RIFF' or blob[8:12] != b'WAVE':
        return None
    pos = 12
    byte_rate = sample_rate = channels = bits = None
    data_size = None
    while pos + 8 <= len(blob):
        chunk_id = blob[pos:pos + 4]
        chunk_size = int.from_bytes(blob[pos + 4:pos + 8], 'little')
        if chunk_id == b'fmt ' and pos + 8 + 16 <= len(blob):
            fmt = blob[pos + 8:pos + 8 + 16]
            channels = int.from_bytes(fmt[2:4], 'little')
            sample_rate = int.from_bytes(fmt[4:8], 'little')
            byte_rate = int.from_bytes(fmt[8:12], 'little')
            bits = int.from_bytes(fmt[14:16], 'little')
        elif chunk_id == b'data':
            data_size = chunk_size
            if data_size in (0, 0xFFFFFFFF) or data_size > file_size:
                data_size = max(0, file_size - (pos + 8))
            break
        pos += 8 + chunk_size + (chunk_size & 1)
    if not byte_rate:
        return None
    if data_size is None:
        data_size = max(0, file_size - 44)
    return {
        'duration_ms': int(round(data_size * 1000.0 / byte_rate)),
        'sample_rate': sample_rate, 'channels': channels, 'bits': bits,
    }


def _probe_wav_duration(device_id, ip, filename, file_size, header_bytes=4096):
    """Read just the header of a file off a device and derive its duration.

    Uses GET /api/file/download and closes the connection after the first few
    KB, so probing a 15-minute file costs the same as probing a short one.
    """
    try:
        with device_lock(device_id, blocking=True, timeout=5.0):
            response = requests.get(f"http://{ip}/api/file/download",
                                    params={'filename': filename},
                                    stream=True, timeout=10)
            if response.status_code != 200:
                return None
            try:
                blob = next(response.iter_content(header_bytes), b'')
            finally:
                response.close()
        return _parse_wav_header(blob, file_size)
    except DeviceBusy:
        logger.info(f"[BUSY] wav probe {device_id} {filename}")
        return None
    except requests.RequestException as e:
        logger.warning(f"WAV probe failed for {device_id}:{filename}: {e}")
        return None


# ---------------------------------------------------------------------------
# Ensemble readiness
#
# What setup_ensemble.py --verify prints, as structured data the browser can
# act on. Ordered by DEPENDENCY, not by check order: a device publishes its
# trigger subscriptions from the tracks of its ACTIVE scene, and only tracks in
# trigger mode (send_subscribe in main/mur_listener.c), so a wrong active scene
# or a track left in loop mode is the *cause* of "not subscribed", not a
# separate problem. Fixing causes before symptoms is the whole point of the
# ordering - a checklist that lists them alphabetically sends the operator
# chasing the symptom.
# ---------------------------------------------------------------------------

def _readiness_problems(scenes, group, member):
    """Structured problems for one member. Ports setup_ensemble.py:verify_device."""
    scene_name = group.get('scene_name')
    track_idx = group.get('track')
    trigger_name = group.get('trigger_name')
    problems = []

    def add(code, order, severity, message, why, fix, actual=None, expected=None):
        problems.append({'code': code, 'order': order, 'severity': severity,
                         'message': message, 'why': why, 'fix': fix,
                         'actual': actual, 'expected': expected})

    all_scenes = scenes.get('scenes') or {}
    scene = all_scenes.get(scene_name)
    if scene is None:
        add('scene_missing', 10, 'error',
            f"Scene '{scene_name}' does not exist on this device.",
            "The conductor patches this scene to tell the device which file to "
            "play next. Without it, every file push is rejected.",
            f"On the device page, create a scene named '{scene_name}'.",
            actual=', '.join(sorted(all_scenes)) or '(none)', expected=scene_name)
        # Deliberately do NOT return here the way the CLI does - the operator
        # should see every problem in one pass, not one per round trip.
    else:
        tracks = {t.get('track'): t for t in scene.get('tracks', [])}
        t = tracks.get(track_idx)
        if t is None:
            add('track_absent', 20, 'error',
                f"Track {track_idx} is missing from scene '{scene_name}'.",
                "This is the track that carries the ensemble material.",
                f"On the device page, configure track {track_idx} of '{scene_name}'.")
        else:
            if t.get('mode') != 'trigger':
                add('track_mode_wrong', 20, 'error',
                    f"Track {track_idx} is in '{t.get('mode')}' mode, not 'trigger'.",
                    "A loop-mode track starts playing at boot, out of sync with "
                    "everyone else - and the device only tells the gateway about "
                    "triggers on trigger-mode tracks, so it will never be "
                    "subscribed either.",
                    f"On the device page, set track {track_idx} to Trig.",
                    actual=t.get('mode'), expected='trigger')
            if t.get('trigger_type') != 'OneShot':
                add('track_trigger_type_wrong', 20, 'error',
                    f"Track {track_idx} trigger type is '{t.get('trigger_type')}', "
                    "not 'OneShot'.",
                    "OneShot plays the file once per downbeat, which is what the "
                    "conductor sends. On/Off would need a stop event that the "
                    "gateway deliberately drops.",
                    f"On the device page, set track {track_idx}'s trigger type to OneShot.",
                    actual=t.get('trigger_type'), expected='OneShot')
            if t.get('trigger_name') != trigger_name:
                add('track_trigger_name_wrong', 20, 'error',
                    f"Track {track_idx} listens for '{t.get('trigger_name') or '(none)'}', "
                    f"not '{trigger_name}'.",
                    "The downbeat is a trigger event with this group's name. A "
                    "track listening for anything else never hears it.",
                    f"On the device page, set track {track_idx}'s trigger name to "
                    f"'{trigger_name}'.",
                    actual=t.get('trigger_name') or '', expected=trigger_name)
            if not t.get('active'):
                add('track_inactive', 20, 'error',
                    f"Track {track_idx} is not enabled.",
                    "A disabled track ignores triggers entirely.",
                    f"On the device page, enable track {track_idx}.",
                    actual='disabled', expected='enabled')
            # No check on file_path. An empty one is the *preferred* resting
            # state for a conductor-driven track: the firmware ignores a
            # matching trigger when no file is set, so the track is guaranteed
            # silent until the conductor pushes the entry to play. A stale
            # filename is what's mildly undesirable - it means a failed file
            # push still plays something on the next downbeat - and the
            # conductor overwrites it before every downbeat regardless.

        if scenes.get('active_scene') != scene_name:
            add('active_scene_wrong', 30, 'error',
                f"The active scene is '{scenes.get('active_scene')}', not '{scene_name}'.",
                "A device publishes its trigger subscriptions from the ACTIVE "
                "scene's tracks only. While another scene is active this device "
                "is not subscribed to the downbeat, whatever the ensemble scene "
                "says.",
                f"On the device page, click Activate on the '{scene_name}' scene.",
                actual=scenes.get('active_scene'), expected=scene_name)
        if scenes.get('default_scene') != scene_name:
            add('default_scene_wrong', 40, 'error',
                f"The boot default scene is '{scenes.get('default_scene')}', "
                f"not '{scene_name}'.",
                "After a reboot the device lands on the default scene. If that "
                "is not the ensemble scene, a rebooted device drops out of the "
                "ensemble silently.",
                f"On the device page, click Set Default on the '{scene_name}' scene.",
                actual=scenes.get('default_scene'), expected=scene_name)
        if scene.get('synchronized'):
            add('synchronized_true', 50, 'error',
                f"Scene '{scene_name}' has synchronized=true.",
                "That flag gates scene *activation*, which this design never "
                "does after boot. On a default scene it makes the firmware log "
                "an error every boot and blocks the gateway's get_scene "
                "backstop from restoring the scene.",
                "On the device page, untick Synchronized on this scene.",
                actual='true', expected='false')

    if member.get('present') and not member.get('subscribed'):
        add('not_subscribed', 60, 'error',
            f"The gateway does not have this device subscribed to '{trigger_name}'.",
            "Subscriptions are published by the device from its active scene, so "
            "this is a consequence of the problems above rather than something "
            "to fix directly.",
            "Fix the problems above; the device re-subscribes as soon as a scene "
            "patch is accepted, and the conductor notices within about 10 seconds.")

    # No "remember to save" row. It cannot be verified remotely, so it appeared
    # on every member on every check regardless of truth - and Configure always
    # saves, so the one path that needs it already handles it.

    problems.sort(key=lambda p: p['order'])
    return problems


@app.route('/api/ensemble/<group_name>/readiness')
def ensemble_readiness(group_name):
    """Per-member ensemble setup check, on demand (it contacts each device)."""
    try:
        group = _group_status(group_name)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 502

    results = []
    for member in group.get('members', []):
        device_id = member.get('id')
        ip = member.get('ip') or ''
        in_registry = registry.get_device(device_id) is not None
        entry = {
            'id': device_id,
            'ip': ip,
            'at_gateway': bool(member.get('present')),
            'subscribed': bool(member.get('subscribed')),
            'in_registry': in_registry,
            # /device/<id> resolves through the registry, so a device the
            # conductor knows but this server has never scanned would 404.
            'device_page': (f"/device/{device_id}?scene={group.get('scene_name')}"
                            f"&track={group.get('track')}") if in_registry else None,
            'reachable': False,
            'problems': [],
        }
        if not in_registry:
            entry['problems'].append({
                'code': 'not_in_registry', 'order': 5, 'severity': 'warn',
                'message': 'This server has never scanned this device.',
                'why': 'It is at the gateway, so the ensemble can still use it, '
                       'but there is no device page to link to.',
                'fix': 'Run a network scan from the dashboard.',
                'actual': None, 'expected': None,
            })
        if not ip:
            entry['problems'].append({
                'code': 'not_at_gateway', 'order': 5, 'severity': 'error',
                'message': 'Not connected to the gateway.',
                'why': 'Downbeats reach devices through their outbound gateway '
                       'connection. Without it this device cannot play.',
                'fix': 'Check the device is powered, on the network, and pointed '
                       'at the right mur_gateway_ip.',
                'actual': None, 'expected': None,
            })
            results.append(entry)
            continue
        try:
            with device_lock(device_id, blocking=True, timeout=3.0):
                response = requests.get(f"http://{ip}/api/scenes", timeout=5)
            if response.status_code != 200:
                raise requests.RequestException(f"HTTP {response.status_code}")
            scenes = response.json()
            entry['reachable'] = True
            _feed_cache(device_id, scene_data=scenes)
            entry['problems'].extend(_readiness_problems(scenes, group, member))
        except DeviceBusy:
            entry['problems'].append({
                'code': 'device_busy', 'order': 5, 'severity': 'warn',
                'message': 'Device was busy; setup not checked.',
                'why': 'Another request to this device was in flight. This is '
                       'transient, not a misconfiguration.',
                'fix': 'Check again in a moment.',
                'actual': None, 'expected': None,
            })
        except (requests.RequestException, ValueError) as e:
            entry['problems'].append({
                'code': 'unreachable', 'order': 5, 'severity': 'error',
                'message': f'Could not read scenes from this device: {e}',
                'why': 'The device is at the gateway but its HTTP server did not '
                       'answer, so its setup cannot be verified.',
                'fix': 'Check the device is responsive.',
                'actual': None, 'expected': None,
            })
        entry['problems'].sort(key=lambda p: p['order'])
        entry['ok'] = not any(p['severity'] == 'error' for p in entry['problems'])
        results.append(entry)

    for entry in results:
        entry.setdefault('ok', False)
    return jsonify({
        'group': group_name,
        'expected': {
            'scene_name': group.get('scene_name'),
            'track': group.get('track'),
            'trigger_name': group.get('trigger_name'),
            'playlist_length': group.get('playlist_length'),
        },
        'checked_at': datetime.now().isoformat(),
        'member_count': len(results),
        'ready_count': sum(1 for e in results if e['ok']),
        'members': results,
    })


def _configure_member(device_id, ip, group, problems, force=False):
    """Apply only the setup steps this member is actually missing.

    Mirrors setup_ensemble.py's five steps, but driven by the readiness check so
    a device that is 90% correct gets one patch instead of the whole sequence.
    Order matters: the scene must exist before it can be patched, and activation
    is what makes the device publish its trigger subscriptions.

    file_path is cleared to "". The conductor rewrites it before every downbeat,
    so anything left here is only ever the file that plays when a file push has
    FAILED - and the firmware ignores a trigger on a track with no file
    (mur_listener.c), so an empty one means such a device stays silent rather than
    playing stale material. Silence is the property this design exists to
    guarantee, so provisioning should leave the track armed and empty.

    On a group that is already running this can cost that member a single entry:
    if the clear lands between a prep and its downbeat, that downbeat is ignored
    and the device rejoins at the next one.
    """
    scene = group.get('scene_name')
    track = group.get('track')
    trigger = group.get('trigger_name')
    codes = {p['code'] for p in problems}
    if force:
        # Re-apply what can be redone harmlessly, even when the check says it is
        # already right. Two steps are deliberately excluded:
        #
        #   scene_missing  - create is wrong to repeat; it is in `codes` already
        #                    if the scene genuinely is not there.
        #   active_scene_wrong - scene_activate has no already-active early
        #                    return, so it always runs config_apply and can cut a
        #                    track that is currently playing. Re-activating an
        #                    already-active scene achieves nothing and risks an
        #                    audible blip on a live member, so only do it when the
        #                    real check says the active scene is wrong.
        codes |= {'track_mode_wrong', 'default_scene_wrong'}
    applied, failed = [], []

    def post(path, payload, label, tolerate=None):
        try:
            r = requests.post(f"http://{ip}{path}", json=payload, timeout=5)
            body = r.json() if r.content else {}
            # The firmware answers a rejected patch with HTTP 200 + success:false,
            # so the status code alone is not an outcome.
            if r.status_code != 200 or body.get('success') is False:
                err = str(body.get('error') or f'HTTP {r.status_code}')
                # e.g. creating a scene that already exists is a no-op, not a
                # failure - same tolerance setup_ensemble.py applies.
                if tolerate and tolerate in err.lower():
                    applied.append(f'{label} (already done)')
                    return True
                failed.append({'step': label, 'error': err})
                return False
            applied.append(label)
            return True
        except (requests.RequestException, ValueError) as e:
            failed.append({'step': label, 'error': str(e)})
            return False

    if 'scene_missing' in codes:
        if not post('/api/scene', {'action': 'create', 'name': scene}, 'create scene',
                    tolerate='exist'):
            return applied, failed          # nothing else can succeed

    # One patch covers every track problem plus synchronized. `active: true` is
    # load-bearing: it routes scene_apply_patch into the trigger-mode branch,
    # which never restarts a playing track. See AGENTS.md.
    track_codes = {'scene_missing', 'track_absent', 'track_mode_wrong',
                   'track_trigger_type_wrong', 'track_trigger_name_wrong',
                   'track_inactive'}
    if codes & track_codes or 'synchronized_true' in codes:
        # active:true keeps this on the trigger-mode branch of scene_apply_patch,
        # which never restarts a playing track - required, and it is also what
        # makes clearing file_path safe here. See AGENTS.md.
        tracks = [{'track': track, 'mode': 'trigger', 'trigger_type': 'OneShot',
                   'trigger_name': trigger, 'active': True, 'file_path': ''}]
        if 'scene_missing' in codes:
            # We just created this scene, so its other tracks hold whatever the
            # firmware defaults to (loop mode, track1/2/3.wav). An ensemble scene
            # must be silent apart from the conducted track, or those defaults
            # start playing the moment it is activated. Only assert this on
            # creation - on an existing scene another track may be doing
            # something deliberate, and clobbering it would be worse.
            for other in range(MAX_TRACKS):
                if other != track:
                    tracks.append({'track': other, 'active': False})
        patch = {scene: {'tracks': tracks}}
        if 'synchronized_true' in codes or 'scene_missing' in codes:
            patch[scene]['synchronized'] = False
        post('/api/scenes', patch, 'configure track')

    if 'default_scene_wrong' in codes or 'scene_missing' in codes:
        post('/api/scene', {'action': 'set_default', 'name': scene}, 'set boot default')

    if 'active_scene_wrong' in codes or 'scene_missing' in codes:
        post('/api/scene', {'action': 'activate', 'name': scene}, 'activate scene')

    # Always save: nothing above survives a reboot otherwise, and it cannot be
    # verified remotely so the checklist can never tell us it is already done.
    if applied or force:
        post('/api/config/save', {}, 'save to SD')
    return applied, failed


@app.route('/api/ensemble/<group_name>/configure', methods=['POST'])
def ensemble_configure(group_name):
    """Provision one member (or all) for this group, fixing only what is wrong.

    Body: {"device_id": "30"} or {"device_ids": [...]}, or {} for every member.
    """
    try:
        group = _group_status(group_name)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 502

    data = request.json or {}
    force = bool(data.get('force'))
    wanted = data.get('device_ids')
    if data.get('device_id'):
        wanted = [data['device_id']]

    results = []
    for member in group.get('members', []):
        device_id, ip = member.get('id'), member.get('ip') or ''
        if wanted is not None and device_id not in wanted:
            continue
        entry = {'id': device_id, 'applied': [], 'failed': [], 'ok': False}
        if not ip:
            entry['failed'].append({'step': 'reach device',
                                    'error': 'not connected to the gateway'})
            results.append(entry)
            continue
        try:
            with device_lock(device_id, blocking=True, timeout=5.0):
                resp = requests.get(f"http://{ip}/api/scenes", timeout=5)
                if resp.status_code != 200:
                    raise requests.RequestException(f'HTTP {resp.status_code}')
                problems = _readiness_problems(resp.json(), group, member)
                entry['applied'], entry['failed'] = _configure_member(
                    device_id, ip, group, problems, force=force)
                # Re-read so the answer reflects the device, not our intentions.
                after = requests.get(f"http://{ip}/api/scenes", timeout=5)
                if after.status_code == 200:
                    scenes_after = after.json()
                    _feed_cache(device_id, scene_data=scenes_after)
                    remaining = [p for p in _readiness_problems(scenes_after, group, member)
                                 if p['severity'] == 'error'
                                 and p['code'] != 'not_subscribed']
                    entry['remaining'] = remaining
                    entry['ok'] = not remaining and not entry['failed']
        except DeviceBusy:
            entry['failed'].append({'step': 'acquire device', 'error': 'device busy'})
        except (requests.RequestException, ValueError) as e:
            entry['failed'].append({'step': 'read scenes', 'error': str(e)})
        results.append(entry)

    if not results:
        return jsonify({'error': 'no matching members in this group'}), 200
    return jsonify({
        'group': group_name,
        'configured': sum(1 for r in results if r['ok']),
        'total': len(results),
        # not_subscribed is excluded from `remaining` above: the gateway's view is
        # a 10s poll, so it is still stale right after a successful fix.
        'note': 'the Status column can lag ~10s behind this result',
        'members': results,
    })


@app.route('/api/ensemble/<group_name>/probe')
def ensemble_probe_duration(group_name):
    """Duration of one file, read from its WAV header on the first member that has it.

    Exists so the playlist editor can fill duration_ms in automatically. The
    full /files route needs a fan-out to every member; this is one 4 KB read
    (plus one /api/files call if the caller cannot supply the size), which is
    cheap enough to run on every file selection.

    Always 200: a failed probe must leave the editor usable, not error it out.
    """
    filename = (request.args.get('file') or '').strip().replace('/sdcard/', '')
    if not filename:
        return jsonify({'error': 'file is required'}), 200
    # The caller usually has the size already from a loaded inventory; it is a
    # sanity bound on the header's declared data size, not trusted input.
    try:
        size_hint = int(request.args.get('size') or 0)
    except ValueError:
        size_hint = 0

    try:
        members = _group_members(group_name)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 200

    tried = []
    for device_id, ip in members:
        if not ip:
            continue
        size = size_hint
        if size <= 0:
            try:
                with device_lock(device_id, blocking=True, timeout=5.0):
                    resp = requests.get(f"http://{ip}/api/files", timeout=5)
                if resp.status_code == 200:
                    match = next((f for f in resp.json().get('files', [])
                                  if f.get('name') == filename), None)
                    size = int(match.get('size', 0)) if match else 0
            except (DeviceBusy, requests.RequestException, ValueError):
                size = 0
        if size <= 0:
            tried.append(f'{device_id}: not found')
            continue
        info = _probe_wav_duration(device_id, ip, filename, size)
        if info:
            info = dict(info, file=filename, source=device_id, size=size)
            return jsonify(info)
        tried.append(f'{device_id}: header unreadable')

    return jsonify({'error': f"Could not read a duration for {filename}"
                             + (f" ({'; '.join(tried)})" if tried else '')}), 200


@app.route('/api/ensemble/<group_name>/files')
def ensemble_files(group_name):
    """File inventory across a group, for the playlist picker and sync matrix.

    Every member must hold a file for the group to be able to play it, so
    `common` (present on all reachable members, same size) is what the playlist
    picker offers. `differs` means same name but a different size on some
    member: reported, never auto-resolved, because per-device stems under a
    shared filename are a legitimate setup.
    """
    probe = request.args.get('probe', '').strip()
    try:
        members = _group_members(group_name)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 502

    reachable, unreachable = [], []
    per_device = {}
    for device_id, ip in members:
        if not ip:
            unreachable.append({'id': device_id, 'reason': 'not connected to gateway'})
            continue
        try:
            with device_lock(device_id, blocking=True, timeout=5.0):
                response = requests.get(f"http://{ip}/api/files", timeout=5)
            if response.status_code != 200:
                unreachable.append({'id': device_id, 'reason': f'HTTP {response.status_code}'})
                continue
            files = response.json().get('files', [])
            per_device[device_id] = {f['name']: f.get('size', 0) for f in files}
            reachable.append((device_id, ip))
        except DeviceBusy:
            unreachable.append({'id': device_id, 'reason': 'device busy'})
        except (requests.RequestException, ValueError) as e:
            unreachable.append({'id': device_id, 'reason': str(e)})

    all_names = sorted({name for names in per_device.values() for name in names})
    matrix, common = [], []
    for name in all_names:
        sizes = {d: per_device[d].get(name) for d, _ in reachable}
        present = [d for d, size in sizes.items() if size is not None]
        absent = [d for d, size in sizes.items() if size is None]
        distinct = {size for size in sizes.values() if size is not None}
        state = ('common' if not absent and len(distinct) == 1
                 else 'differs' if not absent
                 else 'partial')
        if state == 'common':
            common.append(name)
        matrix.append({
            'name': name, 'state': state,
            'sizes': sizes, 'present_on': present, 'absent_on': absent,
            'size': min(distinct) if distinct else 0,
        })

    result = {
        'group': group_name,
        'members': [d for d, _ in reachable],
        'unreachable': unreachable,
        'common': common,
        'files': matrix,
    }

    # Duration probing is opt-in because it costs one short HTTP read per file.
    if probe:
        wanted = [n.strip() for n in probe.split(',') if n.strip()]
        durations = {}
        for name in wanted:
            entry = next((m for m in matrix if m['name'] == name), None)
            if not entry or not entry['present_on']:
                continue
            source_id = entry['present_on'][0]
            source_ip = dict(reachable).get(source_id, '')
            info = _probe_wav_duration(source_id, source_ip, name,
                                       entry['sizes'].get(source_id) or 0)
            if info:
                durations[name] = info
        result['durations'] = durations

    return jsonify(result)


# --- File sync: one transfer at a time, deliberately slow -------------------
#
# Copies run on a single worker thread under the normal per-device locks, so a
# transfer never overlaps another operation on the same device. Throttled
# because saturating an ESP32's HTTP server starves everything else it does.

SYNC_RATE_BYTES_PER_SEC = 200 * 1024
SYNC_CHUNK = 16 * 1024

_sync_lock = threading.Lock()
_sync_queue = deque()
_sync_state = {'running': False, 'current': None, 'done': [], 'queued': 0}
_sync_thread = None


def _throttled(iterable, rate=SYNC_RATE_BYTES_PER_SEC):
    """Yield chunks no faster than `rate` bytes/sec."""
    start = time.time()
    sent = 0
    for chunk in iterable:
        yield chunk
        sent += len(chunk)
        target = sent / float(rate)
        drift = target - (time.time() - start)
        if drift > 0:
            time.sleep(drift)


def _sync_worker():
    global _sync_thread
    while True:
        with _sync_lock:
            if not _sync_queue:
                _sync_state['running'] = False
                _sync_state['current'] = None
                _sync_thread = None
                return
            job = _sync_queue.popleft()
            _sync_state['current'] = dict(job, status='copying')
            _sync_state['queued'] = len(_sync_queue)

        result = dict(job)
        tmp_path = None
        try:
            tmp_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'mur_config_server', f".sync-{job['filename']}.tmp")
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)

            with device_lock(job['src_id'], blocking=True, timeout=15.0):
                response = requests.get(f"http://{job['src_ip']}/api/file/download",
                                        params={'filename': job['filename']},
                                        stream=True, timeout=300)
                response.raise_for_status()
                written = 0
                with open(tmp_path, 'wb') as out:
                    for chunk in _throttled(response.iter_content(SYNC_CHUNK)):
                        out.write(chunk)
                        written += len(chunk)
                response.close()

            with device_lock(job['dst_id'], blocking=True, timeout=15.0):
                with open(tmp_path, 'rb') as src:
                    upload = requests.post(
                        f"http://{job['dst_ip']}/api/upload",
                        params={'filename': job['filename']},
                        data=_throttled(iter(lambda: src.read(SYNC_CHUNK), b'')),
                        headers={'Content-Type': 'application/octet-stream'},
                        timeout=300)
                upload.raise_for_status()

            result.update(status='ok', bytes=written)
            logger.info(f"[SYNC] {job['filename']}: {job['src_id']} -> "
                        f"{job['dst_id']} ({written} bytes)")
        except DeviceBusy as e:
            result.update(status='failed', error=f'device busy: {e}')
            logger.warning(f"[SYNC] busy: {job['filename']} -> {job['dst_id']}")
        except (requests.RequestException, OSError, ValueError) as e:
            result.update(status='failed', error=str(e))
            logger.error(f"[SYNC] failed {job['filename']} -> {job['dst_id']}: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        with _sync_lock:
            _sync_state['done'].append(result)
            _sync_state['current'] = None


@app.route('/api/ensemble/<group_name>/sync', methods=['POST'])
def ensemble_sync(group_name):
    """Queue copies so every member of the group holds the named files.

    Body: {"filenames": ["a.wav", ...]}  (omit to sync everything that is
    missing somewhere). Files whose size merely differs between devices are
    never touched - that case needs a human decision.
    """
    global _sync_thread
    data = request.json or {}
    requested = data.get('filenames')

    inventory = ensemble_files(group_name)
    if isinstance(inventory, tuple):
        return inventory
    inventory = inventory.get_json()
    if inventory.get('error'):
        return jsonify(inventory), 502

    try:
        members = dict((d, ip) for d, ip in _group_members(group_name))
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 502

    queued = []
    for entry in inventory['files']:
        if entry['state'] != 'partial':
            continue
        if requested is not None and entry['name'] not in requested:
            continue
        source_id = entry['present_on'][0]
        for dst_id in entry['absent_on']:
            if not members.get(source_id) or not members.get(dst_id):
                continue
            queued.append({
                'filename': entry['name'],
                'src_id': source_id, 'src_ip': members[source_id],
                'dst_id': dst_id, 'dst_ip': members[dst_id],
                'size': entry['size'],
            })

    if not queued:
        return jsonify({'queued': 0, 'message': 'nothing to copy'})

    with _sync_lock:
        _sync_queue.extend(queued)
        _sync_state['queued'] = len(_sync_queue)
        _sync_state['running'] = True
        if _sync_thread is None or not _sync_thread.is_alive():
            _sync_thread = threading.Thread(target=_sync_worker, daemon=True)
            _sync_thread.start()

    total_mb = sum(j['size'] for j in queued) / (1024.0 * 1024.0)
    logger.info(f"[SYNC] queued {len(queued)} copy job(s), {total_mb:.1f} MB")
    return jsonify({
        'queued': len(queued),
        'jobs': [{'filename': j['filename'], 'src': j['src_id'], 'dst': j['dst_id']}
                 for j in queued],
        'estimated_seconds': int(total_mb * 1024 * 1024 / SYNC_RATE_BYTES_PER_SEC),
    })


@app.route('/api/ensemble/sync-status')
def ensemble_sync_status():
    with _sync_lock:
        return jsonify({
            'running': _sync_state['running'],
            'current': _sync_state['current'],
            'queued': len(_sync_queue),
            'done': list(_sync_state['done'])[-25:],
            'rate_kb_per_sec': SYNC_RATE_BYTES_PER_SEC // 1024,
        })


@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {'message': 'Connected to Murmura Device Manager'})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.info(f"Client disconnected: {request.sid}")

@socketio.on('request_scan')
def handle_scan_request():
    """Handle scan request from client."""
    logger.info("WebSocket scan request received")
    start_scan()

@socketio.on('start_auto_scan')
def handle_auto_scan_start():
    """Start automatic scanning."""
    global scan_thread, scan_active
    if not scan_active:
        scan_active = True
        scan_thread = threading.Thread(target=background_scan)
        scan_thread.daemon = True
        scan_thread.start()
        logger.info("Auto-scan started")
        emit('auto_scan_started', {'status': 'started'})

@socketio.on('stop_auto_scan')
def handle_auto_scan_stop():
    """Stop automatic scanning."""
    global scan_active
    scan_active = False
    logger.info("Auto-scan stopped")
    emit('auto_scan_stopped', {'status': 'stopped'})

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Starting Device Manager Server")
    logger.info("Using device-manager scripts for efficient scanning")
    logger.info("=" * 60)
    
    # Check if running in production mode
    debug_mode = os.environ.get('FLASK_ENV') != 'production'
    
    if debug_mode:
        logger.info("Running in DEBUG mode")
    else:
        logger.info("Running in PRODUCTION mode")
    
    logger.info(f"Access the web interface at: http://localhost:{SERVER_PORT}")
    logger.info(f"Or from network: http://<your-ip>:{SERVER_PORT}")
    logger.info("=" * 60)

    # Start the probe loop only once, even with the Werkzeug reloader running
    # in debug mode (which would otherwise spawn two threads talking to the
    # same devices in parallel — exactly the bug we're trying to avoid).
    if not debug_mode or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        _start_probe_loop()

    socketio.run(app, host='0.0.0.0', port=SERVER_PORT, debug=debug_mode, allow_unsafe_werkzeug=True)
