"""
Flask web application for managing ESP32 Loudframe devices.
Uses device-manager scripts for efficient network scanning.
"""
import os
import json
import time
import threading
import logging
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import requests
from network_wrapper import NetworkConfig, DeviceScannerWrapper, DeviceRegistry

# Configure logging with detailed output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('scape_server')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'loudframe-scape-server-2025'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize components
network_config = NetworkConfig()
registry = DeviceRegistry()

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
        
        network_config.save_config()
        
        return jsonify({'status': 'success', 'config': network_config.config})

@app.route('/api/devices')
def get_devices():
    """Get all registered devices with detailed loop information."""
    # Reload registry to get latest data
    registry.load_registry()
    devices = registry.get_device_list()
    
    # Format devices for web display with loop information
    formatted_devices = []
    online_count = 0
    
    for device in devices:
        formatted = {
            'id': device.get('id', device.get('ip_address', 'unknown')),
            'ip': device.get('ip_address', 'unknown'),
            'status': 'online' if device.get('online', False) else 'offline',
            'playing': False,  # Will be updated from loop status
            'volume': 0,  # Will be updated to global volume
            'ssid': device.get('wifi_ssid', 'Unknown'),
            'mac_address': device.get('mac_address', 'Unknown'),
            'firmware_version': device.get('firmware_version', 'Unknown'),
            'last_seen': device.get('last_seen', ''),
            'loops': [],  # Will hold detailed loop information
            'global_volume': 0,
            'active_loops': 0
        }
        
        # Always probe device to check if it's really online
        ip_address = device.get('ip_address')
        is_actually_online = False
        
        # Get probe timeout from config (default 3 seconds)
        probe_timeout = network_config.config.get('probe_timeout', 3)
        
        # Log probe attempt
        logger.info(f"[PROBE START] Device: {formatted['id']} | IP: {ip_address} | Timeout: {probe_timeout}s")
        probe_start_time = time.time()
        
        # First, check if device is reachable using /api/status (like device_controller.py does)
        try:
            status_response = requests.get(f"http://{ip_address}/api/status", timeout=probe_timeout)
            probe_elapsed = time.time() - probe_start_time
            
            if status_response.status_code == 200:
                is_actually_online = True
                # Update device info from status
                status_data = status_response.json()
                
                # ALWAYS capture MAC address - it's the fundamental unique identifier
                mac_address = status_data.get('mac_address')
                if mac_address:
                    # Always update MAC address in registry - it's the key identifier
                    device['mac_address'] = mac_address
                    formatted['mac_address'] = mac_address
                    logger.debug(f"MAC Address confirmed: {mac_address}")
                else:
                    logger.error(f"WARNING: No MAC address returned from {ip_address}/api/status!")
                
                # Check if device ID has changed (ID can be user-configured)
                new_id = status_data.get('id')
                if new_id and new_id != device.get('id'):
                    logger.warning(f"[ID CHANGE] Device ID changed from {device.get('id')} to {new_id} at IP {ip_address} (MAC: {mac_address})")
                    # Update the device ID in the registry
                    device['id'] = new_id
                    formatted['id'] = new_id
                else:
                    formatted['id'] = status_data.get('id', formatted['id'])
                
                # Update other device info
                formatted['firmware_version'] = status_data.get('firmware_version', formatted['firmware_version'])
                formatted['ssid'] = status_data.get('wifi_ssid', device.get('wifi_ssid', 'Unknown'))
                
                # Always update registry with latest info including MAC
                registry.update_device(device)
                
                logger.info(f"[PROBE SUCCESS] Device: {formatted['id']} | MAC: {mac_address} | Response time: {probe_elapsed:.2f}s | Status: ONLINE")
            else:
                logger.warning(f"[PROBE FAILED] Device: {formatted['id']} | HTTP {status_response.status_code} | Response time: {probe_elapsed:.2f}s")
                
        except requests.Timeout:
            probe_elapsed = time.time() - probe_start_time
            logger.warning(f"[PROBE TIMEOUT] Device: {formatted['id']} | Timeout after {probe_elapsed:.2f}s | Status: OFFLINE")
            is_actually_online = False
        except requests.ConnectionError as e:
            probe_elapsed = time.time() - probe_start_time
            logger.warning(f"[PROBE CONNECTION ERROR] Device: {formatted['id']} | Error: {str(e)[:100]} | Time: {probe_elapsed:.2f}s | Status: OFFLINE")
            is_actually_online = False
        except requests.RequestException as e:
            probe_elapsed = time.time() - probe_start_time
            logger.error(f"[PROBE ERROR] Device: {formatted['id']} | Error: {str(e)[:100]} | Time: {probe_elapsed:.2f}s | Status: OFFLINE")
            is_actually_online = False
        
        # Update the actual status based on probe
        formatted['status'] = 'online' if is_actually_online else 'offline'
        
        # If device is actually online, get detailed loop information
        if is_actually_online:
            online_count += 1
            
            try:
                # Get loop status
                response = requests.get(f"http://{ip_address}/api/loops", timeout=1)
                
                if response.status_code == 200:
                    loop_data = response.json()
                    
                    # Update with actual loop information
                    formatted['global_volume'] = loop_data.get('global_volume', 0)
                    formatted['volume'] = formatted['global_volume']  # For compatibility
                    formatted['active_loops'] = loop_data.get('active_count', 0)
                    
                    # Process each loop/track
                    loops = []
                    any_playing = False
                    for loop in loop_data.get('loops', []):
                        loop_info = {
                            'track': loop.get('track', 0),
                            'playing': loop.get('playing', False),
                            'volume': loop.get('volume', 0),
                            'file': loop.get('file', ''),
                            'filename': loop.get('file', '').split('/')[-1] if loop.get('file') else 'No file'
                        }
                        loops.append(loop_info)
                        if loop_info['playing']:
                            any_playing = True
                    
                    formatted['loops'] = loops
                    formatted['playing'] = any_playing
                    
                    logger.debug(f"Device {formatted['id']}: {formatted['active_loops']} active loops, global vol: {formatted['global_volume']}")
                    
            except requests.RequestException as e:
                logger.debug(f"Could not get loop status for {formatted['id']}: {e}")
                # Keep default values if we can't get loop status
        else:
            # Device is offline, update registry to reflect this
            device['online'] = False
            registry.update_device(device)
        
        formatted_devices.append(formatted)
    
    logger.info(f"Returning {len(formatted_devices)} devices ({online_count} online)")
    
    return jsonify({
        'devices': formatted_devices,
        'count': len(formatted_devices),
        'online': online_count
    })

