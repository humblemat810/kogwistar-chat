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

document.addEventListener('htmx:beforeRequest', (evt) => {
    // Helpful when debugging which fragment HTMX is about to fetch.
    const path = evt.detail?.pathInfo?.requestPath || evt.detail?.requestConfig?.path || 'unknown';
    logHtmxEvent('beforeRequest', { path, elt: evt.detail?.elt?.id || evt.detail?.elt?.tagName });
});

document.addEventListener('htmx:afterRequest', (evt) => {
    const xhr = evt.detail?.xhr;
    logHtmxEvent('afterRequest', {
        status: xhr?.status,
        path: evt.detail?.pathInfo?.requestPath || evt.detail?.requestConfig?.path || 'unknown',
        successful: evt.detail?.successful,
    });
});

document.addEventListener('htmx:sseOpen', (evt) => {
    // Fires when the HTMX SSE extension opens the server-push connection.
    logHtmxEvent('sseOpen', { sourceUrl: evt.detail?.source?.url || 'unknown' });
});

document.addEventListener('htmx:sseError', (evt) => {
    console.warn('[HTMX:SSE] error', evt.detail || {});
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
});
