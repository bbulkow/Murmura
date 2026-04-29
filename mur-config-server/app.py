"""
Flask web application for managing ESP32 Murmura devices.
Uses device-manager scripts for efficient network scanning.
"""
import os
import json
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
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

# Probe grace window state (in-memory, ephemeral across server restarts).
# Keyed by the same device_id the registry uses (id || ip_address). A single
# missed probe does not flip the UI to offline if the device answered
# successfully within GRACE_WINDOW_SEC and we have fewer than
# GRACE_MAX_FAILURES misses in a row. Hides single-probe blips on a marginal
# Wi-Fi link without delaying real outage detection.
_probe_state = {}
_probe_state_lock = threading.Lock()
GRACE_WINDOW_SEC = 30
GRACE_MAX_FAILURES = 2

# Background scanning thread
scan_thread = None
scan_active = False

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
    return render_template('index.html')

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

        network_config.save_config()
        
        return jsonify({'status': 'success', 'config': network_config.config})

def _probe_one_device(device, probe_timeout):
    """Probe a single device's /api/device and (if reachable) /api/scenes.

    Returns (formatted, device, is_actually_online) where formatted is the
    UI dict, device is the (possibly mutated) registry record, and
    is_actually_online is the raw network outcome before any grace window.
    """
    formatted = {
        'id': device.get('id', device.get('ip_address', 'unknown')),
        'ip': device.get('ip_address', 'unknown'),
        'status': 'online' if device.get('online', False) else 'offline',
        'playing': False,
        'volume': 0,
        'ssid': device.get('wifi_ssid', 'Unknown'),
        'mac_address': device.get('mac_address', 'Unknown'),
        'firmware_version': device.get('firmware_version', 'Unknown'),
        'last_seen': device.get('last_seen', ''),
        'loops': [],
        'global_volume': 0,
        'active_loops': 0,
    }

    ip_address = device.get('ip_address')
    is_actually_online = False

    logger.info(f"[PROBE START] Device: {formatted['id']} | IP: {ip_address} | Timeout: {probe_timeout}s")
    probe_start_time = time.time()

    try:
        status_response = requests.get(f"http://{ip_address}/api/device", timeout=probe_timeout)
        probe_elapsed = time.time() - probe_start_time

        if status_response.status_code == 200:
            is_actually_online = True
            status_data = status_response.json()

            mac_address = status_data.get('mac_address')
            if mac_address:
                device['mac_address'] = mac_address
                formatted['mac_address'] = mac_address
                logger.debug(f"MAC Address confirmed: {mac_address}")
            else:
                logger.error(f"WARNING: No MAC address returned from {ip_address}/api/device!")

            new_id = status_data.get('id')
            if new_id and new_id != device.get('id'):
                logger.warning(f"[ID CHANGE] Device ID changed from {device.get('id')} to {new_id} at IP {ip_address} (MAC: {mac_address})")
                device['id'] = new_id
                formatted['id'] = new_id
            else:
                formatted['id'] = status_data.get('id', formatted['id'])

            formatted['firmware_version'] = status_data.get('firmware_version', formatted['firmware_version'])
            formatted['ssid'] = status_data.get('wifi_ssid', device.get('wifi_ssid', 'Unknown'))
            formatted['mur_gateway_ip'] = status_data.get('mur_gateway_ip', '')
            formatted['mur_gateway_port'] = status_data.get('mur_gateway_port', 4000)

            logger.info(f"[PROBE SUCCESS] Device: {formatted['id']} | MAC: {mac_address} | Response time: {probe_elapsed:.2f}s | Status: ONLINE")
        else:
            logger.warning(f"[PROBE FAILED] Device: {formatted['id']} | HTTP {status_response.status_code} | Response time: {probe_elapsed:.2f}s")
    except requests.Timeout:
        probe_elapsed = time.time() - probe_start_time
        logger.warning(f"[PROBE TIMEOUT] Device: {formatted['id']} | Timeout after {probe_elapsed:.2f}s | Status: OFFLINE")
    except requests.ConnectionError as e:
        probe_elapsed = time.time() - probe_start_time
        logger.warning(f"[PROBE CONNECTION ERROR] Device: {formatted['id']} | Error: {str(e)[:100]} | Time: {probe_elapsed:.2f}s | Status: OFFLINE")
    except requests.RequestException as e:
        probe_elapsed = time.time() - probe_start_time
        logger.error(f"[PROBE ERROR] Device: {formatted['id']} | Error: {str(e)[:100]} | Time: {probe_elapsed:.2f}s | Status: OFFLINE")

    if is_actually_online:
        try:
            response = requests.get(f"http://{ip_address}/api/scenes", timeout=probe_timeout)

            if response.status_code == 200:
                scenes_data = response.json()

                active_scene = scenes_data.get('active_scene', '')
                scene_data = scenes_data.get('scenes', {}).get(active_scene, {})

                formatted['global_volume'] = scene_data.get('global_volume', 0)
                formatted['volume'] = formatted['global_volume']
                formatted['active_scene'] = active_scene
                formatted['scenes'] = scenes_data.get('scenes', {})

                loops = []
                active_count = 0
                for track in scene_data.get('tracks', []):
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

                formatted['loops'] = loops
                formatted['active_loops'] = active_count
                formatted['playing'] = active_count > 0

                logger.debug(f"Device {formatted['id']}: active_scene={active_scene}, {active_count} active tracks, global vol: {formatted['global_volume']}")

        except requests.RequestException as e:
            logger.debug(f"Could not get scene status for {formatted['id']}: {e}")

    return formatted, device, is_actually_online


