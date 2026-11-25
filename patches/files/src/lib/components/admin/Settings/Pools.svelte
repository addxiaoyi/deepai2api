<!--
  号池总览 + Clever AI Hub / Gab AI 两张卡（Open WebUI fork addition）。

  为什么单独一个组件：DeepAI.svelte 已经很长（网关、账号、Key、模型、Allmodels、
  注册机日志、Poe、Provider 运行时、MoonAI），再塞两条线会失控。这里把它们收在一处，
  仍然挂在同一个「设置 → Admin → DeepAI」页底部。

  它只读网关接口，不持有任何凭据：
    GET  /api/v1/deepai/pools            六条线号池总览
    GET  /api/v1/deepai/cleverai/status   Clever AI 的 job + App Check 状态
    GET  /api/v1/deepai/gabai/status      Gab AI 的 job 快照
  号池文件、注册锁、job 状态都在网关侧（data/<line>-pool.json）。
-->
<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getPoolsOverview,
		getCleveraiStatus,
		startCleveraiRegister,
		stopCleveraiRegister,
		getCleveraiPool,
		importCleveraiAccount,
		deleteCleveraiAccount,
		refreshCleveraiAppCheck,
		getCleveraiConnectInfo,
		downloadCleveraiPool,
		getGabaiStatus,
		startGabaiRegister,
		stopGabaiRegister,
		getGabaiPool,
		importGabaiAccount,
		mintGabaiKey,
		deleteGabaiAccount,
		getGabaiConnectInfo,
		downloadGabaiPool
	} from '$lib/apis/deepai';

	const token = () => localStorage.token ?? '';
	const list = (v: string) =>
		v
			.split(/[\n,]/)
			.map((s) => s.trim())
			.filter(Boolean);

	let loading = true;
	let busy = '';
	let error = '';

	let overview: any = null;

	// Clever AI
	let cvJob: any = null;
	let cvPool: any = null;
	let cvConnect: any = null;
	let cvAppCheck = false;
	let cvCount = 1;
	let cvConcurrency = 1;
	let cvEngine: 'auto' | 'protocol' | 'browser' = 'auto';
	let cvMailProvider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol' = 'imap';
	let cvDomains = '';
	let cvAppcheck: 'browser' | 'capsolver' | 'manual' = 'browser';
	let cvCaptchaKey = '';
	let cvAppcheckToken = '';
	let cvProxies = '';
	let cvHeadful = false;
	let cvWarmup = true;
	let cvExport: '' | 'jsonl' | 'csv' | 'pool' = 'jsonl';
	let cvImport = '';
	let cvRevealed: string[] = [];

	// Gab AI
	let gbJob: any = null;
	let gbPool: any = null;
	let gbConnect: any = null;
	let gbCount = 1;
	let gbConcurrency = 1;
	let gbEngine: 'auto' | 'protocol' | 'browser' = 'auto';
	let gbMailProvider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol' = 'imap';
	let gbDomains = '';
	let gbCaptcha: 'none' | 'capsolver' | 'manual' = 'none';
	let gbCaptchaKey = '';
	let gbProxies = '';
	let gbVerify = false;
	let gbExport: '' | 'jsonl' | 'csv' | 'pool' = 'jsonl';
	let gbImport = '';
	let gbRevealed: string[] = [];

	let timer: any = null;

	async function load() {
		error = '';
		try {
			overview = await getPoolsOverview(token());
		} catch (e: any) {
			error = e?.message ?? String(e);
		}
		try {
			const [cs, cp] = await Promise.all([
				getCleveraiStatus(token()),
				getCleveraiPool(token(), 200)
			]);
			cvJob = cs?.job ?? null;
			cvAppCheck = Boolean(cs?.relay?.appCheck?.ready);
			cvPool = cp;
		} catch {
			cvPool = null;
		}
		try {
			const [gs, gp] = await Promise.all([getGabaiStatus(token()), getGabaiPool(token(), 200)]);
			gbJob = gs?.job ?? null;
			gbPool = gp;
		} catch {
			gbPool = null;
		}
		try {
			cvConnect = await getCleveraiConnectInfo(token());
		} catch {
			cvConnect = null;
		}
		try {
			gbConnect = await getGabaiConnectInfo(token());
		} catch {
			gbConnect = null;
		}
		loading = false;
	}

	async function poll() {
		try {
			const [cs, gs] = await Promise.all([
				getCleveraiStatus(token()).catch(() => null),
				getGabaiStatus(token()).catch(() => null)
			]);
			if (cs?.job) cvJob = cs.job;
			if (cs?.relay?.appCheck) cvAppCheck = Boolean(cs.relay.appCheck.ready);
			if (gs?.job) gbJob = gs.job;
			const running = cs?.running || gs?.running;
			if (!running) await load();
		} catch {
			/* 轮询失败不打扰操作者 */
		}
	}

	onMount(async () => {
		await load();
		timer = setInterval(poll, 5000);
	});

	onDestroy(() => timer && clearInterval(timer));

	/* ------------------------------------------------------------- Clever AI */

	async function startCleverai() {
		busy = 'cv-start';
		try {
			const r = await startCleveraiRegister(token(), {
				count: cvCount,
				concurrency: cvConcurrency,
				engine: cvEngine,
				browser_stack: 'auto',
				mail_provider: cvMailProvider,
				mail_domains: list(cvDomains),
				appcheck: cvAppcheck,
				captcha_key: cvCaptchaKey || null,
				appcheck_token: cvAppcheckToken || null,
				chat_engine: null,
				proxies: list(cvProxies),
				headful: cvHeadful,
				humanize: 1,
				verify_email: true,
				warmup: cvWarmup,
				export: cvExport || null
			});
			cvJob = r?.job ?? cvJob;
			toast.success('已启动 Clever AI 注册');
		} catch (e: any) {
			toast.error(e?.message ?? '启动失败');
		} finally {
			busy = '';
		}
	}

	async function stopCleverai() {
		busy = 'cv-stop';
		try {
			await stopCleveraiRegister(token());
			toast.success('已停止');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '停止失败');
		} finally {
			busy = '';
		}
	}

	async function renewAppCheck() {
		busy = 'cv-appcheck';
		try {
			const r = await refreshCleveraiAppCheck(token());
			cvAppCheck = Boolean(r?.ok);
			toast.success(cvAppCheck ? 'App Check 已续期' : '续期未返回令牌');
		} catch (e: any) {
			toast.error(e?.message ?? '续期失败（先确认已装 camoufox）');
		} finally {
			busy = '';
		}
	}

	async function importCleverai() {
		busy = 'cv-import';
		try {
			const rows = JSON.parse(cvImport);
			const items = Array.isArray(rows) ? rows : [rows];
			for (const item of items) {
				const r = await importCleveraiAccount(token(), item, true);
				if (r?.gatewayKey?.plaintext) cvRevealed = [...cvRevealed, `${r.account?.email}: ${r.gatewayKey.plaintext}`];
			}
			cvImport = '';
			toast.success(`已导入 ${items.length} 个`);
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '导入失败（需要 email + refreshToken 的 JSON）');
		} finally {
			busy = '';
		}
	}

	async function removeCleverai(id: string, email: string) {
		if (!confirm(`移除 ${email}？已发的网关 Key 不会被撤销。`)) return;
		busy = `cv-rm-${id}`;
		try {
			await deleteCleveraiAccount(token(), id);
			toast.success('已移除');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '移除失败');
		} finally {
			busy = '';
		}
	}

	async function exportCleverai() {
		busy = 'cv-export';
		try {
			const fmt = (cvExport || 'jsonl') as 'jsonl' | 'csv' | 'pool';
			const blob = await downloadCleveraiPool(token(), fmt);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `cleverai-accounts.${fmt === 'csv' ? 'csv' : fmt === 'jsonl' ? 'jsonl' : 'json'}`;
			a.click();
			URL.revokeObjectURL(url);
			toast.success('已导出');
		} catch (e: any) {
			toast.error(e?.message ?? '导出失败');
		} finally {
			busy = '';
		}
	}

	/* ---------------------------------------------------------------- Gab AI */

	async function startGabai() {
		busy = 'gb-start';
		try {
			const r = await startGabaiRegister(token(), {
				count: gbCount,
				concurrency: gbConcurrency,
				engine: gbEngine,
				browser_stack: 'auto',
				mail_provider: gbMailProvider,
				mail_domains: list(gbDomains),
				email: null,
				captcha: gbCaptcha,
				captcha_key: gbCaptchaKey || null,
				proxies: list(gbProxies),
				headful: false,
				humanize: 1,
				verify_email: gbVerify,
				create_api_key: true,
				export: gbExport || null
			});
			gbJob = r?.job ?? gbJob;
			toast.success('已启动 Gab AI 注册');
		} catch (e: any) {
			toast.error(e?.message ?? '启动失败');
		} finally {
			busy = '';
		}
	}

	async function stopGabai() {
		busy = 'gb-stop';
		try {
			await stopGabaiRegister(token());
			toast.success('已停止');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '停止失败');
		} finally {
			busy = '';
		}
	}

	async function mintGabai(id: string, email: string) {
		busy = `gb-mint-${id}`;
		try {
			const r = await mintGabaiKey(token(), id);
			toast.success(r?.apiKey ? `${email} 已补建 ${String(r.apiKey).slice(0, 12)}…` : `${email} 已处理`);
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '补建 key 失败');
		} finally {
			busy = '';
		}
	}

	async function importGabai() {
		busy = 'gb-import';
		try {
			const rows = JSON.parse(gbImport);
			const items = Array.isArray(rows) ? rows : [rows];
			for (const item of items) {
				const r = await importGabaiAccount(token(), item, true);
				if (r?.gatewayKey?.plaintext) gbRevealed = [...gbRevealed, `${r.account?.email}: ${r.gatewayKey.plaintext}`];
			}
			gbImport = '';
			toast.success(`已导入 ${items.length} 个`);
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '导入失败（需要 email + apiKey 的 JSON）');
		} finally {
			busy = '';
		}
	}

	async function removeGabai(id: string, email: string) {
		if (!confirm(`移除 ${email}？已发的网关 Key 不会被撤销。`)) return;
		busy = `gb-rm-${id}`;
		try {
			await deleteGabaiAccount(token(), id);
			toast.success('已移除');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '移除失败');
		} finally {
			busy = '';
		}
	}

	async function exportGabai() {
		busy = 'gb-export';
		try {
			const fmt = (gbExport || 'jsonl') as 'jsonl' | 'csv' | 'pool';
			const blob = await downloadGabaiPool(token(), fmt);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `gabai-accounts.${fmt === 'csv' ? 'csv' : fmt === 'jsonl' ? 'jsonl' : 'json'}`;
			a.click();
			URL.revokeObjectURL(url);
			toast.success('已导出');
		} catch (e: any) {
			toast.error(e?.message ?? '导出失败');
		} finally {
			busy = '';
		}
	}

	const badge = (ok: boolean, text: string) =>
		ok ? `inline-flex items-center rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700 dark:bg-green-900 dark:text-green-200`
			: `inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300`;