@app.route('/api/scan', methods=['POST'])
def start_scan():
    """Start a network scan."""
    logger.info("=== Manual scan requested ===")
    
    def scan_with_progress():
        try:
            def progress_callback(current, total, percent):
                logger.debug(f"Scan progress: {current}/{total} ({percent:.1f}%)")
                socketio.emit('scan_progress', {
                    'current': current,
                    'total': total,
                    'percent': percent
                })
            
            def network_callback(network, current, total):
                logger.info(f"Scanning network {current}/{total}: {network}")
                socketio.emit('scanning_network', {
                    'network': network,
                    'current': current,
                    'total': total
                })
            
            scanner = DeviceScannerWrapper(network_config, progress_callback)
            devices = scanner.scan_all_networks(progress_callback, network_callback)
            
            # Reload registry
            registry.load_registry()
            
            socketio.emit('scan_complete', {
                'devices': devices,
                'count': len(devices),
                'status': 'success'
            })
            
            logger.info(f"Manual scan complete: {len(devices)} devices found")
            
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
        
        # Try to get fresh status
        try:
            logger.info(f"Getting status for device {device_id} at {device.get('ip_address')}")
            response = requests.get(f"http://{device.get('ip_address')}/api/status", timeout=2)
            if response.status_code == 200:
                data = response.json()
                device.update(data)
                device['online'] = True
                
                # Log the actual data received from device (for debugging)
                logger.debug(f"[DEVICE STATUS] Device {device_id} status: {data}")
                
                # Extract and format uptime - device returns it as 'uptime_seconds'
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
        
        # Format for web display with uptime and firmware
        formatted = {
            'id': device.get('id', device_id),
            'ip': device.get('ip_address', 'unknown'),
            'status': 'online' if device.get('online', False) else 'offline',
            'playing': device.get('playing', False),
            'volume': device.get('volume', 0),
            'mac_address': device.get('mac_address', 'Unknown'),
            'firmware_version': device.get('firmware_version', 'Unknown'),
            'last_seen': device.get('last_seen', ''),
            'uptime': uptime_str  # Add formatted uptime to response
        }
        
        return jsonify(formatted)
    else:
        return jsonify({'error': 'Device not found'}), 404

