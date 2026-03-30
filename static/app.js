/* Front-end glue for the chat UI.

This file has four jobs:
1. manage the optional Pyodide worker demo
2. attach a few debug logs so HTMX requests are easier to inspect
3. enhance SSE-rendered code blocks with a "Run in Browser" button
4. maintain the front-end-only script queue panel

If you know jQuery, think of the HTMX event listeners as global delegated
callbacks that fire when requests and SSE messages happen.
*/

const DEFAULT_PYWORKER_TIMEOUT_MS = 5000;
let pyWorker = null;
let pyWorkerGeneration = 0;
let pyWorkerCurrentJob = null;
const sseTraceState = {
    items: [],
    counter: 0,
};
const scriptQueueState = {
    items: [],
    selectedId: '',
    counter: 0,
    boundList: null,
};

function getPyWorkerTimeoutMs() {
    const input = document.getElementById('py-worker-timeout-ms');
    const parsed = Number.parseInt(input?.value, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        return DEFAULT_PYWORKER_TIMEOUT_MS;
    }
    return Math.max(500, parsed);
}

function clearPyWorkerJob(job) {
    if (!job) {
        return;
    }

    if (job.timeoutId) {
        clearTimeout(job.timeoutId);
    }

    if (pyWorkerCurrentJob && pyWorkerCurrentJob.id === job.id) {
        pyWorkerCurrentJob = null;
    }
}

function spawnPyWorker(reason = 'init') {
    const previousWorker = pyWorker;
    const generation = ++pyWorkerGeneration;

    if (previousWorker) {
        try {
            previousWorker.terminate();
        } catch (_err) {
            // Terminating a worker should never break the UI path.
        }
    }

    const worker = new Worker('/static/worker.js');
    pyWorker = worker;
    worker.onmessage = (event) => handlePyWorkerMessage(event, generation);
    worker.onerror = (event) => handlePyWorkerError(event, generation);
    worker.postMessage({ type: 'init', reason });
    return worker;
}

function restartPyWorker(reason) {
    console.warn('[PyWorker] restarting worker', { reason });
    spawnPyWorker(reason);
}

function handlePyWorkerError(event, generation) {
    if (generation !== pyWorkerGeneration) {
        return;
    }

    const job = pyWorkerCurrentJob;
    const message = event?.message || 'Pyodide worker crashed';
    console.error('[PyWorker] error', message, event);

    if (job) {
        clearPyWorkerJob(job);
        renderWorkerResult(job.id, {
            success: false,
            error: message,
        });
    }

    restartPyWorker('error');
}

function handlePyWorkerTimeout(job) {
    if (!job || pyWorkerCurrentJob?.id !== job.id) {
        return;
    }

    clearPyWorkerJob(job);
    renderWorkerResult(job.id, {
        success: false,
        error: `Pyodide worker timed out after ${job.timeoutMs} ms and was restarted.`,
    });

    restartPyWorker('timeout');
}

function handlePyWorkerMessage(event, generation) {
    if (generation !== pyWorkerGeneration) {
        return;
    }

    const { type } = event.data;

    if (type === 'ready') {
        console.log('[PyWorker] ready');
        return;
    }

    if (type === 'result') {
        const job = pyWorkerCurrentJob;
        if (job && job.id === event.data.id) {
            clearPyWorkerJob(job);
        }

        renderWorkerResult(event.data.id, event.data);
    }
}

// Start the worker right away so it can load Pyodide before the user clicks
// any "Run in Browser" button.
spawnPyWorker();

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function nowPerfMs() {
    return typeof performance !== 'undefined' && typeof performance.now === 'function'
        ? Math.round(performance.now())
        : Date.now();
}

function pushSseTrace(stage, extra = {}) {
    const entry = {
        idx: ++sseTraceState.counter,
        stage,
        isoTime: new Date().toISOString(),
        perfMs: nowPerfMs(),
        ...extra,
    };
    sseTraceState.items.push(entry);
    if (sseTraceState.items.length > 200) {
        sseTraceState.items = sseTraceState.items.slice(-200);
    }
    window.__sseTraceLog = [...sseTraceState.items];
    console.debug('[SSE TRACE]', entry);
    return entry;
}

