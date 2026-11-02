/**
 * Agent Relay — Pipeline Builder, Stage Editor & Pipeline Launcher
 */

import { api } from './api.js';
import {
  escapeHtml,
  renderProviderBadge,
  renderRoleBadge,
  showToast,
} from './components.js';

let appStore = null;
let appCallbacks = {};
let isInitialized = false;

let loadedPipelineId = null;
let isDirty = false;

/**
 * In-memory working copy of stages currently being edited in the builder.
 * Array of { name: string, session_id: string, require_approval: boolean }
 */
let editingStages = [];

/**
 * Formats a role ID into a display label.
 * @param {string} roleId
 * @returns {string}
 */
function formatRoleName(roleId) {
  const map = {
    designer: 'Designer',
    planner: 'Planner',
    implementer: 'Implementer',
    reviewer: 'Code Reviewer',
    custom: 'Custom',
  };
  return map[(roleId || '').toLowerCase()] || roleId || 'Role';
}

/**
 * Switches subtabs on compact laptop/mobile view (<= 1024px).
 * @param {"builder"|"run"} tab
 */
export function showSubtab(tab) {
  const btnBuilder = document.getElementById('pipe-subtab-builder');
  const btnRun = document.getElementById('pipe-subtab-run');
  const panelBuilder = document.getElementById('pipeline-builder-panel');
  const panelRun = document.getElementById('pipeline-active-run-panel');

  if (tab === 'builder') {
    if (btnBuilder) btnBuilder.classList.add('active');
    if (btnRun) btnRun.classList.remove('active');
    if (window.innerWidth <= 1024) {
      if (panelBuilder) panelBuilder.style.display = 'flex';
      if (panelRun) panelRun.style.display = 'none';
    }
  } else {
    if (btnRun) btnRun.classList.add('active');
    if (btnBuilder) btnBuilder.classList.remove('active');
    if (window.innerWidth <= 1024) {
      if (panelBuilder) panelBuilder.style.display = 'none';
      if (panelRun) panelRun.style.display = 'flex';
    }
  }
}

/**
 * Renders the ordered stage rows in #pipeline-stages-list.
 */
function renderStagesList() {
  const container = document.getElementById('pipeline-stages-list');
  if (!container || !appStore) return;

  const sessions = appStore.sessions || [];

  if (!editingStages || editingStages.length === 0) {
    container.innerHTML = `
      <div class="sessions-empty-state" style="padding: 16px;">
        <p>No stages added yet. Click <strong>+ Add Stage</strong> or <strong>Auto-Fill 4-Stage Pipeline</strong> to configure this workflow.</p>
      </div>
    `;
    return;
  }

  const stagesHtml = editingStages
    .map((stage, idx) => {
      const isFirst = idx === 0;
      const isLast = idx === editingStages.length - 1;
      const selectedSession = sessions.find((s) => s.id === stage.session_id) || null;
      const isReviewer = selectedSession && selectedSession.role === 'reviewer';

      let optionsHtml = '';
      if (sessions.length === 0) {
        optionsHtml = '<option value="">No sessions available (create a session first)</option>';
      } else {
        optionsHtml = sessions
          .map((session) => {
            const isSel = session.id === stage.session_id;
            const provUpper = (session.provider || '').toUpperCase();
            const roleLabel = formatRoleName(session.role);
            const label = `${session.name} [${provUpper} · ${roleLabel} · ${session.model}]`;
            return `<option value="${escapeHtml(session.id)}"${isSel ? ' selected' : ''}>${escapeHtml(label)}</option>`;
          })
          .join('');
      }

      return `
        <div class="stage-editor-card" data-stage-index="${idx}">
          <div class="stage-card-header">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span class="badge-mono" style="font-weight: 700;">#${idx + 1}</span>
              <span style="font-size: 12px; font-weight: 600; color: var(--text-secondary);">Stage ${idx + 1}</span>
            </div>
            <div class="stage-card-controls">
              <button
                type="button"
                class="btn-stage-ctrl btn-move-up"
                data-index="${idx}"
                title="Move Up"
                ${isFirst ? 'disabled' : ''}
              >↑</button>
              <button
                type="button"
                class="btn-stage-ctrl btn-move-down"
                data-index="${idx}"
                title="Move Down"
                ${isLast ? 'disabled' : ''}
              >↓</button>
              <button
                type="button"
                class="btn-stage-ctrl btn-remove-stage"
                data-index="${idx}"
                title="Remove Stage"
              >×</button>
            </div>
          </div>

          <div class="form-group">
            <label class="form-label">Stage Name</label>
            <input
              type="text"
              class="input-text stage-name-input"
              data-index="${idx}"
              value="${escapeHtml(stage.name)}"
              placeholder="e.g., Architecture Design"
              required
            >
          </div>

          <div class="form-group">
            <label class="form-label">Bound Session</label>
            <select class="select-input stage-session-select" data-index="${idx}">
              ${optionsHtml}
            </select>
            <div class="stage-session-badges" style="display: flex; align-items: center; gap: 6px; margin-top: 4px; flex-wrap: wrap;">
              ${selectedSession ? renderProviderBadge(selectedSession.provider) : ''}
              ${selectedSession ? renderRoleBadge(selectedSession.role) : ''}
              ${
                isReviewer
                  ? '<span class="badge-role badge-role-reviewer" style="font-size: 10.5px;">🔍 Emits APPROVED / CHANGES_REQUESTED verdict</span>'
                  : ''
              }
            </div>
          </div>

          <label class="stage-approval-toggle">
            <input
              type="checkbox"
              class="stage-approval-checkbox"
              data-index="${idx}"
              ${stage.require_approval ? 'checked' : ''}
            >
            <span>Pause for my approval before passing on</span>
          </label>
        </div>
      `;
    })
    .join('');

  container.innerHTML = stagesHtml;
}