@app.route('/api/device/<device_id>/volume', methods=['POST'])
def set_device_volume(device_id):
    """Set volume for a specific device."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    
    data = request.json
    volume = data.get('volume', 50)
    
    try:
        logger.info(f"Setting volume to {volume} for device {device_id}")
        response = requests.post(
            f"http://{device.get('ip_address')}/api/volume",
            json={'volume': volume},
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
    """Control playback on a device."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    
    data = request.json
    action = data.get('action', 'toggle')
    
    try:
        ip_address = device.get('ip_address')
        logger.info(f"Sending {action} command to device {device_id} at {ip_address}")
        
        if action == 'play':
            response = requests.post(f"http://{ip_address}/api/play", timeout=2)
        elif action == 'pause':
            response = requests.post(f"http://{ip_address}/api/pause", timeout=2)
        elif action == 'stop':
            response = requests.post(f"http://{ip_address}/api/stop", timeout=2)
        else:
            # Toggle
            response = requests.post(f"http://{ip_address}/api/toggle", timeout=2)
        
        if response.status_code == 200:
            return jsonify({'status': 'success', 'action': action})
        else:
            return jsonify({'error': 'Failed to control playback'}), 500
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

@app.route('/api/device/<device_id>/loops')
def get_device_loops(device_id):
    """Get loop configuration for a device."""
    device = registry.get_device(device_id)
    if not device:
        logger.error(f"Device not found in registry: {device_id}")
        # Return empty loops if device not found to avoid errors
        return jsonify({
            'loops': [],
            'global_volume': 0,
            'active_count': 0
        })
    
    ip_address = device.get('ip_address')
    if not ip_address:
        logger.error(f"Device {device_id} has no IP address")
        return jsonify({
            'loops': [],
            'global_volume': 0,
            'active_count': 0
        })
    
    try:
        logger.debug(f"Getting loops for {device_id} at {ip_address}")
        response = requests.get(f"http://{ip_address}/api/loops", timeout=2)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            logger.warning(f"Failed to get loops from {device_id}: HTTP {response.status_code}")
            return jsonify({
                'loops': [],
                'global_volume': 0,
                'active_count': 0
            })
    except requests.RequestException as e:
        logger.error(f"Failed to get loops for {device_id}: {e}")
        # Return empty loops structure instead of error
        return jsonify({
            'loops': [],
            'global_volume': 0,
            'active_count': 0
        })

