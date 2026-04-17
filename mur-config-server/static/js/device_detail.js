// Device Detail Page JavaScript - Standalone version

// Global variables
let currentDevice = null;
let refreshInterval = null;
let volumeDebounceTimers = {};  // For debouncing volume changes
let lastVolumeValues = {};      // Track last sent values to avoid duplicates
let activeSceneName = 'default'; // Active scene name from last fetch
let lastScenesData = null;       // Full scenes response from last fetch
let deviceMurGatewayIp = '';     // Mur Gateway IP for this device
let deviceMurGatewayPort = 4000; // Mur Gateway device port
let cachedTriggerData = null;    // Cached trigger list with type info [{name, type}]
let triggerNamesFetchPromise = null; // Dedup concurrent fetches
// Blocks DOM rebuilds while a slider is being dragged. Number.MAX_SAFE_INTEGER
// while a pointer is down on a slider; after release, a future timestamp that
// spans the POST round-trip and at least one poll tick so the server-confirmed
// value lands before the next rebuild.
let sliderInteractionBlockUntil = 0;

async function fetchTriggerData(forceRefresh = false) {
    if (cachedTriggerData && !forceRefresh) return cachedTriggerData;
    if (triggerNamesFetchPromise) return triggerNamesFetchPromise;
    if (!deviceMurGatewayIp) return [];

    triggerNamesFetchPromise = (async () => {
        try {
            const resp = await fetch(`/api/triggers?gateway_ip=${encodeURIComponent(deviceMurGatewayIp)}&gateway_port=${deviceMurGatewayPort}`);
            if (resp.ok) {
                const data = await resp.json();
                // Prefer typed triggers array; fall back to names-only
                cachedTriggerData = data.triggers || (data.trigger_names || []).map(n => ({name: n, type: ''}));
            } else {
                cachedTriggerData = [];
            }
        } catch (e) {
            console.warn('[TRIGGERS] Failed to fetch trigger names:', e);
            cachedTriggerData = [];
        }
        triggerNamesFetchPromise = null;
        return cachedTriggerData;
    })();
    return triggerNamesFetchPromise;
}

// Filter trigger names by type (e.g. 'On/Off', 'Discrete')
async function fetchTriggerNamesByType(type, forceRefresh = false) {
    const triggers = await fetchTriggerData(forceRefresh);
    return triggers.filter(t => t.type === type).map(t => t.name);
}

// Backward compat: return all trigger names
async function fetchTriggerNames(forceRefresh = false) {
    const triggers = await fetchTriggerData(forceRefresh);
    return triggers.map(t => t.name);
}

// Helper: build a scenes patch body for a track update in a specific scene
function buildSceneTrackPatch(trackIndex, fields, sceneName) {
    const body = {};
    body[sceneName || activeSceneName] = { tracks: [{ track: trackIndex, ...fields }] };
    return body;
}

// Helper: build a scenes patch body for scene-level fields (e.g. global_volume)
function buildScenePatch(fields, sceneName) {
    const body = {};
    body[sceneName || activeSceneName] = fields;
    return body;
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    if (!deviceInfo) {
        console.error('No device information available');
        return;
    }
    
    currentDevice = deviceInfo.id || deviceInfo.ip;
    console.log(`[DEVICE-DETAIL] Initializing for device: ${currentDevice}`);
    
    attachEventListeners();
    setupSliderGuards();
    loadDeviceData();
    loadWiFiStatus();  // One-time WiFi status fetch
    startAutoRefresh();
});

function setupSliderGuards() {
    const isRange = (el) => el && el.matches && el.matches('input[type="range"]');

    document.addEventListener('pointerdown', (e) => {
        if (isRange(e.target)) {
            sliderInteractionBlockUntil = Number.MAX_SAFE_INTEGER;
        }
    }, true);

    const endDrag = () => {
        if (sliderInteractionBlockUntil === Number.MAX_SAFE_INTEGER) {
            sliderInteractionBlockUntil = Date.now() + 2500;
        }
    };
    window.addEventListener('pointerup', endDrag, true);
    window.addEventListener('pointercancel', endDrag, true);
}

// Attach event listeners
function attachEventListeners() {
    // Control buttons
    document.getElementById('saveConfigBtn').addEventListener('click', saveConfig);
    document.getElementById('rebootBtn').addEventListener('click', rebootDevice);
    document.getElementById('viewFilesBtn').addEventListener('click', toggleFiles);
    // Global volume is now per-scene, handled in attachAllSceneHandlers
}

// Load device data and loops
async function loadDeviceData() {
    if (!currentDevice) return;
    
    try {
        // Get device status
        const statusResponse = await fetch(`/api/device/${currentDevice}`);
        if (statusResponse.ok) {
            const deviceData = await statusResponse.json();
            updateDeviceInfo(deviceData);
        }
        
        // Get scenes (skip re-render if user is editing a trigger name)
        const scenesResponse = await fetch(`/api/device/${currentDevice}/scenes`);
        if (scenesResponse.ok) {
            const scenesData = await scenesResponse.json();
            lastScenesData = scenesData;
            activeSceneName = scenesData.active_scene || 'default';
            const editOpen = document.querySelector('.trigger-name-edit-row[style*="flex"]');
            const ae = document.activeElement;
            const keyboardHoldsSlider = ae && ae.matches && ae.matches('input[type="range"]');
            const sliderBusy = Date.now() < sliderInteractionBlockUntil;
            if (!editOpen && !keyboardHoldsSlider && !sliderBusy) {
                renderAllScenes(scenesData);
            }
        }

        // Get mur gateway config + scene trigger config
        loadMurGateway();
        loadSceneTriggerConfig();
    } catch (error) {
        console.error('[DEVICE-DETAIL] Error loading device data:', error);
        // Show clear error message if Flask server is down
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server connection error - Please check if the server is running', 'error');
        }
    }
}

