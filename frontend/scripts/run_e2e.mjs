import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const children = [];
function launch(command, args, env = {}) {
  const child = spawn(command, args, {
    cwd: root,
    env: { ...process.env, ...env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.push(child);
  child.stderr.on("data", (data) => process.stderr.write(data));
  return child;
}
function line(child, pattern) {
  return new Promise((accept, reject) => {
    let text = "";
    const timer = setTimeout(
      () => reject(new Error(`服务启动超时：${pattern}`)),
      20000,
    );
    const read = (data) => {
      text += data.toString();
      const match = text.match(pattern);
      if (match) {
        clearTimeout(timer);
        child.stdout.off("data", read);
        accept(match[1]);
      }
    };
    child.stdout.on("data", read);
    child.once("error", reject);
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`服务提前退出：${code}`));
    });
  });
}
try {
  const backend = launch(process.env.EVENTIDE_PYTHON || "python3", [
    "scripts/e2e_server.py",
  ]);
  const port = await line(backend, /E2E_PORT=(\d+)/);
  const vite = launch(
    process.execPath,
    ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "0"],
    { EVENTIDE_API_URL: `http://127.0.0.1:${port}` },
  );
  const url = await line(vite, /Local:\s+(http:\/\/127\.0\.0\.1:\d+\/)/);
  const runner = launch(
    process.execPath,
    ["node_modules/@playwright/test/cli.js", "test"],
    { EVENTIDE_WEB_URL: url },
  );
  runner.stdout.on("data", (data) => process.stdout.write(data));
  process.exitCode = await new Promise((accept, reject) => {
    runner.once("error", reject);
    runner.once("exit", (code) => accept(code ?? 1));
  });
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  for (const child of children.reverse())
    if (child.exitCode === null) child.kill("SIGTERM");
}