@app.route('/api/device/<device_id>/loops', methods=['POST'])
def set_device_loops(device_id):
    """Set loop configuration for a device."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    
    data = request.json
    
    try:
        response = requests.post(
            f"http://{device.get('ip_address')}/api/loops",
            json=data,
            timeout=2
        )
        if response.status_code == 200:
            return jsonify({'status': 'success'})
        else:
            return jsonify({'error': 'Failed to set loops'}), 500
    except requests.RequestException as e:
        logger.error(f"Failed to set loops for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/batch/volume', methods=['POST'])
def batch_set_volume():
    """Set global volume for multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    volume = data.get('volume', 50)
    
    logger.info(f"Batch setting global volume to {volume} for {len(device_ids)} devices")
    results = []
    
    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            try:
                # Use the correct global volume endpoint
                response = requests.post(
                    f"http://{device.get('ip_address')}/api/global/volume",
                    json={'volume': volume},
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

@app.route('/api/device/<device_id>/track/control', methods=['POST'])
def control_track(device_id):
    """Control individual track on a device."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    
    data = request.json
    track = data.get('track', 0)
    action = data.get('action', 'stop')
    
    try:
        ip_address = device.get('ip_address')
        endpoint = '/api/loop/start' if action == 'start' else '/api/loop/stop'
        
        response = requests.post(
            f"http://{ip_address}{endpoint}",
            json={'track': track},
            timeout=2
        )
        
        if response.status_code == 200:
            return jsonify({'status': 'success', 'track': track, 'action': action})
        else:
            return jsonify({'error': f'Failed to {action} track {track}'}), 500
    except requests.RequestException as e:
        logger.error(f"Error controlling track {track} on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/track/volume', methods=['POST'])
def set_track_volume(device_id):
    """Set volume for a specific track on a device."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    
    data = request.json
    track = data.get('track', 0)
    volume = data.get('volume', 50)
    
    try:
        response = requests.post(
            f"http://{device.get('ip_address')}/api/loop/volume",
            json={'track': track, 'volume': volume},
            timeout=2
        )
        
        if response.status_code == 200:
            return jsonify({'status': 'success', 'track': track, 'volume': volume})
        else:
            return jsonify({'error': f'Failed to set volume for track {track}'}), 500
    except requests.RequestException as e:
        logger.error(f"Error setting volume for track {track} on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/device/<device_id>/track/file', methods=['POST'])
def set_track_file(device_id):
    """Set file for a specific track on a device."""
    device = registry.get_device(device_id)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    
    data = request.json
    track = data.get('track', 0)
    file_index = data.get('file_index')
    filename = data.get('filename')
    
    try:
        # Build request body based on what was provided
        request_body = {'track': track}
        if file_index is not None:
            request_body['file_index'] = file_index
        elif filename:
            request_body['filename'] = filename
        
        response = requests.post(
            f"http://{device.get('ip_address')}/api/loop/file",
            json=request_body,
            timeout=5
        )
        
        if response.status_code == 200:
            return jsonify({'status': 'success', 'track': track})
        else:
            return jsonify({'error': f'Failed to set file for track {track}'}), 500
    except requests.RequestException as e:
        logger.error(f"Error setting file for track {track} on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/batch/play', methods=['POST'])
def batch_control_playback():
    """Control playback for multiple devices."""
    data = request.json
    device_ids = data.get('device_ids', [])
    action = data.get('action', 'play')
    
    logger.info(f"Batch {action} for {len(device_ids)} devices")
    results = []
    
    for device_id in device_ids:
        device = registry.get_device(device_id)
        if device:
            ip_address = device.get('ip_address')
            device_success = True
            
            # For each device, we need to control all 3 tracks
            for track in range(3):
                try:
                    if action == 'play' or action == 'start':
                        # Use /api/loop/start to start each track
                        response = requests.post(
                            f"http://{ip_address}/api/loop/start",
                            json={'track': track},
                            timeout=2
                        )
                    elif action == 'stop' or action == 'pause':
                        # Use /api/loop/stop to stop each track
                        response = requests.post(
                            f"http://{ip_address}/api/loop/stop",
                            json={'track': track},
                            timeout=2
                        )
                    else:
                        continue
                    
                    if response.status_code != 200:
                        device_success = False
                        logger.warning(f"Failed to {action} track {track} on {device_id}")
                        
                except requests.RequestException as e:
                    logger.error(f"Error controlling track {track} on {device_id}: {e}")
                    device_success = False
            
            if device_success:
                results.append({'device_id': device_id, 'status': 'success'})
            else:
                results.append({'device_id': device_id, 'status': 'partial'})
        else:
            results.append({'device_id': device_id, 'status': 'not_found'})
    
    return jsonify({'results': results})

@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {'message': 'Connected to Scape Server'})

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
    
    logger.info("Access the web interface at: http://localhost:5000")
    logger.info("Or from network: http://<your-ip>:5000")
    logger.info("=" * 60)
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=debug_mode)