window.dumpSseTrace = () => [...sseTraceState.items];

function collectStreamShimSnapshot(scope = document) {
    const shims = scope?.querySelectorAll ? scope.querySelectorAll('.stream-transport-shim') : [];
    return Array.from(shims).map((shim) => ({
        id: shim.id || '',
        runId: shim.dataset?.runId || '',
        routeSource: shim.dataset?.routeSource || '',
        streamMode: shim.dataset?.streamMode || '',
        sseUrl: shim.dataset?.sseUrl || shim.getAttribute?.('sse_connect') || '',
        hasSseExt: shim.getAttribute?.('hx-ext') || '',
        hasSseConnect: shim.getAttribute?.('sse_connect') || '',
        isConnected: Boolean(shim.isConnected),
        parentId: shim.parentElement?.id || '',
    }));
}

window.dumpStreamShims = () => collectStreamShimSnapshot(document);

function traceStreamShimState(stage, scope = document, extra = {}) {
    const shims = collectStreamShimSnapshot(scope);
    pushSseTrace(stage, {
        shimCount: shims.length,
        shims,
        ...extra,
    });
    return shims;
}

let streamShimObserverBound = false;

function bindStreamShimObserver() {
    if (streamShimObserverBound || !document.body || typeof MutationObserver === 'undefined') {
        return;
    }

    const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            mutation.addedNodes.forEach((node) => {
                if (!(node instanceof Element)) {
                    return;
                }
                if (node.matches?.('.stream-transport-shim') || node.querySelector?.('.stream-transport-shim')) {
                    traceStreamShimState('stream-shim-added', node, {
                        targetId: mutation.target?.id || '',
                    });
                }
            });
            mutation.removedNodes.forEach((node) => {
                if (!(node instanceof Element)) {
                    return;
                }
                if (node.matches?.('.stream-transport-shim') || node.querySelector?.('.stream-transport-shim')) {
                    pushSseTrace('stream-shim-removed', {
                        targetId: mutation.target?.id || '',
                        removedHtmlPreview: (node.outerHTML || '').slice(0, 220),
                    });
                }
            });
        });
    });

    observer.observe(document.body, { childList: true, subtree: true });
    streamShimObserverBound = true;
    traceStreamShimState('stream-shim-observer-bound', document);
}

function traceSseRenderFrame(stage, el, startedPerfMs, extra = {}) {
    requestAnimationFrame(() => {
        const runNode = el?.closest?.('[data-run-id]') || el;
        pushSseTrace(stage, {
            renderDelayMs: Math.max(0, nowPerfMs() - startedPerfMs),
            elementId: el?.id || '',
            runId: runNode?.dataset?.runId || '',
            routeSource: runNode?.dataset?.routeSource || el?.dataset?.routeSource || '',
            isConnected: Boolean(el?.isConnected),
            eventsCount: document.querySelectorAll?.('#events-window .event-log-item').length || 0,
            ...extra,
        });
    });
}

function getScriptQueueList() {
    return document.getElementById('script-queue-list');
}

function getScriptQueueEmpty() {
    return document.getElementById('script-queue-empty');
}

function getScriptQueueApproveButton() {
    return document.getElementById('script-queue-approve-btn');
}

function getScriptQueueCancelButton() {
    return document.getElementById('script-queue-cancel-btn');
}

function getScriptQueueItem(id) {
    return scriptQueueState.items.find((item) => item.id === id) || null;
}

function getSelectedScriptQueueItem() {
    return getScriptQueueItem(scriptQueueState.selectedId);
}

function prettyScriptStatus(status) {
    if (status === 'running') {
        return 'Running';
    }
    if (status === 'completed') {
        return 'Completed';
    }
    if (status === 'failed') {
        return 'Failed';
    }
    if (status === 'cancelled') {
        return 'Cancelled';
    }
    return 'Pending';
}

function prettyScriptVerdict(verdict) {
    if (verdict === 'ok') {
        return 'OK';
    }
    if (verdict === 'bomb') {
        return 'Bomb';
    }
    return 'Pending';
}