// Update device information display
function updateDeviceInfo(data) {
    document.getElementById('deviceStatus').textContent = data.status.toUpperCase();
    document.getElementById('deviceStatus').className = `device-status ${data.status}`;
    
    if (data.mac_address) {
        document.getElementById('deviceMac').textContent = data.mac_address;
    }
    if (data.firmware_version) {
        document.getElementById('deviceFirmware').textContent = data.firmware_version;
    }
    // Don't update SSID here - it's handled by loadWiFiStatus()
    if (data.uptime) {
        document.getElementById('deviceUptime').textContent = data.uptime;
    }
    
    // Handle offline status
    if (data.status === 'offline') {
        document.getElementById('loopsConfig').innerHTML = '<p style="color: #dc3545;">Device is offline</p>';
        document.getElementById('deviceUptime').textContent = 'N/A';
        disableControls(true);
    } else {
        disableControls(false);
    }
}

// Control playback (start/stop all tracks)
// Set global volume with throttling and immediate final value
async function setGlobalVolume(volume, isFinal = false) {
    const volumeKey = 'global';
    const volumeInt = parseInt(volume);
    
    // Reset auto-refresh timer to give server time to process the change
    resetAutoRefresh();
    
    // Clear any existing timer for this control
    if (volumeDebounceTimers[volumeKey]) {
        clearTimeout(volumeDebounceTimers[volumeKey]);
    }
    
    // If this is the final value (user released slider), send immediately
    if (isFinal) {
        // Cancel any pending request and send final value now
        const previousValue = lastVolumeValues[volumeKey];
        lastVolumeValues[volumeKey] = volumeInt;
        
        try {
            const response = await fetch('/api/batch/volume', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    device_ids: [currentDevice],
                    volume: volumeInt
                })
            });
            
            if (!response.ok) {
                console.error('Failed to set volume');
            }
        } catch (error) {
            console.error('Error setting volume:', error);
            if (error.message.includes('Failed to fetch')) {
                showMessage('Flask server error - Volume change not saved', 'error');
            }
        }
        return;
    }
    
    // Otherwise, throttle the requests during dragging
    volumeDebounceTimers[volumeKey] = setTimeout(async () => {
        // Only send if value actually changed
        if (lastVolumeValues[volumeKey] === volumeInt) {
            return;
        }
        
        const previousValue = lastVolumeValues[volumeKey];
        lastVolumeValues[volumeKey] = volumeInt;
        
        try {
            const response = await fetch('/api/batch/volume', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    device_ids: [currentDevice],
                    volume: volumeInt
                })
            });
            
            if (!response.ok) {
                console.error('Failed to set volume');
            }
        } catch (error) {
            console.error('Error setting volume:', error);
            if (error.message.includes('Failed to fetch')) {
                showMessage('Flask server error - Volume change not saved', 'error');
            }
        }
    }, 200);  // 200ms = max 5 requests per second while dragging
}

// Save configuration
async function saveConfig() {
    try {
        const response = await fetch('/api/batch/save-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                device_ids: [currentDevice]
            })
        });
        
        if (response.ok) {
            showMessage('Configuration saved', 'success');
        } else {
            showMessage('Failed to save configuration', 'error');
        }
    } catch (error) {
        console.error('Error saving configuration:', error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server connection error - Cannot save configuration', 'error');
        } else {
            showMessage('Error saving configuration', 'error');
        }
    }
}

// Reboot device
async function rebootDevice() {
    try {
        const response = await fetch('/api/batch/reboot', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                device_ids: [currentDevice],
                delay_ms: 1000
            })
        });
        
        if (response.ok) {
            showMessage('Device reboot initiated', 'success');
            // Pause refresh during reboot
            stopAutoRefresh();
            setTimeout(() => {
                startAutoRefresh();
                loadDeviceData();
            }, 10000);
        } else {
            showMessage('Failed to reboot device', 'error');
        }
    } catch (error) {
        console.error('Error rebooting device:', error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server connection error - Cannot send reboot command', 'error');
        } else {
            showMessage('Error rebooting device', 'error');
        }
    }
}

// Control individual track (enable/disable)
async function controlTrack(track, action, sceneName) {
    try {
        const active = (action === 'start');
        const body = buildSceneTrackPatch(track, { active: active }, sceneName);
        const response = await fetch(`/api/device/${currentDevice}/scenes`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });

        if (response.ok) {
            setTimeout(loadDeviceData, 500);
        }
    } catch (error) {
        console.error(`Error controlling track ${track}:`, error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server error - Cannot control track', 'error');
        }
    }
}

// Set track mode (loop/trigger)
async function setTrackMode(track, mode, sceneName) {
    try {
        const body = buildSceneTrackPatch(track, { mode: mode }, sceneName);
        const response = await fetch(`/api/device/${currentDevice}/scenes`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });

        if (response.ok) {
            setTimeout(loadDeviceData, 500);
        }
    } catch (error) {
        console.error(`Error setting mode for track ${track}:`, error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server error - Cannot set track mode', 'error');
        }
    }
}

