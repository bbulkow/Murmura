# Web Interface Debug Testing Guide

## After Flashing the Device

### 1. Access the Web Interface
- Connect to the same network as your ESP32
- Open a web browser on your phone or computer
- Navigate to the device's IP address (e.g., `http://192.168.1.xxx`)

### 2. Open Browser Developer Console
**On Desktop (Chrome/Firefox/Edge):**
- Press `F12` or right-click and select "Inspect"
- Go to the "Console" tab

**On Mobile (Chrome Android):**
- Type `chrome://inspect` in Chrome on a computer
- Enable USB debugging on your phone
- Connect phone via USB
- Open the page on your phone's Chrome
- Click "Inspect" on the computer

**On Mobile (Safari iOS):**
- Enable Web Inspector in Settings > Safari > Advanced
- Connect iPhone to Mac
- Open Safari on Mac > Develop menu > Select your iPhone

### 3. Check Debug Output
The console should show debug messages like:
```
[DEBUG] Script tag started executing at 2025-01-17T10:53:02.123Z
[DEBUG] About to call refreshData() for initial load
[DEBUG] Current document.readyState: loading
[DEBUG] refreshData() called at 2025-01-17T10:53:02.125Z
[DEBUG] Document readyState: loading
[DEBUG] status-content element exists? true
[DEBUG] loops-content element exists? true
[DEBUG] fetchStatus() called at 2025-01-17T10:53:02.126Z
[DEBUG] About to fetch /api/status
[DEBUG] Status response received: 200 OK
[DEBUG] Status data parsed: {mac_address: "...", id: "...", ...}
[DEBUG] Building status HTML
[DEBUG] Status HTML updated successfully
[DEBUG] fetchLoops() called at 2025-01-17T10:53:02.127Z
[DEBUG] About to fetch /api/loops
[DEBUG] Loops response received: 200 OK
[DEBUG] Loops data parsed: {loops: [...], ...}
[DEBUG] Building loops HTML for 3 tracks
[DEBUG] Loops HTML updated successfully
[DEBUG] Initial refreshData() called
[DEBUG] Auto-refresh interval set with ID: 1
[DEBUG] Script finished executing at 2025-01-17T10:53:02.130Z
```

### 4. What to Look For

**If data loads successfully:**
- You should see the debug messages showing successful fetch operations
- The status and loops sections should populate with data
- Auto-refresh should trigger every 5 seconds

**If data doesn't load:**
Look for:
- Missing debug messages (script not executing)
- Error messages in the console
- Network errors (404, 500, etc.)
- JavaScript errors
- Elements not found messages

### 5. Common Issues and Solutions

**Issue: No debug messages at all**
- Hard refresh the page (Ctrl+Shift+R or Cmd+Shift+R)
- Clear browser cache
- Check if JavaScript is enabled

**Issue: Network errors (ERR_CONNECTION_REFUSED)**
- Verify device IP address
- Check WiFi connection
- Ensure HTTP server is running on device

**Issue: 404 errors on /api/status or /api/loops**
- API endpoints not registered properly
- Need to restart the device

**Issue: Elements not found**
- DOM not fully loaded
- Check for HTML syntax errors

### 6. Testing on Mobile
- The interface should be responsive and look good on phones
- Test both portrait and landscape orientations
- Check that all text is readable
- Verify touch targets are large enough
- Test the refresh button works

### 7. Report Back
Please share:
1. Whether data loads on first page visit
2. Any error messages from the console
3. Which debug messages you see (or don't see)
4. Screenshots if possible

## Quick Test Checklist
- [ ] Device flashed successfully
- [ ] Can access web page at device IP
- [ ] Browser console shows debug messages
- [ ] Status section populates with data
- [ ] Loops section shows track information
- [ ] Auto-refresh works (watch for updates every 5 seconds)
- [ ] Page looks good on mobile
- [ ] Refresh button works when clicked
