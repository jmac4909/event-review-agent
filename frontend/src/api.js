export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      typeof body.detail === "string"
        ? body.detail
        : "The request could not be completed.",
    );
  }
  return response.status === 204 ? null : response.json();
}

export function supervisorApi(accountId, onSessionChanged) {
  let active = true;
  function checkActive() {
    if (!active) throw new DOMException("This workspace is closed.", "AbortError");
  }
  return {
    close() { active = false; },
    async request(path, options = {}) {
      checkActive();
      try {
        const result = await api(path, {
          ...options,
          headers: { ...options.headers, "X-Expected-Supervisor": accountId },
        });
        checkActive(); // Ignore a delayed response from a closed workspace.
        return result;
      } catch (error) {
        checkActive();
        if (error.status === 401 || error.status === 412) {
          active = false;
          onSessionChanged(error.message);
        }
        throw error;
      }
    },
  };
}
