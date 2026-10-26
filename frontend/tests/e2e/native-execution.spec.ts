import { test, expect } from "@playwright/test";

test("packaged native file journey keeps source unchanged and freezes read-only permissions", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const capabilities = await (
    await page.request.get("/api/runtime/native-capabilities")
  ).json();
  expect(capabilities.verification).toBe("unverified");
  expect(
    capabilities.backends.find(
      (item: { kind: string }) => item.kind === "seatbelt",
    ).available,
  ).toBe(true);

  async function newChat() {
    const previous = page.url();
    await page
      .getByRole("button", { name: "新建对话 New chat", exact: true })
      .click();
    await expect(page).not.toHaveURL(previous);
    await expect(page.getByLabel("执行环境", { exact: true })).toBeEnabled();
    return decodeURIComponent(new URL(page.url()).hash.split("/").at(-1)!);
  }

  const writable = await newChat();
  await page.getByLabel("执行环境", { exact: true }).selectOption("native");
  const options = page
    .locator(".sandbox-options")
    .filter({ hasText: "原生隔离" });
  await expect(options).toContainText("内核隔离未验证");
  await expect(options).toContainText("真实内核越界验证");
  await page
    .getByLabel("原生文件权限", { exact: true })
    .selectOption("workspace-write");
  await page
    .getByLabel("原生平台后端", { exact: true })
    .selectOption("seatbelt");
  await page
    .getByRole("textbox", { name: "任务输入" })
    .fill("原生文件场景：创建候选文件");
  const acceptedWrite = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/sessions/${writable}/runs`),
  );
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const writeResponse = await acceptedWrite;
  expect(writeResponse.status()).toBe(202);
  expect(writeResponse.request().postDataJSON()).toMatchObject({
    execution_profile: "native",
    execution_target: "worktree",
    native_profile: "workspace-write",
    native_backend: "seatbelt",
  });
  const writeRun = (await writeResponse.json()).run_id;
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/runs/${writeRun}`)).json()).status,
    )
    .toBe("completed");
  await expect(
    page.getByText("原生文件检查完成", { exact: true }),
  ).toBeVisible();
  const writeState = await (
    await page.request.get(`/api/runs/${writeRun}`)
  ).json();
  const read = Object.values(writeState.tools).find(
    (item: any) => item.name === "read_file",
  ) as any;
  expect(read.metadata.source_evidence.native_worker).toBe(true);
  const proof = await (
    await page.request.get(`/api/__e2e/native-proof/${writeRun}`)
  ).json();
  expect(proof.fixture_launcher).toBe("stdlib-worker-without-os-wrapper");
  expect(proof.verification).toBe("unverified");
  expect(proof.candidate_root).not.toBe(proof.source_root);
  expect(proof.source_status).toBe("");
  expect(proof.source_note_exists).toBe(false);
  expect(proof.candidate_note).toBe("native worker candidate\n");
  expect(proof.source_demo_sha256).toBe(read.metadata.source_evidence.sha256);
  expect(
    proof.worker_calls.map((item: { operation: string }) => item.operation),
  ).toEqual(expect.arrayContaining(["read_file", "write_file"]));
  expect(
    proof.worker_calls.every(
      (item: { pid: number }) => item.pid > 0 && item.pid !== proof.server_pid,
    ),
  ).toBe(true);
  expect(proof.workers_closed).toBe(true);

  await page.reload();
  await expect(page.getByLabel("执行环境", { exact: true })).toHaveValue(
    "native",
  );
  await expect(page.getByLabel("原生文件权限", { exact: true })).toHaveValue(
    "workspace-write",
  );
  await expect(page.getByLabel("原生文件权限", { exact: true })).toBeDisabled();
  await expect(page.getByLabel("原生平台后端", { exact: true })).toHaveValue(
    "seatbelt",
  );
  await expect(page.getByLabel("原生平台后端", { exact: true })).toBeDisabled();

  const readonly = await newChat();
  await page.getByLabel("执行环境", { exact: true }).selectOption("native");
  await page
    .getByLabel("原生文件权限", { exact: true })
    .selectOption("read-only");
  await page
    .getByLabel("原生平台后端", { exact: true })
    .selectOption("seatbelt");
  for (const turn of ["首次只读", "下一条消息仍只读"]) {
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill(`原生文件场景：${turn}`);
    const received = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/api/sessions/${readonly}/runs`),
    );
    await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
    const response = await received;
    expect(response.status()).toBe(202);
    expect(response.request().postDataJSON()).toMatchObject({
      execution_profile: "native",
      native_profile: "read-only",
      native_backend: "seatbelt",
      execution_target: "worktree",
    });
    const run = (await response.json()).run_id;
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/runs/${run}`)).json()).status,
      )
      .toBe("completed");
    const readonlyProof = await (
      await page.request.get(`/api/__e2e/native-proof/${run}`)
    ).json();
    expect(readonlyProof.profile.profile).toBe("read-only");
    expect(readonlyProof.candidate_note).toBeNull();
    expect(readonlyProof.source_status).toBe("");
    expect(
      readonlyProof.worker_calls.some(
        (item: { operation: string }) => item.operation === "write_file",
      ),
    ).toBe(false);
    await page.reload();
    await expect(page.getByLabel("原生文件权限", { exact: true })).toHaveValue(
      "read-only",
    );
    await expect(
      page.getByLabel("原生文件权限", { exact: true }),
    ).toBeDisabled();
  }
  const rejected = await page.request.post(`/api/sessions/${readonly}/runs`, {
    headers: { "Idempotency-Key": "native-e2e-reject-upgrade" },
    data: {
      prompt: "do not upgrade",
      execution_profile: "native",
      execution_target: "worktree",
      native_profile: "workspace-write",
      native_backend: "seatbelt",
    },
  });
  expect(rejected.status()).toBe(422);
  expect(
    await (await page.request.get(`/api/sessions/${readonly}/runs`)).json(),
  ).toHaveLength(2);
  await page.setViewportSize({ width: 320, height: 844 });
  await expect(options).toContainText("内核隔离未验证");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
