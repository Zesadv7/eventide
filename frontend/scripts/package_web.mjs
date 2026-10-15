// Bundle the built workbench into Python package data; no Node runtime is shipped.
import { createHash } from "node:crypto";
import { cp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const target = resolve(root, "../eventide/workbench");
async function files(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) result.push(...(await files(path)));
    else if (entry.isFile()) result.push(path);
    else throw new Error(`Unexpected bundle entry: ${path}`);
  }
  return result.sort();
}
const sourceFiles = [
  ...(await files(resolve(root, "src"))),
  ...[
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "scripts/package_web.mjs",
  ].map((path) => resolve(root, path)),
].sort();
const sourceHash = createHash("sha256");
for (const file of sourceFiles)
  sourceHash
    .update(relative(root, file).split("\\").join("/") + "\0")
    .update(await readFile(file))
    .update("\0");
const built = await files(resolve(root, "dist"));
if (!built.includes(resolve(root, "dist/index.html")))
  throw new Error("Vite index.html is missing");
await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
await cp(resolve(root, "dist"), target, { recursive: true });
const artifacts = {};
for (const file of await files(target))
  artifacts[relative(target, file).split("\\").join("/")] = createHash("sha256")
    .update(await readFile(file))
    .digest("hex");
await writeFile(
  resolve(target, "manifest.json"),
  JSON.stringify(
    { version: 1, source_sha256: sourceHash.digest("hex"), files: artifacts },
    null,
    2,
  ) + "\n",
);
console.log(`Packaged ${Object.keys(artifacts).length} workbench resources`);
