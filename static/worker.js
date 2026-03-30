// Dedicated Web Worker for running small Python snippets in the browser.
//
// A worker keeps Pyodide isolated from the main UI thread, so the page stays
// responsive while the Python code loads and executes.

importScripts("https://cdn.jsdelivr.net/pyodide/v0.25.0/full/pyodide.js");

let pyodide;

async function initPyodide() {
    // Load the runtime once and cache it for later requests.
    pyodide = await loadPyodide();

    // If you want extra libraries, preload them here.
    // Example:
    // await pyodide.loadPackage(["numpy", "pandas"]);

    self.postMessage({ type: "ready" });
}

self.onmessage = async (event) => {
    const { type } = event.data;

    if (type === "init") {
        await initPyodide();
        return;
    }

    if (type === "execute") {
        const { code, id } = event.data;

        try {
            // Capture print() output by redirecting sys.stdout to a buffer.
            pyodide.runPython(`
import sys
import io
sys.stdout = io.StringIO()
            `);

            const result = await pyodide.runPythonAsync(code);
            const stdout = pyodide.runPython("sys.stdout.getvalue()");

            self.postMessage({
                type: "result",
                id,
                result: result === undefined || result === null ? "None" : result.toString(),
                stdout: stdout || "",
                success: true
            });
        } catch (error) {
            self.postMessage({
                type: "result",
                id,
                error: error.message,
                success: false
            });
        }
    }
};