/**
 * Synchronizes the pipeline builder form inputs and stages list with state.
 */
export function renderPipelineBuilder() {
  if (!appStore) return;

  const selectEl = document.getElementById('pipeline-select');
  const nameInput = document.getElementById('pipeline-name-input');
  const descInput = document.getElementById('pipeline-desc-input');
  const maxLoopsInput = document.getElementById('pipeline-max-loops-input');
  const deleteBtn = document.getElementById('btn-delete-pipeline');

  const pipelines = appStore.pipelines || [];

  // Update pipeline dropdown
  if (selectEl) {
    const currentVal = appStore.activePipelineId || '';
    const optionsHtml = [
      '<option value="">-- New Pipeline --</option>',
      ...pipelines.map((p) => {
        const isSel = p.id === currentVal;
        return `<option value="${escapeHtml(p.id)}"${isSel ? ' selected' : ''}>${escapeHtml(p.name)}</option>`;
      }),
    ].join('');
    selectEl.innerHTML = optionsHtml;
  }

  // Update delete button visibility
  if (deleteBtn) {
    deleteBtn.style.opacity = appStore.activePipelineId ? '1' : '0.4';
    deleteBtn.style.pointerEvents = appStore.activePipelineId ? 'auto' : 'none';
  }

  // Load pipeline data if activePipelineId matches an existing pipeline
  if (appStore.activePipelineId) {
    const pipe = pipelines.find((p) => p.id === appStore.activePipelineId);
    if (pipe) {
      if (loadedPipelineId !== appStore.activePipelineId || !isDirty) {
        if (nameInput) nameInput.value = pipe.name || '';
        if (descInput) descInput.value = pipe.description || '';
        if (maxLoopsInput) maxLoopsInput.value = pipe.max_review_loops ?? 2;
        editingStages = (pipe.stages || []).map((s) => ({
          name: s.name,
          session_id: s.session_id,
          require_approval: Boolean(s.require_approval),
        }));
        loadedPipelineId = appStore.activePipelineId;
        isDirty = false;
      }
    }
  } else if (!isDirty && (!editingStages || editingStages.length === 0)) {
    // Brand new pipeline defaults
    if (nameInput && !nameInput.value.trim()) nameInput.value = '';
    if (descInput && !descInput.value.trim()) descInput.value = '';
    if (maxLoopsInput && !maxLoopsInput.value.trim()) maxLoopsInput.value = '2';
    loadedPipelineId = null;
  }

  renderStagesList();
}

