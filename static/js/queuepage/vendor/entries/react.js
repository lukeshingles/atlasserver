// Entry point for the self-hosted "react" bundle referenced by the import map in
// tasklist-react.html. React 19 publishes CommonJS only -- the UMD builds were removed in 19 and
// there has never been an ESM build -- so something has to convert it, which is the one job
// esbuild does here. It is not used to bundle this project's own code.
//
// Both forms are re-exported because the sources use both: tasklist.jsx and newrequest.jsx do
// `import React from "react"`, while the bundled react-dom reaches for named exports.
import React from "react";

export * from "react";
export default React;
