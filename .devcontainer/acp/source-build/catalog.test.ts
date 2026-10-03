// Runs inside the pinned upstream tree; stub clients, no sockets, keys, prompts or MCP calls.
import { beforeEach, afterEach, expect, test } from "bun:test"
import { ACPService } from "../../src/acp/service"

const keys = ["OPENCODE_ACP_REQUIRED_MODEL", "OPENCODE_ACP_REQUIRED_VARIANT", "OPENCODE_ACP_CATALOG_TIMEOUT_MS"]
let saved: (string | undefined)[]
beforeEach(() => {
  saved = keys.map((key) => process.env[key])
  process.env.OPENCODE_ACP_REQUIRED_MODEL = "test/wanted"
  process.env.OPENCODE_ACP_REQUIRED_VARIANT = "medium"
  process.env.OPENCODE_ACP_CATALOG_TIMEOUT_MS = "150"
})
afterEach(() => keys.forEach((key, i) => {
  if (saved[i] === undefined) delete process.env[key]
  else process.env[key] = saved[i]
}))
const wanted = { providerID: "test", id: "wanted", name: "Wanted", enabled: true, variants: [{ id: "medium" }] }
const builtin = { ...wanted, providerID: "builtin", id: "early" }
const done = new Error("synthetic session boundary")
function fixture(mode = "ready") {
  const state = { mode, calls: 0, signals: [] as AbortSignal[], created: [] as any[] }
  const read = (kind: string) => async (_: any, options: any) => {
    expect(options.signal).toBeInstanceOf(AbortSignal)
    state.signals.push(options.signal)
    if (state.mode === "hang") return new Promise(() => {}) // Deliberately ignores abort.
    if (kind === "model") {
      state.calls++
      return { data: state.mode === "missing" || (state.mode === "late" && state.calls < 3) ? [builtin] : [builtin, wanted] }
    }
    if (kind === "default") return { data: builtin }
    return { data: kind === "agent" ? [{ id: "build", mode: "primary", hidden: false }] : [] }
  }
  const client = { model: { list: read("model"), default: read("default") },
    agent: { list: read("agent") }, command: { list: read("command") },
    session: { create: async (value: any) => { state.created.push(value); throw done } } }
  const service = ACPService.make({ client, connection: {} } as any)
  const request = () => service.newSession({ cwd: "/synthetic-readiness", mcpServers: [] })
  return { state, request }
}

test("waits past builtin catalog, selects explicit target and medium, caches only readiness", async () => {
  const { state, request } = fixture("late")
  await expect(request()).rejects.toBe(done)
  expect(state.calls).toBe(3)
  expect(state.created[0].model).toEqual({ providerID: "test", id: "wanted", variant: "medium" })
  await expect(request()).rejects.toBe(done)
  expect(state.calls).toBe(3)
  expect(state.signals.every((signal) => signal.aborted)).toBe(true)
})

test("missing target fails bounded without fallback; same-cwd failure is evicted", async () => {
  const { state, request } = fixture("missing")
  const start = performance.now()
  await expect(request()).rejects.toMatchObject({ safeMessage: "ACP catalog readiness deadline exceeded" })
  expect(performance.now() - start).toBeGreaterThanOrEqual(120)
  expect(performance.now() - start).toBeLessThan(1500)
  expect(state.created).toHaveLength(0)
  state.mode = "ready"
  await expect(request()).rejects.toBe(done)
  expect(state.created).toHaveLength(1)
})

test("all four hanging reads receive abort and caller remains bounded even if ignored", async () => {
  const { state, request } = fixture("hang")
  const start = performance.now()
  await expect(request()).rejects.toMatchObject({ safeMessage: "ACP catalog readiness deadline exceeded" })
  expect(performance.now() - start).toBeLessThan(1500)
  expect(state.signals).toHaveLength(4)
  expect(state.signals.every((signal) => signal.aborted)).toBe(true)
  expect(state.created).toHaveLength(0)
})

test("invalid deadline, absent target and unavailable variant fail closed", async () => {
  for (const value of ["0", "NaN", "1.5", "120001"]) {
    process.env.OPENCODE_ACP_CATALOG_TIMEOUT_MS = value
    const { state, request } = fixture()
    await expect(request()).rejects.toThrow("deadline")
    expect(state.calls).toBe(0)
  }
  process.env.OPENCODE_ACP_CATALOG_TIMEOUT_MS = "150"
  delete process.env.OPENCODE_ACP_REQUIRED_MODEL
  await expect(fixture().request()).rejects.toThrow("explicit")
  process.env.OPENCODE_ACP_REQUIRED_MODEL = "test/wanted"
  process.env.OPENCODE_ACP_REQUIRED_VARIANT = "unavailable"
  const { state, request } = fixture()
  await expect(request()).rejects.toBeDefined()
  expect(state.created).toHaveLength(0)
})