/**
 * Automatically creates a 4-stage pipeline template bound to existing role sessions.
 */
function handleAutoFillPipeline() {
  if (!appStore) return;
  const sessions = appStore.sessions || [];

  if (sessions.length === 0) {
    showToast('Create sessions first before configuring a pipeline', 'warning');
    return;
  }

  const findSessionByRole = (role) => sessions.find((s) => s.role === role) || sessions[0];

  const designer = findSessionByRole('designer');
  const planner = findSessionByRole('planner');
  const implementer = findSessionByRole('implementer');
  const reviewer = findSessionByRole('reviewer');

  editingStages = [
    {
      name: 'Architecture Design',
      session_id: designer ? designer.id : sessions[0].id,
      require_approval: false,
    },
    {
      name: 'Implementation Plan',
      session_id: planner ? planner.id : sessions[0].id,
      require_approval: true,
    },
    {
      name: 'Implementation',
      session_id: implementer ? implementer.id : sessions[0].id,
      require_approval: false,
    },
    {
      name: 'Code Review',
      session_id: reviewer ? reviewer.id : sessions[0].id,
      require_approval: false,
    },
  ];

  const nameInput = document.getElementById('pipeline-name-input');
  if (nameInput && !nameInput.value.trim()) {
    nameInput.value = 'Design → Plan → Implement → Review';
  }

  const descInput = document.getElementById('pipeline-desc-input');
  if (descInput && !descInput.value.trim()) {
    descInput.value = '4-stage feature workflow with automated review and rework loop';
  }

  isDirty = true;
  renderStagesList();
  showToast('Auto-filled 4-stage pipeline template', 'info');
}

/**
 * Saves current pipeline configuration (POST /api/pipelines or PUT /api/pipelines/{id}).
 * @returns {Promise<object|null>}
 */
async function handleSavePipeline() {
  if (!appStore) return null;

  const nameInput = document.getElementById('pipeline-name-input');
  const descInput = document.getElementById('pipeline-desc-input');
  const maxLoopsInput = document.getElementById('pipeline-max-loops-input');

  const name = nameInput ? nameInput.value.trim() : '';
  if (!name) {
    showToast('Pipeline name is required', 'error');
    if (nameInput) nameInput.focus();
    return null;
  }

  const description = descInput ? descInput.value.trim() : '';
  let maxReviewLoops = maxLoopsInput ? parseInt(maxLoopsInput.value, 10) : 2;
  if (isNaN(maxReviewLoops) || maxReviewLoops < 0) maxReviewLoops = 0;
  if (maxReviewLoops > 5) maxReviewLoops = 5;

  if (!editingStages || editingStages.length === 0) {
    showToast('Pipeline must have at least one stage', 'error');
    return null;
  }

  for (let i = 0; i < editingStages.length; i++) {
    const stage = editingStages[i];
    if (!stage.name || !stage.name.trim()) {
      showToast(`Stage #${i + 1} must have a name`, 'error');
      return null;
    }
    if (!stage.session_id) {
      showToast(`Stage #${i + 1} must be bound to a session`, 'error');
      return null;
    }
  }

  const payload = {
    name,
    description,
    max_review_loops: maxReviewLoops,
    stages: editingStages.map((s) => ({
      name: s.name.trim(),
      session_id: s.session_id,
      require_approval: Boolean(s.require_approval),
    })),
  };

  const saveBtn = document.getElementById('btn-save-pipeline');
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
  }

  try {
    let savedPipeline;
    if (appStore.activePipelineId) {
      savedPipeline = await api.updatePipeline(appStore.activePipelineId, payload);
      showToast('Pipeline updated successfully', 'success');
    } else {
      savedPipeline = await api.createPipeline(payload);
      appStore.activePipelineId = savedPipeline.id;
      showToast('Pipeline created successfully', 'success');
    }

    loadedPipelineId = appStore.activePipelineId;
    isDirty = false;

    if (appCallbacks.refreshPipelines) {
      await appCallbacks.refreshPipelines();
    } else {
      renderPipelineBuilder();
    }

    return savedPipeline;
  } catch (err) {
    showToast(`Failed to save pipeline: ${err.message || err}`, 'error');
    return null;
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save Pipeline';
    }
  }
}

