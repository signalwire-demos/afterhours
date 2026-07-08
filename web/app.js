/**
 * ===============================================================================
 * Wire Heating and Air - After-Hours Service Frontend
 * ===============================================================================
 *
 * Handles WebRTC connection to the after-hours service agent and displays
 * service requests in real-time. Uses @signalwire/js v4.
 *
 * ===============================================================================
 */

// -------------------------------------------------------------------------------
// Global State
// -------------------------------------------------------------------------------

let client = null;
let call = null;
let currentToken = null;
let currentDestination = null;
let isConnected = false;

// v4: track every RxJS Subscription so teardown can unsubscribe them all
let subscriptions = [];
let remoteVideoEl = null;
let lastRemoteSig = '';
let teardownDone = false;


// -------------------------------------------------------------------------------
// DOM Element References
// -------------------------------------------------------------------------------

const videoContainer = document.getElementById('video-container');
const connectBtn = document.getElementById('connect-btn');
const disconnectBtn = document.getElementById('disconnect-btn');
const statusEl = document.getElementById('status');
const eventLogEl = document.getElementById('event-log');
const requestsContainer = document.getElementById('requests-container');
const emergencyCountEl = document.getElementById('emergency-count');
const totalCountEl = document.getElementById('total-count');


// -------------------------------------------------------------------------------
// v4 helpers
// -------------------------------------------------------------------------------

function track(sub) {
    if (sub) subscriptions.push(sub);
    return sub;
}

function streamSignature(stream) {
    return stream.getTracks().map(t => t.kind + ':' + t.id).sort().join(',');
}

// Hardened token fetch: tolerate the FastAPI tuple-return array shape and
// validate the payload so a bad response fails loudly.
async function fetchGuestToken() {
    const resp = await fetch('/get_token');
    let data = await resp.json();
    if (Array.isArray(data)) data = data[0] || {};
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    if (!data.token || !data.address) throw new Error('Token response missing token/address');
    return data;
}

// Gate the dial on the client connecting (replays synchronously; never errors
// on bad creds -> needs a timeout).
function waitForConnected(swClient, timeoutMs) {
    return new Promise((resolve, reject) => {
        let settled = false;
        let sub = null;
        const timer = setTimeout(() => {
            if (settled) return;
            settled = true;
            if (sub) { try { sub.unsubscribe(); } catch (e) {} }
            reject(new Error('Timed out waiting for SignalWire connection'));
        }, timeoutMs);
        sub = swClient.isConnected$.subscribe(connected => {
            if (connected && !settled) {
                settled = true;
                clearTimeout(timer);
                setTimeout(() => { if (sub) { try { sub.unsubscribe(); } catch (e) {} } }, 0);
                resolve();
            }
        });
    });
}

// Render the remote (Sigmond avatar) stream ourselves. Leave it UNMUTED (carries
// the remote audio). Re-attach on track-set change.
function attachRemoteStream(stream) {
    if (!stream) return;
    if (!videoContainer) return;

    const placeholder = videoContainer.querySelector('.placeholder');
    if (placeholder) placeholder.style.display = 'none';

    if (!remoteVideoEl) {
        remoteVideoEl = document.createElement('video');
        remoteVideoEl.autoplay = true;
        remoteVideoEl.playsInline = true;
        remoteVideoEl.setAttribute('playsinline', '');
        remoteVideoEl.style.width = '100%';
        remoteVideoEl.style.height = '100%';
        remoteVideoEl.style.objectFit = 'cover';
        videoContainer.appendChild(remoteVideoEl);
    }

    const sig = streamSignature(stream);
    if (sig !== lastRemoteSig) {
        lastRemoteSig = sig;
        remoteVideoEl.srcObject = stream;
        remoteVideoEl.play().catch(e => console.log('Remote video play blocked:', e.message));
    }
}


// -------------------------------------------------------------------------------
// Connection Functions (v4)
// -------------------------------------------------------------------------------

