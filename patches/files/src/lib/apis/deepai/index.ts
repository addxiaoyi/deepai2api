// DeepAI Code gateway API client (Open WebUI fork addition).
//
// Thin wrapper over the fork's /api/v1/deepai router; the router itself proxies
// to the Node gateway that owns OAuth and the keys.
import { WEBUI_API_BASE_URL } from '$lib/constants';

const request = async (token: string, path: string, options: RequestInit = {}) => {
	let error = null;
	const res = await fetch(`${WEBUI_API_BASE_URL}/deepai${path}`, {
		...options,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`,
			...(options.headers ?? {})
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err?.detail ?? err?.message ?? '请求失败';
			return null;
		});

	if (error) throw new Error(error);
	return res;
};

export const getDeepAIStatus = async (token: string = '') => request(token, '/status');

export const updateDeepAISettings = async (
	token: string = '',
	gatewayUrl: string | null = null,
	adminToken: string | null = null
) =>
	request(token, '/settings', {
		method: 'POST',
		body: JSON.stringify({ gateway_url: gatewayUrl, admin_token: adminToken })
	});

export const startDeepAILogin = async (token: string = '') =>
	request(token, '/login/start', { method: 'POST' });

export const exchangeDeepAICode = async (token: string = '', code: string, state: string | null = null) =>
	request(token, '/login/exchange', { method: 'POST', body: JSON.stringify({ code, state }) });

export const removeDeepAIAccount = async (token: string = '', accountId: string) =>
	request(token, `/accounts/${accountId}/remove`, { method: 'POST' });

export const listDeepAIKeys = async (token: string = '') => request(token, '/keys');

export const createDeepAIKey = async (
	token: string = '',
	label: string = 'openwebui',
	rpm: number = 0,
	dailyRequests: number = 0
) =>
	request(token, '/keys', {
		method: 'POST',
		body: JSON.stringify({ label, rpm, daily_requests: dailyRequests })
	});

export const revokeDeepAIKey = async (token: string = '', keyId: string) =>
	request(token, `/keys/${keyId}/revoke`, { method: 'POST' });

export const deleteDeepAIKey = async (token: string = '', keyId: string) =>
	request(token, `/keys/${keyId}`, { method: 'DELETE' });

export const connectDeepAIToOpenWebUI = async (token: string = '', key: string | null = null) =>
	request(token, '/connect', { method: 'POST', body: JSON.stringify({ key }) });

export const listDeepAIModels = async (token: string = '') => request(token, '/models');

// ---------------------------------------------------------------- bulk signup

export type RegisterOptions = {
	count: number;
	engine: 'auto' | 'protocol' | 'browser';
	concurrency: number;
	mailProvider: 'tempmail_lol' | 'mailtm' | 'imap' | 'fixed';
	mailDomains: string[];
	proxies: string[];
	verifyEmail: boolean;
	headless: boolean;
};

export const getDeepAIRegister = async (token: string = '') => request(token, '/register');

export const startDeepAIRegister = async (token: string = '', options: RegisterOptions) =>
	request(token, '/register', {
		method: 'POST',
		body: JSON.stringify({
			count: options.count,
			engine: options.engine,
			concurrency: options.concurrency,
			mail_provider: options.mailProvider,
			mail_domains: options.mailDomains,
			proxies: options.proxies,
			verify_email: options.verifyEmail,
			headless: options.headless
		})
	});

export const stopDeepAIRegister = async (token: string = '') =>
	request(token, '/register/stop', { method: 'POST' });

export const getDeepAIRegisterLog = async (token: string = '', level: string = '', limit: number = 120) =>
	request(token, `/register/log?level=${encodeURIComponent(level)}&limit=${limit}`);

export const getDeepAIRegisterDomains = async (token: string = '', force: boolean = false) =>
	request(token, `/register/domains${force ? '?force=1' : ''}`);

export const downloadDeepAIRegisterLog = async (token: string = '', level: string = '') => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/deepai/register/log?download=1&level=${encodeURIComponent(level)}`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};

// ------------------------------------------------------------------ poe pool

export type PoeRegisterOptions = {
	count: number;
	engine: 'auto' | 'protocol' | 'browser';
	concurrency: number;
	mailProvider: 'tempmail_lol' | 'mailtm' | 'imap' | 'fixed';
	mailDomains: string[];
	smsProvider: 'off' | 'manual' | 'sms_activate' | 'smspool';
	headless: boolean;
};

export const getPoeAccounts = async (token: string = '', limit: number = 100) =>
	request(token, `/poe/accounts?limit=${limit}`);

export const deletePoeAccount = async (token: string = '', id: string) =>
	request(token, `/poe/accounts/${id}`, { method: 'DELETE' });

export const setPoeAccountStatus = async (token: string = '', email: string, status: string) =>
	request(token, '/poe/accounts/status', { method: 'POST', body: JSON.stringify({ email, status }) });

export const getPoeRegister = async (token: string = '') => request(token, '/poe/register');

export const startPoeRegister = async (token: string = '', options: PoeRegisterOptions) =>
	request(token, '/poe/register', {
		method: 'POST',
		body: JSON.stringify({
			count: options.count,
			engine: options.engine,
			concurrency: options.concurrency,
			mail_provider: options.mailProvider,
			mail_domains: options.mailDomains,
			sms_provider: options.smsProvider,
			headless: options.headless
		})
	});

export const stopPoeRegister = async (token: string = '') =>
	request(token, '/poe/register/stop', { method: 'POST' });

export const downloadPoeLog = async (token: string = '', level: string = '') => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/deepai/poe/register/log?download=1&level=${encodeURIComponent(level)}`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};

// ------------------------------------------------------------------ providers
//
// 通用 provider 面（目前面板只接 moonai）。网关侧号池文件是
// data/<provider>-pool.json，与 DeepAI / Allmodels / Poe 的池完全隔离。

export type ProviderRegisterOptions = {
	count: number;
	concurrency: number;
	engine: 'auto' | 'protocol' | 'browser' | 'hybrid';
	browserStack: 'auto' | 'camoufox' | 'patchright' | 'playwright';
	mailProvider: 'imap' | 'domain' | 'mailtm' | 'fixed';
	mailDomains: string[];
	email?: string;
	proxies: string[];
	cdp?: string;
	headful: boolean;
	humanize: number;
	keysPerAccount: number;
	export?: '' | 'jsonl' | 'csv' | 'pool';
	mintKeys: boolean;
	push: boolean;
	verifyEmail: boolean;
	antiBan?: Record<string, unknown>;
};

export const listProviders = async (token: string = '') => request(token, '/providers');

export const getProvider = async (token: string = '', providerId: string = 'moonai') =>
	request(token, `/providers/${providerId}`);

export const getProviderRegister = async (token: string = '', providerId: string = 'moonai') =>
	request(token, `/providers/${providerId}/register`);

export const startProviderRegister = async (
	token: string = '',
	providerId: string = 'moonai',
	options: ProviderRegisterOptions
) =>
	request(token, `/providers/${providerId}/register`, {
		method: 'POST',
		body: JSON.stringify({
			count: options.count,
			concurrency: options.concurrency,
			engine: options.engine,
			browser_stack: options.browserStack,
			mail_provider: options.mailProvider,
			mail_domains: options.mailDomains,
			email: options.email ?? '',
			proxies: options.proxies,
			cdp: options.cdp ?? '',
			headful: options.headful,
			humanize: options.humanize,
			keys_per_account: options.keysPerAccount,
			export: options.export ?? '',
			mint_keys: options.mintKeys,
			push: options.push,
			verify_email: options.verifyEmail,
			anti_ban: options.antiBan ?? {}
		})
	});

export const stopProviderRegister = async (token: string = '', providerId: string = 'moonai') =>
	request(token, `/providers/${providerId}/register/stop`, { method: 'POST' });

export const getProviderPool = async (token: string = '', providerId: string = 'moonai', limit = 200) =>
	request(token, `/providers/${providerId}/pool?limit=${limit}`);

export const importProviderAccount = async (
	token: string = '',
	providerId: string = 'moonai',
	account: Record<string, unknown>,
	mintKey = true
) =>
	request(token, `/providers/${providerId}/pool/import`, {
		method: 'POST',
		body: JSON.stringify({ account, mint_key: mintKey })
	});

export const reportProviderHealth = async (
	token: string = '',
	providerId: string = 'moonai',
	accountId: string,
	ok: boolean,
	error: string | null = null,
	cooldownMs = 0
) =>
	request(token, `/providers/${providerId}/pool/${accountId}/health`, {
		method: 'POST',
		body: JSON.stringify({ ok, error, cooldown_ms: cooldownMs })
	});

export const deleteProviderAccount = async (
	token: string = '',
	providerId: string = 'moonai',
	accountId: string
) => request(token, `/providers/${providerId}/pool/${accountId}`, { method: 'DELETE' });

export const probeProvider = async (token: string = '', providerId: string = 'moonai') =>
	request(token, `/providers/${providerId}/probe`, { method: 'POST' });

export const getProviderKeys = async (token: string = '', providerId: string = 'moonai') =>
	request(token, `/providers/${providerId}/keys`);

export const getProviderConnectInfo = async (token: string = '', providerId: string = 'moonai') =>
	request(token, `/providers/${providerId}/connect`);

export const exportProviderPool = async (
	token: string = '',
	providerId: string = 'moonai',
	format: 'jsonl' | 'csv' | 'pool' = 'jsonl'
) => request(token, `/providers/${providerId}/pool/export?format=${format}`, { method: 'POST' });

export const downloadProviderPool = async (
	token: string = '',
	providerId: string = 'moonai',
	format: 'jsonl' | 'csv' | 'pool' = 'jsonl'
) => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/deepai/providers/${providerId}/pool/export?format=${format}&download=1`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};
/* ------------------------------------------------- 号池总览 + 两条新线 */

export const getPoolsOverview = async (token: string = '') => request(token, '/pools');

export type CleveraiRegisterOptions = {
	count: number;
	concurrency: number;
	engine: 'auto' | 'protocol' | 'browser';
	browser_stack: 'auto' | 'camoufox' | 'patchright' | 'playwright';
	mail_provider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol';
	mail_domains: string[];
	appcheck: 'browser' | 'capsolver' | 'manual';
	captcha_key?: string | null;
	appcheck_token?: string | null;
	chat_engine?: string | null;
	proxies: string[];
	headful: boolean;
	humanize: number;
	verify_email: boolean;
	warmup: boolean;
	export?: 'jsonl' | 'csv' | 'pool' | null;
};

export const getCleveraiStatus = async (token: string = '') =>
	request(token, '/cleverai/status');

export const startCleveraiRegister = async (
	token: string = '',
	options: CleveraiRegisterOptions
) => request(token, '/cleverai/register', { method: 'POST', body: JSON.stringify(options) });

export const stopCleveraiRegister = async (token: string = '') =>
	request(token, '/cleverai/register/stop', { method: 'POST' });

export const getCleveraiPool = async (token: string = '', limit: number = 200) =>
	request(token, `/cleverai/pool?limit=${limit}`);

export const importCleveraiAccount = async (
	token: string = '',
	account: Record<string, unknown>,
	mintKey: boolean = true
) =>
	request(token, '/cleverai/pool/import', {
		method: 'POST',
		body: JSON.stringify({ account, mint_key: mintKey })
	});

export const deleteCleveraiAccount = async (token: string = '', accountId: string) =>
	request(token, `/cleverai/pool/${accountId}`, { method: 'DELETE' });

export const refreshCleveraiAppCheck = async (token: string = '') =>
	request(token, '/cleverai/appcheck', { method: 'POST' });

export const getCleveraiConnectInfo = async (token: string = '') =>
	request(token, '/cleverai/connect');

export const exportCleveraiPool = async (
	token: string = '',
	format: 'jsonl' | 'csv' | 'pool' = 'jsonl'
) => request(token, `/cleverai/pool/export?format=${format}`, { method: 'POST' });

export const downloadCleveraiPool = async (
	token: string = '',
	format: 'jsonl' | 'csv' | 'pool' = 'jsonl'
) => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/deepai/cleverai/pool/export?format=${format}&download=1`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};

export type GabaiRegisterOptions = {
	count: number;
	concurrency: number;
	engine: 'auto' | 'protocol' | 'browser';
	browser_stack: 'auto' | 'camoufox' | 'patchright' | 'playwright';
	mail_provider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol';
	mail_domains: string[];
	email?: string | null;
	captcha: 'none' | 'capsolver' | 'manual';
	captcha_key?: string | null;
	proxies: string[];
	headful: boolean;
	humanize: number;
	verify_email: boolean;
	create_api_key: boolean;
	export?: 'jsonl' | 'csv' | 'pool' | null;
};

export const getGabaiStatus = async (token: string = '') => request(token, '/gabai/status');

export const startGabaiRegister = async (token: string = '', options: GabaiRegisterOptions) =>
	request(token, '/gabai/register', { method: 'POST', body: JSON.stringify(options) });

export const stopGabaiRegister = async (token: string = '') =>
	request(token, '/gabai/register/stop', { method: 'POST' });

export const getGabaiPool = async (token: string = '', limit: number = 200) =>
	request(token, `/gabai/pool?limit=${limit}`);

export const importGabaiAccount = async (
	token: string = '',
	account: Record<string, unknown>,
	mintKey: boolean = true
) =>
	request(token, '/gabai/pool/import', {
		method: 'POST',
		body: JSON.stringify({ account, mint_key: mintKey })
	});

export const mintGabaiKey = async (token: string = '', accountId: string) =>
	request(token, `/gabai/pool/${accountId}/mint`, { method: 'POST' });

export const deleteGabaiAccount = async (token: string = '', accountId: string) =>
	request(token, `/gabai/pool/${accountId}`, { method: 'DELETE' });

export const getGabaiConnectInfo = async (token: string = '') =>
	request(token, '/gabai/connect');

export const exportGabaiPool = async (
	token: string = '',
	format: 'jsonl' | 'csv' | 'pool' = 'jsonl'
) => request(token, `/gabai/pool/export?format=${format}`, { method: 'POST' });

export const downloadGabaiPool = async (
	token: string = '',
	format: 'jsonl' | 'csv' | 'pool' = 'jsonl'
) => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/deepai/gabai/pool/export?format=${format}&download=1`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};

