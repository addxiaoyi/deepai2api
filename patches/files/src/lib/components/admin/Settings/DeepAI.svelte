<!--
  DeepAI Code provider settings (Open WebUI fork addition).

  One page that does the whole "subscription -> API key" job: link the DeepAI
  account with OAuth, mint gateway keys, and register the gateway as an OpenAI
  connection so the models appear in the picker.
-->
<script lang="ts">
	import { onMount, getContext } from 'svelte';
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
		connectDeepAIToOpenWebUI
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

	onMount(load);
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