// Shared volume-update path used by both scene and per-track sliders.
// isFinal=false → debounced (200 ms) with last-value dedup for smooth dragging.
// isFinal=true  → sent immediately on release, cancelling any pending debounce.
async function sendVolumeChange(volumeKey, buildPatch, volumeInt, isFinal, errContext) {
    resetAutoRefresh();

    if (volumeDebounceTimers[volumeKey]) {
        clearTimeout(volumeDebounceTimers[volumeKey]);
    }

    const post = async () => {
        try {
            const body = buildPatch();
            const response = await fetch(`/api/device/${currentDevice}/scenes`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            if (!response.ok) {
                console.error(`Failed to set ${errContext} volume`);
            }
        } catch (error) {
            console.error(`Error setting ${errContext} volume:`, error);
            if (error.message && error.message.includes('Failed to fetch')) {
                showMessage(`Flask server error - ${errContext} volume change not saved`, 'error');
            }
        }
    };

    if (isFinal) {
        lastVolumeValues[volumeKey] = volumeInt;
        await post();
        return;
    }

    volumeDebounceTimers[volumeKey] = setTimeout(() => {
        if (lastVolumeValues[volumeKey] === volumeInt) return;
        lastVolumeValues[volumeKey] = volumeInt;
        post();
    }, 200);
}

async function setTrackVolume(track, volume, isFinal = false, sceneName = null) {
    const sn = sceneName || activeSceneName;
    const volumeInt = parseInt(volume);
    await sendVolumeChange(
        `${sn}-track-${track}`,
        () => buildSceneTrackPatch(track, { volume: volumeInt }, sn),
        volumeInt,
        isFinal,
        `track ${track}`
    );
}

async function setSceneVolume(volume, isFinal = false, sceneName = null) {
    const sn = sceneName || activeSceneName;
    const volumeInt = parseInt(volume);
    await sendVolumeChange(
        `${sn}-scene`,
        () => buildScenePatch({ global_volume: volumeInt }, sn),
        volumeInt,
        isFinal,
        `scene ${sn}`
    );
}

// Global variable for track selection
let currentTrackForFileSelection = null;
let currentSceneForFileSelection = null;

// Select file for track - opens the file selection panel
async function selectTrackFile(track, sceneName) {
    currentTrackForFileSelection = track;
    currentSceneForFileSelection = sceneName || activeSceneName;
    
    try {
        // Get available files
        const filesResponse = await fetch(`/api/device/${currentDevice}/files`);
        if (!filesResponse.ok) {
            showMessage('Failed to load files', 'error');
            return;
        }
        
        const filesData = await filesResponse.json();
        if (!filesData.files || filesData.files.length === 0) {
            showMessage('No audio files found on device', 'error');
            return;
        }
        
        // Show file selection panel
        showFileSelectionPanel(track, filesData.files);
        
    } catch (error) {
        console.error(`Error loading files for track ${track}:`, error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server error - Cannot load files', 'error');
        } else {
            showMessage('Error loading files', 'error');
        }
    }
}

// Show the file selection panel with clickable files
function showFileSelectionPanel(track, files) {
    // Update track label
    document.getElementById('fileSelectionTrack').textContent = `Track ${track}`;
    
    // Build file list with clickable items
    const fileListContainer = document.getElementById('fileSelectionList');
    fileListContainer.innerHTML = '';
    
    files.forEach((file, index) => {
        const fileItem = document.createElement('div');
        fileItem.style.cssText = `
            padding: 10px;
            margin: 5px 0;
            background: #f8f9fa;
            border-radius: 5px;
            cursor: pointer;
            transition: background 0.2s;
        `;
        fileItem.innerHTML = `
            <strong>${file.name}</strong>
            <span style="float: right; color: #666;">${formatFileSize(file.size)}</span>
        `;
        
        // Add hover effect
        fileItem.onmouseover = function() {
            this.style.background = '#e8f4f8';
        };
        fileItem.onmouseout = function() {
            this.style.background = '#f8f9fa';
        };
        
        // Add click handler
        fileItem.onclick = function() {
            selectFileForTrack(file.path, file.name);
        };
        
        fileListContainer.appendChild(fileItem);
    });
    
    // Show panel and overlay
    document.getElementById('fileSelectionPanel').style.display = 'block';
    document.getElementById('fileSelectionOverlay').style.display = 'block';
}

// Handle file selection
async function selectFileForTrack(filePath, fileName) {
    if (currentTrackForFileSelection === null) return;

    try {
        const body = buildSceneTrackPatch(currentTrackForFileSelection, { file_path: filePath }, currentSceneForFileSelection);
        const response = await fetch(`/api/device/${currentDevice}/scenes`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        
        if (response.ok) {
            showMessage(`Set "${fileName}" for Track ${currentTrackForFileSelection}`, 'success');
            closeFileSelection();
            setTimeout(loadDeviceData, 500);
        } else {
            showMessage(`Failed to set file for track ${currentTrackForFileSelection}`, 'error');
        }
    } catch (error) {
        console.error(`Error setting file for track ${currentTrackForFileSelection}:`, error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server error - Cannot set track file', 'error');
        } else {
            showMessage('Error setting file', 'error');
        }
    }
}

// Close file selection panel
window.closeFileSelection = function() {
    document.getElementById('fileSelectionPanel').style.display = 'none';
    document.getElementById('fileSelectionOverlay').style.display = 'none';
    currentTrackForFileSelection = null;
}

// Toggle files display
async function toggleFiles() {
    const filesSection = document.getElementById('filesSection');
    
    if (filesSection.style.display === 'none') {
        // Load and show files
        const filesList = document.getElementById('filesList');
        filesList.innerHTML = '<p>Loading files...</p>';
        filesSection.style.display = 'block';
        
        try {
            const response = await fetch(`/api/device/${currentDevice}/files`);
            const data = await response.json();
            
            if (data.files && data.files.length > 0) {
                filesList.innerHTML = '';
                data.files.forEach(file => {
                    const fileItem = document.createElement('div');
                    fileItem.className = 'file-item';
                    fileItem.innerHTML = `
                        <span>${file.name}</span>
                        <span>${formatFileSize(file.size)}</span>
                    `;
                    filesList.appendChild(fileItem);
                });
            } else {
                filesList.innerHTML = '<p>No files found on device</p>';
            }
        } catch (error) {
            console.error('Error loading files:', error);
            if (error.message.includes('Failed to fetch')) {
                filesList.innerHTML = '<p>Flask server error - Cannot load files</p>';
            } else {
                filesList.innerHTML = '<p>Error loading files</p>';
            }
        }
    } else {
        // Hide files
        filesSection.style.display = 'none';
    }
}

// Format file size
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

// Show message to user
function showMessage(message, type = 'info') {
    const messageArea = document.getElementById('messageArea');
    const messageText = messageArea.querySelector('.message-text');
    
    messageText.textContent = message;
    messageArea.style.display = 'block';
    
    // Set color based on type
    if (type === 'success') {
        messageArea.style.background = '#d4edda';
        messageArea.style.color = '#155724';
        messageArea.style.borderColor = '#c3e6cb';
    } else if (type === 'error') {
        messageArea.style.background = '#f8d7da';
        messageArea.style.color = '#721c24';
        messageArea.style.borderColor = '#f5c6cb';
    }
    
    // Auto-hide after 5 seconds
    setTimeout(() => {
        messageArea.style.display = 'none';
    }, 5000);
}

// Disable/enable controls
function disableControls(disabled) {
    document.getElementById('saveConfigBtn').disabled = disabled;
    document.getElementById('rebootBtn').disabled = disabled;
}

// Start auto-refresh
function startAutoRefresh() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
    }
    
    console.log('[DEVICE-DETAIL] Starting auto-refresh every 2 seconds');
    refreshInterval = setInterval(() => {
        loadDeviceData();
    }, 2000);
}