/* ------------------------------------------------------- proxyscrape 代理池 */

export type ProxyscrapeRegisterOptions = {
	count: number;
	concurrency: number;
	mail_provider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol';
	mail_domains: string[];
	captcha: 'browser' | 'capsolver' | 'manual' | 'none';
	captcha_key?: string | null;
	engine: 'auto' | 'protocol' | 'browser';
	browser_stack: 'auto' | 'camoufox' | 'patchright' | 'playwright';
	proxies: string[];
	headful: boolean;
	verify_email: boolean;
	claim_trial: boolean;
	export?: 'proxies' | 'jsonl' | 'csv' | null;
};

export const getProxyscrapeStatus = async (token: string = '') =>
	request(token, '/proxyscrape/status');

export const startProxyscrapeRegister = async (
	token: string = '',
	options: ProxyscrapeRegisterOptions
) => request(token, '/proxyscrape/register', { method: 'POST', body: JSON.stringify(options) });

export const stopProxyscrapeRegister = async (token: string = '') =>
	request(token, '/proxyscrape/register/stop', { method: 'POST' });

export const getProxyscrapePool = async (token: string = '', limit: number = 200) =>
	request(token, `/proxyscrape/pool?limit=${limit}`);

export const getProxyscrapeProxies = async (token: string = '') =>
	request(token, '/proxyscrape/proxies');

