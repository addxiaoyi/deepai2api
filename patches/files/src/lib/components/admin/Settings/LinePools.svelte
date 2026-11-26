<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { token } from '$lib/stores';
	import {
		getLinesOverview,
		getLineStatus,
		startLineRegister,
		stopLineRegister,
		getLinePool,
		getLineKeys,
		applyLine,
		getLineConnect,
		downloadLineKeys,
		deleteLineAccount
	} from '$lib/apis/deepai';

	// 六条 OpenAI 兼容线的"每条一页"视图：左边是号池列表（等于侧边栏），右边是该线的独立页面。
	// `initial` 让路由页（/admin/pools/[line]）能直接指定打开哪条线。
	export let initial = '';

	let lines: any[] = [];
	let current = initial || '';
	let status: any = null;
	let pool: any = null;
	let keys: any[] = [];
	let connect: any = null;
	let loading = true;
	let busy = '';
	let timer: any = null;

	let count = 1;
	let domains = '';
	let keyName = '';
	let importEmail = '';
	let importKey = '';
	let revealed = '';

	const list = (value: string) =>
		String(value ?? '')
			.split(/[\s,]+/)
			.map((v) => v.trim())
			.filter(Boolean);

	async function loadOverview() {
		try {
			const r = await getLinesOverview(token());
			lines = r?.lines ?? [];
			if (current && !lines.some((line) => line.id === current)) current = '';
			if (!current && lines.length) current = initial || lines[0].id;
		} catch (e: any) {
			lines = [];
			toast.error(e?.message ?? '读取线列表失败');
		}
		loading = false;
	}

	async function loadLine(id: string) {
		if (!id) return;
		try {
			const [st, pl, ks, cn] = await Promise.all([
				getLineStatus(token(), id),
				getLinePool(token(), id, 200),
				getLineKeys(token(), id),
				getLineConnect(token(), id).catch(() => null)
			]);
			status = st;
			pool = pl;
			keys = ks?.keys ?? [];
			connect = cn;
		} catch (e: any) {
			status = null;
			toast.error(e?.message ?? `读取 ${id} 失败`);
		}
	}

	async function select(id: string) {
		if (id === current) return;
		current = id;
		busy = 'switch';
		await loadLine(id);
		busy = '';
	}

	async function refresh() {
		await loadOverview();
		if (current) await loadLine(current);
	}

	async function startRegister() {
		busy = 'start';
		try {
			await startLineRegister(token(), current, {
				count,
				concurrency: 1,
				mail_provider: 'imap',
				mail_domains: list(domains),
				key_name: keyName,
				proxies: [],
				verify_email: true,
				export: 'keys'
			});
			toast.success(`已启动 ${current} 注册`);
			await loadLine(current);
		} catch (e: any) {
			toast.error(e?.message ?? '启动失败');
		} finally {
			busy = '';
		}
	}

	async function stopRegister() {
		busy = 'stop';
		try {
			await stopLineRegister(token(), current);
			toast.success('已停止');
			await loadLine(current);
		} catch (e: any) {
			toast.error(e?.message ?? '停止失败');
		} finally {
			busy = '';
		}
	}

	async function exportKeys() {
		busy = 'export';
		try {
			const r = await applyLine(token(), current);
			toast.success(`已导出 ${r?.keys ?? 0} 把 Key`);
			await loadLine(current);
		} catch (e: any) {
			toast.error(e?.message ?? '导出失败');
		} finally {
			busy = '';
		}
	}

	async function download() {
		busy = 'download';
		try {
			const blob = await downloadLineKeys(token(), current, 'keys');
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `${current}-keys.txt`;
			a.click();
			URL.revokeObjectURL(url);
			toast.success('已下载');
		} catch (e: any) {
			toast.error(e?.message ?? '下载失败');
		} finally {
			busy = '';
		}
	}

	async function manualImport() {
		if (!importEmail) {
			toast.error('要填邮箱');
			return;
		}
		busy = 'import';
		try {
			await importLineAccount(token(), current, { email: importEmail, apiKey: importKey });
			toast.success('已入池');
			importEmail = '';
			importKey = '';
			await loadLine(current);
		} catch (e: any) {
			toast.error(e?.message ?? '入池失败');
		} finally {
			busy = '';
		}
	}

	async function removeAccount(id: string) {
		if (!confirm('从号池移除这个账号？')) return;
		busy = `del-${id}`;
		try {
			await deleteLineAccount(token(), current, id);
			await loadLine(current);
		} catch (e: any) {
			toast.error(e?.message ?? '移除失败');
		} finally {
			busy = '';
		}
	}

	function copy(value: string) {
		navigator.clipboard?.writeText(value).then(
			() => toast.success('已复制'),
			() => toast.error('复制失败')
		);
	}

	const badge = (ok: boolean, text: string) =>
		ok
			? `rounded bg-emerald-100 px-2 py-0.5 text-[11px] text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300`
			: `rounded bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600 dark:bg-gray-800 dark:text-gray-300`;

	function lineBadge(row: any) {
		if (row.running) return `rounded bg-blue-100 px-1.5 py-0.5 text-[10px] text-blue-700 dark:bg-blue-900/40 dark:text-blue-300`;
		if ((row.stats?.total ?? 0) > 0) return `rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300`;
		return `rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500 dark:bg-gray-800 dark:text-gray-400`;
	}

	onMount(async () => {
		await loadOverview();
		if (current) await loadLine(current);
		timer = setInterval(async () => {
			await loadOverview();
			if (current) {
				const row = lines.find((l) => l.id === current);
				if (row?.running || status?.running) await loadLine(current);
			}
		}, 5000);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	$: active = lines.find((l) => l.id === current) ?? null;
</script>

<div class="mt-4 grid gap-4 md:grid-cols-[220px_1fr]">
	<!-- 左栏：每条线一个入口（等于"号池侧边栏"） -->
	<div class="rounded-lg border border-gray-200 p-2 dark:border-gray-800">
		<div class="mb-2 flex items-center justify-between px-1">
			<div class="text-sm font-medium">号池</div>
			<button class="text-xs text-blue-600 hover:underline" on:click={refresh}>刷新</button>
		</div>
		{#if loading}
			<div class="px-1 py-2 text-xs text-gray-500">读取中…</div>
		{:else}
			<ul class="space-y-1">
				{#each lines as row (row.id)}
					<li>
						<button
							class="flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-800 {row.id ===
							current
								? 'bg-gray-100 font-medium dark:bg-gray-800'
								: ''}"
							on:click={() => select(row.id)}
							disabled={busy === 'switch'}
						>
							<span class="truncate">{row.name}</span>
							<span class="flex items-center gap-1">
								<span class="text-[10px] text-gray-500">{row.stats?.total ?? 0}</span>
								<span class={lineBadge(row)}>{row.running ? '跑' : row.stats?.keys ?? 0}</span>
							</span>
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</div>

	<!-- 右栏：选中这条线的独立页面 -->
	<div class="min-w-0 rounded-lg border border-gray-200 p-4 dark:border-gray-800">
		{#if !active}
			<div class="text-sm text-gray-500">选一条线查看号池。</div>
		{:else}
			<div class="mb-3 flex flex-wrap items-center gap-2">
				<div class="text-sm font-medium">{active.name}</div>
				<span class={badge(active.running, active.running ? '注册中' : '空闲')}>
					{status?.job?.status ?? '空闲'}
				</span>
				<span class="text-xs text-gray-500">
					{active.stats?.total ?? 0} 号 / {active.stats?.keys ?? 0} Key / 健康 {active.stats?.active ?? 0}
				</span>
				{#if active.anonymous}<span class="text-[11px] text-gray-500">匿名线（不需要 Key）</span>{/if}
				{#if active.freeSuffix}<span class="text-[11px] text-gray-500">免费后缀 {active.freeSuffix}</span>{/if}
			</div>

			<div class="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
				<label class="flex flex-col gap-1">
					<span class="text-gray-500">数量</span>
					<input type="number" min="1" bind:value={count} class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
				<label class="flex flex-col gap-1">
					<span class="text-gray-500">Key 名称</span>
					<input bind:value={keyName} placeholder="dcg" class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
				<label class="flex flex-col gap-1 md:col-span-2">
					<span class="text-gray-500">邮箱域名（一次性域名多数会被拒）</span>
					<input bind:value={domains} placeholder="example.com" class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
			</div>

			<div class="mb-3 flex flex-wrap gap-2">
				<button
					class="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
					on:click={startRegister}
					disabled={active.running || busy === 'start'}
				>
					{busy === 'start' ? '启动中…' : '开始注册'}
				</button>
				<button
					class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
					on:click={stopRegister}
					disabled={!active.running || busy === 'stop'}>停止</button
				>
				<button
					class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700"
					on:click={exportKeys}
					disabled={busy === 'export'}>导出并接入</button
				>
				<button
					class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700"
					on:click={download}
					disabled={busy === 'download'}>下载 Key</button
				>
			</div>

			{#if connect}
				<div class="mb-3 rounded border border-dashed border-gray-300 p-2 text-[11px] dark:border-gray-700">
					<div class="text-gray-500">接入（OpenAI 兼容）</div>
					<div class="font-mono">Base: {connect.gatewayBase}</div>
					<div class="font-mono">上游: {connect.upstreamBase}</div>
					<div class="text-gray-500">
						Key 前缀 {connect.keyPrefix || '（匿名）'}；网关 Key {connect.gatewayKeyPrefix || '—'}
					</div>
				</div>
			{/if}

			<div class="mb-3 flex flex-wrap items-end gap-2 text-xs">
				<label class="flex flex-col gap-1">
					<span class="text-gray-500">手工入池 · 邮箱</span>
					<input bind:value={importEmail} class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
				<label class="flex flex-col gap-1">
					<span class="text-gray-500">上游 Key</span>
					<input bind:value={importKey} class="rounded border px-2 py-1 font-mono dark:bg-gray-900" />
				</label>
				<button
					class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700"
					on:click={manualImport}
					disabled={busy === 'import'}>入池</button
				>
			</div>

			{#if keys.length}
				<div class="overflow-x-auto">
					<table class="w-full text-left text-[11px]">
						<thead class="text-gray-500">
							<tr>
								<th class="py-1 pr-3">邮箱</th>
								<th class="py-1 pr-3">Key</th>
								<th class="py-1 pr-3">已验证</th>
								<th class="py-1 pr-3">网关 Key</th>
								<th class="py-1"></th>
							</tr>
						</thead>
						<tbody>
							{#each keys.slice(0, 30) as row}
								<tr class="border-t border-gray-100 dark:border-gray-900">
									<td class="py-1 pr-3">{row.email}</td>
									<td class="py-1 pr-3 font-mono">{row.apiKeyPreview || '—'}</td>
									<td class="py-1 pr-3">{row.emailVerified ? '是' : '否'}</td>
									<td class="py-1 pr-3 font-mono">{row.gatewayKey?.prefix ?? '—'}</td>
									<td class="py-1 text-right">
										<button
											class="text-red-500 hover:underline"
											on:click={() => removeAccount(row.id)}
											disabled={busy === `del-${row.id}`}>移除</button
										>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{:else}
				<div class="text-xs text-gray-500">
					号池是空的：跑一次注册，或用上面的"手工入池"。
				</div>
			{/if}

			{#if status?.job?.lines?.length}
				<div class="mt-3 max-h-40 overflow-y-auto rounded bg-gray-50 p-2 font-mono text-[10px] leading-4 dark:bg-gray-900">
					{#each status.job.lines.slice(-25) as line}
						<div>{line.at?.slice(11, 19)} {line.text}</div>
					{/each}
				</div>
			{/if}
		{/if}
	</div>
</div>