/**
 * Launches a new pipeline run with the user's task prompt.
 */
async function handleRunPipeline() {
  if (!appStore) return;

  const taskInput = document.getElementById('pipeline-task-input');
  const taskPrompt = taskInput ? taskInput.value.trim() : '';

  if (!taskPrompt) {
    showToast('Please enter a task prompt for Stage 1', 'warning');
    if (taskInput) taskInput.focus();
    return;
  }

  // If no pipeline is selected yet or unsaved edits exist, save definition first
  let pipelineId = appStore.activePipelineId;
  if (!pipelineId || isDirty) {
    const saved = await handleSavePipeline();
    if (!saved) return;
    pipelineId = saved.id;
  }

  const runBtn = document.getElementById('btn-run-pipeline');
  if (runBtn) {
    runBtn.disabled = true;
    runBtn.textContent = 'Launching...';
  }

  try {
    const res = await api.startPipelineRun(pipelineId, { task_prompt: taskPrompt });
    if (taskInput) taskInput.value = '';

    if (res && res.run) {
      appStore.activeRunId = res.run.id;
      const existingRuns = (
        appStore.activeRunDetail &&
        appStore.activeRunDetail.run &&
        appStore.activeRunDetail.run.id === res.run.id &&
        Array.isArray(appStore.activeRunDetail.stage_runs)
      ) ? appStore.activeRunDetail.stage_runs : [];

      if (existingRuns.length > 0 && (!res.stage_runs || res.stage_runs.length === 0)) {
        appStore.activeRunDetail = { ...res, stage_runs: existingRuns };
      } else if (existingRuns.length > 0 && Array.isArray(res.stage_runs)) {
        const merged = res.stage_runs.map((rStage) => {
          const prev = existingRuns.find((e) => e.id === rStage.id);
          if (!prev) return rStage;
          return {
            ...rStage,
            turn_id: rStage.turn_id || prev.turn_id || null,
            output_text: rStage.output_text || prev.output_text || '',
            findings: rStage.findings || prev.findings || null,
          };
        });
        for (const prev of existingRuns) {
          if (!merged.some((m) => m.id === prev.id)) {
            merged.push(prev);
          }
        }
        appStore.activeRunDetail = { ...res, stage_runs: merged };
      } else {
        appStore.activeRunDetail = res;
      }
    }

    showToast('Pipeline run started', 'success');

    // Focus Panel 2 and switch sub-tab on compact screens
    showSubtab('run');

    if (appCallbacks.renderActiveRunPanel) {
      appCallbacks.renderActiveRunPanel();
    }
    if (appCallbacks.refreshRuns) {
      appCallbacks.refreshRuns();
    }
  } catch (err) {
    showToast(`Failed to launch pipeline: ${err.message || err}`, 'error');
  } finally {
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.textContent = '▶ Run Pipeline';
    }
  }
}

/**
 * Deletes the currently selected pipeline definition.
 */
async function handleDeletePipeline() {
  if (!appStore || !appStore.activePipelineId) {
    showToast('No pipeline selected to delete', 'warning');
    return;
  }

  const found = (appStore.pipelines || []).find((p) => p.id === appStore.activePipelineId);
  const pipeName = found ? found.name : 'this pipeline';

  if (!confirm(`Are you sure you want to delete "${pipeName}"?`)) {
    return;
  }

  try {
    await api.deletePipeline(appStore.activePipelineId);
    showToast('Pipeline deleted', 'info');
    appStore.activePipelineId = null;
    loadedPipelineId = null;
    isDirty = false;
    editingStages = [];

    const nameInput = document.getElementById('pipeline-name-input');
    const descInput = document.getElementById('pipeline-desc-input');
    const maxLoopsInput = document.getElementById('pipeline-max-loops-input');
    if (nameInput) nameInput.value = '';
    if (descInput) descInput.value = '';
    if (maxLoopsInput) maxLoopsInput.value = '2';

    if (appCallbacks.refreshPipelines) {
      await appCallbacks.refreshPipelines();
    } else {
      renderPipelineBuilder();
    }
  } catch (err) {
    showToast(`Failed to delete pipeline: ${err.message || err}`, 'error');
  }
}