function syncScriptQueueControls() {
    const hasSelection = Boolean(getSelectedScriptQueueItem());
    const approveButton = getScriptQueueApproveButton();
    const cancelButton = getScriptQueueCancelButton();

    if (approveButton) {
        approveButton.disabled = !hasSelection;
        approveButton.innerText = hasSelection ? 'Approve Selected' : 'Approve Selected';
    }

    if (cancelButton) {
        cancelButton.disabled = !hasSelection;
    }
}

function renderScriptQueueList() {
    const list = getScriptQueueList();
    const empty = getScriptQueueEmpty();
    if (!list) {
        return;
    }

    const validSelection = scriptQueueState.items.some((item) => item.id === scriptQueueState.selectedId)
        ? scriptQueueState.selectedId
        : (scriptQueueState.items.length ? scriptQueueState.items[scriptQueueState.items.length - 1].id : '');

    scriptQueueState.selectedId = validSelection;
    list.innerHTML = '';

    if (!scriptQueueState.items.length) {
        if (empty) {
            empty.style.display = 'block';
        }
        syncScriptQueueControls();
        return;
    }

    if (empty) {
        empty.style.display = 'none';
    }

    scriptQueueState.items.forEach((item) => {
        const card = document.createElement('article');
        card.className = `script-queue-item${item.id === scriptQueueState.selectedId ? ' is-selected' : ''}`;
        card.dataset.scriptId = item.id;
        card.dataset.scriptStatus = item.status;
        card.dataset.scriptVerdict = item.verdict || 'pending';
        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.setAttribute('aria-pressed', item.id === scriptQueueState.selectedId ? 'true' : 'false');
        card.innerHTML = `
            <div class="script-queue-item-head">
                <div class="script-queue-item-copy">
                    <div class="script-queue-item-title">${escapeHtml(item.title)}</div>
                    <div class="script-queue-item-meta">${escapeHtml(item.source)}</div>
                </div>
                <div class="script-queue-item-badges">
                    <div class="script-queue-item-status">${escapeHtml(prettyScriptStatus(item.status))}</div>
                    <div class="script-queue-item-verdict verdict-${escapeHtml(item.verdict || 'pending')}">${escapeHtml(prettyScriptVerdict(item.verdict))}</div>
                </div>
            </div>
            <pre class="script-queue-item-code"><code>${escapeHtml(item.code)}</code></pre>
            <div id="queued-result-${item.id}" class="queued-result-box" style="display: ${item.resultHtml ? 'block' : 'none'};">${item.resultHtml || ''}</div>
        `;

        const selectItem = () => {
            scriptQueueState.selectedId = item.id;
            renderScriptQueueList();
        };

        card.addEventListener('click', selectItem);
        card.addEventListener('keydown', (evt) => {
            if (evt.key === 'Enter' || evt.key === ' ') {
                evt.preventDefault();
                selectItem();
            }
        });

        list.appendChild(card);
    });

    syncScriptQueueControls();
}

function bindScriptQueuePanel() {
    const list = getScriptQueueList();
    if (list && scriptQueueState.boundList !== list) {
        scriptQueueState.boundList = list;
    }

    renderScriptQueueList();
}

function queueScriptItem(code, { source = 'synthetic-sse', title = 'Queued script', verdict = 'pending' } = {}) {
    const id = `queued-script-${Date.now()}-${++scriptQueueState.counter}`;
    const item = {
        id,
        code,
        source,
        title,
        verdict,
        status: 'pending',
        result: '',
        resultHtml: '',
    };

    scriptQueueState.items.push(item);
    scriptQueueState.selectedId = id;
    renderScriptQueueList();
    return item;
}

function updateQueuedScriptItem(id, patch, { render = true } = {}) {
    const item = getScriptQueueItem(id);
    if (!item) {
        return null;
    }

    Object.assign(item, patch);
    if (render) {
        renderScriptQueueList();
    }
    return item;
}

function removeQueuedScriptItem(id) {
    scriptQueueState.items = scriptQueueState.items.filter((item) => item.id !== id);
    if (scriptQueueState.selectedId === id) {
        scriptQueueState.selectedId = scriptQueueState.items.length ? scriptQueueState.items[scriptQueueState.items.length - 1].id : '';
    }
    renderScriptQueueList();
}

