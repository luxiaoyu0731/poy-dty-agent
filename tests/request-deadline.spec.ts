import { expect, test } from "@playwright/test";
import { requestDeadline } from "../src/utils/requestDeadline";

test("request deadline preserves success and the original rejection", async () => {
  const payload = { status: "ready" };
  expect(await requestDeadline(Promise.resolve(payload), 1000, "状态")).toBe(payload);
  const failure = new Error("service unavailable");
  await expect(requestDeadline(Promise.reject(failure), 1000, "状态")).rejects.toBe(failure);
});

test("request deadline reports timeout and safely consumes a late rejection", async () => {
  let rejectRequest!: (error: Error) => void;
  const underlying = new Promise<never>((_, reject) => { rejectRequest = reject; });
  await expect(requestDeadline(underlying, 5, "状态")).rejects.toThrow("状态 暂未在 0 秒内返回");
  rejectRequest(new Error("late network failure"));
  await new Promise((resolve) => setTimeout(resolve, 10));
});
