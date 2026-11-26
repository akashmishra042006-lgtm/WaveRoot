// The public, read-only build (for example on Vercel) is built with
// VITE_WAVEROOT_HOSTED=1. It never contacts an analysis service: analyses run
// only on the user's own machine, where the simulators are.
export const HOSTED_BUILD = import.meta.env.VITE_WAVEROOT_HOSTED === "1";
export const REPO_URL = "https://github.com/akashmishra042006-lgtm/WaveRoot";
