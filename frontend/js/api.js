/**
 * Baton — Frontend API Client
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
        // FastAPI request-validation errors return a list of {loc, msg} objects
        detail = Array.isArray(data.detail)
          ? data.detail
              .map((d) => {
                const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== 'body').join('.') : '';
                return loc ? `${loc}: ${d.msg}` : d.msg || JSON.stringify(d);
              })
              .join('; ')
          : String(data.detail);
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

export async function forwardTurn(turnId, payload) {
  return request(`/api/turns/${encodeURIComponent(turnId)}/forward`, {
    method: 'POST',
    body: payload,
  });
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
  let body = {};
  if (typeof payload === 'string') {
    body = { task_prompt: payload };
  } else if (payload && typeof payload === 'object') {
    body = {
      task_prompt: payload.task_prompt != null ? payload.task_prompt : '',
    };
    if (payload.start_stage_index !== undefined && payload.start_stage_index !== null) {
      body.start_stage_index = Number(payload.start_stage_index);
    }
    if (payload.initial_input !== undefined && payload.initial_input !== null) {
      body.initial_input = payload.initial_input;
    }
    if (payload.initial_input_turn_id !== undefined && payload.initial_input_turn_id !== null) {
      body.initial_input_turn_id = payload.initial_input_turn_id;
    }
    for (const [k, v] of Object.entries(payload)) {
      if (!(k in body) && v !== undefined) {
        body[k] = v;
      }
    }
  }

  return request(`/api/pipelines/${encodeURIComponent(pipelineId)}/runs`, {
    method: 'POST',
    body,
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

// --- Teams & Team Runs ---
export async function listTeams() {
  return request('/api/teams');
}

export async function createTeam(payload) {
  return request('/api/teams', {
    method: 'POST',
    body: payload,
  });
}

export async function updateTeam(teamId, payload) {
  return request(`/api/teams/${encodeURIComponent(teamId)}`, {
    method: 'PUT',
    body: payload,
  });
}

export async function deleteTeam(teamId) {
  return request(`/api/teams/${encodeURIComponent(teamId)}`, {
    method: 'DELETE',
  });
}

export async function startTeamRun(teamIdOrPayload, maybePayload) {
  let teamId = null;
  let payload = {};
  if (typeof teamIdOrPayload === 'string') {
    teamId = teamIdOrPayload;
    payload = maybePayload || {};
  } else if (teamIdOrPayload && typeof teamIdOrPayload === 'object') {
    payload = { ...teamIdOrPayload };
    if (payload.team_id) {
      teamId = payload.team_id;
    }
  }
  const url = teamId
    ? `/api/teams/${encodeURIComponent(teamId)}/runs`
    : '/api/team-runs';
  return request(url, {
    method: 'POST',
    body: payload,
  });
}

export async function listTeamRuns() {
  return request('/api/team-runs');
}

export async function getTeamRun(runId) {
  return request(`/api/team-runs/${encodeURIComponent(runId)}`);
}

export async function answerTeamQuestions(runId, answersOrPayload) {
  const body = Array.isArray(answersOrPayload)
    ? { answers: answersOrPayload }
    : (answersOrPayload || { answers: [] });
  return request(`/api/team-runs/${encodeURIComponent(runId)}/answer`, {
    method: 'POST',
    body,
  });
}

export async function reviewTeamMockup(runId, payload) {
  return request(`/api/team-runs/${encodeURIComponent(runId)}/mockup-review`, {
    method: 'POST',
    body: payload,
  });
}

export async function getTeamRunDiff(runId) {
  return request(`/api/team-runs/${encodeURIComponent(runId)}/diff`);
}

export async function resumeTeamRun(runId, payload = {}) {
  return request(`/api/team-runs/${encodeURIComponent(runId)}/resume`, {
    method: 'POST',
    body: payload,
  });
}

export async function signoffTeamRun(runId, payload) {
  return request(`/api/team-runs/${encodeURIComponent(runId)}/signoff`, {
    method: 'POST',
    body: payload,
  });
}

export async function stopTeamRun(runId) {
  return request(`/api/team-runs/${encodeURIComponent(runId)}/stop`, {
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
  forwardTurn,
  listPipelines,
  createPipeline,
  updatePipeline,
  deletePipeline,
  startPipelineRun,
  listPipelineRuns,
  getPipelineRun,
  approvePipelineRun,
  stopPipelineRun,
  listTeams,
  createTeam,
  updateTeam,
  deleteTeam,
  startTeamRun,
  listTeamRuns,
  getTeamRun,
  answerTeamQuestions,
  reviewTeamMockup,
  getTeamRunDiff,
  resumeTeamRun,
  signoffTeamRun,
  stopTeamRun,
};

export default api;