@app.route('/api/devices')
def get_devices():
    """Get all registered devices with detailed loop information."""
    registry.load_registry()
    devices = registry.get_device_list()
    probe_timeout = network_config.config.get('probe_timeout', 3)

    # Probe devices in parallel; each worker does /api/device + /api/scenes.
    # Bounded pool so the request doesn't fan out unboundedly as registry grows.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe") as pool:
        results = list(pool.map(lambda d: _probe_one_device(d, probe_timeout), devices))

    formatted_devices = []
    online_count = 0
    now = time.time()

    for formatted, device, is_actually_online in results:
        device_id = formatted['id']

        with _probe_state_lock:
            state = _probe_state.setdefault(device_id, {'last_ok_at': 0.0, 'consecutive_failures': 0})
            if is_actually_online:
                state['last_ok_at'] = now
                state['consecutive_failures'] = 0
                display_online = True
                grace = False
                age = 0.0
                failures = 0
            else:
                state['consecutive_failures'] += 1
                age = now - state['last_ok_at']
                failures = state['consecutive_failures']
                display_online = age < GRACE_WINDOW_SEC and failures < GRACE_MAX_FAILURES
                grace = display_online

        if grace:
            logger.info(f"[GRACE] Device: {device_id} | Last OK: {age:.1f}s ago | Failures: {failures} | Status: ONLINE (grace)")

        formatted['status'] = 'online' if display_online else 'offline'
        formatted['source'] = 'scanned' if is_actually_online else 'registry'

        if not is_actually_online:
            device['online'] = False
        registry.update_device(device)

        if display_online:
            online_count += 1

        formatted_devices.append(formatted)

    registry_only = len(formatted_devices) - online_count
    logger.info(f"Returning {len(formatted_devices)} devices ({online_count} online, {registry_only} from registry only)")

    return jsonify({
        'devices': formatted_devices,
        'count': len(formatted_devices),
        'online': online_count,
        'registry_only': registry_only
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
            # Reload empty registry
            registry.load_registry()
            
            return jsonify({
                'status': 'success',
                'message': 'All devices cleared'
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

@app.route('/api/device/<device_id>')
def get_device(device_id):
    """Get information about a specific device."""
    device = registry.get_device(device_id)
    if device:
        uptime_str = 'Unknown'
        ssid = device.get('wifi_ssid', 'Unknown')  # Default from registry
        probe_timeout = network_config.config.get('probe_timeout', 3)
        is_actually_online = False

        try:
            logger.info(f"Getting status for device {device_id} at {device.get('ip_address')}")
            response = requests.get(f"http://{device.get('ip_address')}/api/device", timeout=probe_timeout)
            if response.status_code == 200:
                is_actually_online = True
                data = response.json()
                device.update(data)
                device['online'] = True

                logger.debug(f"[DEVICE STATUS] Device {device_id} status: {data}")

                uptime_seconds = data.get('uptime_seconds', 0)
                if uptime_seconds > 0:
                    days = uptime_seconds // 86400
                    hours = (uptime_seconds % 86400) // 3600
                    minutes = (uptime_seconds % 3600) // 60

                    if days > 0:
                        uptime_str = f"{days}d {hours}h {minutes}m"
                    elif hours > 0:
                        uptime_str = f"{hours}h {minutes}m"
                    else:
                        uptime_str = f"{minutes}m"
                else:
                    uptime_str = 'Just started'

                registry.update_device(device)
        except requests.RequestException as e:
            logger.warning(f"Failed to get status for {device_id}: {e}")
            device['online'] = False
            uptime_str = 'N/A'

        # Apply the same grace window used by /api/devices so a single missed
        # probe during a detail-page refresh doesn't flip a healthy device
        # to offline. Uses the live device id (which may have been refreshed
        # from the probe response) for consistency with get_devices().
        live_device_id = device.get('id', device_id)
        now = time.time()
        with _probe_state_lock:
            state = _probe_state.setdefault(live_device_id, {'last_ok_at': 0.0, 'consecutive_failures': 0})
            if is_actually_online:
                state['last_ok_at'] = now
                state['consecutive_failures'] = 0
                display_online = True
            else:
                state['consecutive_failures'] += 1
                age = now - state['last_ok_at']
                failures = state['consecutive_failures']
                display_online = age < GRACE_WINDOW_SEC and failures < GRACE_MAX_FAILURES
                if display_online:
                    logger.info(f"[GRACE] Device: {live_device_id} | Last OK: {age:.1f}s ago | Failures: {failures} | Status: ONLINE (grace)")

        formatted = {
            'id': live_device_id,
            'ip': device.get('ip_address', 'unknown'),
            'status': 'online' if display_online else 'offline',
            'playing': device.get('playing', False),
            'volume': device.get('volume', 0),
            'mac_address': device.get('mac_address', 'Unknown'),
            'firmware_version': device.get('firmware_version', 'Unknown'),
            'last_seen': device.get('last_seen', ''),
            'uptime': uptime_str
        }

        return jsonify(formatted)
    else:
        return jsonify({'error': 'Device not found'}), 404

@app.route('/api/device/<device_id>/volume', methods=['POST'])
def set_device_volume(device_id):
    """Set volume for a specific device (via POST /api/scenes with global_volume)."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    data = request.json
    volume = data.get('volume', 50)

    try:
        logger.info(f"Setting volume to {volume} for device {device_id} via /api/scenes")
        # First get the active scene name
        scenes_resp = requests.get(f"http://{device.get('ip_address')}/api/scenes", timeout=2)
        if scenes_resp.status_code != 200:
            return jsonify({'error': 'Failed to get scenes'}), 500
        active_scene = scenes_resp.json().get('active_scene', 'default')

        # Patch the active scene with new global_volume
        response = requests.post(
            f"http://{device.get('ip_address')}/api/scenes",
            json={active_scene: {'global_volume': volume}},
            timeout=2
        )
        if response.status_code == 200:
            device['volume'] = volume
            registry.update_device(device)
            return jsonify({'status': 'success', 'volume': volume})
        else:
            return jsonify({'error': 'Failed to set volume'}), 500
    except requests.RequestException as e:
        logger.error(f"Failed to set volume for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/play', methods=['POST'])
def control_playback(device_id):
    """Control playback on a device via scenes API."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    data = request.json
    action = data.get('action', 'toggle')

    try:
        ip_address = device.get('ip_address')
        active = (action == 'play')
        logger.info(f"Sending {action} (active={active}) to all tracks on device {device_id} at {ip_address}")

        # Get the active scene name
        scenes_resp = requests.get(f"http://{ip_address}/api/scenes", timeout=2)
        if scenes_resp.status_code != 200:
            return jsonify({'error': 'Failed to get scenes'}), 500
        scenes_data = scenes_resp.json()
        active_scene = scenes_data.get('active_scene', 'default')
        scene = scenes_data.get('scenes', {}).get(active_scene, {})
        tracks = scene.get('tracks', [])

        # Build a patch for the active scene setting all tracks active/inactive
        track_list = []
        for track in tracks:
            track_list.append({'track': track.get('track', 0), 'active': active})

        # Patch scene via POST /api/scenes
        response = requests.post(
            f"http://{ip_address}/api/scenes",
            json={active_scene: {'tracks': track_list}},
            timeout=2
        )

        if response.status_code == 200:
            return jsonify({'status': 'success', 'action': action})
        else:
            return jsonify({'error': f'Failed to {action} tracks'}), 500
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
        response = requests.get(f"http://{device.get('ip_address')}/api/files", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': 'Failed to get files'}), 500
    except requests.RequestException as e:
        logger.error(f"Failed to get files for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/scenes')
def get_device_scenes(device_id):
    """Get scenes configuration for a device."""
    device = registry.get_device(device_id)
    if not device:
        logger.error(f"Device not found in registry: {device_id}")
        return jsonify({
            'active_scene': '',
            'scenes': {}
        })

    ip_address = device.get('ip_address')
    if not ip_address:
        logger.error(f"Device {device_id} has no IP address")
        return jsonify({
            'active_scene': '',
            'scenes': {}
        })

    try:
        logger.debug(f"Getting scenes for {device_id} at {ip_address}")
        response = requests.get(f"http://{ip_address}/api/scenes", timeout=2)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            logger.warning(f"Failed to get scenes from {device_id}: HTTP {response.status_code}")
            return jsonify({
                'active_scene': '',
                'scenes': {}
            })
    except requests.RequestException as e:
        logger.error(f"Failed to get scenes for {device_id}: {e}")
        return jsonify({
            'active_scene': '',
            'scenes': {}
        })

@app.route('/api/device/<device_id>/scenes', methods=['POST'])
def set_device_scenes(device_id):
    """Patch scene configuration for a device (body keys = scene names)."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404

    data = request.json

    try:
        response = requests.post(
            f"http://{device.get('ip_address')}/api/scenes",
            json=data,
            timeout=2
        )
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': 'Failed to update scenes'}), response.status_code
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

    try:
        response = requests.post(
            f"http://{device.get('ip_address')}/api/scene",
            json=data,
            timeout=2
        )
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': 'Scene action failed'}), response.status_code
    except requests.RequestException as e:
        logger.error(f"Failed scene action for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/batch/volume', methods=['POST'])
def batch_set_volume():
    """Set global volume for multiple devices via scenes API."""
    data = request.json
    device_ids = data.get('device_ids', [])
    volume = data.get('volume', 50)

    logger.info(f"Batch setting global volume to {volume} for {len(device_ids)} devices")
    results = []

    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                ip_address = device.get('ip_address')
                # Get active scene name
                scenes_resp = requests.get(f"http://{ip_address}/api/scenes", timeout=2)
                if scenes_resp.status_code != 200:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    continue
                active_scene = scenes_resp.json().get('active_scene', 'default')

                # Patch active scene with new global_volume
                response = requests.post(
                    f"http://{ip_address}/api/scenes",
                    json={active_scene: {'global_volume': volume}},
                    timeout=2
                )
                if response.status_code == 200:
                    device['global_volume'] = volume
                    device['volume'] = volume  # For compatibility
                    registry.update_device(device)
                    results.append({'device_id': device_id, 'status': 'success'})
                    logger.debug(f"Set global volume on {device_id} to {volume}%")
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    logger.warning(f"Failed to set volume on {device_id}: HTTP {response.status_code}")
            except requests.RequestException as e:
                results.append({'device_id': device_id, 'status': 'error'})
                logger.error(f"Error setting volume on {device_id}: {e}")
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})

    return jsonify({'results': results})

@app.route('/api/batch/scene/activate', methods=['POST'])
def batch_activate_scene():
    """Activate a scene across multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    scene_name = data.get('scene')

    if not scene_name:
        return jsonify({'error': 'Missing required field: scene'}), 400

    logger.info(f"Batch activating scene '{scene_name}' on {len(device_ids)} devices")
    results = []

    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/scene",
                    json={'action': 'activate', 'name': scene_name},
                    timeout=2
                )
                if response.status_code == 200:
                    resp_data = response.json()
                    if resp_data.get('success'):
                        results.append({'device_id': device_id, 'status': 'success'})
                        logger.debug(f"Activated scene '{scene_name}' on {device_id}")
                    else:
                        results.append({'device_id': device_id, 'status': 'failed', 'error': resp_data.get('error')})
                        logger.warning(f"Failed to activate scene on {device_id}: {resp_data.get('error')}")
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    logger.warning(f"Failed to activate scene on {device_id}: HTTP {response.status_code}")
            except requests.RequestException as e:
                results.append({'device_id': device_id, 'status': 'error'})
                logger.error(f"Error activating scene on {device_id}: {e}")
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})

    return jsonify({'results': results})

@app.route('/api/batch/scene/create', methods=['POST'])
def batch_create_scene():
    """Create a scene across multiple devices (idempotent — 'already exists' counts as success)."""
    data = request.json
    device_ids = data.get('device_ids', [])
    scene_name = data.get('scene')

    if not scene_name:
        return jsonify({'error': 'Missing required field: scene'}), 400

    logger.info(f"Batch creating scene '{scene_name}' on {len(device_ids)} devices")
    results = []

    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/scene",
                    json={'action': 'create', 'name': scene_name},
                    timeout=2
                )
                if response.status_code == 200:
                    resp_data = response.json()
                    # Treat "already exists" as success (idempotent)
                    if resp_data.get('success') or resp_data.get('error') == 'Scene already exists':
                        results.append({'device_id': device_id, 'status': 'success'})
                        logger.debug(f"Created scene '{scene_name}' on {device_id}")
                    else:
                        results.append({'device_id': device_id, 'status': 'failed', 'error': resp_data.get('error')})
                        logger.warning(f"Failed to create scene on {device_id}: {resp_data.get('error')}")
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    logger.warning(f"Failed to create scene on {device_id}: HTTP {response.status_code}")
            except requests.RequestException as e:
                results.append({'device_id': device_id, 'status': 'error'})
                logger.error(f"Error creating scene on {device_id}: {e}")
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})

    return jsonify({'results': results})