function queueScriptsFromElement(root, sourceLabel = 'sse') {
    const codeBlocks = root?.querySelectorAll ? root.querySelectorAll('pre code') : [];
    const verdict = root?.dataset?.codeVerdict || 'pending';
    const eventTitle = root?.dataset?.codeTitle || 'Queued script';
    const eventSource = root?.dataset?.codeSource || sourceLabel;

    codeBlocks.forEach((block, idx) => {
        const code = (block.innerText || '').trim();
        if (!code) {
            return;
        }

        queueScriptItem(code, {
            source: eventSource,
            title: codeBlocks.length > 1 ? `${eventTitle} ${idx + 1}` : eventTitle,
            verdict,
        });
    });
}

function createMockSseEventNode({ code, title, source, verdict, eventName = 'code.run' }) {
    const node = document.createElement('article');
    node.className = `event-log-item mock-sse-event verdict-${escapeHtml(verdict || 'pending')}`;
    node.dataset.routeSource = source;
    node.dataset.codeVerdict = verdict;
    node.dataset.codeSource = source;
    node.dataset.codeTitle = title;
    node.dataset.sseEventType = eventName;
    node.innerHTML = `
        <div class="event-run-id">${escapeHtml(source)}</div>
        <div class="event-text">
            <div class="mock-sse-head">
                <span class="mock-sse-event-name">${escapeHtml(eventName)}</span>
                <span class="mock-sse-verdict verdict-${escapeHtml(verdict || 'pending')}">${escapeHtml(prettyScriptVerdict(verdict))}</span>
            </div>
            <div class="mock-sse-title">${escapeHtml(title)}</div>
        </div>
        <pre class="mock-sse-code"><code>${escapeHtml(code)}</code></pre>
    `;
    return node;
}

function appendMockSseEventToThirdPanel({ code, title, source, verdict, eventName = 'code.run' }) {
    const windowEl = getEventsWindow();
    if (!windowEl) {
        return null;
    }

    const node = createMockSseEventNode({ code, title, source, verdict, eventName });
    windowEl.appendChild(node);
    queueEventsWindowFollow();
    return node;
}

function dispatchSyntheticSseCodeEvent({ code, title, source, verdict, eventName = 'code.run' }) {
    const node = appendMockSseEventToThirdPanel({ code, title, source, verdict, eventName });
    if (!node) {
        return;
    }

    document.dispatchEvent(new CustomEvent('htmx:sseMessage', {
        detail: {
            elt: node,
            source: { url: `synthetic://${source}` },
        },
    }));
}

function sendCodeToWorker(id, code, button, resultBoxId, pendingLabel = 'Running...') {
    const resultBox = document.getElementById(resultBoxId);
    const timeoutMs = getPyWorkerTimeoutMs();

    if (pyWorkerCurrentJob) {
        console.warn('[PyWorker] busy, refusing a second run request', { currentJobId: pyWorkerCurrentJob.id, requestedId: id });
        if (button) {
            button.innerText = button.classList.contains('run-py-btn') ? 'Run in Browser' : 'Approve Selected';
            button.disabled = false;
        }
        return false;
    }

    if (resultBox) {
        resultBox.style.display = 'none';
        resultBox.className = resultBox.className.replace(/\b(success|error)\b/g, '').trim();
        resultBox.innerHTML = '';
    }

    if (button) {
        button.innerText = pendingLabel;
        button.disabled = true;
    }

    if (!pyWorker) {
        spawnPyWorker('auto-start');
    }

    const job = {
        id,
        code,
        button: button || null,
        resultBoxId,
        timeoutMs,
        timeoutId: null,
        generation: pyWorkerGeneration,
    };

    clearPyWorkerJob(pyWorkerCurrentJob);
    pyWorkerCurrentJob = job;
    job.timeoutId = setTimeout(() => handlePyWorkerTimeout(job), timeoutMs);

    try {
        pyWorker.postMessage({ type: 'execute', id, code });
    } catch (error) {
        clearPyWorkerJob(job);
        renderWorkerResult(id, {
            success: false,
            error: error?.message || String(error),
        });
        restartPyWorker('postMessage-error');
        return false;
    }
    return true;
}

