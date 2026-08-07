// Kept as .js rather than .json so that these notes can live next to the settings they explain:
// babel parses a .json config as JSON5 and would accept comments, but the repo's check-json
// pre-commit hook parses it as strict JSON and rejects them.
export default {
    presets: [
        [
            "@babel/preset-react",
            {
                // Both of these were the Babel 7 defaults and both changed in Babel 8, so they are
                // pinned explicitly rather than left implicit.
                //
                // runtime: Babel 8 defaults to "automatic", which compiles JSX to an import from
                // "react/jsx-runtime". These bundles are loaded as ES modules against the import
                // map in tasklist-react.html, which maps the bare specifier "react" but has no
                // "react/" prefix entry, so the browser cannot resolve that specifier and the whole
                // module graph fails to load. "classic" keeps the React.createElement output that
                // the import map already serves; both .jsx sources import React themselves, so it
                // has what it needs.
                runtime: "classic",
                // development: Babel 8 turns this on whenever the env is "development", which is
                // the default when build.sh runs without NODE_ENV set. It adds __source/__self
                // props that bake absolute build-machine paths into a bundle served to every
                // visitor.
                development: false,
            },
        ],
    ],
};