@app.route('/api/batch/scene-trigger', methods=['POST'])
def batch_set_scene_trigger():
    """Set scene trigger name on multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    scene_trigger_name = data.get('scene_trigger_name', '')

    logger.info(f"Batch setting scene trigger '{scene_trigger_name}' on {len(device_ids)} devices")
    results = []

    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/device",
                    json={'scene_trigger_name': scene_trigger_name},
                    timeout=2
                )
                if response.status_code == 200:
                    resp_data = response.json()
                    if resp_data.get('success'):
                        results.append({'device_id': device_id, 'status': 'success'})
                    else:
                        results.append({'device_id': device_id, 'status': 'failed', 'error': resp_data.get('error')})
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
            except requests.RequestException as e:
                results.append({'device_id': device_id, 'status': 'error'})
                logger.error(f"Error setting scene trigger on {device_id}: {e}")
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})

    return jsonify({'results': results})

@app.route('/api/batch/save-config', methods=['POST'])
def batch_save_config():
    """Save configuration on multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    
    logger.info(f"Batch saving configuration for {len(device_ids)} devices")
    results = []
    
    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                # Call /api/config/save to persist current configuration
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/config/save",
                    timeout=5  # Longer timeout for save operation
                )
                if response.status_code == 200:
                    results.append({'device_id': device_id, 'status': 'success'})
                    logger.info(f"Configuration saved on {device_id}")
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    logger.warning(f"Failed to save config on {device_id}: HTTP {response.status_code}")
            except requests.RequestException as e:
                results.append({'device_id': device_id, 'status': 'error'})
                logger.error(f"Error saving config on {device_id}: {e}")
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})
    
    return jsonify({'results': results})