</script>

<div class="mt-4 flex flex-col gap-4">
	<!-- 总览 -->
	<div class="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
		<div class="mb-2 flex items-center justify-between">
			<div class="text-sm font-medium">号池总览</div>
			{#if overview?.gateway}
				<div class="text-xs text-gray-500">{overview.gateway}</div>
			{/if}
		</div>
		{#if error}
			<div class="text-xs text-red-500">{error}</div>
		{:else if loading}
			<div class="text-xs text-gray-500">加载中…</div>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full text-left text-sm">
					<thead class="text-xs uppercase text-gray-500">
						<tr>
							<th class="py-1 pr-3">线</th>
							<th class="py-1 pr-3">账号</th>
							<th class="py-1 pr-3">健康</th>
							<th class="py-1 pr-3">网关 Key</th>
							<th class="py-1 pr-3">反代前缀</th>
							<th class="py-1">备注</th>
						</tr>
					</thead>
					<tbody>
						{#each overview?.lines ?? [] as line}
							<tr class="border-t border-gray-100 dark:border-gray-900">
								<td class="py-1 pr-3">{line.label}</td>
								<td class="py-1 pr-3">{line.accounts}</td>
								<td class="py-1 pr-3">{line.healthy}</td>
								<td class="py-1 pr-3">{line.keys}</td>
								<td class="py-1 pr-3 font-mono text-xs">{line.proxy_base || '—'}</td>
								<td class="py-1 text-xs text-gray-500">
									{#if line.id === 'cleverai'}
										App Check {line.extra?.app_check ? '就绪' : '待续期'} · {line.extra?.credits ?? 0} credit
									{:else if line.id === 'poe'}
										待绑手机 {line.extra?.phone_required ?? 0} · 失效 {line.extra?.dead ?? 0}
									{:else if line.id === 'deepai'}
										{line.extra?.is_pro ? 'Pro' : 'Free'}
									{:else if line.running}
										注册中
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>

	<!-- Clever AI Hub -->
	<div class="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
		<div class="mb-3 flex flex-wrap items-center gap-2">
			<div class="text-sm font-medium">Clever AI Hub 号池</div>
			<span class={badge(cvJob?.status === 'running', cvJob?.status === 'running' ? '注册中' : '空闲')}>
				{cvJob?.status ?? '空闲'}
			</span>
			<span class={badge(cvAppCheck, cvAppCheck ? 'App Check 就绪' : 'App Check 待续期')}>
				{cvAppCheck ? 'App Check 就绪' : 'App Check 待续期'}
			</span>
			{#if cvJob?.created !== undefined}
				<span class="text-xs text-gray-500">本轮 +{cvJob.created ?? 0} / 失败 {cvJob.failed ?? 0}</span>
			{/if}
			<button
				class="ml-auto rounded bg-gray-100 px-2 py-1 text-xs hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={renewAppCheck}
				disabled={busy === 'cv-appcheck'}
			>
				{busy === 'cv-appcheck' ? '续期中…' : '续期 App Check'}
			</button>
		</div>

		<div class="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">数量</span>
				<input type="number" min="1" bind:value={cvCount} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">并发</span>
				<input type="number" min="1" bind:value={cvConcurrency} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">引擎</span>
				<select bind:value={cvEngine} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="auto">auto（协议→浏览器）</option>
					<option value="protocol">protocol</option>
					<option value="browser">browser</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">App Check 来源</span>
				<select bind:value={cvAppcheck} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="browser">browser（Camoufox）</option>
					<option value="capsolver">capsolver</option>
					<option value="manual">manual（贴令牌）</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">邮箱来源</span>
				<select bind:value={cvMailProvider} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="imap">imap（自有域名）</option>
					<option value="fixed">fixed</option>
					<option value="mailtm">mailtm</option>
					<option value="tempmail_lol">tempmail_lol</option>
				</select>
			</label>
			<label class="flex flex-col gap-1 md:col-span-2">
				<span class="text-gray-500">邮箱域名（逗号/换行分隔）</span>
				<input bind:value={cvDomains} placeholder="example.com, mail.example.com" class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">导出格式</span>
				<select bind:value={cvExport} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="jsonl">jsonl</option>
					<option value="csv">csv</option>
					<option value="pool">pool</option>
					<option value="">不导出</option>
				</select>
			</label>
			{#if cvAppcheck === 'capsolver'}
				<label class="flex flex-col gap-1 md:col-span-2">
					<span class="text-gray-500">capsolver clientKey</span>
					<input bind:value={cvCaptchaKey} class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
			{/if}
			{#if cvAppcheck === 'manual'}
				<label class="flex flex-col gap-1 md:col-span-2">
					<span class="text-gray-500">App Check JWT</span>
					<input bind:value={cvAppcheckToken} class="rounded border px-2 py-1 font-mono text-xs dark:bg-gray-900" />
				</label>
			{/if}
			<label class="flex flex-col gap-1 md:col-span-2">
				<span class="text-gray-500">代理（每行一个）</span>
				<textarea rows="2" bind:value={cvProxies} class="rounded border px-2 py-1 dark:bg-gray-900"></textarea>
			</label>
			<label class="flex items-center gap-2">
				<input type="checkbox" bind:checked={cvHeadful} />
				<span>有头浏览器</span>
			</label>
			<label class="flex items-center gap-2">
				<input type="checkbox" bind:checked={cvWarmup} />
				<span>注册后预热一句</span>
			</label>
		</div>

		<div class="mb-3 flex flex-wrap gap-2">
			<button
				class="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
				on:click={startCleverai}
				disabled={cvJob?.status === 'running' || busy === 'cv-start'}
			>
				{busy === 'cv-start' ? '启动中…' : '开始注册'}
			</button>
			<button
				class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={stopCleverai}
				disabled={cvJob?.status !== 'running' || busy === 'cv-stop'}
			>
				停止
			</button>
			<button class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={exportCleverai} disabled={busy === 'cv-export'}>
				导出
			</button>
			{#if cvConnect?.key_prefix}
				<span class="ml-auto text-xs text-gray-500">
					OpenAI 基址 <code class="font-mono">{cvConnect.openai_base}</code> · Key {cvConnect.key_prefix}…
				</span>
			{/if}
		</div>

		{#if cvJob?.lines?.length}
			<details class="mb-3 text-xs">
				<summary class="cursor-pointer text-gray-500">最近日志（{cvJob.lines.length} 行）</summary>
				<pre class="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-gray-50 p-2 dark:bg-gray-900">{cvJob.lines
						.slice(-40)
						.map((l: any) => `${l.at?.slice(11, 19) ?? ''} ${l.level ?? ''} ${l.text ?? ''}`)
						.join('\n')}</pre>
			</details>
		{/if}

		{#if cvRevealed.length}
			<div class="mb-3 rounded bg-yellow-50 p-2 text-xs text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-200">
				<div class="mb-1 font-medium">新发的网关 Key（只显示这一次）：</div>
				{#each cvRevealed as row}
					<div class="font-mono">{row}</div>
				{/each}
			</div>
		{/if}

		{#if cvPool?.accounts?.length}
			<div class="overflow-x-auto">
				<table class="w-full text-left text-xs">
					<thead class="text-gray-500">
						<tr>
							<th class="py-1 pr-3">邮箱</th>
							<th class="py-1 pr-3">引擎</th>
							<th class="py-1 pr-3">额度</th>
							<th class="py-1 pr-3">状态</th>
							<th class="py-1 pr-3">网关 Key</th>
							<th class="py-1"></th>
						</tr>
					</thead>
					<tbody>
						{#each cvPool.accounts as acc}
							<tr class="border-t border-gray-100 dark:border-gray-900">
								<td class="py-1 pr-3">{acc.email}</td>
								<td class="py-1 pr-3">{acc.engine}</td>
								<td class="py-1 pr-3">{acc.wallet?.totalCredit ?? 0}</td>
								<td class="py-1 pr-3">{acc.healthy ? 'ok' : acc.lastError ?? 'bad'}</td>
								<td class="py-1 pr-3 font-mono">{acc.gatewayKey?.prefix ?? '—'}</td>
								<td class="py-1 text-right">
									<button class="text-red-500 hover:underline" on:click={() => removeCleverai(acc.id, acc.email)} disabled={busy === `cv-rm-${acc.id}`}>
										移除
									</button>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<div class="text-xs text-gray-500">号池还是空的：用上面的注册表单，或手工导入（需要 email + refreshToken）。</div>
		{/if}

		<div class="mt-3 flex flex-col gap-1">
			<textarea
				rows="2"
				bind:value={cvImport}
				placeholder={'{"email":"a@example.com","refreshToken":"AMf-vBz…","uid":"…"}'}
				class="rounded border px-2 py-1 font-mono text-xs dark:bg-gray-900"
			></textarea>
			<button class="w-fit rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={importCleverai} disabled={busy === 'cv-import'}>
				导入账号
			</button>
		</div>
	</div>

	<!-- Gab AI -->
	<div class="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
		<div class="mb-3 flex flex-wrap items-center gap-2">
			<div class="text-sm font-medium">Gab AI 号池</div>
			<span class={badge(gbJob?.status === 'running', gbJob?.status === 'running' ? '注册中' : '空闲')}>
				{gbJob?.status ?? '空闲'}
			</span>
			{#if gbJob?.created !== undefined}
				<span class="text-xs text-gray-500">本轮 +{gbJob.created ?? 0} / 失败 {gbJob.failed ?? 0}</span>
			{/if}
		</div>

		<div class="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">数量</span>
				<input type="number" min="1" bind:value={gbCount} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">并发</span>
				<input type="number" min="1" bind:value={gbConcurrency} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">引擎</span>
				<select bind:value={gbEngine} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="auto">auto</option>
					<option value="protocol">protocol</option>
					<option value="browser">browser</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">人机校验</span>
				<select bind:value={gbCaptcha} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="none">none</option>
					<option value="capsolver">capsolver</option>
					<option value="manual">manual</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">邮箱来源</span>
				<select bind:value={gbMailProvider} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="imap">imap（自有域名）</option>
					<option value="fixed">fixed</option>
					<option value="mailtm">mailtm</option>
					<option value="tempmail_lol">tempmail_lol</option>
				</select>
			</label>
			<label class="flex flex-col gap-1 md:col-span-2">
				<span class="text-gray-500">邮箱域名</span>
				<input bind:value={gbDomains} placeholder="example.com" class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">导出格式</span>
				<select bind:value={gbExport} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="jsonl">jsonl</option>
					<option value="csv">csv</option>
					<option value="pool">pool</option>
					<option value="">不导出</option>
				</select>
			</label>
			{#if gbCaptcha !== 'none'}
				<label class="flex flex-col gap-1 md:col-span-2">
					<span class="text-gray-500">capsolver clientKey / 手工 token</span>
					<input bind:value={gbCaptchaKey} class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
			{/if}
			<label class="flex flex-col gap-1 md:col-span-2">
				<span class="text-gray-500">代理（每行一个）</span>
				<textarea rows="2" bind:value={gbProxies} class="rounded border px-2 py-1 dark:bg-gray-900"></textarea>
			</label>
			<label class="flex items-center gap-2">
				<input type="checkbox" bind:checked={gbVerify} />
				<span>验证邮箱</span>
			</label>
		</div>

		<div class="mb-3 flex flex-wrap gap-2">
			<button
				class="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
				on:click={startGabai}
				disabled={gbJob?.status === 'running' || busy === 'gb-start'}
			>
				{busy === 'gb-start' ? '启动中…' : '开始注册'}
			</button>
			<button
				class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={stopGabai}
				disabled={gbJob?.status !== 'running' || busy === 'gb-stop'}
			>
				停止
			</button>
			<button class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={exportGabai} disabled={busy === 'gb-export'}>
				导出
			</button>
			{#if gbConnect?.key_prefix}
				<span class="ml-auto text-xs text-gray-500">
					OpenAI 基址 <code class="font-mono">{gbConnect.openai_base}</code>
				</span>
			{/if}
		</div>

		{#if gbRevealed.length}
			<div class="mb-3 rounded bg-yellow-50 p-2 text-xs text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-200">
				<div class="mb-1 font-medium">新发的网关 Key（只显示这一次）：</div>
				{#each gbRevealed as row}
					<div class="font-mono">{row}</div>
				{/each}
			</div>
		{/if}

		{#if gbPool?.accounts?.length}
			<div class="overflow-x-auto">
				<table class="w-full text-left text-xs">
					<thead class="text-gray-500">
						<tr>
							<th class="py-1 pr-3">邮箱</th>
							<th class="py-1 pr-3">引擎</th>
							<th class="py-1 pr-3">状态</th>
							<th class="py-1 pr-3">套餐</th>
							<th class="py-1 pr-3">网关 Key</th>
							<th class="py-1"></th>
						</tr>
					</thead>
					<tbody>
						{#each gbPool.accounts as acc}
							<tr class="border-t border-gray-100 dark:border-gray-900">
								<td class="py-1 pr-3">{acc.email}</td>
								<td class="py-1 pr-3">{acc.engine}</td>
								<td class="py-1 pr-3">{acc.healthy ? 'ok' : acc.lastError ?? 'bad'}</td>
								<td class="py-1 pr-3">{acc.plusBlocked ? 'Plus 门槛' : '—'}</td>
								<td class="py-1 pr-3 font-mono">{acc.gatewayKey?.prefix ?? '—'}</td>
								<td class="py-1 text-right">
									<button class="mr-2 text-blue-600 hover:underline" on:click={() => mintGabai(acc.id, acc.email)} disabled={busy === `gb-mint-${acc.id}`}>
										补建 key
									</button>
									<button class="text-red-500 hover:underline" on:click={() => removeGabai(acc.id, acc.email)} disabled={busy === `gb-rm-${acc.id}`}>
										移除
									</button>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<div class="text-xs text-gray-500">号池还是空的：用上面的注册表单，或手工导入（需要 email + apiKey）。</div>
		{/if}

		<div class="mt-3 flex flex-col gap-1">
			<textarea
				rows="2"
				bind:value={gbImport}
				placeholder={'{"email":"a@example.com","apiKey":"gab_live_…"}'}
				class="rounded border px-2 py-1 font-mono text-xs dark:bg-gray-900"
			></textarea>
			<button class="w-fit rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={importGabai} disabled={busy === 'gb-import'}>
				导入账号
			</button>
		</div>
	</div>
</div>