export const applySharedProxies = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/proxies/apply', { method: 'POST', body: JSON.stringify(options) });

export const getProxyShareStatus = async (token: string = '') => request(token, '/proxies');

export const downloadProxyscrapeProxies = async (
	token: string = '',
	format: 'proxies' | 'jsonl' | 'csv' = 'proxies'
) => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/deepai/proxyscrape/pool/export?format=${format}&download=1`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};

/* ------------------------------------------------- 代理导出 / 订阅（别的设备） */

export const getProxyFormats = async (token: string = '') => request(token, '/proxies/formats');

export const exportProxiesAs = async (
	token: string = '',
	options: { format?: string; alive?: boolean; limit?: number } = {}
) => {
	const format = options.format ?? 'url';
	const alive = options.alive ? 1 : 0;
	const limit = options.limit ?? 0;
	return request(token, `/proxies/export?format=${format}&alive=${alive}&limit=${limit}`);
};

export const checkProxies = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/proxies/check', { method: 'POST', body: JSON.stringify(options) });

export const listProxyShares = async (token: string = '') => request(token, '/proxies/shares');

export const mintProxyShare = async (
	token: string = '',
	options: { name?: string; format?: string; note?: string } = {}
) => request(token, '/proxies/share', { method: 'POST', body: JSON.stringify(options) });

export const revokeProxyShare = async (token: string = '', shareToken: string, purge = false) =>
	request(token, `/proxies/shares/${shareToken}?purge=${purge ? 1 : 0}`, { method: 'DELETE' });

export const downloadProxiesAs = async (token: string = '', format: string = 'url') => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/deepai/proxies/export?format=${format}&download=1`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};

