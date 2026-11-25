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