// Reset auto-refresh timer (delays next refresh by 2 seconds)
function resetAutoRefresh() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
        refreshInterval = setInterval(() => {
            loadDeviceData();
        }, 2000);
    }
}

// Stop auto-refresh
function stopAutoRefresh() {
    if (refreshInterval) {
        console.log('[DEVICE-DETAIL] Stopping auto-refresh');
        clearInterval(refreshInterval);
        refreshInterval = null;
    }
}

// Load WiFi status from consolidated /api/device endpoint (one-time, not refreshed)
async function loadWiFiStatus() {
    if (!currentDevice || !deviceInfo.ip) return;

    try {
        const response = await fetch(`http://${deviceInfo.ip}/api/device`);

        if (response.ok) {
            const deviceData = await response.json();
            const wifiData = deviceData.wifi || {};

            // Update SSID
            if (wifiData.ssid) {
                document.getElementById('deviceSsid').textContent = wifiData.ssid;

                // Add signal strength display if element exists
                const signalElement = document.getElementById('deviceSignal');
                if (signalElement) {
                    // Display signal strength percentage and RSSI
                    const signalPercent = wifiData.signal_strength || 0;
                    const rssi = wifiData.rssi || 0;
                    signalElement.textContent = `${signalPercent}% (${rssi} dBm)`;
                } else {
                    // If signal element doesn't exist, add it to SSID display
                    const ssidElement = document.getElementById('deviceSsid');
                    if (wifiData.signal_strength && wifiData.rssi) {
                        ssidElement.textContent = `${wifiData.ssid} (Signal: ${wifiData.signal_strength}%, ${wifiData.rssi} dBm)`;
                    }
                }
            }
        } else {
            console.error(`[WIFI-STATUS] Failed to fetch device info: ${response.status}`);
        }
    } catch (error) {
        console.error('[WIFI-STATUS] Error fetching device info:', error);
        // Don't show error to user - WiFi status is not critical
    }
}

// Load and display Mur Gateway config
async function loadMurGateway() {
    try {
        const response = await fetch(`/api/device/${currentDevice}/mur-gateway`);
        if (!response.ok) return;
        const data = await response.json();
        const ip = data.mur_gateway_ip || '';
        const port = data.mur_gateway_port || 4000;
        deviceMurGatewayIp = ip;
        deviceMurGatewayPort = port;
        const display = ip ? `${ip}${port ? ':' + port : ''}` : '—';
        document.getElementById('murGatewayDisplay').textContent = display;
    } catch (e) {
        // silently ignore — not critical
    }
}

window.showMurGatewayEdit = function() {
    const row = document.getElementById('murGatewayEditRow');
    row.style.display = 'flex';
};

window.hideMurGatewayEdit = function() {
    document.getElementById('murGatewayEditRow').style.display = 'none';
};