@app.route('/api/batch/reboot', methods=['POST'])
def batch_reboot():
    """Reboot multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    delay_ms = data.get('delay_ms', 1000)  # Default 1 second delay before reboot
    
    logger.info(f"Batch rebooting {len(device_ids)} devices with {delay_ms}ms delay")
    results = []
    
    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                # Call /api/system/reboot to reboot the device
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/system/reboot",
                    json={'delay_ms': delay_ms},
                    timeout=3  # Short timeout since device will reboot
                )
                if response.status_code == 200:
                    results.append({'device_id': device_id, 'status': 'success'})
                    logger.info(f"Reboot initiated on {device_id}")
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    logger.warning(f"Failed to reboot {device_id}: HTTP {response.status_code}")
            except requests.RequestException as e:
                results.append({'device_id': device_id, 'status': 'error'})
                logger.error(f"Error rebooting {device_id}: {e}")
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})
    
    return jsonify({'results': results})

# NOTE: /api/device/<id>/track/control and /api/device/<id>/track/volume
# have been removed. Use POST /api/device/<id>/scenes to patch track
# properties within a scene instead.

@app.route('/api/device/<device_id>/mur-gateway', methods=['GET'])
def get_device_mur_gateway(device_id):
    """Get Mur Gateway config for a device (via consolidated /api/device)."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    try:
        response = requests.get(f"http://{device.get('ip_address')}/api/device", timeout=2)
        if response.status_code == 200:
            data = response.json()
            return jsonify({
                'mur_gateway_ip': data.get('mur_gateway_ip', ''),
                'mur_gateway_port': data.get('mur_gateway_port', 4000),
                'scene_trigger_name': data.get('scene_trigger_name', '')
            })
        return jsonify({'error': f'HTTP {response.status_code}'}), 500
    except requests.RequestException as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/mur-gateway', methods=['POST'])