async function connect() {
    if (isConnected) {
        logEvent('system', 'Already connected');
        return;
    }

    // Reset per-connection state
    teardownDone = false;
    subscriptions = [];
    remoteVideoEl = null;
    lastRemoteSig = '';

    updateStatus('connecting', 'Getting token...');
    logEvent('system', 'Fetching authentication token...');

    try {
        const tokenData = await fetchGuestToken();
        currentToken = tokenData.token;
        currentDestination = tokenData.address;

        logEvent('system', `Token received, destination: ${currentDestination}`);
        updateStatus('connecting', 'Initializing client...');

        const SW = window.SignalWire;
        if (!SW || typeof SW.SignalWire !== 'function') {
            throw new Error('SignalWire v4 SDK not loaded');
        }

        // v4: constructor auto-connects; guest SAT via StaticCredentialProvider
        client = new SW.SignalWire(new SW.StaticCredentialProvider({ token: currentToken }));

        // v4: surface SDK errors/warnings (replaces logLevel: 'debug')
        track(client.errors$.subscribe(e => logEvent('error', `SDK error: ${e && e.message || e}`)));
        track(client.warnings$.subscribe(w => console.warn('SDK warning:', w && w.code, w && w.message)));

        await waitForConnected(client, 15000);
        logEvent('system', 'Client initialized');

        updateStatus('connecting', 'Dialing agent...');

        // No vision on this agent -> receive-only avatar video, no camera prompt.
        call = await client.dial(currentDestination, {
            audio: true,
            video: false,
            receiveAudio: true,
            receiveVideo: true,
            userVariables: {
                userName: 'Web Client',
                interface: 'web-ui-v4'
            }
        });

        logEvent('system', 'Call initiated, waiting for connection...');

        // Remote avatar video + audio
        track(call.remoteStream$.subscribe(stream => attachRemoteStream(stream)));

        // Single user_event subscription (handleUserEvent unwraps .event/.params)
        track(call.subscribe('user_event').subscribe(evt => {
            const params = (evt && evt.params) ? evt.params : evt;
            handleUserEvent(params);
        }));

        // Call lifecycle
        track(call.status$.subscribe({
            next: (status) => {
                console.log('call.status:', status);
                if (status === 'connected') {
                    onConnected();
                } else if (status === 'disconnected' || status === 'failed' || status === 'destroyed') {
                    logEvent('system', 'Disconnected from agent');
                    handleDisconnect();
                }
            },
            complete: () => handleDisconnect()
        }));

    } catch (error) {
        console.error('Connection error:', error);
        logEvent('error', `Connection failed: ${error.message}`);
        updateStatus('error', 'Connection failed');
        handleDisconnect();
    }
}


function onConnected() {
    logEvent('system', 'Connected to Wire Heating and Air');
    updateStatus('connected', 'Connected');
    isConnected = true;
    updateButtons();

    const placeholder = videoContainer.querySelector('.placeholder');
    if (placeholder) placeholder.style.display = 'none';
}


async function disconnect() {
    if (!isConnected && !call) {
        logEvent('system', 'Not connected');
        return;
    }

    logEvent('system', 'Disconnecting...');
    updateStatus('disconnecting', 'Disconnecting...');

    try {
        if (call) {
            await call.hangup();
        }
    } catch (error) {
        console.error('Disconnect error:', error);
    }

    handleDisconnect();
}


function handleDisconnect() {
    if (teardownDone) return;
    teardownDone = true;

    // Unsubscribe every tracked RxJS subscription
    subscriptions.forEach(s => { try { s.unsubscribe(); } catch (e) {} });
    subscriptions = [];

    if (client) {
        try { client.disconnect(); } catch (e) {}
        client = null;
    }
    call = null;
    isConnected = false;
    remoteVideoEl = null;
    lastRemoteSig = '';

    videoContainer.innerHTML = `
        <div class="placeholder">
            <div class="placeholder-content">
                <div class="placeholder-icon">HVAC</div>
                <p>Click Connect to speak with our after-hours service</p>
            </div>
        </div>
    `;

    updateStatus('disconnected', 'Disconnected');
    updateButtons();
}


// -------------------------------------------------------------------------------
// User Event Handling
// -------------------------------------------------------------------------------

function handleUserEvent(params) {
    console.log('Processing user event:', params);

    let eventData = params;
    if (params && params.params) {
        eventData = params.params;
    }
    if (params && params.event) {
        eventData = params.event;
    }

    if (!eventData || typeof eventData.type !== 'string') {
        console.log('Skipping non-application event:', params);
        return;
    }

    const internalTypes = ['room.joined', 'room.left', 'member.joined', 'member.left', 'playback.started', 'playback.ended'];
    if (internalTypes.includes(eventData.type)) {
        console.log('Skipping internal event type:', eventData.type);
        return;
    }

    const eventType = eventData.type;

    switch (eventType) {
        case 'request_submitted':
            const req = eventData.request;
            const urgency = req.is_emergency ? 'EMERGENCY' : 'Service';
            const issueType = req.issue_type === 'ac_repair' ? 'AC' : 'Heating';
            logEvent('request', `New ${urgency} ${issueType} request: ${req.customer_name} at ${req.service_address}`);
            refreshRequests();
            break;

        default:
            console.log('Unknown event type:', eventType, eventData);
            logEvent('event', `Event: ${eventType}`);
    }
}