window.saveMurGateway = async function() {
    const ip = document.getElementById('murGatewayIpInput').value.trim();
    const portRaw = document.getElementById('murGatewayPortInput').value.trim();
    if (!ip) { showMessage('Enter a Mur Gateway IP', 'error'); return; }
    const payload = { mur_gateway_ip: ip };
    if (portRaw) payload.mur_gateway_port = parseInt(portRaw);
    try {
        const response = await fetch(`/api/device/${currentDevice}/mur-gateway`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        if (response.ok) {
            showMessage('Mur Gateway updated', 'success');
            hideMurGatewayEdit();
            loadMurGateway();
        } else {
            showMessage('Failed to set Mur Gateway', 'error');
        }
    } catch (e) {
        showMessage('Error setting Mur Gateway', 'error');
    }
};

// --- Scene Trigger per-device ---

async function loadSceneTriggerConfig() {
    try {
        const response = await fetch(`/api/device/${currentDevice}/mur-gateway`);
        if (!response.ok) return;
        const data = await response.json();
        const stn = data.scene_trigger_name || '';
        document.getElementById('sceneTriggerDisplay').textContent = stn || '--';
        // Check for scene/trigger value mismatches
        if (stn) {
            checkSceneTriggerMismatch(stn);
        } else {
            const warningDiv = document.getElementById('sceneTriggerWarnings');
            if (warningDiv) warningDiv.style.display = 'none';
        }
    } catch (e) {
        // not critical
    }
    // Pre-populate the button trigger datalist (On/Off triggers)
    populateButtonTriggerDatalist();
}

async function checkSceneTriggerMismatch(triggerName) {
    const warningDiv = document.getElementById('sceneTriggerWarnings');
    if (!warningDiv) return;

    try {
        // Get scene names from the device
        const scenesResp = await fetch(`/api/device/${currentDevice}/scenes`);
        if (!scenesResp.ok) return;
        const scenesData = await scenesResp.json();
        const sceneNames = new Set(Object.keys(scenesData.scenes || {}));

        // Get trigger definition with values from the gateway
        const triggers = await fetchTriggerData();
        const triggerDef = triggers.find(t => t.name === triggerName);

        if (!triggerDef) {
            warningDiv.innerHTML = `<div style="color:#856404;background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:6px 10px;font-size:12px;">Trigger '${triggerName}' not found in gateway</div>`;
            warningDiv.style.display = 'block';
            return;
        }

        if (triggerDef.type !== 'Discrete') {
            warningDiv.innerHTML = `<div style="color:#856404;background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:6px 10px;font-size:12px;">Trigger '${triggerName}' is type '${triggerDef.type}', expected Discrete</div>`;
            warningDiv.style.display = 'block';
            return;
        }

        const triggerValues = new Set((triggerDef.range && triggerDef.range.values) || []);
        if (triggerValues.size === 0) {
            warningDiv.style.display = 'none';
            return;
        }

        // Convert trigger values to strings for comparison (they may be ints)
        const triggerStrValues = new Set(Array.from(triggerValues).map(String));

        const warnings = [];

        // Trigger values with no matching scene (will fall back to default)
        const noScene = Array.from(triggerStrValues).filter(v => !sceneNames.has(v));
        if (noScene.length > 0) {
            const defaultScene = scenesData.default_scene || '(none)';
            warnings.push(`Trigger values with no matching scene (will use default '${defaultScene}'): ${noScene.join(', ')}`);
        }

        if (warnings.length > 0) {
            warningDiv.innerHTML = warnings.map(w =>
                `<div style="color:#856404;background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:6px 10px;font-size:12px;margin-bottom:4px;">${w}</div>`
            ).join('');
            warningDiv.style.display = 'block';
        } else {
            warningDiv.innerHTML = `<div style="color:#155724;background:#d4edda;border:1px solid #28a745;border-radius:4px;padding:6px 10px;font-size:12px;">Scene trigger values match scene names</div>`;
            warningDiv.style.display = 'block';
        }
    } catch (e) {
        // best effort
        console.warn('Scene trigger mismatch check failed:', e);
    }
}

async function populateButtonTriggerDatalist() {
    if (!deviceMurGatewayIp) return;
    try {
        const names = await fetchTriggerNamesByType('On/Off');
        const dl = document.getElementById('buttonTriggerDatalist');
        if (dl) {
            dl.innerHTML = '';
            names.forEach(n => { const o = document.createElement('option'); o.value = n; dl.appendChild(o); });
        }
    } catch (e) { /* ignore */ }
}

window.showSceneTriggerEdit = function() {
    document.getElementById('sceneTriggerEditRow').style.display = 'flex';
    populateSceneTriggerDatalist();
};

window.hideSceneTriggerEdit = function() {
    document.getElementById('sceneTriggerEditRow').style.display = 'none';
};

async function populateSceneTriggerDatalist() {
    if (!deviceMurGatewayIp) return;
    try {
        const names = await fetchTriggerNamesByType('Discrete');
        const dl = document.getElementById('sceneTriggerDatalist');
        dl.innerHTML = '';
        names.forEach(n => { const o = document.createElement('option'); o.value = n; dl.appendChild(o); });
    } catch (e) { /* ignore */ }
}

window.saveSceneTrigger = async function() {
    const name = document.getElementById('sceneTriggerInput').value.trim();
    if (!name) { showMessage('Enter a trigger name', 'error'); return; }
    try {
        const response = await fetch(`/api/device/${currentDevice}/device-config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ scene_trigger_name: name })
        });
        if (response.ok) {
            showMessage('Scene trigger set', 'success');
            hideSceneTriggerEdit();
            loadSceneTriggerConfig();
        } else { showMessage('Failed to set scene trigger', 'error'); }
    } catch (e) { showMessage('Error setting scene trigger', 'error'); }
};

window.clearSceneTrigger = async function() {
    try {
        const response = await fetch(`/api/device/${currentDevice}/device-config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ scene_trigger_name: '' })
        });
        if (response.ok) {
            showMessage('Scene trigger cleared', 'success');
            hideSceneTriggerEdit();
            loadSceneTriggerConfig();
        } else { showMessage('Failed to clear scene trigger', 'error'); }
    } catch (e) { showMessage('Error clearing scene trigger', 'error'); }
};

window.refreshSceneTriggerList = function() {
    cachedTriggerData = null;
    populateSceneTriggerDatalist();
    populateButtonTriggerDatalist();
};

