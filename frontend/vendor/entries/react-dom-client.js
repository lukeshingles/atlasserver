// Entry point for the self-hosted "react-dom" bundle. tasklist.jsx does
// `import ReactDOM from 'react-dom'` and calls ReactDOM.createRoot, and the import map has always
// pointed that specifier at react-dom's *client* entry, so this keeps that mapping.
//
// react is deliberately left external in build.sh: inlining it here would give the page a second,
// separate copy of React, and hooks dispatch through module-level state, so the two copies do not
// interoperate. Left external, the bare "react" import in the output resolves through the same
// import map entry the app itself uses.
import ReactDOMClient from "react-dom/client";

export * from "react-dom/client";
export default ReactDOMClient;
