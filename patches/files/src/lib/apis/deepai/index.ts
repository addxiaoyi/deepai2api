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