// Set trigger_name / trigger_mode for a track
async function setTrackTriggerConfig(track, fields, sceneName) {
    try {
        const body = buildSceneTrackPatch(track, fields, sceneName);
        const response = await fetch(`/api/device/${currentDevice}/scenes`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        if (!response.ok) {
            showMessage(`Failed to update trigger config for track ${track}`, 'error');
        }
    } catch (e) {
        showMessage('Error updating trigger config', 'error');
    }
}

// ============================================================================
// Scene Management
// ============================================================================

// Render ALL scenes with their full track configuration
function renderAllScenes(scenesData) {
    const container = document.getElementById('loopsConfig');
    if (!container) return;

    const scenes = scenesData.scenes || {};
    const activeScene = scenesData.active_scene || '';
    const defaultScene = scenesData.default_scene || '';
    const sceneNames = Object.keys(scenes).sort((a, b) => {
        // Active scene first, then default scene, then alphabetical
        if (a === activeScene) return -1;
        if (b === activeScene) return 1;
        if (a === defaultScene) return -1;
        if (b === defaultScene) return 1;
        return a.localeCompare(b);
    });

    if (sceneNames.length === 0) {
        container.innerHTML = '<p>No scenes configured</p>';
        return;
    }

    let html = '';

    sceneNames.forEach(sceneName => {
        const scene = scenes[sceneName];
        const isActive = (sceneName === activeScene);
        const isDefault = (sceneName === defaultScene);
        const borderColor = isActive ? '#28a745' : '#ccc';
        const headerBg = isActive ? '#e8f5e9' : '#f5f5f5';

        // Badges
        let badges = '';
        if (isActive) badges += '<span style="background:#28a745;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;margin-left:6px;">ACTIVE</span>';
        if (isDefault) badges += '<span style="background:#007bff;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;margin-left:4px;">DEFAULT</span>';

        // Action buttons
        let actions = '';
        if (!isActive) actions += `<button onclick="activateScene('${sceneName}')" class="btn btn-success" style="padding:2px 8px;font-size:11px;">Activate</button>`;
        if (!isDefault) actions += `<button onclick="setDefaultScene('${sceneName}')" class="btn btn-primary" style="padding:2px 8px;font-size:11px;">Set Default</button>`;
        if (!isActive) actions += `<button onclick="deleteScene('${sceneName}')" class="btn btn-danger" style="padding:2px 8px;font-size:11px;">Delete</button>`;

        html += `<div class="scene-block" data-scene="${sceneName}" style="border:2px solid ${borderColor};border-radius:8px;margin-bottom:16px;overflow:hidden;">`;
        html += `<div style="background:${headerBg};padding:8px 14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;">`;
        html += `<div><strong style="font-size:15px;">${sceneName}</strong>${badges}</div>`;
        html += `<div style="display:flex;align-items:center;gap:6px;">`;
        html += `<label style="font-size:13px;color:#555;">Volume:</label>`;
        html += `<input type="range" class="scene-volume-slider" data-scene="${sceneName}" min="0" max="100" value="${scene.global_volume || 0}" style="width:120px;">`;
        html += `<span class="scene-volume-value" data-scene="${sceneName}">${scene.global_volume || 0}%</span>`;
        if (actions) html += `<span style="margin-left:10px;display:inline-flex;gap:4px;">${actions}</span>`;
        html += `</div></div>`;

        // Tracks
        html += `<div style="padding:8px 14px;">`;
        const tracks = scene.tracks || [];
        tracks.forEach(track => {
            const isTrigger = track.mode === 'trigger';
            const activeClass = track.active ? 'playing' : 'stopped';
            const filename = track.file_path ? track.file_path.split('/').pop() : 'No file';
            const triggerName = track.trigger_name || '';
            const triggerMode = track.trigger_mode || 'momentary';
            const enableBtnLabel = track.active ? 'ON' : 'OFF';

            const triggerDisplayName = triggerName || '(none)';
            const triggerSection = isTrigger ? `
                <div class="track-trigger-config" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:4px;">
                    <span class="trigger-name-display" data-scene="${sceneName}" data-track="${track.track}"
                          style="font-size:13px;color:#555;min-width:100px;">${triggerDisplayName}</span>
                    <button class="trigger-name-edit-btn" data-scene="${sceneName}" data-track="${track.track}"
                            style="padding:2px 8px;font-size:12px;border:1px solid #aaa;border-radius:4px;cursor:pointer;background:#eee;color:#333;">Edit</button>
                    <div class="trigger-name-edit-row" data-scene="${sceneName}" data-track="${track.track}" style="display:none;align-items:center;gap:4px;">
                        <input type="text" class="trigger-name-input" data-scene="${sceneName}" data-track="${track.track}"
                               placeholder="trigger name" value="${triggerName}"
                               list="triggerList-${sceneName}-${track.track}" autocomplete="off"
                               style="width:200px;padding:3px 6px;border:1px solid #ccc;border-radius:4px;font-size:13px;">
                        <datalist id="triggerList-${sceneName}-${track.track}"></datalist>
                        <button class="trigger-name-ok-btn" data-scene="${sceneName}" data-track="${track.track}"
                                style="padding:2px 8px;font-size:12px;border:1px solid #2a5298;border-radius:4px;cursor:pointer;background:#2a5298;color:#fff;">OK</button>
                        <button class="trigger-name-cancel-btn" data-scene="${sceneName}" data-track="${track.track}"
                                style="padding:2px 8px;font-size:12px;border:1px solid #aaa;border-radius:4px;cursor:pointer;background:#eee;color:#333;">Cancel</button>
                        <button class="trigger-list-refresh-btn" data-scene="${sceneName}" data-track="${track.track}"
                                style="padding:2px 6px;font-size:11px;border:1px solid #aaa;border-radius:4px;cursor:pointer;background:#f0f0f0;color:#555;"
                                title="Refresh trigger list">&#x21bb;</button>
                    </div>
                    <button class="trigger-mode-opt ${triggerMode === 'momentary' ? 'active' : ''}"
                            data-scene="${sceneName}" data-track="${track.track}" data-tmode="momentary"
                            style="padding:3px 8px;font-size:12px;border:1px solid #aaa;border-radius:4px;cursor:pointer;
                                   background:${triggerMode === 'momentary' ? '#2a5298' : '#eee'};color:${triggerMode === 'momentary' ? '#fff' : '#333'};">Momentary</button>
                    <button class="trigger-mode-opt ${triggerMode === 'oneshot' ? 'active' : ''}"
                            data-scene="${sceneName}" data-track="${track.track}" data-tmode="oneshot"
                            style="padding:3px 8px;font-size:12px;border:1px solid #aaa;border-radius:4px;cursor:pointer;
                                   background:${triggerMode === 'oneshot' ? '#2a5298' : '#eee'};color:${triggerMode === 'oneshot' ? '#fff' : '#333'};">Oneshot</button>
                </div>` : '';

            html += `
                <div class="modal-loop-item ${activeClass}" data-scene="${sceneName}" data-track="${track.track}" style="padding:8px;margin-bottom:6px;">
                    <div style="font-weight:600;font-size:14px;margin-bottom:6px;">Track ${track.track}</div>
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px;">
                        <button class="track-enable-btn ${activeClass}" data-scene="${sceneName}" data-track="${track.track}" data-active="${track.active}"
                                title="${track.active ? 'Disable' : 'Enable'} Track ${track.track}">
                            ${enableBtnLabel}
                        </button>
                        <div class="track-mode-selector">
                            <button class="track-mode-option ${track.mode === 'loop' ? 'active' : ''}"
                                    data-scene="${sceneName}" data-track="${track.track}" data-mode="loop" title="Loop">↻ Loop</button>
                            <button class="track-mode-option ${track.mode === 'trigger' ? 'active' : ''}"
                                    data-scene="${sceneName}" data-track="${track.track}" data-mode="trigger" title="Trigger">▶ Trig</button>
                        </div>
                        <span class="loop-file clickable" data-scene="${sceneName}" data-track="${track.track}"
                              title="Click to change file" style="color:#2a5298;text-decoration:underline;cursor:pointer;">${filename}</span>
                    </div>
                    ${triggerSection}
                    <div style="display:flex;align-items:center;gap:6px;margin-top:4px;">
                        <label style="font-size:12px;color:#666;white-space:nowrap;">Volume:</label>
                        <input type="range" class="track-volume-slider" data-scene="${sceneName}" data-track="${track.track}"
                               min="0" max="100" value="${track.volume}" style="flex:1;max-width:200px;">
                        <span class="track-volume-value" style="min-width:35px;font-size:13px;">${track.volume}%</span>
                    </div>
                </div>`;
        });

        // Button trigger field — same Edit/OK/Cancel pattern as track triggers
        const btnTrigger = scene.button_trigger || '';
        const btnTriggerDisplay = btnTrigger || '(none)';
        html += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid #eee;display:flex;align-items:center;gap:6px;flex-wrap:wrap;">`;
        html += `<label style="font-size:12px;color:#666;white-space:nowrap;">Trigger:</label>`;
        html += `<span class="btn-trigger-display" data-scene="${sceneName}" style="font-size:13px;color:#555;min-width:80px;">${btnTriggerDisplay}</span>`;
        html += `<button class="btn-trigger-edit-btn" data-scene="${sceneName}" style="padding:2px 8px;font-size:12px;border:1px solid #aaa;border-radius:4px;cursor:pointer;background:#eee;color:#333;">Edit</button>`;
        html += `<div class="btn-trigger-edit-row" data-scene="${sceneName}" style="display:none;align-items:center;gap:4px;">`;
        html += `<input type="text" class="btn-trigger-input" data-scene="${sceneName}" placeholder="trigger name" value="${btnTrigger}" list="buttonTriggerDatalist" autocomplete="off" style="width:200px;padding:3px 6px;border:1px solid #ccc;border-radius:4px;font-size:13px;">`;
        html += `<button class="btn-trigger-ok-btn" data-scene="${sceneName}" style="padding:2px 8px;font-size:12px;border:1px solid #2a5298;border-radius:4px;cursor:pointer;background:#2a5298;color:#fff;">OK</button>`;
        html += `<button class="btn-trigger-cancel-btn" data-scene="${sceneName}" style="padding:2px 8px;font-size:12px;border:1px solid #aaa;border-radius:4px;cursor:pointer;background:#eee;color:#333;">Cancel</button>`;
        html += `<button class="btn-trigger-refresh-btn" data-scene="${sceneName}" style="padding:2px 6px;font-size:11px;border:1px solid #aaa;border-radius:4px;cursor:pointer;background:#f0f0f0;color:#555;" title="Refresh trigger list">&#x21bb;</button>`;
        html += `</div>`;
        html += `</div>`;

        html += `</div></div>`;
    });

    container.innerHTML = html;
    attachAllSceneHandlers();
}

// Attach handlers for all scene controls (scene-aware version)
function attachAllSceneHandlers() {
    // Scene volume sliders
    document.querySelectorAll('.scene-volume-slider').forEach(slider => {
        const sceneName = slider.dataset.scene;
        const valueSpan = document.querySelector(`.scene-volume-value[data-scene="${sceneName}"]`);

        slider.addEventListener('input', function() {
            if (valueSpan) valueSpan.textContent = `${this.value}%`;
            setSceneVolume(this.value, false, sceneName);
        });

        const sendFinal = async () => {
            await setSceneVolume(slider.value, true, sceneName);
        };
        slider.addEventListener('mouseup', sendFinal);
        slider.addEventListener('touchend', sendFinal);
        slider.addEventListener('keyup', sendFinal);
        slider.addEventListener('blur', sendFinal);
    });

    // Track enable/disable
    document.querySelectorAll('.track-enable-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const track = parseInt(this.dataset.track);
            const sceneName = this.dataset.scene;
            const isActive = this.dataset.active === 'true';
            await controlTrack(track, isActive ? 'stop' : 'start', sceneName);
        });
    });

    // Track mode
    document.querySelectorAll('.track-mode-option').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            if (this.classList.contains('active')) return;
            await setTrackMode(parseInt(this.dataset.track), this.dataset.mode, this.dataset.scene);
        });
    });

    // Track volume sliders
    document.querySelectorAll('.track-volume-slider').forEach(slider => {
        const sceneName = slider.dataset.scene;

        slider.addEventListener('input', function() {
            const valueDisplay = this.nextElementSibling;
            if (valueDisplay) valueDisplay.textContent = `${this.value}%`;
            setTrackVolume(parseInt(this.dataset.track), parseInt(this.value), false, sceneName);
        });

        const sendFinal = async () => {
            await setTrackVolume(parseInt(slider.dataset.track), parseInt(slider.value), true, sceneName);
        };
        slider.addEventListener('mouseup', sendFinal);
        slider.addEventListener('touchend', sendFinal);
        slider.addEventListener('keyup', sendFinal);
        slider.addEventListener('blur', sendFinal);
    });

    // Track file selection
    document.querySelectorAll('.loop-file.clickable').forEach(el => {
        el.addEventListener('click', async function(e) {
            e.stopPropagation();
            await selectTrackFile(parseInt(this.dataset.track), this.dataset.scene);
        });
    });

    // Trigger name edit/OK/cancel
    document.querySelectorAll('.trigger-name-edit-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const {track, scene} = this.dataset;
            const editRow = document.querySelector(`.trigger-name-edit-row[data-scene="${scene}"][data-track="${track}"]`);
            const display = document.querySelector(`.trigger-name-display[data-scene="${scene}"][data-track="${track}"]`);
            this.style.display = 'none';
            if (display) display.style.display = 'none';
            if (editRow) {
                editRow.style.display = 'flex';
                const input = editRow.querySelector('.trigger-name-input');
                input.value = '';  // clear so datalist shows all options (current value shown in display span)
                input.focus();
                // Populate datalist from gateway in background (On/Off triggers only)
                fetchTriggerNamesByType('On/Off').then(names => {
                    const datalist = document.getElementById(`triggerList-${scene}-${track}`);
                    if (datalist && names.length > 0) {
                        datalist.innerHTML = names.map(n => `<option value="${n}">`).join('');
                    }
                });
            }
        });
    });

    document.querySelectorAll('.trigger-name-ok-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const {track, scene} = this.dataset;
            const input = document.querySelector(`.trigger-name-input[data-scene="${scene}"][data-track="${track}"]`);
            const editRow = document.querySelector(`.trigger-name-edit-row[data-scene="${scene}"][data-track="${track}"]`);
            await setTrackTriggerConfig(parseInt(track), { trigger_name: input.value.trim() }, scene);
            if (editRow) editRow.style.display = 'none';
            setTimeout(loadDeviceData, 400);
        });
    });

    document.querySelectorAll('.trigger-name-cancel-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const {track, scene} = this.dataset;
            const editRow = document.querySelector(`.trigger-name-edit-row[data-scene="${scene}"][data-track="${track}"]`);
            const display = document.querySelector(`.trigger-name-display[data-scene="${scene}"][data-track="${track}"]`);
            const editBtn = document.querySelector(`.trigger-name-edit-btn[data-scene="${scene}"][data-track="${track}"]`);
            if (editRow) editRow.style.display = 'none';
            if (display) display.style.display = '';
            if (editBtn) editBtn.style.display = '';
        });
    });

    document.querySelectorAll('.trigger-name-input').forEach(input => {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                const okBtn = document.querySelector(`.trigger-name-ok-btn[data-scene="${this.dataset.scene}"][data-track="${this.dataset.track}"]`);
                if (okBtn) okBtn.click();
            } else if (e.key === 'Escape') {
                const cancelBtn = document.querySelector(`.trigger-name-cancel-btn[data-scene="${this.dataset.scene}"][data-track="${this.dataset.track}"]`);
                if (cancelBtn) cancelBtn.click();
            }
        });
    });

    // Trigger list refresh button (On/Off triggers only for track triggers)
    document.querySelectorAll('.trigger-list-refresh-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const {track, scene} = this.dataset;
            const names = await fetchTriggerNamesByType('On/Off', true);
            const datalist = document.getElementById(`triggerList-${scene}-${track}`);
            if (datalist) {
                datalist.innerHTML = names.map(n => `<option value="${n}">`).join('');
            }
        });
    });

    // Trigger mode buttons
    document.querySelectorAll('.trigger-mode-opt').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            if (this.classList.contains('active')) return;
            await setTrackTriggerConfig(parseInt(this.dataset.track), { trigger_mode: this.dataset.tmode }, this.dataset.scene);
            setTimeout(loadDeviceData, 400);
        });
    });

    // Per-scene button trigger: Edit button
    document.querySelectorAll('.btn-trigger-edit-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const scene = this.dataset.scene;
            const display = document.querySelector(`.btn-trigger-display[data-scene="${scene}"]`);
            const editRow = document.querySelector(`.btn-trigger-edit-row[data-scene="${scene}"]`);
            this.style.display = 'none';
            if (display) display.style.display = 'none';
            if (editRow) {
                editRow.style.display = 'flex';
                const input = editRow.querySelector('.btn-trigger-input');
                input.value = '';  // clear so datalist shows all options
                input.focus();
                // Populate datalist from gateway in background (On/Off triggers)
                fetchTriggerNamesByType('On/Off').then(names => {
                    const dl = document.getElementById('buttonTriggerDatalist');
                    if (dl && names.length > 0) {
                        dl.innerHTML = names.map(n => `<option value="${n}">`).join('');
                    }
                });
            }
        });
    });

    // Per-scene button trigger: OK button
    document.querySelectorAll('.btn-trigger-ok-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const scene = this.dataset.scene;
            const input = document.querySelector(`.btn-trigger-input[data-scene="${scene}"]`);
            const value = input ? input.value.trim() : '';
            try {
                const body = buildScenePatch({ button_trigger: value }, scene);
                const response = await fetch(`/api/device/${currentDevice}/scenes`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (response.ok) {
                    showMessage(`Button trigger ${value ? "'" + value + "'" : 'cleared'} for scene '${scene}'`, 'success');
                    setTimeout(loadDeviceData, 400);
                } else {
                    showMessage('Failed to set button trigger', 'error');
                }
            } catch (err) {
                showMessage('Error setting button trigger', 'error');
            }
        });
    });

    // Per-scene button trigger: Cancel button
    document.querySelectorAll('.btn-trigger-cancel-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const scene = this.dataset.scene;
            const editRow = document.querySelector(`.btn-trigger-edit-row[data-scene="${scene}"]`);
            const display = document.querySelector(`.btn-trigger-display[data-scene="${scene}"]`);
            const editBtn = document.querySelector(`.btn-trigger-edit-btn[data-scene="${scene}"]`);
            if (editRow) editRow.style.display = 'none';
            if (display) display.style.display = '';
            if (editBtn) editBtn.style.display = '';
        });
    });

    // Per-scene button trigger: Enter/Escape key handling
    document.querySelectorAll('.btn-trigger-input').forEach(input => {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                const okBtn = document.querySelector(`.btn-trigger-ok-btn[data-scene="${this.dataset.scene}"]`);
                if (okBtn) okBtn.click();
            } else if (e.key === 'Escape') {
                const cancelBtn = document.querySelector(`.btn-trigger-cancel-btn[data-scene="${this.dataset.scene}"]`);
                if (cancelBtn) cancelBtn.click();
            }
        });
    });

    // Per-scene button trigger: Refresh button (On/Off triggers)
    document.querySelectorAll('.btn-trigger-refresh-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const names = await fetchTriggerNamesByType('On/Off', true);
            const dl = document.getElementById('buttonTriggerDatalist');
            if (dl) {
                dl.innerHTML = names.map(n => `<option value="${n}">`).join('');
            }
        });
    });
}