/**
 * Initializes the Pipelines view and binds all event listeners.
 * @param {object} store
 * @param {object} callbacks
 */
export function initPipelinesView(store, callbacks) {
  appStore = store;
  appCallbacks = callbacks || {};

  if (isInitialized) {
    return;
  }
  isInitialized = true;

  // Pipeline definition input change listeners to track dirty state
  const nameInput = document.getElementById('pipeline-name-input');
  if (nameInput) {
    nameInput.addEventListener('input', () => {
      isDirty = true;
    });
  }

  const descInput = document.getElementById('pipeline-desc-input');
  if (descInput) {
    descInput.addEventListener('input', () => {
      isDirty = true;
    });
  }

  const maxLoopsInput = document.getElementById('pipeline-max-loops-input');
  if (maxLoopsInput) {
    maxLoopsInput.addEventListener('input', () => {
      isDirty = true;
    });
    maxLoopsInput.addEventListener('change', () => {
      isDirty = true;
    });
  }

  // Saved pipelines dropdown
  const selectEl = document.getElementById('pipeline-select');
  if (selectEl) {
    selectEl.addEventListener('change', () => {
      const selectedId = selectEl.value;
      if (!selectedId) {
        appStore.activePipelineId = null;
        loadedPipelineId = null;
        isDirty = false;
        editingStages = [];
        const nameInput = document.getElementById('pipeline-name-input');
        const descInput = document.getElementById('pipeline-desc-input');
        const maxLoopsInput = document.getElementById('pipeline-max-loops-input');
        if (nameInput) nameInput.value = '';
        if (descInput) descInput.value = '';
        if (maxLoopsInput) maxLoopsInput.value = '2';
        renderStagesList();
      } else {
        appStore.activePipelineId = selectedId;
        loadedPipelineId = selectedId;
        isDirty = false;
        const found = (appStore.pipelines || []).find((p) => p.id === selectedId);
        if (found) {
          const nameInput = document.getElementById('pipeline-name-input');
          const descInput = document.getElementById('pipeline-desc-input');
          const maxLoopsInput = document.getElementById('pipeline-max-loops-input');
          if (nameInput) nameInput.value = found.name || '';
          if (descInput) descInput.value = found.description || '';
          if (maxLoopsInput) maxLoopsInput.value = found.max_review_loops ?? 2;
          editingStages = (found.stages || []).map((s) => ({
            name: s.name,
            session_id: s.session_id,
            require_approval: Boolean(s.require_approval),
          }));
          renderStagesList();
        }
      }
    });
  }

  // + New Pipeline button
  const btnNew = document.getElementById('btn-new-pipeline');
  if (btnNew) {
    btnNew.addEventListener('click', () => {
      appStore.activePipelineId = null;
      loadedPipelineId = null;
      isDirty = false;
      editingStages = [];
      if (selectEl) selectEl.value = '';
      const nameInput = document.getElementById('pipeline-name-input');
      const descInput = document.getElementById('pipeline-desc-input');
      const maxLoopsInput = document.getElementById('pipeline-max-loops-input');
      if (nameInput) {
        nameInput.value = '';
        nameInput.focus();
      }
      if (descInput) descInput.value = '';
      if (maxLoopsInput) maxLoopsInput.value = '2';
      renderStagesList();
    });
  }

  // Delete Pipeline button
  const btnDelete = document.getElementById('btn-delete-pipeline');
  if (btnDelete) {
    btnDelete.addEventListener('click', handleDeletePipeline);
  }

  // + Add Stage button
  const btnAddStage = document.getElementById('btn-add-stage');
  if (btnAddStage) {
    btnAddStage.addEventListener('click', () => {
      isDirty = true;
      const sessions = appStore.sessions || [];
      const defaultSessionId = sessions.length > 0 ? sessions[0].id : '';
      editingStages.push({
        name: `Stage ${editingStages.length + 1}`,
        session_id: defaultSessionId,
        require_approval: false,
      });
      renderStagesList();
    });
  }

  // Auto-Fill 4-Stage Pipeline button
  const btnAutoFill = document.getElementById('btn-autofill-pipeline');
  if (btnAutoFill) {
    btnAutoFill.addEventListener('click', handleAutoFillPipeline);
  }

  // Save Pipeline button
  const btnSave = document.getElementById('btn-save-pipeline');
  if (btnSave) {
    btnSave.addEventListener('click', handleSavePipeline);
  }

  // Run Pipeline button
  const btnRun = document.getElementById('btn-run-pipeline');
  if (btnRun) {
    btnRun.addEventListener('click', handleRunPipeline);
  }

  // Compact layout sub-tab buttons
  const subtabBuilder = document.getElementById('pipe-subtab-builder');
  const subtabRun = document.getElementById('pipe-subtab-run');
  if (subtabBuilder) {
    subtabBuilder.addEventListener('click', () => showSubtab('builder'));
  }
  if (subtabRun) {
    subtabRun.addEventListener('click', () => showSubtab('run'));
  }

  // Window resize listener to restore display styles on desktop layout (> 1024px)
  window.addEventListener('resize', () => {
    const panelBuilder = document.getElementById('pipeline-builder-panel');
    const panelRun = document.getElementById('pipeline-active-run-panel');
    if (window.innerWidth > 1024) {
      if (panelBuilder) panelBuilder.style.display = '';
      if (panelRun) panelRun.style.display = '';
    } else {
      const btnR = document.getElementById('pipe-subtab-run');
      const isRunActive = btnR && btnR.classList.contains('active');
      showSubtab(isRunActive ? 'run' : 'builder');
    }
  });

  // Delegated event listeners for ordered stages list
  const stagesList = document.getElementById('pipeline-stages-list');
  if (stagesList) {
    stagesList.addEventListener('input', (e) => {
      if (e.target.classList.contains('stage-name-input')) {
        const idx = Number(e.target.dataset.index);
        if (editingStages[idx]) {
          editingStages[idx].name = e.target.value;
          isDirty = true;
        }
      }
    });

    stagesList.addEventListener('change', (e) => {
      if (e.target.classList.contains('stage-session-select')) {
        const idx = Number(e.target.dataset.index);
        if (editingStages[idx]) {
          editingStages[idx].session_id = e.target.value;
          isDirty = true;
          renderStagesList();
        }
      } else if (e.target.classList.contains('stage-approval-checkbox')) {
        const idx = Number(e.target.dataset.index);
        if (editingStages[idx]) {
          editingStages[idx].require_approval = e.target.checked;
          isDirty = true;
        }
      }
    });

    stagesList.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;

      const idx = Number(btn.dataset.index);
      if (isNaN(idx)) return;

      if (btn.classList.contains('btn-move-up') && idx > 0) {
        const temp = editingStages[idx];
        editingStages[idx] = editingStages[idx - 1];
        editingStages[idx - 1] = temp;
        isDirty = true;
        renderStagesList();
      } else if (btn.classList.contains('btn-move-down') && idx < editingStages.length - 1) {
        const temp = editingStages[idx];
        editingStages[idx] = editingStages[idx + 1];
        editingStages[idx + 1] = temp;
        isDirty = true;
        renderStagesList();
      } else if (btn.classList.contains('btn-remove-stage')) {
        editingStages.splice(idx, 1);
        isDirty = true;
        renderStagesList();
      }
    });
  }
}

/**
 * Handles incoming SSE events relevant to the Pipeline Builder.
 * @param {string} eventName
 * @param {object} data
 */
export function handlePipelineSseEvent(eventName, data) {
  if (eventName === 'session_updated') {
    // Keep session options up to date in stage selector rows
    renderStagesList();
  }
}

export default {
  initPipelinesView,
  renderPipelineBuilder,
  handlePipelineSseEvent,
  showSubtab,
};