def set_device_mur_gateway(device_id):
    """Set Mur Gateway config for a device (via consolidated /api/device)."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    data = request.json
    payload = {'mur_gateway_ip': data.get('mur_gateway_ip', '')}
    if data.get('mur_gateway_port'):
        payload['mur_gateway_port'] = int(data['mur_gateway_port'])
    try:
        response = requests.post(
            f"http://{device.get('ip_address')}/api/device",
            json=payload, timeout=2
        )
        if response.status_code == 200:
            return jsonify({'status': 'success'})
        return jsonify({'error': f'HTTP {response.status_code}'}), 500
    except requests.RequestException as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/device-config', methods=['POST'])
def set_device_config(device_id):
    """Proxy arbitrary fields to a device's POST /api/device endpoint."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    try:
        response = requests.post(
            f"http://{device.get('ip_address')}/api/device",
            json=request.json, timeout=2
        )
        return jsonify(response.json()), response.status_code
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
    mur_gateway_port = data.get('mur_gateway_port')

    logger.info(f"Batch setting mur gateway to {mur_gateway_ip} for {len(device_ids)} devices")
    results = []

    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            payload = {'mur_gateway_ip': mur_gateway_ip}
            if mur_gateway_port:
                payload['mur_gateway_port'] = int(mur_gateway_port)
            try:
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/device",
                    json=payload, timeout=2
                )
                results.append({'device_id': device_id,
                                 'status': 'success' if response.status_code == 200 else 'failed'})
            except requests.RequestException:
                results.append({'device_id': device_id, 'status': 'error'})
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})

    return jsonify({'results': results})

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
    results = []

    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/device",
                    json={'device_volume': device_volume},
                    timeout=2
                )
                if response.status_code == 200:
                    device['device_volume'] = device_volume
                    registry.update_device(device)
                    results.append({'device_id': device_id, 'status': 'success'})
                    logger.debug(f"Set device_volume on {device_id} to {device_volume}%")
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    logger.warning(f"Failed to set device_volume on {device_id}: HTTP {response.status_code}")
            except requests.RequestException as e:
                results.append({'device_id': device_id, 'status': 'error'})
                logger.error(f"Error setting device_volume on {device_id}: {e}")
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})

    return jsonify({'results': results})