/* ------------------------------------------------------- jev-ai.org Key 池 */

export type JevaiRegisterOptions = {
	count: number;
	concurrency: number;
	mail_provider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol';
	mail_domains: string[];
	key_name?: string;
	proxies: string[];
	verify_email: boolean;
	checkin: boolean;
	export?: 'keys' | 'jsonl' | 'csv' | null;
};

export const getJevaiStatus = async (token: string = '') => request(token, '/jevai/status');

export const startJevaiRegister = async (token: string = '', options: JevaiRegisterOptions) =>
	request(token, '/jevai/register', { method: 'POST', body: JSON.stringify(options) });

export const stopJevaiRegister = async (token: string = '') =>
	request(token, '/jevai/register/stop', { method: 'POST' });

export const getJevaiPool = async (token: string = '', limit: number = 200) =>
	request(token, `/jevai/pool?limit=${limit}`);

export const getJevaiKeys = async (token: string = '') => request(token, '/jevai/keys');

export const applyJevai = async (token: string = '') => request(token, '/jevai/apply', { method: 'POST' });

export const getJevaiConnect = async (token: string = '') => request(token, '/jevai/connect');

export const downloadJevaiKeys = async (token: string = '', format: 'keys' | 'jsonl' | 'csv' = 'keys') => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/deepai/jevai/pool/export?format=${format}&download=1`, {
		headers: { authorization: `Bearer ${token}` }
	});
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};

export const startJevaiCheckin = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/jevai/checkin', { method: 'POST', body: JSON.stringify(options) });

/* -------------------------------------- 六条 OpenAI 兼容线（每条一个页面） */

export type LineRegisterOptions = {
	count: number;
	concurrency: number;
	mail_provider: 'imap' | 'fixed' | 'mailtm' | 'tempmail_lol';
	mail_domains: string[];
	key_name?: string;
	proxies: string[];
	verify_email: boolean;
	export?: 'keys' | 'jsonl' | 'csv' | null;
};

export const getLinesOverview = async (token: string = '') => request(token, '/lines');

export const getLineStatus = async (token: string = '', line: string) =>
	request(token, `/lines/${line}/status`);

export const startLineRegister = async (token: string = '', line: string, options: LineRegisterOptions) =>
	request(token, `/lines/${line}/register`, { method: 'POST', body: JSON.stringify(options) });

export const stopLineRegister = async (token: string = '', line: string) =>
	request(token, `/lines/${line}/register/stop`, { method: 'POST' });

export const getLinePool = async (token: string = '', line: string, limit: number = 200) =>
	request(token, `/lines/${line}/pool?limit=${limit}`);

export const importLineAccount = async (token: string = '', line: string, account: Record<string, unknown>) =>
	request(token, `/lines/${line}/pool/import`, { method: 'POST', body: JSON.stringify(account) });

export const getLineKeys = async (token: string = '', line: string) =>
	request(token, `/lines/${line}/keys`);

export const applyLine = async (token: string = '', line: string) =>
	request(token, `/lines/${line}/apply`, { method: 'POST' });

export const getLineConnect = async (token: string = '', line: string) =>
	request(token, `/lines/${line}/connect`);

export const deleteLineAccount = async (token: string = '', line: string, accountId: string) =>
	request(token, `/lines/${line}/pool/${accountId}`, { method: 'DELETE' });

export const downloadLineKeys = async (token: string = '', line: string, format: 'keys' | 'jsonl' | 'csv' = 'keys') => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/deepai/lines/${line}/pool/export?format=${format}&download=1`, {
		headers: { authorization: `Bearer ${token}` }
	});
	if (!res.ok) throw new Error('导出失败');
	return res.blob();
};


