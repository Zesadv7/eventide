import { test, expect } from "@playwright/test";
import { execFile } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const execute = promisify(execFile);

test("a lost file result is checked on the built page before an explicit, unique continuation", async ({
  page,
}) => {
  const root = await mkdtemp(join(tmpdir(), "eventide-outcome-reconcile-e2e-"));
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await mkdir(join(root, "src"));
    await writeFile(
      join(root, "README.md"),
      "# outcome reconciliation fixture\n",
    );
    await writeFile(join(root, "src", "fixture.txt"), "preserved source\n");
    await execute("git", ["-C", root, "init"]);
    await execute("git", ["-C", root, "add", "."]);
    await execute("git", [
      "-C",
      root,
      "-c",
      "user.name=Outcome E2E",
      "-c",
      "user.email=outcome@example.invalid",
      "commit",
      "-m",
      "fixture",
    ]);
    const registered = await page.request.post("/api/workspaces", {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { path: root },
    });
    expect(registered.status()).toBe(201);
    const workspace = await registered.json();
    const created = await page.request.post("/api/sessions", {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { workspace_id: workspace.workspace_id },
    });
    expect(created.status()).toBe(201);
    const session: string = (await created.json()).session_id;
    await page.goto(`./#/chat/${session}`);
    await expect(page.getByRole("textbox", { name: "任务输入" })).toBeEnabled();
    await page.getByLabel("运行模式", { exact: true }).selectOption("agent");
    await page.getByLabel("执行位置", { exact: true }).selectOption("source");
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill(`文件核查场景 ${crypto.randomUUID()}`);
    const accepted = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/runs"),
    );
    await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
    const admission = await accepted;
    expect(admission.status()).toBe(202);
    const original: string = (await admission.json()).run_id;
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/runs/${original}`)).json())
            .status,
      )
      .toBe("interrupted");
    const before = await (
      await page.request.get(`/api/runs/${original}/export`)
    ).text();
    const originalEvents = before
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    const unknowns = originalEvents.filter(
      (event) => event.type === "tool.outcome_unknown",
    );
    expect(unknowns).toHaveLength(1);
    expect(unknowns[0].payload.name).toBe("write_file");
    expect(
      originalEvents.filter((event) => event.type === "tool.completed"),
    ).toHaveLength(0);
    const file = join(root, "src", "reconciled.txt");
    expect(await readFile(file, "utf8")).toBe("reconciled once\n");
    const writtenAt = (await stat(file)).mtimeMs;
    const operationsBefore = await (
      await page.request.get(`/api/sessions/${session}/file-operations`)
    ).json();
    expect(operationsBefore.operations).toHaveLength(1);
    expect(operationsBefore.operations[0].status).toBe("needs_review");

    await page.getByRole("link", { name: "查看恢复详情", exact: true }).click();
    const panel = page.locator(".recovery-panel");
    await expect(panel.getByTestId("unknown-tools")).toContainText(
      "write_file",
    );
    await expect(panel.getByTestId("continue-run")).toHaveCount(0);
    const reconcileUrl = `/api/runs/${original}/recovery/reconcile`;
    const checked = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(reconcileUrl),
    );
    await panel
      .getByRole("button", { name: "只读核查文件结果", exact: true })
      .click();
    const checkedResponse = await checked;
    expect(checkedResponse.status()).toBe(200);
    const receipt = await checkedResponse.json();
    expect(receipt.unresolved).toBe(0);
    expect(receipt.checks).toHaveLength(1);
    expect(receipt.checks[0].status).toBe("verified_after");
    expect(receipt.checks[0].unknown_event_id).toBe(unknowns[0].event_id);
    const checkKey = (await checkedResponse.request().allHeaders())[
      "idempotency-key"
    ]!;
    const repeated = await page.request.post(reconcileUrl, {
      headers: { "Idempotency-Key": checkKey },
      data: {},
    });
    expect(repeated.status()).toBe(200);
    expect(await repeated.json()).toEqual(receipt);
    await expect(panel.getByTestId("outcome-checks")).toContainText(
      "预期文件状态已核清",
    );
    await expect(panel.getByTestId("unknown-tools")).toHaveCount(0);
    await expect(panel.getByTestId("continue-run")).toBeVisible();
    await page.reload();
    await expect(panel.getByTestId("outcome-checks")).toContainText(
      "预期文件状态已核清",
    );
    const afterCheck = await (
      await page.request.get(`/api/runs/${original}/recovery`)
    ).json();
    expect(afterCheck.outcome_checks).toHaveLength(1);
    expect(afterCheck.recovery.successor_run_id).toBeNull();
    expect(
      await (await page.request.get(`/api/sessions/${session}/runs`)).json(),
    ).toHaveLength(1);
    expect(
      (await (await page.request.get(`/api/sessions/${session}/usage`)).json())
        .attempt_count,
    ).toBe(1);
    expect(
      await (await page.request.get(`/api/runs/${original}/export`)).text(),
    ).toBe(before);
    await page.setViewportSize({ width: 320, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);

    const continued = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/api/sessions/${session}/continue`),
    );
    await panel
      .getByRole("button", { name: "Continue · 检查并继续", exact: true })
      .click();
    const continuedResponse = await continued;
    expect(continuedResponse.status()).toBe(202);
    expect(continuedResponse.request().postDataJSON()).toEqual({
      expected_run_id: original,
    });
    const successor: string = (await continuedResponse.json()).run_id;
    const continueKey = (await continuedResponse.request().allHeaders())[
      "idempotency-key"
    ]!;
    const duplicateContinue = await page.request.post(
      `/api/sessions/${session}/continue`,
      {
        headers: { "Idempotency-Key": continueKey },
        data: { expected_run_id: original },
      },
    );
    expect(duplicateContinue.status()).toBe(202);
    expect((await duplicateContinue.json()).run_id).toBe(successor);
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/runs/${successor}`)).json())
            .status,
      )
      .toBe("completed");
    await panel
      .getByRole("link", { name: "查看续做运行", exact: true })
      .click();
    await expect(panel.getByText("已正常完成", { exact: true })).toBeVisible();
    await page.goto(`./#/chat/${session}`);
    await expect(
      page.getByText("核查后继续完成", { exact: true }),
    ).toBeVisible();
    const runs = await (
      await page.request.get(`/api/sessions/${session}/runs`)
    ).json();
    expect(runs).toHaveLength(2);
    expect(
      runs.filter(
        (run: { continuation_of: string | null }) =>
          run.continuation_of === original,
      ),
    ).toHaveLength(1);
    const operationsAfter = await (
      await page.request.get(`/api/sessions/${session}/file-operations`)
    ).json();
    expect(operationsAfter.operations).toHaveLength(1);
    expect(operationsAfter.operations[0].operation_id).toBe(
      operationsBefore.operations[0].operation_id,
    );
    expect(await readFile(file, "utf8")).toBe("reconciled once\n");
    expect((await stat(file)).mtimeMs).toBe(writtenAt);
    const usage = await (
      await page.request.get(`/api/sessions/${session}/usage`)
    ).json();
    expect(usage.attempt_count).toBe(2);
    expect(usage.unknown_attempts).toBe(0);
    const recoveredEvents = (
      await (await page.request.get(`/api/runs/${successor}/export`)).text()
    )
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    const recoveredCalls = recoveredEvents.filter(
      (event) => event.type === "tool.completed",
    );
    expect(recoveredCalls).toHaveLength(1);
    expect(recoveredCalls[0].payload.call_id).toBe(unknowns[0].payload.call_id);
    expect(recoveredCalls[0].payload.metadata.outcome_reconciled).toBe(true);
    expect(
      recoveredEvents.filter((event) => event.type === "tool.prepared"),
    ).toHaveLength(0);
    expect(
      await (await page.request.get(`/api/runs/${original}/export`)).text(),
    ).toBe(before);
    expect(errors).toEqual([]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
