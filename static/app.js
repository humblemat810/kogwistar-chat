const pyWorker = new Worker('/static/worker.js');

pyWorker.postMessage({ type: 'init' });

pyWorker.onmessage = (event) => {
    const { type, id, result, stdout, error, success } = event.data;
    if (type === 'ready') console.log('Pyodide Worker Ready');
    if (type === 'result') {
        const target = document.getElementById(`exec-result-${id}`);
        if (target) {
            target.style.display = 'block';
            if (success) {
                target.innerHTML = `<strong>Output:</strong><pre>${stdout}</pre><strong>Result:</strong> <code>${result}</code>`;
                target.className = 'py-res-box success';
            } else {
                target.innerHTML = `<strong>Error:</strong><pre>${error}</pre>`;
                target.className = 'py-res-box error';
            }
        }
    }
};

window.runPyCode = (id, btn) => {
    const code = btn.previousElementSibling.innerText;
    btn.innerText = 'Running...';
    btn.disabled = true;
    pyWorker.postMessage({ type: 'execute', id, code });
};

// Auto-enhance code blocks after SSE updates
document.addEventListener('htmx:sseMessage', function(evt) {
    const el = evt.detail.elt;
    const msgData = evt.detail.data;
    
    // Intercept different event types based on content
    if (el.id === 'events-window' && msgData.includes('thinking-state')) {
        // SSE routing for right panel events
        // HTMX handles swapping based on hx-swap, but we can do custom logic if needed
        return;
    }

    const codeBlocks = el.querySelectorAll('pre code');
    codeBlocks.forEach((block, idx) => {
        if (!block.parentElement.nextElementSibling?.classList.contains('run-py-btn')) {
            if (block.innerText.includes('print') || block.innerText.length > 5) {
                const btn = document.createElement('button');
                btn.className = 'run-py-btn';
                btn.innerText = 'Run in Browser';
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
