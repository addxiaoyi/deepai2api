// Provider 运行时装配 —— 把 provider-catalog 的描述符变成"能跑的东西"：
// 每个 provider 一份号池、一份注册控制器，再加一个共用反代层。
//
// 这是唯一的装配点：网关（src/server.mjs）与 CLI（src/moonai-cli.mjs）都走这里，
// 所以"号池文件 / 注册锁 / job 状态"三处永远一致，不会出现两个进程各写一份。
//
// 物理隔离约定（保证和其它模块互不冲突）：
//   data/<provider>-pool.json        号池，schema = dcg.pool/<provider>@1
//   data/<provider>-register.lock    注册互斥锁
//   data/<provider>-register-job.json job 状态
//   tools/<provider>-reg/            Python worker（-m <provider>_reg）
// 以上名字全部由 provider.id 派生，moonai 与 allmodels / poe / deepai 完全不重叠。
import { PROVIDERS, providerSummary, resolveEndpoints } from "./provider-catalog.mjs";
import { ProviderPool, importAccount, maskAccount } from "./provider-pool.mjs";
import { ProviderRegistrations, DEFAULT_ANTI_BAN } from "./provider-register.mjs";
import { createProviderRelay, probeProvider } from "./provider-relay.mjs";

/** 只装配 config.enabledProviders 里列出的 provider；默认全装。 */
export function createProviderRuntime({
  dataDir,
  port,
  token,
  store,
  config = {},
  log = () => {},
  enabled = null,
  endpointOverrides = null,
  onAccount = null,
} = {}) {
  const wanted = Array.isArray(enabled) && enabled.length ? enabled.map((id) => String(id).toLowerCase()) : null;
  const overrides = endpointOverrides && typeof endpointOverrides === "object" ? endpointOverrides : {};
  // 端点覆盖优先于 catalog 默认值（测试 / 自建反代 / 走内网时用）；
  // 环境变量仍然是最高优先级，由 resolveEndpoints 负责。
  const providers = PROVIDERS.filter((provider) => !wanted || wanted.includes(provider.id)).map((provider) => {
    const override = overrides[provider.id];
    return override && typeof override === "object" ? Object.freeze({ ...provider, ...override }) : provider;
  });
  const byId = new Map(providers.map((provider) => [provider.id, provider]));

  /** @type {Map<string, ProviderPool>} */
  const pools = new Map();
  /** @type {Map<string, ProviderRegistrations>} */
  const registrations = new Map();

  for (const provider of providers) {
    const pool = ProviderPool.at(dataDir, provider, { log });
    pools.set(provider.id, pool);
    registrations.set(provider.id, new ProviderRegistrations({
      dataDir,
      port,
      token,
      provider,
      pool,
      store,
      log,
      onAccount: (result) => {
        log(
          "info",
          `${provider.id} 入池 ${result.account?.email ?? "?"}（号池 ${pool.size}，网关 Key ${result.gatewayKey?.prefix ?? "-"}）`,
        );
        try {
          onAccount?.({ provider, ...result });
        } catch (error) {
          log("warn", `${provider.id} onAccount 回调失败：${error.message}`);
        }
      },
    }));
  }

  const relay = createProviderRelay({ pools, store, config, log });

  const requireProvider = (id) => {
    const key = String(id ?? "").trim().toLowerCase();
    if (!byId.has(key) || !pools.has(key)) {
      const error = new Error(`未知 provider：${id}（可用：${[...pools.keys()].join(", ") || "无"}）`);
      error.status = 404;
      error.code = "unknown_provider";
      throw error;
    }
    return { provider: byId.get(key), pool: pools.get(key), registrations: registrations.get(key) };
  };

  /** 一次拿到所有 provider 的概览（管理台 / CLI / /health 共用）。 */
  const list = () => providers.map((provider) => {
    const pool = pools.get(provider.id);
    const registration = registrations.get(provider.id);
    return {
      ...providerSummary(provider),
      pool: pool.stats(),
      running: registration?.job?.status === "running",
      job: registration?.job
        ? {
            id: registration.job.id,
            status: registration.job.status,
            requested: registration.job.requested,
            created: registration.job.created ?? 0,
            failed: registration.job.failed ?? 0,
            startedAt: registration.job.startedAt,
            finishedAt: registration.job.finishedAt ?? null,
          }
        : null,
      toolPresent: registration?.toolPresent ?? false,
      antiBan: DEFAULT_ANTI_BAN,
    };
  });

  const stats = () => Object.fromEntries(providers.map((provider) => {
    const pool = pools.get(provider.id);
    return [provider.id, {
      accounts: pool.size,
      healthy: pool.healthy().length,
      keys: pool.accounts.filter((account) => account.gatewayKey).length,
      running: registrations.get(provider.id)?.job?.status === "running",
    }];
  }));

  const reloadAll = () => {
    for (const pool of pools.values()) pool.reload?.();
  };

  return {
    providers,
    pools,
    registrations,
    relay,
    list,
    stats,
    reloadAll,
    requireProvider,
    // 便捷转发
    maskAccount,
    importAccount,
    probe: (providerId) => {
      const { provider, pool } = requireProvider(providerId);
      return probeProvider({ provider, pool });
    },
    endpoints: (providerId) => resolveEndpoints(requireProvider(providerId).provider),
  };
}