// -------------------------------------------------------------------------------
// Service Requests Display
// -------------------------------------------------------------------------------

async function refreshRequests() {
    try {
        const resp = await fetch('/api/requests');
        const data = await resp.json();

        displayRequests(data.requests);
        updateStats(data);
    } catch (error) {
        console.error('Failed to fetch requests:', error);
        logEvent('error', 'Failed to fetch service requests');
    }
}


function displayRequests(requests) {
    const container = requestsContainer;

    if (!requests || requests.length === 0) {
        container.innerHTML = `
            <div class="no-requests">
                <p>No service requests yet.</p>
                <p class="hint">Connect to submit an after-hours service request.</p>
            </div>
        `;
        return;
    }

    let html = '';
    for (const req of requests) {
        const issueType = req.issue_type === 'ac_repair' ? 'AC Repair' : 'Heating Repair';
        const urgencyClass = req.is_emergency ? 'emergency' : '';
        const urgencyBadge = req.is_emergency ? '<span class="badge emergency">EMERGENCY</span>' : '<span class="badge">Service Request</span>';
        const ownershipText = req.ownership === 'rent' ? 'Renter' : 'Owner';
        const timeAgo = formatTimeAgo(req.created_at);

        html += `
            <div class="request-card ${urgencyClass}">
                <div class="request-header">
                    <div class="request-type">${issueType}</div>
                    ${urgencyBadge}
                </div>
                <div class="request-details">
                    <div class="request-customer">${escapeHtml(req.customer_name)}</div>
                    <div class="request-address">${escapeHtml(req.service_address)}</div>
                    <div class="request-meta">
                        <span class="request-phone">${escapeHtml(req.callback_primary)}</span>
                        <span class="request-ownership">${ownershipText}</span>
                    </div>
                    ${req.unit_info ? `<div class="request-unit">Unit: ${escapeHtml(req.unit_info)}</div>` : ''}
                    <div class="request-issue">${escapeHtml(req.issue_description)}</div>
                </div>
                <div class="request-footer">
                    <span class="request-id">${req.id}</span>
                    <span class="request-time">${timeAgo}</span>
                </div>
            </div>
        `;
    }

    container.innerHTML = html;
}


function updateStats(data) {
    emergencyCountEl.textContent = data.emergency_count || 0;
    totalCountEl.textContent = data.total_count || 0;
}


// -------------------------------------------------------------------------------
// UI Helper Functions
// -------------------------------------------------------------------------------

function updateStatus(state, text) {
    statusEl.className = `status ${state}`;
    statusEl.querySelector('.status-text').textContent = text;
}


function updateButtons() {
    connectBtn.disabled = isConnected;
    disconnectBtn.disabled = !isConnected;
}


function logEvent(type, message) {
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;

    const timestamp = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="log-time">${timestamp}</span> ${escapeHtml(message)}`;

    eventLogEl.appendChild(entry);
    eventLogEl.scrollTop = eventLogEl.scrollHeight;

    // Keep only last 50 entries
    while (eventLogEl.children.length > 50) {
        eventLogEl.removeChild(eventLogEl.firstChild);
    }
}


function formatTimeAgo(dateStr) {
    const date = new Date(dateStr);
    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);

    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return date.toLocaleDateString();
}


function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}


// -------------------------------------------------------------------------------
// Config Loading
// -------------------------------------------------------------------------------

async function loadConfig() {
    try {
        const resp = await fetch('/api/config');
        const config = await resp.json();

        if (config.phone_number) {
            const phoneDisplay = document.getElementById('phone-display');
            phoneDisplay.innerHTML = `Call us: <a href="tel:${config.phone_number}">${config.phone_number}</a>`;
            phoneDisplay.style.display = 'block';
        }
    } catch (error) {
        console.error('Failed to load config:', error);
    }
}


// -------------------------------------------------------------------------------
// Initialization
// -------------------------------------------------------------------------------

// Buttons are wired via inline onclick= in index.html (connect/disconnect are
// module-global functions), so no addEventListener here.

logEvent('system', 'Wire Heating and Air loaded');
logEvent('system', 'Ready to connect');

// Load config and requests on page load
loadConfig();
refreshRequests();
