<!--
  DeepAI Code provider settings (Open WebUI fork addition).

  One page that does the whole "subscription -> API key" job: link the DeepAI
  account with OAuth, mint gateway keys, and register the gateway as an OpenAI
  connection so the models appear in the picker.
-->
<script lang="ts">
	import { onMount, onDestroy, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getDeepAIStatus,
		updateDeepAISettings,
		startDeepAILogin,
		exchangeDeepAICode,
		removeDeepAIAccount,
		createDeepAIKey,
		revokeDeepAIKey,
		deleteDeepAIKey,
		connectDeepAIToOpenWebUI,
		getDeepAIRegister,
		startDeepAIRegister,
		stopDeepAIRegister,
		getDeepAIRegisterLog,
		getDeepAIRegisterDomains,
		downloadDeepAIRegisterLog,
		getPoeAccounts,
		deletePoeAccount,
		setPoeAccountStatus,
		getPoeRegister,
		startPoeRegister,
		stopPoeRegister,
		downloadPoeLog
	} from '$lib/apis/deepai';

	const i18n: any = getContext('i18n');

	// Kept for signature parity with the other admin panels, which all receive
	// the shared save handler; this page saves through the DeepAI router itself.
	export let saveHandler: Function = () => {};

	let loading = true;
	let busy = '';
	let status: any = null;
	let error = '';

	let gatewayUrl = '';
	let adminToken = '';
	let manualCode = '';
	let showManual = false;

	let newKeyLabel = 'openwebui';
	let newKeyRpm = 0;
	let newKeyDaily = 0;
	let revealedKey = '';

	const token = () => localStorage.token ?? '';

	async function load() {
		loading = true;
		error = '';
		try {
			status = await getDeepAIStatus(token());
			gatewayUrl = status?.gateway_url ?? '';
		} catch (err: any) {
			error = err?.message ?? '读取网关状态失败';
		}
		loading = false;
	}

	async function saveSettings() {
		busy = 'settings';
		try {
			await updateDeepAISettings(token(), gatewayUrl || null, adminToken || null);
			adminToken = '';
			toast.success('已保存网关设置');
			await load();
		} catch (err: any) {
			toast.error(err?.message ?? '保存失败');
		}
		busy = '';
	}

	async function signIn() {
		busy = 'login';
		try {
			const res = await startDeepAILogin(token());
			if (res?.url) window.open(res.url, '_blank', 'noopener');
			toast.success('已打开 DeepAI 登录页；完成后点刷新');
		} catch (err: any) {
			toast.error(err?.message ?? '无法发起登录');
		}
		busy = '';
	}

	async function submitCode() {
		if (!manualCode.trim()) return;
		busy = 'exchange';
		try {
			const raw = manualCode.trim();
			const code = raw.includes('code=') ? new URLSearchParams(raw.split('?')[1] ?? '').get('code') ?? raw : raw;
			await exchangeDeepAICode(token(), code);
			manualCode = '';
			showManual = false;
			toast.success('账号已连接');
			await load();
		} catch (err: any) {
			toast.error(err?.message ?? '登录未完成');
		}
		busy = '';
	}

	async function removeAccount(id: string) {
		if (!confirm('移除这个账号？')) return;
		busy = 'account-' + id;
		try {
			await removeDeepAIAccount(token(), id);
			toast.success('已移除');
			await load();
		} catch (err: any) {
			toast.error(err?.message ?? '移除失败');
		}
		busy = '';
	}

	async function mintKey() {
		busy = 'key';
		try {
			const res = await createDeepAIKey(token(), newKeyLabel, newKeyRpm, newKeyDaily);
			revealedKey = res?.key ?? '';
			toast.success('Key 已创建，只显示这一次');
			await load();
		} catch (err: any) {
			toast.error(err?.message ?? '创建失败');
		}
		busy = '';
	}

	async function revokeKey(id: string) {
		busy = 'revoke-' + id;
		try {
			await revokeDeepAIKey(token(), id);
			await load();
		} catch (err: any) {
			toast.error(err?.message ?? '撤销失败');
		}
		busy = '';
	}

	async function dropKey(id: string) {
		if (!confirm('删除后该 Key 立即失效，继续？')) return;
		busy = 'drop-' + id;
		try {
			await deleteDeepAIKey(token(), id);
			await load();
		} catch (err: any) {
			toast.error(err?.message ?? '删除失败');
		}
		busy = '';
	}

	async function connect() {
		busy = 'connect';
		try {
			const res = await connectDeepAIToOpenWebUI(token());
			toast.success(`已接入 Open WebUI：${res?.api_base ?? ''}`);
			await load();
		} catch (err: any) {
			toast.error(err?.message ?? '接入失败');
		}
		busy = '';
	}

	async function copyKey() {
		try {
			await navigator.clipboard.writeText(revealedKey);
			toast.success('已复制');
		} catch {
			toast.error('复制失败，请手动选中');
		}
	}

	const formatTime = (iso: string | null) => (iso ? iso.slice(0, 19).replace('T', ' ') : '—');
	const priceLabel = (model: any) => {
		const raw = model?.theos_per_request;
		if (raw === null || raw === undefined) return '未知';
		return String(raw) === '0' ? '免费' : `${raw} Theos/次`;
	};
	const isFree = (model: any) => String(model?.theos_per_request ?? '1') === '0';

	// ------------------------------------------------------------ bulk signup
	//
	// The gateway runs tools/deepai-reg as a child process; every account it
	// creates is pushed back through /api/admin/accounts, so the pool below
	// grows while this panel polls the job.

	let register: any = null;
	let registerTimer: any = null;
	let wasRunning = false;

	let regCount = 2;
	let regConcurrency = 1;
	let regEngine: 'auto' | 'protocol' | 'browser' = 'auto';
	let regProvider: 'tempmail_lol' | 'mailtm' | 'imap' | 'fixed' = 'tempmail_lol';
	let regDomains = '';
	let regProxies = '';

	let logLevel: '' | 'debug' | 'info' | 'warn' | 'error' = '';
	let domainsPayload: any = null;
	let exporting = false;

	const job = () => register?.job ?? null;
	const splitTokens = (raw: string) =>
		raw
			.split(/[\s,;]+/)
			.map((s) => s.trim())
			.filter(Boolean);

	const progress = () => job()?.progress ?? { ratio: 0, settled: 0, requested: 0, etaSeconds: null };
	const progressPct = () => Math.max(0, Math.min(100, Math.round((progress().ratio ?? 0) * 100)));

	const formatDuration = (seconds: number | null | undefined) => {
		if (seconds === null || seconds === undefined) return '—';
		const total = Math.max(0, Math.round(seconds));
		const minutes = Math.floor(total / 60);
		const rest = total % 60;
		return minutes > 0 ? `${minutes} 分 ${rest} 秒` : `${rest} 秒`;
	};

	const visibleLines = () => {
		const lines = job()?.lines ?? [];
		const filtered = logLevel ? lines.filter((line: any) => line.level === logLevel) : lines;
		return filtered.slice(-120);
	};

	// ok/bad per domain, biggest first, for the little bar chart
	const chartRows = () => {
		const rows = domainsPayload?.domains ?? job()?.domainStats ?? [];
		return [...rows]
			.map((row: any) => ({
				domain: row.domain,
				provider: row.provider,
				ok: Number(row.ok_count ?? 0),
				bad: Number(row.bad_count ?? 0)
			}))
			.filter((row) => row.ok + row.bad > 0)
			.sort((a, b) => b.ok + b.bad - (a.ok + a.bad))
			.slice(0, 12);
	};
	const chartMax = () => Math.max(1, ...chartRows().map((row) => row.ok + row.bad));

	async function loadDomains(force = false) {
		try {
			domainsPayload = await getDeepAIRegisterDomains(token(), force);
		} catch (err) {
			console.error(err);
		}
	}

	async function refreshLog() {
		try {
			const res = await getDeepAIRegisterLog(token(), logLevel, 120);
			if (register?.job) register.job.lines = res?.lines ?? register.job.lines;
		} catch (err) {
			console.error(err);
		}
	}

	async function exportLog() {
		exporting = true;
		try {
			const blob = await downloadDeepAIRegisterLog(token(), logLevel);
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = `deepai-register-${logLevel || 'all'}.log`;
			anchor.click();
			URL.revokeObjectURL(url);
			toast.success('日志已导出');
		} catch (err: any) {
			toast.error(err?.message ?? '导出失败');
		}
		exporting = false;
	}

	async function loadRegister() {
		try {
			register = await getDeepAIRegister(token());
		} catch (err: any) {
			// A stopped gateway should not spam toasts; the panel just stays stale.
			console.error(err);
			return;
		}
		const running = register?.running === true;
		if (running) {
			ensurePoller();
		} else if (wasRunning) {
			stopPoller();
			wasRunning = false;
			const created = job()?.created ?? 0;
			if (created) {
				toast.success(`批量注册结束：新增 ${created} 个账号`);
				await load();
			}
			await loadDomains(true);
		}
		wasRunning = running;
	}

	function ensurePoller() {
		if (registerTimer) return;
		registerTimer = setInterval(loadRegister, 2500);
	}

	function stopPoller() {
		if (!registerTimer) return;
		clearInterval(registerTimer);
		registerTimer = null;
	}

	async function startRegister() {
		busy = 'register';
		try {
			register = await startDeepAIRegister(token(), {
				count: Number(regCount) || 1,
				engine: regEngine,
				concurrency: Number(regConcurrency) || 1,
				mailProvider: regProvider,
				mailDomains: splitTokens(regDomains),
				proxies: splitTokens(regProxies),
				verifyEmail: false,
				headless: true
			});
			wasRunning = true;
			ensurePoller();
			toast.success('已开始注册，账号会自动入池');
		} catch (err: any) {
			toast.error(err?.message ?? '启动失败');
		}
		busy = '';
	}

	async function stopRegister() {
		busy = 'register-stop';
		try {
			register = await stopDeepAIRegister(token());
			stopPoller();
			wasRunning = false;
			toast.success('已请求停止');
		} catch (err: any) {
			toast.error(err?.message ?? '停止失败');
		}
		busy = '';
	}

	onMount(() => {
		load();
		loadRegister();
		loadDomains();
		loadPoe();
	});
	onDestroy(() => {
		stopPoller();
		stopPoePoller();
	});

	// ------------------------------------------------------------- poe pool
	//
	// Poe is a separate pool with its own life cycle. Worth knowing before
	// reading the numbers: with no SMS provider every account stops at
	// email_verified/phone_required — Poe refuses to create the account until a
	// phone was verified recently. That is a Poe rule, not a bug here.

	let poe: any = null;
	let poeAccounts: any[] = [];
	let poeCounts: any = {};
	let poeTimer: any = null;
	let poeWasRunning = false;
	let poeCount = 1;
	let poeEngine: 'auto' | 'protocol' | 'browser' = 'protocol';
	let poeMail: 'tempmail_lol' | 'mailtm' | 'imap' | 'fixed' = 'tempmail_lol';
	let poeSms: 'off' | 'manual' | 'sms_activate' | 'smspool' = 'off';

	const poeJob = () => poe?.job ?? null;
	const poePct = () =>
		Math.max(0, Math.min(100, Math.round((poeJob()?.progress?.ratio ?? 0) * 100)));

	async function loadPoe() {
		try {
			const [job, pool] = await Promise.all([getPoeRegister(token()), getPoeAccounts(token(), 100)]);
			poe = job;
			poeAccounts = pool?.accounts ?? [];
			poeCounts = pool?.counts ?? {};
			const running = job?.running === true;
			if (running) {
				ensurePoePoller();
			} else if (poeWasRunning) {
				stopPoePoller();
				if ((poeJob()?.created ?? 0) > 0) toast.success('Poe 注册结束，账号已进独立号池');
			}
			poeWasRunning = running;
		} catch (err) {
			console.error(err);
		}
	}

	function ensurePoePoller() {
		if (poeTimer) return;
		poeTimer = setInterval(loadPoe, 3000);
	}

	function stopPoePoller() {
		if (!poeTimer) return;
		clearInterval(poeTimer);
		poeTimer = null;
	}

	async function startPoe() {
		busy = 'poe-start';
		try {
			poe = await startPoeRegister(token(), {
				count: Number(poeCount) || 1,
				engine: poeEngine,
				concurrency: 1,
				mailProvider: poeMail,
				mailDomains: [],
				smsProvider: poeSms,
				headless: true
			});
			poeWasRunning = true;
			ensurePoePoller();
			toast.success('Poe 注册已开始');
		} catch (err: any) {
			toast.error(err?.message ?? '启动失败');
		}
		busy = '';
	}

	async function stopPoe() {
		busy = 'poe-stop';
		try {
			poe = await stopPoeRegister(token());
			stopPoePoller();
			poeWasRunning = false;
			toast.success('已请求停止');
		} catch (err: any) {
			toast.error(err?.message ?? '停止失败');
		}
		busy = '';
	}

	async function removePoe(account: any) {
		if (!confirm(`移除 Poe 账号 ${account.email}？`)) return;
		busy = 'poe-' + account.id;
		try {
			await deletePoeAccount(token(), account.id);
			toast.success('已移除');
			await loadPoe();
		} catch (err: any) {
			toast.error(err?.message ?? '移除失败');
		}
		busy = '';
	}

	async function markPoe(account: any, status: string) {
		busy = 'poe-' + account.id;
		try {
			await setPoeAccountStatus(token(), account.email, status);
			await loadPoe();
		} catch (err: any) {
			toast.error(err?.message ?? '状态更新失败');
		}
		busy = '';
	}

	async function exportPoeLog() {
		try {
			const blob = await downloadPoeLog(token(), '');
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = 'poe-register.log';
			anchor.click();
			URL.revokeObjectURL(url);
			toast.success('日志已导出');
		} catch (err: any) {
			toast.error(err?.message ?? '导出失败');
		}
	}