function renderWorkerResult(id, resultData) {
    const target = document.getElementById(`exec-result-${id}`) || document.getElementById(`queued-result-${id}`);
    if (!target) {
        return false;
    }

    const { result, stdout, error, success } = resultData;
    const button = target.previousElementSibling;
    target.style.display = 'block';

    const isQueuedTarget = target.id.startsWith('queued-result-');

    if (success) {
        target.innerHTML = `<strong>Output:</strong><pre>${escapeHtml(stdout || '')}</pre><strong>Result:</strong> <code>${escapeHtml(result || '')}</code>`;
        target.className = isQueuedTarget ? 'py-res-box success queued-result-box' : 'py-res-box success';
    } else {
        target.innerHTML = `<strong>Error:</strong><pre>${escapeHtml(error || '')}</pre>`;
        target.className = isQueuedTarget ? 'py-res-box error queued-result-box' : 'py-res-box error';
    }

    if (button && button.classList.contains('run-py-btn')) {
        button.disabled = false;
        button.innerText = 'Run in Browser';
    }

    const queueItem = target.closest?.('.script-queue-item');
    if (queueItem) {
        const item = getScriptQueueItem(queueItem.dataset.scriptId || id);
        if (item) {
            updateQueuedScriptItem(item.id, {
                status: success ? 'completed' : 'failed',
                result: success ? `Output: ${stdout || ''}${result ? ` | Result: ${result}` : ''}` : `Error: ${error || 'Execution failed'}`,
                resultHtml: target.innerHTML,
            }, { render: false });
            queueItem.dataset.scriptStatus = success ? 'completed' : 'failed';
            const statusLabel = queueItem.querySelector('.script-queue-item-status');
            if (statusLabel) {
                statusLabel.innerText = prettyScriptStatus(success ? 'completed' : 'failed');
            }
        }

        const approveButton = getScriptQueueApproveButton();
        const cancelButton = getScriptQueueCancelButton();
        if (approveButton) {
            approveButton.disabled = !Boolean(getSelectedScriptQueueItem());
            approveButton.innerText = 'Approve Selected';
        }
        if (cancelButton) {
            cancelButton.disabled = !Boolean(getSelectedScriptQueueItem());
        }
    }

    return true;
}

window.submitScriptQueueFromEditor = () => {
    const editor = document.getElementById('script-test-editor');
    const code = editor?.value?.trim() || '';
    if (!code) {
        return;
    }

    if (editor) {
        editor.value = '';
    }

    dispatchSyntheticSseCodeEvent({
        code,
        title: 'Editor SSE code',
        source: 'script-editor',
        verdict: 'pending',
    });
};

window.emitMockSseCodeRunEvent = () => {
    const verdict = Math.random() < 0.5 ? 'ok' : 'bomb';
    const samples = verdict === 'ok'
        ? [
            "value = 6 * 7\nprint('mock ok:', value)\nvalue",
            "items = ['alpha', 'beta', 'gamma']\nprint('ok items:', ', '.join(items))\nlen(items)",
            "def square(n):\n    return n * n\nprint('square:', square(9))\n'clear run'",
        ]
        : [
            "raise RuntimeError('mock bomb from SSE')",
            "result = 1 / 0\nprint(result)",
            "data = {'status': 'bad'}\nprint(data['missing_key'])",
        ];
    const code = samples[Math.floor(Math.random() * samples.length)];
    const title = verdict === 'ok' ? 'Mock SSE code (OK)' : 'Mock SSE code (BOMB)';

    dispatchSyntheticSseCodeEvent({
        code,
        title,
        source: 'mock-sse',
        verdict,
        eventName: 'code.run',
    });
};