/* ------------------------------------------------------------ AMD 开发者云 */

export const getAmdStatus = async (token: string = '') => request(token, '/amd/status');

export const getAmdPool = async (token: string = '', limit: number = 200) =>
	request(token, `/amd/pool?limit=${limit}`);

export const importAmdAccount = async (token: string = '', account: Record<string, unknown>) =>
	request(token, '/amd/pool/import', { method: 'POST', body: JSON.stringify(account) });

export const getAmdCourses = async (token: string = '', email: string = '') =>
	request(token, `/amd/courses${email ? `?email=${encodeURIComponent(email)}` : ''}`);

export const startAmdStudy = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/amd/study', { method: 'POST', body: JSON.stringify(options) });

export const applyAmd = async (token: string = '') => request(token, '/amd/apply', { method: 'POST' });

export const startAmdExam = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/amd/exam', { method: 'POST', body: JSON.stringify(options) });

export const getAmdVouchers = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/amd/vouchers', { method: 'POST', body: JSON.stringify(options) });

export const exchangeAmdVoucher = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/amd/exchange', { method: 'POST', body: JSON.stringify(options) });

export const getAmdModels = async (token: string = '') =>
	request(token, '/amd/models', { method: 'POST', body: '{}' });

export const consolidateAmd = async (token: string = '', options: Record<string, unknown> = {}) =>
	request(token, '/amd/consolidate', { method: 'POST', body: JSON.stringify(options) });