@app.route('/api/batch/play', methods=['POST'])
def batch_control_playback():
    """Control playback for multiple devices via scenes API."""
    data = request.json
    device_ids = data.get('device_ids', [])
    action = data.get('action', 'play')

    logger.info(f"Batch {action} for {len(device_ids)} devices")
    results = []

    active = (action in ('play', 'start'))

    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            ip_address = device.get('ip_address')
            try:
                # Get active scene to know which tracks to update
                scenes_resp = requests.get(f"http://{ip_address}/api/scenes", timeout=2)
                if scenes_resp.status_code != 200:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    continue
                scenes_data = scenes_resp.json()
                active_scene = scenes_data.get('active_scene', 'default')
                scene = scenes_data.get('scenes', {}).get(active_scene, {})
                tracks = scene.get('tracks', [])

                # Build a patch for all tracks
                track_list = []
                for track in tracks:
                    track_list.append({'track': track.get('track', 0), 'active': active})

                response = requests.post(
                    f"http://{ip_address}/api/scenes",
                    json={active_scene: {'tracks': track_list}},
                    timeout=2
                )
                if response.status_code == 200:
                    results.append({'device_id': device_id, 'status': 'success'})
                else:
                    results.append({'device_id': device_id, 'status': 'failed'})
                    logger.warning(f"Failed to set playback on {device_id}: HTTP {response.status_code}")
            except requests.RequestException as e:
                logger.error(f"Error setting playback on {device_id}: {e}")
                results.append({'device_id': device_id, 'status': 'error'})
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})

    return jsonify({'results': results})

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

    socketio.run(app, host='0.0.0.0', port=SERVER_PORT, debug=debug_mode, allow_unsafe_werkzeug=True)
