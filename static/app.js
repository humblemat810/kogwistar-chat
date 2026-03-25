/* Front-end glue for the chat UI.

This file has three jobs:
1. manage the optional Pyodide worker demo
2. attach a few debug logs so HTMX requests are easier to inspect
3. enhance SSE-rendered code blocks with a "Run in Browser" button

If you know jQuery, think of the HTMX event listeners as global delegated
callbacks that fire when requests and SSE messages happen.
*/

const pyWorker = new Worker('/static/worker.js');

// Start the worker right away so it can load Pyodide before the user clicks
// any "Run in Browser" button.
pyWorker.postMessage({ type: 'init' });

pyWorker.onmessage = (event) => {
    const { type, id, result, stdout, error, success } = event.data;

    if (type === 'ready') {
        console.log('[PyWorker] ready');
        return;
    }

    if (type === 'result') {
        const target = document.getElementById(`exec-result-${id}`);
        if (target) {
            const button = target.previousElementSibling;
            target.style.display = 'block';

            if (success) {
                // Show both printed output and the returned Python value.
                target.innerHTML = `<strong>Output:</strong><pre>${stdout}</pre><strong>Result:</strong> <code>${result}</code>`;
                target.className = 'py-res-box success';
            } else {
                target.innerHTML = `<strong>Error:</strong><pre>${error}</pre>`;
                target.className = 'py-res-box error';
            }

            // Re-enable the button so the snippet can be rerun without a page reload.
            if (button && button.classList.contains('run-py-btn')) {
                button.disabled = false;
                button.innerText = 'Run in Browser';
            }
        }
    }
};

window.runPyCode = (id, btn) => {
    // The button sits immediately after the code block. We read the text from
    // the previous sibling and send it to the worker as a task payload.
    const code = btn.previousElementSibling.innerText;
    const resultBox = document.getElementById(`exec-result-${id}`);

    if (resultBox) {
        // Clear the previous result so repeated runs feel deterministic.
        resultBox.style.display = 'none';
        resultBox.className = 'py-res-box';
        resultBox.innerHTML = '';
    }

    btn.innerText = 'Running...';
    btn.disabled = true;
    pyWorker.postMessage({ type: 'execute', id, code });
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

function isNearBottom(el, threshold = 72) {
    if (!el) {
        return false;
    }

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
    logHtmxEvent('sseOpen', { sourceUrl: evt.detail?.source?.url || 'unknown', ...requestMeta(evt.detail?.elt || evt.detail?.source) });
    logBubbleLifecycle('sse-open', evt.detail?.elt || evt.detail?.source, {
        sourceUrl: evt.detail?.source?.url || 'unknown',
    });
});

document.addEventListener('htmx:sseError', (evt) => {
    console.warn('[HTMX:SSE] error', { ...requestMeta(evt.detail?.elt || evt.detail?.source), ...(evt.detail || {}) });
    logBubbleLifecycle('sse-error', evt.detail?.elt || evt.detail?.source, evt.detail || {});
});

document.addEventListener('htmx:sseMessage', function (evt) {
    // The SSE extension inserts the HTML fragment into the target element and
    // then emits this event. We can inspect the new DOM and add extra controls.
    const el = evt.detail.elt;
    const codeBlocks = el.querySelectorAll ? el.querySelectorAll('pre code') : [];

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

    logBubbleLifecycle('sse-message', evt.detail?.elt || evt.detail?.source);
    logHtmxEvent('sseMessage', { ...requestMeta(evt.detail?.elt || evt.detail?.source) });
    if (el && el.closest && el.closest('#events-window')) {
        queueEventsWindowFollow();
    }
});

document.addEventListener('htmx:afterSwap', (evt) => {
    const target = evt.detail?.target || evt.target;

    bindEventsWindow();
    announceAssistantBubbles(target);
    logBubbleLifecycle('after-swap', target);
    logDebugControls(target);

    if (target?.dataset?.terminal === 'true') {
        logBubbleLifecycle('terminal-detected', target);
    }

    if (target && (target.id === 'events-window' || target.closest?.('#events-window'))) {
        queueEventsWindowFollow();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    bindEventsWindow();
    announceAssistantBubbles(document);
    logDebugControls(document);
    queueEventsWindowFollow();
});
