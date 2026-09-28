export function apiPrefix(browser = window) {
  return String(browser.FASTLAB_PUBLIC_PATH || "").replace(/\/$/, "");
}

export async function api(path, options = {}, runtime = {}) {
  const browser = runtime.window || window;
  const request = runtime.fetch || fetch;
  const response = await request(`${apiPrefix(browser)}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && payload.loginUrl) {
    browser.location.assign(payload.loginUrl);
    throw new Error("FastResearch 登录已失效。");
  }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（${response.status}）`);
    error.code = payload.code;
    error.payload = payload;
    throw error;
  }
  return payload;
}
