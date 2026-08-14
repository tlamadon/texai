// Tiny fetch helpers. Errors from the backend arrive as
// {detail: {error, message}}; surface `message` so toasts stay readable.

async function unwrap(response) {
  if (response.ok) {
    return response.status === 204 ? null : response.json();
  }
  let message = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    const detail = body.detail;
    if (typeof detail === 'string') {
      message = detail;
    } else if (detail && typeof detail.message === 'string') {
      message = detail.message;
    } else if (Array.isArray(detail) && detail.length) {
      // FastAPI validation errors
      message = detail.map((d) => `${(d.loc || []).join('.')}: ${d.msg}`).join('; ');
    }
  } catch {
    /* keep the status line */
  }
  const error = new Error(message);
  error.status = response.status;
  throw error;
}

export async function getJSON(url) {
  return unwrap(await fetch(url, { cache: 'no-store' }));
}

export async function postJSON(url, body) {
  return unwrap(
    await fetch(url, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}
