/**
 * Agent Relay — Frontend API Client
 * Typed fetch wrapper for all /api/* backend endpoints.
 */

async function request(url, options = {}) {
  const config = {
    ...options,
    headers: {
      Accept: 'application/json',
      ...options.headers,
    },
  };

  if (config.body && typeof config.body === 'object' && !(config.body instanceof FormData)) {
    config.headers['Content-Type'] = 'application/json';
    config.body = JSON.stringify(config.body);
  }

  const res = await fetch(url, config);

  if (!res.ok) {
    let detail = res.statusText || 'Request failed';
    try {
      const data = await res.json();
      if (data && data.detail) {
        detail = data.detail;
      }
    } catch (_) {
      // Body was not JSON; use statusText
    }
    const err = new Error(detail);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }

  if (res.status === 204) {
    return null;
  }

  return await res.json();
}

// --- Metadata ---
export async function getMeta() {
  return request('/api/meta');
}

// --- Sessions ---
export async function listSessions() {
  return request('/api/sessions');
}

export async function createSession(payload) {
  return request('/api/sessions', {
    method: 'POST',
    body: payload,
  });
}

export async function getSession(sessionId) {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}`);
}

export async function updateSession(sessionId, payload) {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: payload,
  });
}

export async function deleteSession(sessionId) {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });
}

export async function startTurn(sessionId, payload) {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/turns`, {
    method: 'POST',
    body: payload,
  });
}

export async function stopSession(sessionId) {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/stop`, {
    method: 'POST',
  });
}

// --- Turns ---
export async function listTurns(limit) {
  const url = limit ? `/api/turns?limit=${encodeURIComponent(limit)}` : '/api/turns';
  return request(url);
}

export async function getTurn(turnId) {
  return request(`/api/turns/${encodeURIComponent(turnId)}`);
}

// --- Pipelines ---
export async function listPipelines() {
  return request('/api/pipelines');
}

export async function createPipeline(payload) {
  return request('/api/pipelines', {
    method: 'POST',
    body: payload,
  });
}

export async function updatePipeline(pipelineId, payload) {
  return request(`/api/pipelines/${encodeURIComponent(pipelineId)}`, {
    method: 'PUT',
    body: payload,
  });
}

export async function deletePipeline(pipelineId) {
  return request(`/api/pipelines/${encodeURIComponent(pipelineId)}`, {
    method: 'DELETE',
  });
}

export async function startPipelineRun(pipelineId, payload) {
  return request(`/api/pipelines/${encodeURIComponent(pipelineId)}/runs`, {
    method: 'POST',
    body: payload,
  });
}

// --- Pipeline Runs ---
export async function listPipelineRuns() {
  return request('/api/pipeline-runs');
}

export async function getPipelineRun(runId) {
  return request(`/api/pipeline-runs/${encodeURIComponent(runId)}`);
}

export async function approvePipelineRun(runId, payload = {}) {
  return request(`/api/pipeline-runs/${encodeURIComponent(runId)}/approve`, {
    method: 'POST',
    body: payload,
  });
}

export async function stopPipelineRun(runId) {
  return request(`/api/pipeline-runs/${encodeURIComponent(runId)}/stop`, {
    method: 'POST',
  });
}

// Grouped object export
export const api = {
  getMeta,
  listSessions,
  createSession,
  getSession,
  updateSession,
  deleteSession,
  startTurn,
  stopSession,
  listTurns,
  getTurn,
  listPipelines,
  createPipeline,
  updatePipeline,
  deletePipeline,
  startPipelineRun,
  listPipelineRuns,
  getPipelineRun,
  approvePipelineRun,
  stopPipelineRun,
};

export default api;