window.approveSelectedScript = () => {
    const item = getSelectedScriptQueueItem();
    if (!item) {
        return;
    }

    updateQueuedScriptItem(item.id, {
        status: 'running',
        result: 'Running...',
        resultHtml: '<strong>Running...</strong>',
    }, { render: false });

    const resultBox = document.getElementById(`queued-result-${item.id}`);
    if (resultBox) {
        resultBox.style.display = 'block';
        resultBox.className = 'py-res-box queued-result-box';
        resultBox.innerHTML = '<strong>Running...</strong>';
    }

    const queueItem = document.querySelector(`.script-queue-item[data-script-id="${item.id}"]`);
    if (queueItem) {
        queueItem.dataset.scriptStatus = 'running';
        const statusLabel = queueItem.querySelector('.script-queue-item-status');
        if (statusLabel) {
            statusLabel.innerText = prettyScriptStatus('running');
        }
    }

    const approveButton = getScriptQueueApproveButton();
    const cancelButton = getScriptQueueCancelButton();
    if (approveButton) {
        approveButton.disabled = true;
        approveButton.innerText = 'Running...';
    }
    if (cancelButton) {
        cancelButton.disabled = true;
    }

    const started = sendCodeToWorker(item.id, item.code, null, `queued-result-${item.id}`);
    if (!started) {
        updateQueuedScriptItem(item.id, {
            status: 'pending',
            result: '',
            resultHtml: '',
        });
        const queueItem = document.querySelector(`.script-queue-item[data-script-id="${item.id}"]`);
        if (queueItem) {
            queueItem.dataset.scriptStatus = 'pending';
            const statusLabel = queueItem.querySelector('.script-queue-item-status');
            if (statusLabel) {
                statusLabel.innerText = prettyScriptStatus('pending');
            }
        }
        syncScriptQueueControls();
    }
};

window.cancelSelectedScript = () => {
    const item = getSelectedScriptQueueItem();
    if (!item) {
        return;
    }

    console.log(`${item.code} is cancelled by user event`);
    removeQueuedScriptItem(item.id);
};

window.runPyCode = (id, btn) => {
    // The button sits immediately after the code block. We read the text from
    // the previous sibling and send it to the worker as a task payload.
    const code = btn.previousElementSibling.innerText;
    sendCodeToWorker(id, code, btn, `exec-result-${id}`);
};

function logHtmxEvent(name, detail) {
    try {
        console.debug(`[HTMX] ${name}`, detail || {});
    } catch (_e) {
        // Console logging should never break the app if the browser blocks it.
    }
}

function bubbleMeta(el) {
    if (!el) {
        return null;
    }

    const target = el.dataset?.runId ? el : el.closest?.('[data-run-id]');
    if (!target) {
        return null;
    }

    return {
        runId: target.dataset.runId || '',
        mode: target.dataset.streamMode || 'static',
        pollUrl: target.dataset.pollUrl || '',
        sseUrl: target.dataset.sseUrl || '',
        terminal: target.dataset.terminal === 'true',
        routeSource: target.dataset.routeSource || '',
    };
}

function requestMeta(elt) {
    if (!elt) {
        return {};
    }

    return {
        routeSource: elt.dataset?.routeSource || elt.closest?.('[data-route-source]')?.dataset?.routeSource || '',
        hxGet: elt.getAttribute?.('hx-get') || '',
        hxPost: elt.getAttribute?.('hx-post') || '',
        hxTrigger: elt.getAttribute?.('hx-trigger') || '',
        hxTarget: elt.getAttribute?.('hx-target') || '',
        id: elt.id || '',
        tag: elt.tagName || '',
    };
}

function logBubbleLifecycle(name, el, extra = {}) {
    const meta = bubbleMeta(el);
    if (!meta) {
        return;
    }

    logHtmxEvent(`bubble:${name}`, { ...meta, ...extra });
}

function getEventsWindow() {
    return document.getElementById('events-window');
}


function getEventsList() {
    return document.getElementById('events-list');
}

function isNearBottom(el, threshold = 72) {
    if (!el) return false;

    const remaining = el.scrollHeight - el.scrollTop - el.clientHeight;
    return remaining <= threshold;
}

const debugViewportState = {
    autoFollow: true,
    bound: false,
};

function bindEventsWindow() {
    const windowEl = getEventsWindow();
    if (!windowEl || debugViewportState.bound) {
        return;
    }

    windowEl.addEventListener('scroll', () => {
        debugViewportState.autoFollow = isNearBottom(windowEl);
    });
    debugViewportState.bound = true;
}