</script>

<div class="flex flex-col gap-5 text-sm">
	<div>
		<div class="mb-1 text-lg font-medium">{$i18n.t('DeepAI Code')}</div>
		<div class="text-xs text-gray-500 dark:text-gray-400">
			把 DeepAI 订阅账号接成 OpenAI 兼容的 API。OAuth 登录在网关侧完成，密钥由网关托管，这里只做管理。
		</div>
	</div>

	{#if error}
		<div class="rounded-lg bg-red-50 px-4 py-3 text-xs text-red-600 dark:bg-red-900/20 dark:text-red-300">
			{error}
		</div>
	{/if}

	<!-- gateway settings -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-3 font-medium">网关</div>
		<div class="flex flex-col gap-2">
			<label class="text-xs text-gray-500 dark:text-gray-400" for="deepai-gateway">网关地址</label>
			<input
				class="w-full rounded-lg bg-transparent px-3 py-2 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				id="deepai-gateway"
				bind:value={gatewayUrl}
				placeholder="http://127.0.0.1:8787"
			/>
			<label class="mt-1 text-xs text-gray-500 dark:text-gray-400" for="deepai-admin-token">管理口令</label>
			<input
				type="password"
				class="w-full rounded-lg bg-transparent px-3 py-2 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				id="deepai-admin-token"
				bind:value={adminToken}
				placeholder={status?.has_admin_token ? '已配置（留空则保持不变）' : '网关启动时打印的 token'}
			/>
		</div>
		<div class="mt-3 flex items-center gap-2">
			<button
				class="rounded-lg bg-black px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
				on:click={saveSettings}
				disabled={busy === 'settings'}
			>
				保存
			</button>
			<button class="rounded-lg px-3 py-1.5 text-sm ring-1 ring-gray-200 dark:ring-gray-700" on:click={load}>
				刷新
			</button>
			{#if status}
				<span class="text-xs {status.reachable ? 'text-green-600' : 'text-red-500'}">
					{status.reachable ? '网关在线' : '网关不可达'}
				</span>
			{/if}
		</div>
	</div>

	<!-- account pool -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-1 font-medium">账号池</div>
		<div class="mb-3 text-xs text-gray-500 dark:text-gray-400">
			可以连接多个账号。调度器按健康状态、冷却和粘性会话分配请求，一个账号被拒会自动换下一个。
		</div>

		{#if (status?.state?.accounts ?? []).length === 0}
			<div class="text-xs text-gray-500 dark:text-gray-400">还没有账号。</div>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full text-xs">
					<thead class="text-gray-500 dark:text-gray-400">
						<tr>
							<th class="py-1 text-left font-normal">名称</th>
							<th class="py-1 text-left font-normal">账号</th>
							<th class="py-1 text-left font-normal">套餐</th>
							<th class="py-1 text-left font-normal">状态</th>
							<th class="py-1 text-left font-normal">调用</th>
							<th class="py-1 text-left font-normal">最近使用</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each status.state.accounts as account (account.id)}
							<tr class="border-t border-gray-100 dark:border-gray-800">
								<td class="py-1.5">{account.label ?? '—'}</td>
								<td class="py-1.5">{account.username || '—'}</td>
								<td class="py-1.5">{account.isPro ? 'Pro' : '免费'}</td>
								<td class="py-1.5">
									<span class={account.healthy ? 'text-green-600' : 'text-red-500'}>
										{account.healthy ? '正常' : '异常'}
									</span>
								</td>
								<td class="py-1.5">{account.requests ?? 0}</td>
								<td class="py-1.5">{formatTime(account.lastUsedAt)}</td>
								<td class="py-1.5 text-right">
									<button
										class="text-red-500 disabled:opacity-50"
										on:click={() => removeAccount(account.id)}
										disabled={busy === 'account-' + account.id}>移除</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		<div class="mt-3 flex flex-wrap items-center gap-2">
			<button
				class="rounded-lg bg-black px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
				on:click={signIn}
				disabled={busy === 'login'}
			>
				用 DeepAI 账号登录
			</button>
			<button
				class="rounded-lg px-3 py-1.5 text-sm ring-1 ring-gray-200 dark:ring-gray-700"
				on:click={() => (showManual = !showManual)}
			>
				手动粘贴 code
			</button>
			{#if status?.state?.usage}
				<span class="text-xs text-gray-500 dark:text-gray-400">
					余额 {status.state.usage.balance ?? '—'} Theos · 刷新 {status.state.usage.refresh_at ?? '—'}
				</span>
			{/if}
		</div>

		{#if showManual}
			<div class="mt-2 flex gap-2">
				<input
					class="flex-1 rounded-lg bg-transparent px-3 py-2 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
					bind:value={manualCode}
					placeholder="粘贴回跳地址里的 code 参数"
				/>
				<button
					class="rounded-lg px-3 py-1.5 text-sm ring-1 ring-gray-200 dark:ring-gray-700 disabled:opacity-50"
					on:click={submitCode}
					disabled={busy === 'exchange'}>完成</button
				>
			</div>
		{/if}
	</div>

	<!-- bulk signup -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-1 font-medium">批量注册 · 自动入池</div>
		<div class="mb-3 text-xs text-gray-500 dark:text-gray-400">
			网关会在本机拉起 <span class="font-mono">tools/deepai-reg</span>，按邮箱域名池自动注册，
			每个新账号的 API Key 立刻写进上面的账号池（调度器同时刷新目录，不用手动登录）。
		</div>

		<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">数量</span>
				<input
					type="number"
					min="1"
					max="200"
					bind:value={regCount}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				/>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">并发</span>
				<input
					type="number"
					min="1"
					max="8"
					bind:value={regConcurrency}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				/>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">引擎</span>
				<select
					bind:value={regEngine}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				>
					<option value="auto">auto（协议优先，被拦换 Camoufox）</option>
					<option value="protocol">protocol（纯 HTTP，最快）</option>
					<option value="browser">browser（Camoufox）</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">邮箱来源</span>
				<select
					bind:value={regProvider}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				>
					<option value="tempmail_lol">tempmail.lol（免注册）</option>
					<option value="mailtm">mail.tm</option>
					<option value="imap">自有域名 catch-all</option>
					<option value="fixed">固定地址 +tag</option>
				</select>
			</label>
		</div>

		<div class="mt-2 grid gap-2 sm:grid-cols-2">
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">
					邮箱域名池（逗号或换行分隔；留空 = 只用自动学习到的可用域名）
				</span>
				<textarea
					rows="2"
					bind:value={regDomains}
					placeholder="gloatinggoat.com, sablereels.com"
					class="w-full rounded-lg bg-transparent px-2 py-1.5 font-mono text-xs outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				></textarea>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">
					代理池（一行一个，留空 = 用网关进程的 HTTPS_PROXY）
				</span>
				<textarea
					rows="2"
					bind:value={regProxies}
					placeholder="http://127.0.0.1:10808"
					class="w-full rounded-lg bg-transparent px-2 py-1.5 font-mono text-xs outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				></textarea>
			</label>
		</div>

		<div class="mt-3 flex flex-wrap items-center gap-2">
			<button
				class="rounded-lg bg-black px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
				on:click={startRegister}
				disabled={busy === 'register' || register?.running}
			>
				开始注册
			</button>
			{#if register?.running}
				<button
					class="rounded-lg px-3 py-1.5 text-sm text-red-500 ring-1 ring-red-200 disabled:opacity-50 dark:ring-red-900"
					on:click={stopRegister}
					disabled={busy === 'register-stop'}
				>
					停止
				</button>
			{/if}
			{#if job()}
				<span class="text-xs text-gray-500 dark:text-gray-400">
					成功 {job().created ?? 0}/{job().requested ?? 0} · 入池 {job().pushed ?? 0} · 失败
					{job().failed ?? 0} · {job().status}
				</span>
			{/if}
			{#if register && register.toolPresent === false}
				<span class="text-xs text-red-500">
					找不到 tools/deepai-reg（可用 DEEPAI_REGISTER_DIR 指定）
				</span>
			{/if}
		</div>

		{#if job()}
			<div class="mt-3">
				<div class="mb-1 flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
					<span>
						进度 {progress().settled ?? 0}/{progress().requested ?? 0}（{progressPct()}%）
						{#if job().retrying}
							· 第 {job().retryRound} 轮重试 {job().retrying} 个
						{/if}
					</span>
					<span>
						{#if register?.running}
							预计剩余 {formatDuration(progress().etaSeconds)} · {progress().ratePerMinute ?? 0} 个/分
						{:else}
							用时 {formatDuration(job().progress?.elapsedSeconds ?? job().summary?.seconds)}
						{/if}
					</span>
				</div>
				<div class="h-2 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
					<div
						class="h-full rounded-full transition-all duration-500 {job().status === 'failed'
							? 'bg-red-500'
							: job().status === 'running'
								? 'bg-blue-500'
								: 'bg-green-500'}"
						style="width: {progressPct()}%"
					></div>
				</div>
			</div>
		{/if}

		{#if job()?.current}
			<div class="mt-1 font-mono text-xs text-gray-500 dark:text-gray-400">
				当前：{job().current.email}（{job().current.domain ?? '—'}）
			</div>
		{/if}

		{#if (job()?.failureReasons && Object.keys(job().failureReasons).length > 0) || (job()?.summary?.failure_reasons ?? []).length > 0}
			<div class="mt-2 text-xs text-gray-500 dark:text-gray-400">
				失败原因：
				{#each Object.entries(job()?.failureReasons ?? {}) as [kind, row]}
					<span class="mr-2 inline-block">
						<span class="font-mono text-red-500">{kind}</span>×{(row as any).count}
						{#if (row as any).example}<span class="text-gray-400">（{(row as any).example}）</span>{/if}
					</span>
				{/each}
				{#each job()?.summary?.failure_reasons ?? [] as row (row.kind)}
					<span class="mr-2 inline-block">
						<span class="font-mono text-red-500">{row.kind}</span>×{row.count}
					</span>
				{/each}
			</div>
		{/if}

		<div class="mt-3 flex flex-wrap items-center gap-1">
			{#each [['', '全部'], ['info', 'info'], ['warn', 'warn'], ['error', 'error'], ['debug', 'debug']] as [value, label]}
				<button
					class="rounded px-2 py-0.5 text-[11px] ring-1 {logLevel === value
						? 'bg-gray-900 text-white ring-gray-900 dark:bg-white dark:text-black dark:ring-white'
						: 'ring-gray-200 text-gray-500 dark:ring-gray-700'}"
					on:click={() => {
						logLevel = value as any;
						refreshLog();
					}}>{label}</button
				>
			{/each}
			<button
				class="ml-auto rounded px-2 py-0.5 text-[11px] ring-1 ring-gray-200 disabled:opacity-50 dark:ring-gray-700"
				on:click={exportLog}
				disabled={exporting || !job()}>导出日志</button
			>
			<button
				class="rounded px-2 py-0.5 text-[11px] ring-1 ring-gray-200 dark:ring-gray-700"
				on:click={() => loadDomains(true)}>刷新域名</button
			>
		</div>

		{#if visibleLines().length > 0}
			<pre
				class="mt-1 max-h-48 overflow-y-auto rounded-lg bg-gray-50 p-2 font-mono text-[11px] leading-4 text-gray-600 dark:bg-gray-900 dark:text-gray-300">{visibleLines()
					.map((line: any) => `${line.at?.slice(11, 19) ?? ''} ${String(line.level ?? '').toUpperCase().padEnd(5)} ${line.text}`)
					.join('\n')}</pre>
		{/if}

		{#if chartRows().length > 0}
			<div class="mt-3">
				<div class="mb-1 flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
					<span>域名战绩（成功/失败）</span>
					<span class="text-[11px]">共 {chartRows().length} 个域 · 数据 {domainsPayload?.source ?? 'job'}</span>
				</div>
				<div class="flex flex-col gap-1">
					{#each chartRows() as row (row.provider + row.domain)}
						<div class="flex items-center gap-2 text-[11px]">
							<span class="w-40 truncate font-mono text-gray-600 dark:text-gray-300" title={row.domain}>
								{row.domain}
							</span>
							<svg class="h-3 flex-1" viewBox="0 0 100 10" preserveAspectRatio="none">
								<rect x="0" y="0" width="100" height="10" fill="currentColor" class="text-gray-100 dark:text-gray-800" />
								<rect
									x="0"
									y="0"
									height="10"
									fill="#22c55e"
									width={(row.ok / chartMax()) * 100}
								/>
								<rect
									x={(row.ok / chartMax()) * 100}
									y="0"
									height="10"
									fill="#ef4444"
									width={(row.bad / chartMax()) * 100}
								/>
							</svg>
							<span class="w-16 text-right">
								<span class="text-green-600">{row.ok}</span>
								{#if row.bad}<span class="text-red-500"> / {row.bad}</span>{/if}
							</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}
	</div>

	<!-- poe pool -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-1 flex items-center gap-2">
			<span class="font-medium">Poe 号池（独立）</span>
			<span class="text-[11px] text-gray-400">
				凭据是 p-b cookie，和上面的 DeepAI api_key 池完全隔离
			</span>
		</div>
		<div class="mb-3 text-xs text-gray-500 dark:text-gray-400">
			Poe 的注册流程：邮箱验证码 → <span class="font-mono">Verify</span> → 要求手机号。
			<b>没有短信供应商时账号会停在</b> <span class="font-mono">email_verified</span> /
			<span class="font-mono">phone_required</span>，这是 Poe 的规则：它只给"最近验证过手机号"的会话建号。
		</div>

		<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">数量</span>
				<input
					type="number"
					min="1"
					max="50"
					bind:value={poeCount}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				/>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">引擎</span>
				<select
					bind:value={poeEngine}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				>
					<option value="protocol">protocol（页内 GraphQL）</option>
					<option value="browser">browser（Camoufox 表单）</option>
					<option value="auto">auto</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">邮箱来源</span>
				<select
					bind:value={poeMail}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				>
					<option value="tempmail_lol">tempmail.lol</option>
					<option value="mailtm">mail.tm</option>
					<option value="imap">自有域名 catch-all</option>
					<option value="fixed">固定地址 +tag</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-xs text-gray-500 dark:text-gray-400">短信</span>
				<select
					bind:value={poeSms}
					class="w-full rounded-lg bg-transparent px-2 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
				>
					<option value="off">off（停在手机号门槛）</option>
					<option value="manual">manual（手动输码）</option>
					<option value="sms_activate">sms-activate</option>
					<option value="smspool">smspool</option>
				</select>
			</label>
		</div>

		<div class="mt-3 flex flex-wrap items-center gap-2">
			<button
				class="rounded-lg bg-black px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
				on:click={startPoe}
				disabled={busy === 'poe-start' || poe?.running}
			>
				开始 Poe 注册
			</button>
			{#if poe?.running}
				<button
					class="rounded-lg px-3 py-1.5 text-sm text-red-500 ring-1 ring-red-200 disabled:opacity-50 dark:ring-red-900"
					on:click={stopPoe}
					disabled={busy === 'poe-stop'}>停止</button
				>
			{/if}
			<button class="rounded-lg px-3 py-1.5 text-sm ring-1 ring-gray-200 dark:ring-gray-700" on:click={loadPoe}>
				刷新
			</button>
			<button
				class="rounded-lg px-3 py-1.5 text-sm ring-1 ring-gray-200 disabled:opacity-50 dark:ring-gray-700"
				on:click={exportPoeLog}
				disabled={!poeJob()}>导出日志</button
			>
			<span class="text-xs text-gray-500 dark:text-gray-400">
				验证 {poeCounts.email_verified ?? 0} · 待手机 {poeCounts.phone_required ?? 0} · 可用
				{poeCounts.active ?? 0} · 失效 {poeCounts.dead ?? 0}
			</span>
		</div>

		{#if poeJob()}
			<div class="mt-3">
				<div class="mb-1 flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
					<span>进度 {poeJob().created ?? 0}/{poeJob().requested ?? 0}（{poePct()}%）· {poeJob().status}</span>
					<span>
						{#if poe?.running && poeJob()?.progress?.etaSeconds !== null}
							预计剩余 {formatDuration(poeJob().progress?.etaSeconds)}
						{:else}
							用时 {formatDuration(poeJob().progress?.elapsedSeconds)}
						{/if}
					</span>
				</div>
				<div class="h-2 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
					<div
						class="h-full rounded-full {poeJob().status === 'running' ? 'bg-purple-500' : 'bg-green-500'}"
						style="width: {poePct()}%"
					></div>
				</div>
			</div>
			{#if (poeJob()?.lines ?? []).length > 0}
				<pre
					class="mt-2 max-h-40 overflow-y-auto rounded-lg bg-gray-50 p-2 font-mono text-[11px] leading-4 text-gray-600 dark:bg-gray-900 dark:text-gray-300">{(poeJob().lines ?? [])
						.slice(-60)
						.map((line: any) => `${line.at?.slice(11, 19) ?? ''} ${line.text}`)
						.join('\n')}</pre>
			{/if}
		{/if}

		{#if poeAccounts.length > 0}
			<div class="mt-3 max-h-64 overflow-y-auto">
				<table class="w-full text-xs">
					<thead class="sticky top-0 bg-white text-gray-500 dark:bg-gray-900 dark:text-gray-400">
						<tr>
							<th class="py-1 text-left font-normal">邮箱</th>
							<th class="py-1 text-left font-normal">状态</th>
							<th class="py-1 text-left font-normal">引擎</th>
							<th class="py-1 text-left font-normal">Cookie</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each poeAccounts as account (account.id)}
							<tr class="border-t border-gray-100 dark:border-gray-800">
								<td class="py-1.5 font-mono">{account.email}</td>
								<td class="py-1.5">
									<span
										class={account.status === 'active'
											? 'text-green-600'
											: account.status === 'dead'
												? 'text-red-500'
												: 'text-amber-600'}>{account.status}</span
									>
								</td>
								<td class="py-1.5">{account.engine || '—'}</td>
								<td class="py-1.5">{(account.cookieNames ?? []).length} 个</td>
								<td class="py-1.5 text-right">
									{#if account.status !== 'active'}
										<button
											class="mr-2 text-green-600 disabled:opacity-50"
											on:click={() => markPoe(account, 'active')}
											disabled={busy === 'poe-' + account.id}>标记可用</button
										>
									{/if}
									<button
										class="text-red-500 disabled:opacity-50"
										on:click={() => removePoe(account)}
										disabled={busy === 'poe-' + account.id}>移除</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>

	<!-- api keys -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-1 font-medium">API Keys</div>
		<div class="mb-3 text-xs text-gray-500 dark:text-gray-400">
			给 Open WebUI 和其它 OpenAI 兼容工具用。完整 Key 只在创建时显示一次（网关只存 SHA-256）。
		</div>

		{#if (status?.state?.keys ?? []).length === 0}
			<div class="text-xs text-gray-500 dark:text-gray-400">还没有 Key。</div>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full text-xs">
					<thead class="text-gray-500 dark:text-gray-400">
						<tr>
							<th class="py-1 text-left font-normal">备注</th>
							<th class="py-1 text-left font-normal">前缀</th>
							<th class="py-1 text-left font-normal">限额</th>
							<th class="py-1 text-left font-normal">调用</th>
							<th class="py-1 text-left font-normal">最近使用</th>
							<th class="py-1 text-left font-normal">状态</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each status.state.keys as key (key.id)}
							<tr class="border-t border-gray-100 dark:border-gray-800">
								<td class="py-1.5">{key.label}</td>
								<td class="py-1.5 font-mono">{key.prefix}…</td>
								<td class="py-1.5">
									{key.limits?.rpm ? key.limits.rpm + ' 次/分' : '不限'}{key.limits?.dailyRequests
										? ' · ' + key.limits.dailyRequests + ' 次/日'
										: ''}
								</td>
								<td class="py-1.5">{key.requests ?? 0}</td>
								<td class="py-1.5">{formatTime(key.lastUsedAt)}</td>
								<td class="py-1.5">
									<span class={key.revoked ? 'text-red-500' : 'text-green-600'}>
										{key.revoked ? '已撤销' : '启用'}
									</span>
								</td>
								<td class="py-1.5 text-right">
									{#if !key.revoked}
										<button
											class="mr-2 disabled:opacity-50"
											on:click={() => revokeKey(key.id)}
											disabled={busy === 'revoke-' + key.id}>撤销</button
										>
									{/if}
									<button
										class="text-red-500 disabled:opacity-50"
										on:click={() => dropKey(key.id)}
										disabled={busy === 'drop-' + key.id}>删除</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		<div class="mt-3 flex flex-wrap items-end gap-2">
			<div class="flex flex-col">
				<label class="mb-1 text-xs text-gray-500 dark:text-gray-400" for="deepai-key-label">备注</label>
				<input
					class="rounded-lg bg-transparent px-3 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
					id="deepai-key-label"
					bind:value={newKeyLabel}
				/>
			</div>
			<div class="flex flex-col">
				<label class="mb-1 text-xs text-gray-500 dark:text-gray-400" for="deepai-key-rpm">次/分</label>
				<input
					type="number"
					min="0"
					class="w-20 rounded-lg bg-transparent px-3 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
					id="deepai-key-rpm"
					bind:value={newKeyRpm}
				/>
			</div>
			<div class="flex flex-col">
				<label class="mb-1 text-xs text-gray-500 dark:text-gray-400" for="deepai-key-daily">次/日</label>
				<input
					type="number"
					min="0"
					class="w-20 rounded-lg bg-transparent px-3 py-1.5 text-sm outline-none ring-1 ring-gray-200 dark:ring-gray-700"
					id="deepai-key-daily"
					bind:value={newKeyDaily}
				/>
			</div>
			<button
				class="rounded-lg bg-black px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
				on:click={mintKey}
				disabled={busy === 'key'}>创建 Key</button
			>
		</div>

		{#if revealedKey}
			<div
				class="mt-3 flex items-center gap-2 rounded-lg bg-gray-50 px-3 py-2 dark:bg-gray-900/40"
			>
				<code class="flex-1 break-all font-mono text-xs">{revealedKey}</code>
				<button class="text-xs" on:click={copyKey}>复制</button>
			</div>
		{/if}
	</div>

	<!-- connect -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-1 font-medium">接到 Open WebUI</div>
		<div class="mb-3 text-xs text-gray-500 dark:text-gray-400">
			首次打开本页时已自动接入并生成 Key，模型列表里应当直接出现 DeepAI 的全部模型。
		如果换了网关或删掉了 Key，点下面的按钮重新同步一次。
		</div>
		<button
			class="rounded-lg bg-black px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
			on:click={connect}
			disabled={busy === 'connect' || !status?.reachable}
		>
			重新同步接入
		</button>
	</div>

	<!-- models -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-3 font-medium">模型目录</div>
		{#if (status?.state?.models ?? []).length === 0}
			<div class="text-xs text-gray-500 dark:text-gray-400">目录不可用（先连接账号）。</div>
		{:else}
			<div class="max-h-80 overflow-y-auto">
				<table class="w-full text-xs">
					<thead class="sticky top-0 bg-white text-gray-500 dark:bg-gray-900 dark:text-gray-400">
						<tr>
							<th class="py-1 text-left font-normal">模型</th>
							<th class="py-1 text-left font-normal">上下文</th>
							<th class="py-1 text-left font-normal">能力</th>
							<th class="py-1 text-right font-normal">价格</th>
						</tr>
					</thead>
					<tbody>
						{#each status.state.models as model (model.id)}
							<tr class="border-t border-gray-100 dark:border-gray-800">
								<td class="py-1.5 font-mono">{model.id}</td>
								<td class="py-1.5">{Math.round((model.context_window ?? 0) / 1000)}k</td>
								<td class="py-1.5">{(model.input ?? []).join('+')}{model.reasoning ? ' · 推理' : ''}</td>
								<td class="py-1.5 text-right {isFree(model) ? 'text-green-600' : 'text-amber-600'}">
									{priceLabel(model)}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>

	{#if loading}
		<div class="text-xs text-gray-500 dark:text-gray-400">加载中…</div>
	{/if}
</div>


