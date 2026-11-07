// ============================================================
// ELECTRON-DEV.JS — `npm run electron:dev`
//
// Starts this checkout's Vite dev server, then Electron pointed at the exact
// URL Vite bound (AIO_DEV_SERVER_URL; electron/main.js grep devServerUrl).
// Closing Electron stops Vite; Ctrl+C stops both.
//
// Replaces `concurrently "vite" "wait-on http://localhost:5173 && electron ."`,
// which had two faults: when 5173 was taken (another checkout's dev server),
// Vite moved to 5174 while wait-on and main.js still used 5173, so Electron
// showed the OTHER checkout's code; and Vite kept running after Electron
// closed. Asking Vite for its bound URL removes the guess.
// (concurrently / wait-on stay in devDependencies only to avoid lockfile churn.)
//
// CommonJS (the package has no "type"), so Vite 7, which is ESM-only, comes in
// through a dynamic import().
// ============================================================

const path = require("path");
const { spawn } = require("child_process");

const APP = path.resolve(__dirname, "..");

async function main() {
  const { createServer } = await import("vite");
  // root → vite.config.js is picked up from APP like `npx vite` would.
  // clearScreen off so Electron's main-process logs aren't wiped.
  const server = await createServer({ root: APP, clearScreen: false });
  await server.listen();
  server.printUrls();
  const url = server.resolvedUrls?.local?.[0];
  if (!url) {
    await server.close();
    throw new Error("Vite did not report a local URL");
  }

  const env = {
    ...process.env,
    NODE_ENV: "development",
    AIO_DEV_SERVER_URL: url,
  };
  // Set by some tool hosts; it would make the electron binary run as plain Node.
  delete env.ELECTRON_RUN_AS_NODE;

  let child;
  let closing = false;
  const shutdown = async (code) => {
    if (closing) return;
    closing = true;
    try {
      await server.close();
    } catch (err) {
      console.error(`[electron-dev] closing Vite: ${err.message}`);
    }
    process.exit(code);
  };

  try {
    // require("electron") from Node returns the path of the Electron binary.
    child = spawn(require("electron"), [APP], { stdio: "inherit", env });
  } catch (err) {
    await server.close();
    throw err;
  }
  child.on("error", (err) => {
    console.error(`[electron-dev] Electron failed to start: ${err.message}`);
    shutdown(1);
  });
  child.on("exit", (code, signal) => {
    shutdown(code ?? (signal ? 1 : 0));
  });

  // Ctrl+C reaches Electron too (same console), so normally it exits on its
  // own and the exit handler above closes Vite. Kill it if it hangs.
  const onSignal = () => {
    if (child.exitCode !== null) return;
    setTimeout(() => {
      if (child.exitCode === null) child.kill();
    }, 5000).unref();
  };
  process.on("SIGINT", onSignal);
  process.on("SIGTERM", onSignal);
  // Windows: Ctrl+Break, and the console window being closed.
  process.on("SIGBREAK", onSignal);
  process.on("SIGHUP", onSignal);
}

main().catch((err) => {
  console.error(`[electron-dev] ${err.stack || err.message}`);
  process.exit(1);
});