let eventsWindowFollowQueued = false;

function queueEventsWindowFollow() {
    if (eventsWindowFollowQueued) {
        return;
    }

    eventsWindowFollowQueued = true;
    requestAnimationFrame(() => {
        eventsWindowFollowQueued = false;
        const windowEl = getEventsWindow();
        if (!windowEl || !debugViewportState.autoFollow) {
            return;
        }

        windowEl.scrollTop = windowEl.scrollHeight;
    });
}

function announceAssistantBubbles(root) {
    const scope = root?.querySelectorAll ? root : document;
    const bubbles = scope.querySelectorAll ? scope.querySelectorAll('.streaming-content[data-run-id]') : [];

    bubbles.forEach((bubble) => {
        if (bubble.dataset.bubbleAnnounced === 'true') {
            return;
        }

        bubble.dataset.bubbleAnnounced = 'true';
        logBubbleLifecycle('mode-selected', bubble);
    });
}

function logDebugControls(root) {
    const controls = root?.id === 'debug-panel-controls'
        ? root
        : root?.querySelector?.('#debug-panel-controls') || document.getElementById('debug-panel-controls');

    if (!controls) {
        return;
    }

    logHtmxEvent('debug:controls', {
        runId: controls.querySelector?.('#debug-run-id')?.value || '',
        mode: controls.dataset.debugMode || 'history',
        paused: controls.dataset.debugPaused === 'true',
        resumeSeq: controls.dataset.resumeSeq || '0',
    });
}

document.addEventListener('htmx:beforeRequest', (evt) => {
    // Helpful when debugging which fragment HTMX is about to fetch.
    const path = evt.detail?.pathInfo?.requestPath || evt.detail?.requestConfig?.path || 'unknown';
    const req = requestMeta(evt.detail?.elt);
    logHtmxEvent('beforeRequest', { path, ...req });
    logBubbleLifecycle('before-request', evt.detail?.elt, { path, ...req });
});

document.addEventListener('htmx:afterRequest', (evt) => {
    const xhr = evt.detail?.xhr;
    logHtmxEvent('afterRequest', {
        status: xhr?.status,
        path: evt.detail?.pathInfo?.requestPath || evt.detail?.requestConfig?.path || 'unknown',
        successful: evt.detail?.successful,
        ...requestMeta(evt.detail?.elt),
    });
    logBubbleLifecycle('after-request', evt.detail?.elt, {
        status: xhr?.status,
        successful: evt.detail?.successful,
        ...requestMeta(evt.detail?.elt),
    });
});

document.addEventListener('htmx:sseOpen', (evt) => {
    // Fires when the HTMX SSE extension opens the server-push connection.
    pushSseTrace('sse-open', {
        sourceUrl: evt.detail?.source?.url || 'unknown',
        ...requestMeta(evt.detail?.elt || evt.detail?.source),
    });
    traceStreamShimState('sse-open-shims', document, {
        sourceUrl: evt.detail?.source?.url || 'unknown',
    });
    logHtmxEvent('sseOpen', { sourceUrl: evt.detail?.source?.url || 'unknown', ...requestMeta(evt.detail?.elt || evt.detail?.source) });
    logBubbleLifecycle('sse-open', evt.detail?.elt || evt.detail?.source, {
        sourceUrl: evt.detail?.source?.url || 'unknown',
    });
});

document.addEventListener('htmx:sseError', (evt) => {
    const readyState = evt.detail?.source?.readyState;
    pushSseTrace('sse-error', {
        sourceUrl: evt.detail?.source?.url || 'unknown',
        readyState,
        ...requestMeta(evt.detail?.elt || evt.detail?.source),
    });
    traceStreamShimState('sse-error-shims', document, {
        sourceUrl: evt.detail?.source?.url || 'unknown',
        readyState,
    });
    const logPayload = { ...requestMeta(evt.detail?.elt || evt.detail?.source), ...(evt.detail || {}), readyState };
    if (readyState === 0 || readyState === 2) {
        console.debug('[HTMX:SSE] reconnect/close', logPayload);
    } else {
        console.warn('[HTMX:SSE] error', logPayload);
    }
    logBubbleLifecycle('sse-error', evt.detail?.elt || evt.detail?.source, evt.detail || {});
});

