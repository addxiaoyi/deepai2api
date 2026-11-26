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
		downloadGabaiPool,
		getProxyscrapeStatus,
		startProxyscrapeRegister,
		stopProxyscrapeRegister,
		getProxyscrapePool,
		getProxyscrapeProxies,
		applySharedProxies,
		getProxyShareStatus,
		downloadProxyscrapeProxies,
		exportProxiesAs,
		checkProxies,
		listProxyShares,
		mintProxyShare,
		revokeProxyShare,
		downloadProxiesAs,
		getJevaiStatus,
		startJevaiRegister,
		stopJevaiRegister,
		getJevaiPool,
		getJevaiKeys,
		applyJevai,
		getJevaiConnect,
		downloadJevaiKeys,
		startJevaiCheckin,
		getAmdStatus,
		getAmdPool,
		importAmdAccount,
		getAmdCourses,
		startAmdStudy,
		getAmdVouchers,
		exchangeAmdVoucher,
		getAmdModels,
		consolidateAmd
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

	// proxyscrape（产物是代理：注册领试用 → 代理池 → 分发给其它线）
	let psJob: any = null;
	let psPool: any = null;
	let psProxies: any[] = [];
	let psShare: any = null;
	let psCount = 1;
	let psConcurrency = 1;
	let psMailProvider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol' = 'imap';
	let psDomains = '';
	let psCaptcha: 'browser' | 'capsolver' | 'manual' | 'none' = 'browser';
	let psCaptchaKey = '';
	let psExport: '' | 'proxies' | 'jsonl' | 'csv' = 'proxies';
	// 给别的设备用：格式 / 存活过滤 / 订阅链接
	let psDeviceFormat: 'url' | 'raw' | 'at' | 'json' | 'csv' | 'clash' | 'singbox' | 'surge' | 'env' = 'clash';
	let psAliveOnly = false;
	let psShareName = 'phone';
	let psShares: any[] = [];
	let psPreview = '';
	let psBusyCheck = false;

	// jev-ai.org（产物是 sk-glm5-… Key，自动写 data/keys/jevai.*）
	let jevJob: any = null;
	let jevPool: any = null;
	let jevKeys: any[] = [];
	let jevConnect: any = null;
	let jevCount = 1;
	let jevDomains = '';
	let jevKeyName = '';

	// AMD 开发者云（GPU 实例 + 自动刷课）
	let amdJob: any = null;
	let amdPool: any = null;
	let amdCourses: any[] = [];
	let amdEmail = '';
	let amdCookie = '';
	let amdCourse = '';
	let amdStep = 30;
	let amdTarget = 3600;
	let amdVouchers: any = null;
	let amdModels: any = null;
	let amdMaster = '';
	let amdVoucherHours = 0;

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
		try {
			const [st, pp, px, sh] = await Promise.all([
				getProxyscrapeStatus(token()),
				getProxyscrapePool(token(), 200),
				getProxyscrapeProxies(token()),
				getProxyShareStatus(token())
			]);
			psJob = st?.job ?? null;
			psPool = pp;
			psProxies = px?.proxies ?? [];
			psShare = sh?.share ?? st?.share ?? null;
		} catch {
			psPool = null;
		}
		await loadShares();
		try {
			const [ast, ap] = await Promise.all([getAmdStatus(token()), getAmdPool(token(), 200)]);
			amdJob = ast?.job ?? null;
			amdPool = ap;
		} catch {
			amdPool = null;
		}
		try {
			const [jst, jp, jk, jc] = await Promise.all([
				getJevaiStatus(token()),
				getJevaiPool(token(), 200),
				getJevaiKeys(token()),
				getJevaiConnect(token())
			]);
			jevJob = jst?.job ?? null;
			jevPool = jp;
			jevKeys = jk?.keys ?? [];
			jevConnect = jc;
		} catch {
			jevPool = null;
		}
		loading = false;
	}

	async function poll() {
		try {
			const [cs, gs, ps] = await Promise.all([
				getCleveraiStatus(token()).catch(() => null),
				getGabaiStatus(token()).catch(() => null),
				getProxyscrapeStatus(token()).catch(() => null)
			]);
			if (cs?.job) cvJob = cs.job;
			if (cs?.relay?.appCheck) cvAppCheck = Boolean(cs.relay.appCheck.ready);
			if (gs?.job) gbJob = gs.job;
			if (ps?.job) psJob = ps.job;
			const running = cs?.running || gs?.running || ps?.running;
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

	/* ---------------------------------------------------------- proxyscrape */

	async function startProxyscrape() {
		busy = 'ps-start';
		try {
			const r = await startProxyscrapeRegister(token(), {
				count: psCount,
				concurrency: psConcurrency,
				mail_provider: psMailProvider,
				mail_domains: list(psDomains),
				captcha: psCaptcha,
				captcha_key: psCaptchaKey || null,
				engine: 'auto',
				browser_stack: 'auto',
				proxies: [],
				headful: false,
				verify_email: true,
				claim_trial: true,
				export: psExport || null
			});
			psJob = r?.job ?? psJob;
			toast.success('已启动 proxyscrape 注册（产物是代理）');
		} catch (e: any) {
			toast.error(e?.message ?? '启动失败');
		} finally {
			busy = '';
		}
	}

	async function stopProxyscrape() {
		busy = 'ps-stop';
		try {
			await stopProxyscrapeRegister(token());
			toast.success('已停止');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '停止失败');
		} finally {
			busy = '';
		}
	}

	async function shareProxies() {
		busy = 'ps-share';
		try {
			const r = await applySharedProxies(token(), {});
			psShare = r;
			toast.success(`已分发 ${r?.total ?? 0} 条代理到 ${r?.lines?.length ?? 0} 条线`);
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '分发失败');
		} finally {
			busy = '';
		}
	}

	async function exportProxies() {
		busy = 'ps-export';
		try {
			const fmt = (psExport || 'proxies') as 'proxies' | 'jsonl' | 'csv';
			const blob = await downloadProxyscrapeProxies(token(), fmt);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `proxyscrape.${fmt === 'proxies' ? 'txt' : fmt}`;
			a.click();
			URL.revokeObjectURL(url);
			toast.success('已导出');
		} catch (e: any) {
			toast.error(e?.message ?? '导出失败');
		} finally {
			busy = '';
		}
	}

	/* ---------------------------------------------- 给别的设备：导出 / 订阅 */

	async function previewProxies() {
		busy = 'ps-preview';
		try {
			const r = await exportProxiesAs(token(), { format: psDeviceFormat, alive: psAliveOnly });
			psPreview = r?.body ?? '';
			toast.success(`已生成 ${r?.rows ?? 0} 条（${psDeviceFormat}）`);
		} catch (e: any) {
			toast.error(e?.message ?? '生成失败');
		} finally {
			busy = '';
		}
	}

	async function downloadProxies() {
		busy = 'ps-download';
		try {
			const blob = await downloadProxiesAs(token(), psDeviceFormat);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `proxyscrape-${psDeviceFormat}`;
			a.click();
			URL.revokeObjectURL(url);
			toast.success('已下载');
		} catch (e: any) {
			toast.error(e?.message ?? '下载失败');
		} finally {
			busy = '';
		}
	}

	async function checkAlive() {
		psBusyCheck = true;
		try {
			const r = await checkProxies(token(), { timeoutMs: 4000, concurrency: 8 });
			toast.success(`探测 ${r?.total ?? 0} 条：活 ${r?.alive ?? 0} / 死 ${r?.dead ?? 0}`);
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '探测失败');
		} finally {
			psBusyCheck = false;
		}
	}

	async function mintShare() {
		busy = 'ps-share-mint';
		try {
			const r = await mintProxyShare(token(), { name: psShareName, format: psDeviceFormat });
			toast.success(r?.reused ? '已更新同名订阅' : '已生成订阅链接');
			await loadShares();
		try {
			const [jst, jp, jk, jc] = await Promise.all([
				getJevaiStatus(token()),
				getJevaiPool(token(), 200),
				getJevaiKeys(token()),
				getJevaiConnect(token())
			]);
			jevJob = jst?.job ?? null;
			jevPool = jp;
			jevKeys = jk?.keys ?? [];
			jevConnect = jc;
		} catch {
			jevPool = null;
		}
		} catch (e: any) {
			toast.error(e?.message ?? '生成失败');
		} finally {
			busy = '';
		}
	}

	async function loadShares() {
		try {
			const r = await listProxyShares(token());
			psShares = r?.shares ?? [];
		} catch {
			psShares = [];
		}
	}

	async function revokeShare(tokenValue: string) {
		if (!confirm('撤销这条订阅？对方设备会立刻拉不到。')) return;
		busy = `ps-revoke-${tokenValue}`;
		try {
			await revokeProxyShare(token(), tokenValue);
			toast.success('已撤销');
			await loadShares();
		try {
			const [jst, jp, jk, jc] = await Promise.all([
				getJevaiStatus(token()),
				getJevaiPool(token(), 200),
				getJevaiKeys(token()),
				getJevaiConnect(token())
			]);
			jevJob = jst?.job ?? null;
			jevPool = jp;
			jevKeys = jk?.keys ?? [];
			jevConnect = jc;
		} catch {
			jevPool = null;
		}
		} catch (e: any) {
			toast.error(e?.message ?? '撤销失败');
		} finally {
			busy = '';
		}
	}

	function copyText(value: string) {
		navigator.clipboard?.writeText(value).then(
			() => toast.success('已复制'),
			() => toast.error('复制失败，请手动选中')
		);
	}

	/* ------------------------------------------------------------- jevai */

	async function startJevai() {
		busy = 'jev-start';
		try {
			const r = await startJevaiRegister(token(), {
				count: jevCount,
				concurrency: 1,
				mail_provider: 'imap',
				mail_domains: list(jevDomains),
				key_name: jevKeyName,
				proxies: [],
				verify_email: true,
				checkin: true,
				export: 'keys'
			});
			jevJob = r?.job ?? jevJob;
			toast.success('已启动 jev-ai 注册（产物是 sk-glm5-… Key）');
		} catch (e: any) {
			toast.error(e?.message ?? '启动失败');
		} finally {
			busy = '';
		}
	}

	async function stopJevai() {
		busy = 'jev-stop';
		try {
			await stopJevaiRegister(token());
			toast.success('已停止');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '停止失败');
		} finally {
			busy = '';
		}
	}

	async function applyJevaiKeys() {
		busy = 'jev-apply';
		try {
			const r = await applyJevai(token());
			toast.success(`已导出 ${r?.keys ?? 0} 把 Key -> data/keys/jevai.txt`);
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '导出失败');
		} finally {
			busy = '';
		}
	}

	async function downloadJevai() {
		busy = 'jev-download';
		try {
			const blob = await downloadJevaiKeys(token(), 'keys');
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = 'jevai-keys.txt';
			a.click();
			URL.revokeObjectURL(url);
			toast.success('已下载');
		} catch (e: any) {
			toast.error(e?.message ?? '下载失败');
		} finally {
			busy = '';
		}
	}

	async function checkinJevai() {
		busy = 'jev-checkin';
		try {
			await startJevaiCheckin(token(), { onlyToday: true, pause: 3 });
			toast.success('已开始签到（只签今天没签过的）');
		} catch (e: any) {
			toast.error(e?.message ?? '签到启动失败');
		} finally {
			busy = '';
		}
	}

	/* --------------------------------------------------------------- AMD */

	async function importAmd() {
		if (!amdEmail || !amdCookie) {
			toast.error('要填邮箱和 cookie');
			return;
		}
		busy = 'amd-import';
		try {
			await importAmdAccount(token(), { email: amdEmail, apiKey: amdCookie });
			toast.success('已入池');
			amdCookie = '';
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '入池失败');
		} finally {
			busy = '';
		}
	}

	async function loadAmdCourses() {
		busy = 'amd-courses';
		try {
			const r = await getAmdCourses(token(), amdEmail);
			const first = Array.isArray(r?.courses) ? r.courses[0] : r?.courses;
			amdCourses = first?.courses ?? [];
			toast.success(`拿到 ${amdCourses.length} 门课`);
		} catch (e: any) {
			toast.error(e?.message ?? '课程列表失败');
		} finally {
			busy = '';
		}
	}

	async function startAmdStudyJob() {
		busy = 'amd-study';
		try {
			await startAmdStudy(token(), {
				email: amdEmail || undefined,
				course: amdCourse || undefined,
				step: amdStep,
				target: amdTarget,
				pause: 1
			});
			toast.success('已开始刷课（后台跑，进度看日志）');
			await load();
		} catch (e: any) {
			toast.error(e?.message ?? '启动失败');
		} finally {
			busy = '';
		}
	}

	async function loadAmdVouchers() {
		busy = 'amd-vouchers';
		try {
			await getAmdVouchers(token(), { email: amdEmail || undefined });
			toast.success('已触发汇总，结果看日志（写 out/vouchers.json）');
		} catch (e: any) {
			toast.error(e?.message ?? '汇总失败');
		} finally {
			busy = '';
		}
	}

	async function consolidateVouchers() {
		busy = 'amd-consolidate';
		try {
			await consolidateAmd(token(), { master: amdMaster || amdEmail || undefined, redeem: true, amount: 1 });
			toast.success('已在主号上集中兑换（详情看日志）');
		} catch (e: any) {
			toast.error(e?.message ?? '汇总失败');
		} finally {
			busy = '';
		}
	}

	async function exchangeVouchers() {
		busy = 'amd-exchange';
		try {
			await exchangeAmdVoucher(token(), { email: amdEmail || undefined, amount: 1 });
			toast.success('已发起兑换');
		} catch (e: any) {
			toast.error(e?.message ?? '兑换失败');
		} finally {
			busy = '';
		}
	}

	async function loadAmdModels() {
		busy = 'amd-models';
		try {
			await getAmdModels(token());
			toast.success('已探测免费模型接入信息（看日志 / out/tokenfactory.json）');
		} catch (e: any) {
			toast.error(e?.message ?? '探测失败');
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
					<option value="solver">solver（本地 captcha-solver）</option>
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
					<option value="solver">solver（本地 captcha-solver）</option>
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

	<!-- proxyscrape 代理池 -->
	<div class="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
		<div class="mb-3 flex flex-wrap items-center gap-2">
			<div class="text-sm font-medium">ProxyScrape 代理池</div>
			<span class={badge(psJob?.status === "running", psJob?.status === "running" ? "注册中" : "空闲")}>
				{psJob?.status ?? "空闲"}
			</span>
			{#if psJob?.proxies !== undefined}
				<span class="text-xs text-gray-500">本轮 +{psJob.created ?? 0} 号 / {psJob.proxies ?? 0} 条代理</span>
			{/if}
			{#if psShare?.total !== undefined}
				<span class="text-xs text-gray-500">已分发 {psShare.total} 条到 {psShare.lines?.length ?? 0} 条线</span>
			{/if}
		</div>

		<div class="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">数量</span>
				<input type="number" min="1" bind:value={psCount} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">并发</span>
				<input type="number" min="1" bind:value={psConcurrency} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">邮箱来源</span>
				<select bind:value={psMailProvider} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="imap">imap（自有域名）</option>
					<option value="fixed">fixed</option>
					<option value="mailtm">mailtm</option>
					<option value="tempmail_lol">tempmail_lol</option>
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">Turnstile</span>
				<select bind:value={psCaptcha} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="browser">browser（Camoufox）</option>
					<option value="capsolver">capsolver</option>
					<option value="solver">solver（本地 captcha-solver）</option>
					<option value="manual">manual（贴 token）</option>
					<option value="none">none</option>
				</select>
			</label>
			<label class="flex flex-col gap-1 md:col-span-2">
				<span class="text-gray-500">邮箱域名</span>
				<input bind:value={psDomains} placeholder="example.com" class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			{#if psCaptcha !== "browser" && psCaptcha !== "none"}
				<label class="flex flex-col gap-1 md:col-span-2">
					<span class="text-gray-500">capsolver clientKey / 手工 token</span>
					<input bind:value={psCaptchaKey} class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
			{/if}
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">导出格式</span>
				<select bind:value={psExport} class="rounded border px-2 py-1 dark:bg-gray-900">
					<option value="proxies">proxies（一行一条）</option>
					<option value="jsonl">jsonl</option>
					<option value="csv">csv</option>
					<option value="">不导出</option>
				</select>
			</label>
		</div>

		<div class="mb-3 flex flex-wrap gap-2">
			<button
				class="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
				on:click={startProxyscrape}
				disabled={psJob?.status === "running" || busy === "ps-start"}
			>
				{busy === "ps-start" ? "启动中…" : "开始注册"}
			</button>
			<button
				class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={stopProxyscrape}
				disabled={psJob?.status !== "running" || busy === "ps-stop"}
			>
				停止
			</button>
			<button class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={shareProxies} disabled={busy === "ps-share"}>
				分发到其它注册机
			</button>
			<button class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={exportProxies} disabled={busy === "ps-export"}>
				导出
			</button>
		</div>

		<!-- 给其他设备：导出 / 存活过滤 / 订阅链接 -->
		<div class="mb-3 rounded border border-dashed border-gray-300 p-3 dark:border-gray-700">
			<div class="mb-2 text-xs font-medium text-gray-600 dark:text-gray-300">给其他设备使用</div>
			<div class="mb-2 flex flex-wrap items-end gap-2 text-xs">
				<label class="flex flex-col gap-1">
					<span class="text-gray-500">格式</span>
					<select bind:value={psDeviceFormat} class="rounded border px-2 py-1 dark:bg-gray-900">
						<option value="sub2api">sub2api JSON（导入数据）</option>
						<option value="sub2api-txt">sub2api 文本（一行一条）</option>
						<option value="cpa">CPA / CLIProxyApi 文本</option>
						<option value="clash">Clash / Mihomo YAML</option>
						<option value="singbox">sing-box JSON</option>
						<option value="surge">Surge / Shadowrocket</option>
						<option value="url">URL 列表</option>
						<option value="raw">host:port:user:pass</option>
						<option value="at">user:pass@host:port</option>
						<option value="json">JSON</option>
						<option value="csv">CSV</option>
						<option value="env">环境变量</option>
					</select>
				</label>
				<label class="flex items-center gap-1">
					<input type="checkbox" bind:checked={psAliveOnly} />
					<span>只导出存活</span>
				</label>
				<button
					class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
					on:click={previewProxies}
					disabled={busy === 'ps-preview'}
				>
					预览
				</button>
				<button
					class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
					on:click={downloadProxies}
					disabled={busy === 'ps-download'}
				>
					下载文件
				</button>
				<button
					class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
					on:click={checkAlive}
					disabled={psBusyCheck}
				>
					{psBusyCheck ? '探测中…' : '探测存活'}
				</button>
			</div>

			{#if psPreview}
				<textarea readonly rows="4" class="mb-2 w-full rounded border px-2 py-1 font-mono text-[11px] dark:bg-gray-900">{psPreview}</textarea>
			{/if}

			<div class="mb-2 flex flex-wrap items-end gap-2 text-xs">
				<label class="flex flex-col gap-1">
					<span class="text-gray-500">订阅名称（设备标识）</span>
					<input bind:value={psShareName} class="rounded border px-2 py-1 dark:bg-gray-900" />
				</label>
				<button
					class="rounded bg-blue-600 px-2 py-1 text-white hover:bg-blue-700 disabled:opacity-50"
					on:click={mintShare}
					disabled={busy === 'ps-share-mint'}
				>
					生成订阅链接
				</button>
			</div>

			{#if psShares.length}
				<div class="overflow-x-auto">
					<table class="w-full text-left text-[11px]">
						<thead class="text-gray-500">
							<tr>
								<th class="py-1 pr-3">名称</th>
								<th class="py-1 pr-3">格式</th>
								<th class="py-1 pr-3">订阅地址</th>
								<th class="py-1 pr-3">命中</th>
								<th class="py-1"></th>
							</tr>
						</thead>
						<tbody>
							{#each psShares as s}
								<tr class="border-t border-gray-100 dark:border-gray-900">
									<td class="py-1 pr-3">{s.name}</td>
									<td class="py-1 pr-3">{s.format}</td>
									<td class="py-1 pr-3 font-mono">
										{#if s.revoked}
											<span class="text-gray-400">已撤销</span>
										{:else}
											<button class="text-blue-600 hover:underline" on:click={() => copyText(s.url)}>{s.url}</button>
										{/if}
									</td>
									<td class="py-1 pr-3">{s.hits ?? 0}</td>
									<td class="py-1 text-right">
										{#if !s.revoked}
											<button class="text-red-500 hover:underline" on:click={() => revokeShare(s.name)} disabled={busy === `ps-revoke-${s.name}`}>
												撤销
											</button>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>

		{#if psProxies.length}
			<div class="mb-2 overflow-x-auto">
				<table class="w-full text-left text-xs">
					<thead class="text-gray-500">
						<tr>
							<th class="py-1 pr-3">代理</th>
							<th class="py-1 pr-3">认证</th>
							<th class="py-1 pr-3">成功/失败</th>
							<th class="py-1 pr-3">冷却</th>
						</tr>
					</thead>
					<tbody>
						{#each psProxies.slice(0, 20) as px}
							<tr class="border-t border-gray-100 dark:border-gray-900">
								<td class="py-1 pr-3 font-mono">{px.endpoint}</td>
								<td class="py-1 pr-3">{px.hasAuth ? "user:pass" : "无"}</td>
								<td class="py-1 pr-3">{px.ok ?? 0} / {px.failures ?? 0}</td>
								<td class="py-1 pr-3">{px.cooldownUntil > Date.now() ? "冷却中" : "—"}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<div class="text-xs text-gray-500">代理池还是空的：注册几个号，试用额度会带出代理。</div>
		{/if}

		{#if psShare?.lines?.length}
			<div class="mt-2 text-xs text-gray-500">
				分发：
				{#each psShare.lines as row}
					<span class="mr-2 font-mono">{row.line}={row.rows}</span>
				{/each}
			</div>
		{/if}
	</div>

	<!-- jev-ai.org Key 池 -->
	<div class="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
		<div class="mb-3 flex flex-wrap items-center gap-2">
			<div class="text-sm font-medium">JEV AI（jev-ai.org）</div>
			<span class={badge(jevJob?.status === "running", jevJob?.status === "running" ? "注册中" : "空闲")}>
				{jevJob?.status ?? "空闲"}
			</span>
			{#if jevPool?.stats?.keys !== undefined}
				<span class="text-xs text-gray-500">
					{jevPool.stats.total} 号 / {jevPool.stats.keys} Key / 健康 {jevPool.stats.active ?? 0}
				</span>
			{/if}
			{#if jevJob?.keys}
				<span class="text-xs text-gray-500">本轮 Key {jevJob.keys}</span>
			{/if}
		</div>

		<div class="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">数量</span>
				<input type="number" min="1" bind:value={jevCount} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">Key 名称</span>
				<input bind:value={jevKeyName} placeholder="dcg-2026-11-26" class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 md:col-span-2">
				<span class="text-gray-500">邮箱域名（一次性邮箱会被拒）</span>
				<input bind:value={jevDomains} placeholder="example.com" class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
		</div>

		<div class="mb-3 flex flex-wrap gap-2">
			<button
				class="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
				on:click={startJevai}
				disabled={jevJob?.status === "running" || busy === "jev-start"}
			>
				{busy === "jev-start" ? "启动中…" : "开始注册"}
			</button>
			<button
				class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={stopJevai}
				disabled={jevJob?.status !== "running" || busy === "jev-stop"}
			>
				停止
			</button>
			<button class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={applyJevaiKeys} disabled={busy === "jev-apply"}>
				导出并接入
			</button>
			<button class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 dark:bg-gray-800 dark:hover:bg-gray-700" on:click={downloadJevai} disabled={busy === "jev-download"}>
				下载 Key
			</button>
			<button
				class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={checkinJevai}
				disabled={busy === "jev-checkin" || jevJob?.status === "running"}
			>
				每日签到
			</button>
		</div>

		{#if jevConnect}
			<div class="mb-2 rounded border border-dashed border-gray-300 p-2 text-[11px] dark:border-gray-700">
				<div class="text-gray-500">接入（OpenAI 兼容）</div>
				<div class="font-mono">Base: {jevConnect.gatewayBase ?? "/jevai/v1"}</div>
				<div class="font-mono">上游: {jevConnect.upstreamBase}</div>
				<div class="text-gray-500">
					网关 Key 前缀 {jevConnect.gatewayKeyPrefix || "—"}；Key 前缀 {jevConnect.key_prefix}
				</div>
			</div>
		{/if}

		{#if jevKeys.length}
			<div class="overflow-x-auto">
				<table class="w-full text-left text-[11px]">
					<thead class="text-gray-500">
						<tr>
							<th class="py-1 pr-3">邮箱</th>
							<th class="py-1 pr-3">Key</th>
							<th class="py-1 pr-3">已验证</th>
							<th class="py-1 pr-3">网关 Key</th>
						</tr>
					</thead>
					<tbody>
						{#each jevKeys.slice(0, 15) as row}
							<tr class="border-t border-gray-100 dark:border-gray-900">
								<td class="py-1 pr-3">{row.email}</td>
								<td class="py-1 pr-3 font-mono">{row.apiKeyPreview}</td>
								<td class="py-1 pr-3">{row.emailVerified ? "是" : "否"}</td>
								<td class="py-1 pr-3 font-mono">{row.gatewayKey?.prefix ?? "—"}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<div class="text-xs text-gray-500">
				号池是空的：注册几个号，Key 会写进池子并导出到 data/keys/jevai.txt。
			</div>
		{/if}
	</div>

	<!-- AMD 开发者云（GPU 实例 + 自动刷课） -->
	<div class="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
		<div class="mb-3 flex flex-wrap items-center gap-2">
			<div class="text-sm font-medium">AMD 开发者云（GPU）</div>
			<span class={badge(amdJob?.status === "running", amdJob?.status === "running" ? "刷课中" : "空闲")}>
				{amdJob?.mode ?? "空闲"}
			</span>
			{#if amdPool?.stats}
				<span class="text-xs text-gray-500">
					{amdPool.stats.total} 号 / 有实例 {amdPool.instances?.withInstance ?? 0} / 积分 {amdPool.instances?.totalPoints ?? 0}
				</span>
			{/if}
		</div>

		<div class="mb-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">账号邮箱</span>
				<input bind:value={amdEmail} placeholder="you@example.com" class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 md:col-span-3">
				<span class="text-gray-500">cookie（浏览器登录 developer.amd.com.cn 后复制 Cookie 头）</span>
				<input bind:value={amdCookie} class="rounded border px-2 py-1 font-mono dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">每次上报秒数</span>
				<input type="number" min="5" bind:value={amdStep} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">每章节刷到（秒）</span>
				<input type="number" min="60" bind:value={amdTarget} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<label class="flex flex-col gap-1 md:col-span-2">
				<span class="text-gray-500">只刷某门课（课程 id，可留空）</span>
				<input bind:value={amdCourse} class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
		</div>

		<div class="mb-3 flex flex-wrap gap-2">
			<button
				class="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
				on:click={importAmd}
				disabled={busy === "amd-import"}
			>
				入池
			</button>
			<button
				class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={loadAmdCourses}
				disabled={busy === "amd-courses"}
			>
				{busy === "amd-courses" ? "查询中…" : "看课程"}
			</button>
			<button
				class="rounded bg-gray-200 px-3 py-1 text-xs hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={startAmdStudyJob}
				disabled={busy === "amd-study" || amdJob?.status === "running"}
			>
				一键刷课
			</button>
		</div>

		<div class="mb-3 flex flex-wrap items-end gap-2 text-xs">
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">主号（算力券累加到它）</span>
				<input bind:value={amdMaster} placeholder="master@example.com" class="rounded border px-2 py-1 dark:bg-gray-900" />
			</label>
			<button
				class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={loadAmdVouchers}
				disabled={busy === 'amd-vouchers'}>算力券汇总</button
			>
			<button
				class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={exchangeVouchers}
				disabled={busy === 'amd-exchange'}>兑换券</button
			>
			<button
				class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={consolidateVouchers}
				disabled={busy === 'amd-consolidate'}>累加到主号</button
			>
			<button
				class="rounded bg-gray-200 px-2 py-1 hover:bg-gray-300 disabled:opacity-50 dark:bg-gray-800 dark:hover:bg-gray-700"
				on:click={loadAmdModels}
				disabled={busy === 'amd-models'}>探测免费模型</button
			>
		</div>

		{#if amdCourses.length}
			<div class="mb-2 overflow-x-auto">
				<table class="w-full text-left text-[11px]">
					<thead class="text-gray-500">
						<tr>
							<th class="py-1 pr-3">课程</th>
							<th class="py-1 pr-3">进度</th>
							<th class="py-1 pr-3">状态</th>
							<th class="py-1"></th>
						</tr>
					</thead>
					<tbody>
						{#each amdCourses as course}
							<tr class="border-t border-gray-100 dark:border-gray-900">
								<td class="py-1 pr-3">{course.title || course.id}</td>
								<td class="py-1 pr-3">{course.studyRate ?? 0}%</td>
								<td class="py-1 pr-3">{course.studyStatus ?? 0}</td>
								<td class="py-1 text-right">
									<button
										class="text-blue-600 hover:underline"
										on:click={() => {
											amdCourse = course.id;
											startAmdStudyJob();
										}}>刷这门</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if amdJob?.lines?.length}
			<div class="max-h-32 overflow-y-auto rounded bg-gray-50 p-2 font-mono text-[10px] leading-4 dark:bg-gray-900">
				{#each amdJob.lines.slice(-20) as line}
					<div>{line.at?.slice(11, 19)} {line.text}</div>
				{/each}
			</div>
		{:else}
			<div class="text-xs text-gray-500">
				流程：浏览器登录 → 复制 cookie → 入池 → 看课程 → 一键刷课 → 积分到手后去起 GPU 实例。
			</div>
		{/if}
	</div>

</div>

