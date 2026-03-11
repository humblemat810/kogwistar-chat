# Future Roadmap: Privacy-First Personal Agent

This document outlines the long-term vision for the HTMX Chat App, focusing on privacy, local computation, and personal data sovereignty.

## Vision Statement
To create an "agentic personal assistant" that runs primarily in the user's browser, leveraging local indexing and retrieval (Small GraphRAG) to ensure that sensitive personal data never leaves the device.

## Milestone: Client-Side GraphRAG (Phase 2+)
- **Sandboxed Execution**: Port core GraphRAG components to run within the Pyodide Web Worker.
- **Local Vectors**: Use a browser-based vector store (e.g., Voy, Voyager, or similar) to handle local embedding search.
- **Privacy-First Memory**:
    - Users can upload personal documents (PDFs, Notes) which are indexed locally.
    - Retrieval-Augmented Generation (RAG) happens by sending only the relevant, anonymized context to the remote LLM, or using a local LLM if the device allows.
- **Data Sovereignty**:
    - Project data and Knowledge Graphs stored in IndexedDB or local filesystem.
    - Exportable and encrypted backups.




## Future Research Areas
- **Local Embedding Models**: Running models like `all-MiniLM-L6-v2` or `BGE-small` via Transformers.js or ONNX Runtime in the browser.
- **Edge LLMs**: Integration with WebLLM for full-local inference when hardware acceleration (WebGPU) is available.
- **Decentralized Sync**: Synchronizing private graphs across the user's devices without a central server.