async function sceneAction(action, name) {
    if (!currentDevice) return;
    try {
        const response = await fetch(`/api/device/${currentDevice}/scene`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ action, name })
        });
        const data = await response.json();
        if (data.success) {
            showMessage(data.message || `Scene ${action} succeeded`, 'success');
            await loadDeviceData();
        } else {
            showMessage(data.error || `Scene ${action} failed`, 'error');
        }
    } catch (error) {
        console.error(`Scene action '${action}' error:`, error);
        showMessage(`Error: ${error.message}`, 'error');
    }
}

async function activateScene(name) {
    await sceneAction('activate', name);
}

async function setDefaultScene(name) {
    await sceneAction('set_default', name);
}

async function deleteScene(name) {
    if (!confirm(`Delete scene "${name}"?`)) return;
    await sceneAction('delete', name);
}

async function createScene() {
    const input = document.getElementById('newSceneName');
    const name = (input.value || '').trim();
    if (!name) {
        showMessage('Enter a scene name', 'error');
        return;
    }
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
        showMessage('Scene name must be alphanumeric, hyphens, underscores only', 'error');
        return;
    }
    await sceneAction('create', name);
    input.value = '';
}

// Clean up on page unload
window.addEventListener('beforeunload', () => {
    stopAutoRefresh();
});
