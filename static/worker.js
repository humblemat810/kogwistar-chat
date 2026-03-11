// static/worker.js
importScripts("https://cdn.jsdelivr.net/pyodide/v0.25.0/full/pyodide.js");

let pyodide;

async function initPyodide() {
    pyodide = await loadPyodide();
    // Pre-load common packages if needed
    // await pyodide.loadPackage(["numpy", "pandas"]);
    self.postMessage({ type: "ready" });
}

self.onmessage = async (event) => {
    if (event.data.type === "init") {
        await initPyodide();
    } else if (event.data.type === "execute") {
        const { code, id } = event.data;
        try {
            // Redirect stdout to capture print statements
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
                result: result?.toString(),
                stdout,
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
