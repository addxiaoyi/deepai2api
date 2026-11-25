<!--
  MoonAI provider card (Open WebUI fork addition).

  一张卡把 MoonAI 这条线走完：
    批量注册（协议腿 / 指纹浏览器腿）→ 号池 → 自检 → 导出 → 编程工具接入地址。

  数据和状态都在网关的 /api/admin/providers/moonai/* 里；这里只是面板，
  号池文件是 data/moonai-pool.json（与 DeepAI / Allmodels / Poe 完全隔离）。
-->
<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getProvider,
		getProviderRegister,
		startProviderRegister,
		stopProviderRegister,
		getProviderPool,
		importProviderAccount,
		deleteProviderAccount,
		probeProvider,
		downloadProviderPool,
		getProviderConnectInfo
	} from '$lib/apis/deepai';

	const PROVIDER = 'moonai';
	const token = () => localStorage.token ?? '';

	let loading = true;
	let busy = '';
	let detail: any = null;
	let job: any = null;
	let pool: any = null;
	let connect: any = null;
	let error = '';

	// 注册表单
	let count = 3;
	let concurrency = 2;
	let engine: 'auto' | 'protocol' | 'browser' | 'hybrid' = 'auto';
	let browserStack: 'auto' | 'camoufox' | 'patchright' | 'playwright' = 'auto';
	let mailProvider: 'imap' | 'domain' | 'mailtm' | 'fixed' = 'imap';
	let mailDomains = '';
	let proxies = '';
	let cdp = '';
	let headful = false;
	let humanize = 0;
	let keysPerAccount = 1;
	let verifyEmail = true;
	let push = true;
	let exportFormat: '' | 'jsonl' | 'csv' = 'jsonl';

	let timer: any = null;

	// 手工导入：一行一个账号（或一个 JSON 数组），最少要有 email + apiKey/accessToken
	let importText = '';
	let importMintKey = true;
	// 导入后新发的网关 Key 明文只展示这一次（Store 只存哈希）
	let revealedKeys: string[] = [];

	// 放在变量里而不是直接写进 placeholder：Svelte 会把属性里的 { 当插值
	const importPlaceholder =
		'{"email":"a@example.com","accessToken":"eyJ…","refreshToken":"…","apiKey":"api_…"}';
	const list = (v: string) =>
		v
			.split(/[\n,]/)
			.map((s) => s.trim())
			.filter(Boolean);

	async function load() {
		error = '';
		try {
			const [d, r, p] = await Promise.all([
				getProvider(token(), PROVIDER),
				getProviderRegister(token(), PROVIDER),
				getProviderPool(token(), PROVIDER, 200)
			]);
			detail = d;
			job = r?.job ?? null;
			pool = p;
		} catch (e: any) {
			error = e?.message ?? String(e);
		}
		try {
			connect = await getProviderConnectInfo(token(), PROVIDER);
		} catch {
			connect = null;
		}
		loading = false;
	}

	async function refreshJob() {
		try {
			const r = await getProviderRegister(token(), PROVIDER);
			job = r?.job ?? null;
			if (job?.status === 'done' || job?.status === 'failed' || job?.status === 'stopped') {
				await load();
			}
		} catch {
			/* 轮询失败不打扰操作者 */
		}
	}

	async function startRegister() {
		busy = 'start';
		try {
			const r = await startProviderRegister(token(), PROVIDER, {
				count,
				concurrency,
				engine,
				browserStack,
				mailProvider,
				mailDomains: list(mailDomains),
				proxies: list(proxies),
				cdp,
				headful,
				humanize,
				keysPerAccount,
				export: exportFormat,
				mintKeys: true,
				push,
				verifyEmail
			});
			job = r?.job ?? job;
			toast.success('已启动注册');
		} catch (e: any) {
			toast.error(e?.message ?? '启动失败');
		} finally {
			busy = '';
		}
	}

	async function stopRegister() {
		busy = 'stop';
		try {
			await stopProviderRegister(token(), PROVIDER);
			toast.success('已停止');
			await refreshJob();
		} catch (e: any) {
			toast.error(e?.message ?? '停止失败');
		} finally {
			busy = '';
		}
	}

	async function doProbe() {
		busy = 'probe';
		try {
			const r = await probeProvider(token(), PROVIDER);
			r?.ok ? toast.success(`自检通过（${r.ms ?? '?'}ms）`) : toast.error(r?.body ?? r?.reason ?? '自检失败');
		} catch (e: any) {
			toast.error(e?.message ?? '自检失败');
		} finally {
			busy = '';
		}
	}

	async function doExport() {
		busy = 'export';
		try {
			const fmt = (exportFormat || 'jsonl') as 'jsonl' | 'csv' | 'pool';
			const blob = await downloadProviderPool(token(), PROVIDER, fmt);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `moonai-accounts.${fmt === 'csv' ? 'csv' : fmt === 'jsonl' ? 'jsonl' : 'json'}`;
			a.click();
			URL.revokeObjectURL(url);
			toast.success('已导出');
		} catch (e: any) {
			toast.error(e?.message ?? '导出失败');
		} finally {
			busy = '';
		}
	}

	async function removeAccount(id: string, email: string) {
		if (!confirm(`移除 ${email}？已发的网关 Key 不会被撤销。`)) return;
		busy = `rm-${id}`;
		try {
			await deleteProviderAccount(token(), PROVIDER, id);
			toast.success('已移除');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '移除失败');
		} finally {
			busy = '';
		}
	}

	async function doImport() {
		const rows = parseAccounts(importText);
		if (!rows.length) {
			toast.error('没有解析到账号：一行一个 JSON，或一个 JSON 数组');
			return;
		}
		busy = 'import';
		let ok = 0;
		let failed = 0;
		const keys: string[] = [];
		for (const row of rows) {
			try {
				const res = await importProviderAccount(token(), PROVIDER, row, importMintKey);
				ok += 1;
				const plaintext = res?.gatewayKey?.plaintext;
				if (plaintext) keys.push(plaintext);
			} catch (e: any) {
				failed += 1;
				console.error('import failed', row?.email, e);
			}
		}
		busy = '';
		if (ok) {
			toast.success(`已导入 ${ok} 个${failed ? `，失败 ${failed} 个` : ''}`);
			if (keys.length) {
				revealedKeys = keys;
				importText = '';
			}
			await load();
		} else {
			toast.error(`导入失败 ${failed} 个，检查字段是否带 email + apiKey/accessToken`);
		}
	}

	/** 一行一个 JSON，或者整段是一个 JSON 数组；两种都认。 */
	function parseAccounts(text: string): any[] {
		const raw = (text ?? '').trim();
		if (!raw) return [];
		if (raw.startsWith('[')) {
			try {
				const parsed = JSON.parse(raw);
				return Array.isArray(parsed) ? parsed.filter(isAccount) : [];
			} catch {
				return [];
			}
		}
		return raw
			.split(/\r?\n/)
			.map((line) => line.trim())
			.filter(Boolean)
			.map((line) => {
				try {
					return JSON.parse(line);
				} catch {
					return null;
				}
			})
			.filter((row) => row && isAccount(row));
	}

	const isAccount = (row: any) =>
		Boolean(row) &&
		typeof row === 'object' &&
		Boolean(row.email || row.apiKey || row.api_key || row.accessToken || row.access_token);

	function copy(text: string) {
		navigator.clipboard.writeText(text);
		toast.success('已复制');
	}

	const tokenState = (a: any) => {
		if (!a.accessTokenExpiresAt) return '—';
		const left = a.accessTokenExpiresAt - Date.now();
		if (left <= 0) return '已过期';
		return `${Math.round(left / 60000)} 分钟后到期`;
	};

	onMount(async () => {
		await load();
		timer = setInterval(refreshJob, 3000);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="flex flex-col gap-5 text-sm">
	<div>
		<div class="mb-1 text-lg font-medium">MoonAI 号池</div>
		<div class="text-xs text-gray-500 dark:text-gray-400">
			注册机跑在网关侧（<code class="font-mono">tools/moonai-reg</code>），产物进
			<code class="font-mono">data/moonai-pool.json</code>，与 DeepAI / Allmodels / Poe 的池互不影响。
			上游只认 OAuth <code class="font-mono">access_token</code>，面板 <code class="font-mono">api_…</code> Key 仅留档。
		</div>
	</div>

	{#if error}
		<div class="rounded-lg bg-red-50 px-4 py-3 text-xs text-red-600 dark:bg-red-900/20 dark:text-red-400">
			{error}
		</div>
	{/if}

	<!-- 概览 -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-3 flex flex-wrap items-center gap-3">
			<div class="font-medium">概览</div>
			{#if detail}
				<span class="rounded bg-gray-100 px-2 py-0.5 text-xs dark:bg-gray-800">
					账号 {pool?.stats?.total ?? 0}
				</span>
				<span class="rounded bg-green-100 px-2 py-0.5 text-xs text-green-700 dark:bg-green-900/30 dark:text-green-400">
					可用 {pool?.stats?.active ?? 0}
				</span>
				<span class="rounded bg-blue-100 px-2 py-0.5 text-xs text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
					网关 Key {pool?.stats?.keys ?? 0}
				</span>
				{#if job}
					<span
						class="rounded px-2 py-0.5 text-xs {job.status === 'running'
							? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
							: 'bg-gray-100 dark:bg-gray-800'}"
					>
						{job.status} {job.created ?? 0}/{job.requested ?? 0}
					</span>
				{/if}
			{/if}
			<div class="ml-auto flex items-center gap-2">
				<button class="rounded-lg border border-gray-200 px-3 py-1 text-xs dark:border-gray-700" on:click={load}>
					刷新
				</button>
				<button
					class="rounded-lg border border-gray-200 px-3 py-1 text-xs dark:border-gray-700"
					on:click={doProbe}
					disabled={busy === 'probe'}
				>
					{busy === 'probe' ? '自检中…' : '自检上游'}
				</button>
			</div>
		</div>

		{#if detail}
			<div class="grid gap-2 text-xs text-gray-500 dark:text-gray-400 sm:grid-cols-2">
				<div>python：<span class="font-mono">{detail.python}</span></div>
				<div>worker：<span class="font-mono">{detail.toolPresent ? '就绪' : '缺失'}</span></div>
				<div>面板：<span class="font-mono">{detail.panelBase}</span></div>
				<div>上游：<span class="font-mono">{detail.endpoints?.apiBase}</span></div>
			</div>
		{/if}

		{#if connect}
			<div class="mt-3 rounded-lg bg-gray-50 p-3 text-xs dark:bg-gray-900/40">
				<div class="mb-1 font-medium text-gray-700 dark:text-gray-200">编程工具接入</div>
				<div class="flex items-center gap-2">
					<span class="text-gray-500 dark:text-gray-400">OpenAI Base URL</span>
					<code class="flex-1 truncate font-mono">{connect.openai_base}</code>
					<button class="rounded border border-gray-200 px-2 py-0.5 dark:border-gray-700" on:click={() => copy(connect.openai_base)}>
						复制
					</button>
				</div>
				<div class="mt-1 flex items-center gap-2">
					<span class="text-gray-500 dark:text-gray-400">Anthropic Base</span>
					<code class="flex-1 truncate font-mono">{connect.anthropic_base}</code>
					<button class="rounded border border-gray-200 px-2 py-0.5 dark:border-gray-700" on:click={() => copy(connect.anthropic_base)}>
						复制
					</button>
				</div>
				<div class="mt-1 text-gray-500 dark:text-gray-400">
					API Key 用网关的 <code class="font-mono">sk-dcg-…</code>（已有 {connect.keys ?? 0} 把，前缀
					<code class="font-mono">{connect.key_prefix || '—'}</code>）。模型：
					<code class="font-mono">{(detail?.models ?? []).join(' / ') || '—'}</code>
				</div>
			</div>
		{/if}
	</div>

	<!-- 批量注册 -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-1 font-medium">批量注册</div>
		<div class="mb-3 text-xs text-gray-500 dark:text-gray-400">
			<code class="font-mono">auto</code> 先走协议，被拦（blocked / captcha / no_token）当场切指纹浏览器；
			域名邮箱建议用自有域名 IMAP（<code class="font-mono">mailtm</code> 投递不稳）。
		</div>

		<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
			<label class="flex flex-col gap-1 text-xs">
				数量
				<input type="number" min="1" max="500" bind:value={count} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 text-xs">
				并发
				<input type="number" min="1" max="16" bind:value={concurrency} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 text-xs">
				引擎
				<select bind:value={engine} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900">
					<option value="auto">auto</option>
					<option value="protocol">protocol</option>
					<option value="browser">browser</option>
					<option value="hybrid">hybrid</option>
				</select>
			</label>
			<label class="flex flex-col gap-1 text-xs">
				指纹内核
				<select bind:value={browserStack} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900">
					<option value="auto">auto</option>
					<option value="camoufox">camoufox</option>
					<option value="patchright">patchright</option>
					<option value="playwright">playwright</option>
				</select>
			</label>
			<label class="flex flex-col gap-1 text-xs">
				邮箱来源
				<select bind:value={mailProvider} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900">
					<option value="imap">imap（自有域名）</option>
					<option value="domain">domain（多域轮换）</option>
					<option value="mailtm">mailtm（兜底）</option>
					<option value="fixed">fixed（指定地址）</option>
				</select>
			</label>
			<label class="flex flex-col gap-1 text-xs">
				每号 Key 数
				<input type="number" min="1" max="10" bind:value={keysPerAccount} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 text-xs">
				放慢倍率
				<input type="number" min="0" step="0.5" bind:value={humanize} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 text-xs">
				结束后导出
				<select bind:value={exportFormat} class="rounded-lg border border-gray-200 px-2 py-1 dark:border-gray-700 dark:bg-gray-900">
					<option value="">不导出</option>
					<option value="jsonl">jsonl</option>
					<option value="csv">csv</option>
				</select>
			</label>
		</div>

		<div class="mt-2 grid gap-2 sm:grid-cols-2">
			<label class="flex flex-col gap-1 text-xs">
				邮箱域名（逗号分隔）
				<input bind:value={mailDomains} placeholder="example.com, mail.example.com" class="rounded-lg border border-gray-200 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 text-xs">
				代理（逗号分隔，可留空）
				<input bind:value={proxies} placeholder="http://user:pass@host:port" class="rounded-lg border border-gray-200 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 text-xs">
				CDP（接管 AdsPower / BitBrowser，可留空）
				<input bind:value={cdp} placeholder="http://127.0.0.1:50325" class="rounded-lg border border-gray-200 px-2 py-1 font-mono dark:border-gray-700 dark:bg-gray-900" />
			</label>
			<div class="flex flex-wrap items-end gap-3 text-xs">
				<label class="flex items-center gap-1">
					<input type="checkbox" bind:checked={headful} /> 显示浏览器
				</label>
				<label class="flex items-center gap-1">
					<input type="checkbox" bind:checked={verifyEmail} /> 走邮箱验证
				</label>
				<label class="flex items-center gap-1">
					<input type="checkbox" bind:checked={push} /> 自动入池并发 Key
				</label>
			</div>
		</div>

		<div class="mt-3 flex flex-wrap items-center gap-2">
			<button
				class="rounded-lg bg-gray-900 px-4 py-1.5 text-xs text-white disabled:opacity-50 dark:bg-white dark:text-gray-900"
				on:click={startRegister}
				disabled={busy === 'start' || job?.status === 'running'}
			>
				{busy === 'start' ? '启动中…' : '开始注册'}
			</button>
			<button
				class="rounded-lg border border-gray-200 px-4 py-1.5 text-xs disabled:opacity-50 dark:border-gray-700"
				on:click={stopRegister}
				disabled={busy === 'stop' || job?.status !== 'running'}
			>
				停止
			</button>
			<button class="rounded-lg border border-gray-200 px-4 py-1.5 text-xs dark:border-gray-700" on:click={doExport} disabled={busy === 'export'}>
				{busy === 'export' ? '导出中…' : '导出号池'}
			</button>
		</div>

		{#if job}
			<div class="mt-3">
				<div class="mb-1 flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
					<span>
						进度 {job.created ?? 0}/{job.requested ?? 0}
						{#if job.failed}· 失败 {job.failed}{/if}
						{#if job.mailProvider}· 邮箱 {job.mailProvider}{/if}
						{#if job.engine}· 引擎 {job.engine}{/if}
					</span>
					<span>{job.status}</span>
				</div>
				<div class="h-2 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
					<div
						class="h-full bg-blue-500 transition-all"
						style="width:{Math.min(100, Math.round(((job.created ?? 0) / Math.max(1, job.requested ?? 1)) * 100))}%"
					></div>
				</div>
				{#if job.failureReasons}
					<div class="mt-2 flex flex-wrap gap-1 text-xs">
						{#each Object.entries(job.failureReasons) as [kind, row]}
							<span class="rounded bg-red-50 px-2 py-0.5 text-red-600 dark:bg-red-900/20 dark:text-red-400">
								{kind} {row.count}
							</span>
						{/each}
					</div>
				{/if}
				<div class="mt-2 max-h-40 overflow-auto rounded-lg bg-gray-50 p-2 font-mono text-[11px] leading-relaxed dark:bg-gray-900/40">
					{#each (job.lines ?? []).slice(-30) as line}
						<div class:text-red-500={line.level === 'error'} class:text-amber-600={line.level === 'warn'}>
							{line.at?.slice(11, 19)} {line.text}
						</div>
					{/each}
				</div>
			</div>
		{/if}

		{#if detail && detail.toolPresent === false}
			<div class="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:bg-amber-900/20 dark:text-amber-400">
				找不到 worker：{detail.toolDir}（用 <code class="font-mono">MOONAI_REGISTER_DIR</code> 指定）
			</div>
		{/if}
	</div>

	<!-- 手工导入 -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-1 font-medium">手工导入</div>
		<div class="mb-3 text-xs text-gray-500 dark:text-gray-400">
			一行一个账号 JSON，或整段是一个数组。最少要有 <code class="font-mono">email</code> 加
			<code class="font-mono">apiKey</code> 或 <code class="font-mono">accessToken</code>；
			按 apiKey → email 去重，重复导入只刷新凭据、不重发网关 Key。
		</div>
		<textarea
			bind:value={importText}
			rows="4"
			placeholder={importPlaceholder}
			class="w-full rounded-lg border border-gray-200 px-2 py-1 font-mono text-[11px] dark:border-gray-700 dark:bg-gray-900"
		></textarea>
		<div class="mt-2 flex flex-wrap items-center gap-3">
			<label class="flex items-center gap-1 text-xs">
				<input type="checkbox" bind:checked={importMintKey} /> 同时发网关 Key
			</label>
			<button
				class="rounded-lg border border-gray-200 px-4 py-1.5 text-xs disabled:opacity-50 dark:border-gray-700"
				on:click={doImport}
				disabled={busy === 'import' || !importText.trim()}
			>
				{busy === 'import' ? '导入中…' : '导入'}
			</button>
		</div>
		{#if revealedKeys.length}
			<div class="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-900/20 dark:text-amber-300">
				<div class="mb-1 font-medium">新发的网关 Key（只显示这一次，请立刻保存）</div>
				{#each revealedKeys as key}
					<div class="flex items-center gap-2">
						<code class="flex-1 truncate font-mono">{key}</code>
						<button class="rounded border border-amber-300 px-2 py-0.5" on:click={() => copy(key)}>复制</button>
					</div>
				{/each}
				<button class="mt-1 underline" on:click={() => (revealedKeys = [])}>我已保存，隐藏</button>
			</div>
		{/if}
	</div>

	<!-- 号池 -->
	<div class="rounded-xl border border-gray-100 p-4 dark:border-gray-800">
		<div class="mb-3 flex items-center gap-2">
			<div class="font-medium">号池</div>
			<div class="text-xs text-gray-500 dark:text-gray-400">
				{#if pool?.updatedAt}更新于 {pool.updatedAt}{/if}
			</div>
			<button class="ml-auto rounded-lg border border-gray-200 px-3 py-1 text-xs dark:border-gray-700" on:click={load}>
				重新读取
			</button>
		</div>

		{#if loading}
			<div class="text-xs text-gray-500 dark:text-gray-400">加载中…</div>
		{:else if (pool?.accounts ?? []).length === 0}
			<div class="text-xs text-gray-500 dark:text-gray-400">还没有账号。跑一轮批量注册，或从命令行导入。</div>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full text-left text-xs">
					<thead class="text-gray-500 dark:text-gray-400">
						<tr>
							<th class="pb-1 font-normal">邮箱</th>
							<th class="pb-1 font-normal">状态</th>
							<th class="pb-1 font-normal">面板 Key</th>
							<th class="pb-1 font-normal">会话令牌</th>
							<th class="pb-1 font-normal">网关 Key</th>
							<th class="pb-1 font-normal">引擎</th>
							<th class="pb-1 font-normal">调用</th>
							<th class="pb-1 font-normal"></th>
						</tr>
					</thead>
					<tbody>
						{#each pool.accounts as account (account.id)}
							<tr class="border-t border-gray-100 dark:border-gray-800">
								<td class="py-1.5 pr-2">{account.email}</td>
								<td class="py-1.5 pr-2">
									<span
										class={account.healthy === false
											? 'text-red-600 dark:text-red-400'
											: 'text-green-600 dark:text-green-400'}
									>
										{account.status}{account.healthy === false ? ' · 异常' : ''}
									</span>
								</td>
								<td class="py-1.5 pr-2 font-mono">{account.apiKeyPreview || '—'}</td>
								<td class="py-1.5 pr-2">{tokenState(account)}</td>
								<td class="py-1.5 pr-2 font-mono">{account.gatewayKey?.prefix ?? '—'}</td>
								<td class="py-1.5 pr-2">{account.engine}</td>
								<td class="py-1.5 pr-2">{account.requests ?? 0}{#if account.failures} / 失败 {account.failures}{/if}</td>
								<td class="py-1.5 text-right">
									<button
										class="text-red-600 disabled:opacity-40 dark:text-red-400"
										on:click={() => removeAccount(account.id, account.email)}
										disabled={busy === `rm-${account.id}`}
									>
										移除
									</button>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>
</div>