document.addEventListener('htmx:sseMessage', function (evt) {
    const el = evt.detail.elt;
    const startedPerfMs = nowPerfMs();
    const codeBlocks = el.querySelectorAll ? el.querySelectorAll('pre code') : [];
    const sseInfo = {
        elementId: el?.id || '',
        routeSource: el?.dataset?.routeSource || '',
        sourceUrl: evt.detail?.source?.url || 'unknown',
        codeBlocks: codeBlocks.length,
        isConnected: Boolean(el?.isConnected),
        runId: el?.dataset?.runId || el?.closest?.('[data-run-id]')?.dataset?.runId || '',
        eventType: el?.dataset?.sseEventType || '',
        textPreview: (el?.innerText || '').trim().slice(0, 160),
    };

    pushSseTrace('sse-message-received', sseInfo);
    console.log('[SSE EVENT NEW]', sseInfo);

    codeBlocks.forEach((block, idx) => {
        // Only add a button if the code block has not already been decorated.
        if (!block.parentElement.nextElementSibling?.classList.contains('run-py-btn')) {
            if (block.innerText.includes('print') || block.innerText.length > 5) {
                const btn = document.createElement('button');
                btn.className = 'run-py-btn';
                btn.innerText = 'Run in Browser';

                // Generate a stable-ish ID so the button can find its result box.
                const runId = (el.id || 'code') + '-' + idx;
                btn.onclick = () => window.runPyCode(runId, btn);
                block.parentElement.after(btn);

                const resBox = document.createElement('div');
                resBox.id = `exec-result-${runId}`;
                resBox.className = 'py-res-box';
                resBox.style.display = 'none';
                btn.after(resBox);
            }
        }
    });

    queueScriptsFromElement(el, evt.detail?.source?.url || 'sse');
    pushSseTrace('sse-message-processed', {
        ...sseInfo,
        queueSize: scriptQueueState.items.length,
        processDelayMs: Math.max(0, nowPerfMs() - startedPerfMs),
    });
    traceSseRenderFrame('sse-message-next-frame', el, startedPerfMs, {
        sourceUrl: evt.detail?.source?.url || 'unknown',
        queueSize: scriptQueueState.items.length,
    });

    logBubbleLifecycle('sse-message', evt.detail?.elt || evt.detail?.source);
    logHtmxEvent('sseMessage', { ...requestMeta(evt.detail?.elt || evt.detail?.source) });

    if (el && (el.closest?.('#events-window') || el.closest?.('#events-list'))) {
        queueEventsWindowFollow();
    }
});

document.addEventListener('htmx:afterSwap', (evt) => {
    const target = evt.detail?.target || evt.target;
    const startedPerfMs = nowPerfMs();

    bindEventsWindow();
    bindScriptQueuePanel();
    announceAssistantBubbles(target);
    logBubbleLifecycle('after-swap', target);
    logDebugControls(target);

    if (target?.id?.startsWith?.('run-') || target?.id === 'events-window' || target?.id === 'events-list' || target?.closest?.('#events-window')) {
        pushSseTrace('after-swap', {
            targetId: target?.id || '',
            routeSource: target?.dataset?.routeSource || '',
            streamMode: target?.dataset?.streamMode || '',
            terminal: target?.dataset?.terminal || '',
        });
        traceStreamShimState('after-swap-shims', target, {
            targetId: target?.id || '',
        });
        traceSseRenderFrame('after-swap-next-frame', target, startedPerfMs, {
            targetId: target?.id || '',
            streamMode: target?.dataset?.streamMode || '',
        });
    }

    if (target?.dataset?.terminal === 'true') {
        logBubbleLifecycle('terminal-detected', target);
    }

    if (target && (target.id === 'events-window' || target.id === 'events-list' || target.closest?.('#events-window'))) {
        queueEventsWindowFollow();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    bindEventsWindow();
    bindScriptQueuePanel();
    bindStreamShimObserver();
    announceAssistantBubbles(document);
    logDebugControls(document);
    traceStreamShimState('dom-content-loaded-shims', document);
    queueEventsWindowFollow();
});
