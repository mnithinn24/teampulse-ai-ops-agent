/**
 * TeamPulse AI Project Planner — Frontend Application Logic
 * Dynamic capacity calculation, template loaders, API interaction, and hierarchy visualizer.
 */

(function () {
  'use strict';

  // Application State
  const state = {
    apiKey: localStorage.getItem('teampulse_admin_key') || '',
    jiraProjectKey: 'KAN',
    jiraBaseUrl: '',
    dryRunDefault: true,
    currentPlan: null,
  };

  // Quick Starter Templates
  const TEMPLATES = {
    rag: {
      title: 'Medical RAG System',
      description: 'Build an enterprise Medical RAG system where doctors can upload patient EHR PDF documents, index into vector embeddings database, and ask clinical questions with cited answers using a modern React frontend and FastAPI backend.',
      developers: 2,
      weeks: 2.0,
      projectKey: 'KAN',
    },
    auth: {
      title: 'OAuth2 & Auth0 Migration',
      description: 'Migrate legacy session auth to OAuth2 JWT tokens with Auth0 SSO integration, role-based access control (RBAC), token rotation, and security audit logging.',
      developers: 2,
      weeks: 1.5,
      projectKey: 'KAN',
    },
    billing: {
      title: 'Stripe Billing & Webhooks',
      description: 'Implement automated subscription microservice with Stripe Checkout, webhook signature validation, customer billing portal, and PDF invoice generation.',
      developers: 3,
      weeks: 2.0,
      projectKey: 'KAN',
    },
    analytics: {
      title: 'Engineering Ops Dashboard',
      description: 'Create real-time engineering velocity dashboard with WebSocket event stream, PR cycle-time heatmaps, Jira sprint burn-up charts, and Slack alert webhooks.',
      developers: 2,
      weeks: 3.0,
      projectKey: 'KAN',
    },
  };

  // DOM Elements
  const descInput = document.getElementById('project-description');
  const devsSlider = document.getElementById('devs-slider');
  const devsValue = document.getElementById('devs-value');
  const weeksSlider = document.getElementById('weeks-slider');
  const weeksValue = document.getElementById('weeks-value');
  const jiraKeyInput = document.getElementById('jira-project-key');
  const dryRunCheckbox = document.getElementById('dry-run-checkbox');
  const btnGenerate = document.getElementById('btn-generate');

  const capacityHoursEl = document.getElementById('capacity-hours');
  const capacityFormulaEl = document.getElementById('capacity-formula');
  const capacityFillEl = document.getElementById('capacity-fill');

  const loadingBox = document.getElementById('loading-box');
  const loadingStatusText = document.getElementById('loading-status-text');

  const emptyState = document.getElementById('empty-state');
  const planResults = document.getElementById('plan-results');
  const planSummaryBar = document.getElementById('plan-summary-bar');
  const epicsContainer = document.getElementById('epics-container');

  // Modal Elements
  const modalOverlay = document.getElementById('api-modal');
  const btnOpenModal = document.getElementById('btn-open-settings');
  const btnCloseModal = document.getElementById('btn-close-modal');
  const apiKeyInput = document.getElementById('modal-api-key');
  const btnSaveApiKey = document.getElementById('btn-save-key');

  // Edit Ticket Modal Elements
  const editTicketModal = document.getElementById('edit-ticket-modal');
  const btnCloseEditModal = document.getElementById('btn-close-edit-modal');
  const btnCancelEdit = document.getElementById('btn-cancel-edit');
  const btnSaveEdit = document.getElementById('btn-save-edit');
  const btnSaveEditText = document.getElementById('btn-save-edit-text');
  const editTitleInput = document.getElementById('edit-title-input');
  const editHoursInput = document.getElementById('edit-hours-input');
  const editDescInput = document.getElementById('edit-desc-input');
  const editModalHeading = document.getElementById('edit-modal-title-heading');
  const editItemBadge = document.getElementById('edit-modal-item-badge');
  const editKeyBadge = document.getElementById('edit-modal-key-badge');
  const editSyncText = document.getElementById('edit-sync-text');

  let activeEditTarget = null;


  // 1. Initialize Configuration from Backend
  async function loadConfig() {
    try {
      const res = await fetch('/project/config');
      if (res.ok) {
        const cfg = await res.json();
        state.jiraProjectKey = cfg.jira_project_key || 'KAN';
        state.jiraBaseUrl = cfg.jira_base_url || '';
        state.dryRunDefault = cfg.dry_run_default !== undefined ? cfg.dry_run_default : true;

        if (!state.apiKey && cfg.admin_api_key) {
          state.apiKey = cfg.admin_api_key;
          localStorage.setItem('teampulse_admin_key', cfg.admin_api_key);
        }

        jiraKeyInput.value = state.jiraProjectKey;
        dryRunCheckbox.checked = state.dryRunDefault;
        apiKeyInput.value = state.apiKey;
      }
    } catch (err) {
      console.warn('Could not load /project/config:', err);
    }
  }

  // 2. Dynamic Capacity Calculation
  function updateCapacityDisplay() {
    const devs = parseInt(devsSlider.value, 10);
    const weeks = parseFloat(weeksSlider.value);
    devsValue.textContent = `${devs} Dev${devs > 1 ? 's' : ''}`;
    weeksValue.textContent = `${weeks} Wk${weeks > 1 ? 's' : ''}`;

    const totalHours = Math.round(devs * weeks * 40);
    const effectiveHours = Math.round(totalHours * 0.8);

    capacityHoursEl.textContent = `${totalHours} Hours`;
    capacityFormulaEl.textContent = `${devs} Devs × ${weeks} Wks × 40h/wk = ${totalHours}h capacity (~${effectiveHours}h coding)`;

    // Visual gauge
    const pct = Math.min(100, Math.max(10, (totalHours / 480) * 100));
    capacityFillEl.style.width = `${pct}%`;
  }

  // 3. Template Selection
  window.loadTemplate = function (key) {
    const tpl = TEMPLATES[key];
    if (!tpl) return;
    descInput.value = tpl.description;
    devsSlider.value = tpl.developers;
    weeksSlider.value = tpl.weeks;
    jiraKeyInput.value = state.jiraProjectKey || tpl.projectKey || 'KAN';
    updateCapacityDisplay();
  };

  // 4. Plan Generation Workflow
  async function handleGeneratePlan() {
    const description = descInput.value.trim();
    if (description.length < 5) {
      alert('Please enter a descriptive project overview (at least 5 characters).');
      descInput.focus();
      return;
    }

    const devs = parseInt(devsSlider.value, 10);
    const weeks = parseFloat(weeksSlider.value);
    const projectKey = jiraKeyInput.value.trim().toUpperCase() || 'KAN';
    const isDryRun = dryRunCheckbox.checked;

    // UI Loading state
    btnGenerate.disabled = true;
    loadingBox.style.display = 'block';
    emptyState.style.display = 'none';
    planResults.style.display = 'none';

    // Step-by-step progress animation
    const steps = [
      '1. Analyzing project requirements and architecture scope...',
      '2. Computing developer capacity & balancing hour budgets...',
      '3. Decomposing into Epics, Tasks, and granular Subtasks...',
      isDryRun ? '4. Provisioning preview keys (Dry Run Mode)...' : '4. Creating tickets and subtasks in Jira Cloud...',
    ];

    let stepIdx = 0;
    loadingStatusText.textContent = steps[0];
    const stepInterval = setInterval(() => {
      stepIdx = (stepIdx + 1) % steps.length;
      loadingStatusText.textContent = steps[stepIdx];
    }, 1200);

    try {
      const headers = { 'Content-Type': 'application/json' };
      if (state.apiKey) {
        headers['X-API-Key'] = state.apiKey;
      }

      const payload = {
        description: description,
        developers: devs,
        weeks: weeks,
        project_key: projectKey,
        dry_run: isDryRun,
      };

      const res = await fetch('/project/plan', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload),
      });

      clearInterval(stepInterval);

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Server responded with HTTP ${res.status}`);
      }

      const plan = await res.json();
      state.currentPlan = plan;
      renderPlanHierarchy(plan, isDryRun);

    } catch (err) {
      clearInterval(stepInterval);
      let errMsg = err.message || '';
      if (errMsg.includes("target project doesn't exist") || errMsg.includes("permission to create issues")) {
        alert(
          `Jira Project Key Not Found!\n\n` +
          `Project '${projectKey}' does not exist in your Jira workspace (${state.jiraBaseUrl || 'teampulse-ai-ops.atlassian.net'}).\n\n` +
          `How to Fix:\n` +
          `1. Change the 'Jira Project Key' box to '${state.jiraProjectKey || 'KAN'}' (your actual Jira project board).\n` +
          `2. OR toggle ON the '🧪 Dry Run' checkbox to preview all tickets safely without creating them in Jira.`
        );
      } else {
        alert(`Plan Generation Error:\n${errMsg}\n\nPlease check your settings if required.`);
      }
      emptyState.style.display = 'flex';
    } finally {
      btnGenerate.disabled = false;
      loadingBox.style.display = 'none';
    }
  }

  // 5. Render Hierarchy to DOM
  function renderPlanHierarchy(plan, isDryRun, openIndices = null) {
    epicsContainer.innerHTML = '';

    // Calculate metrics
    let taskCount = 0;
    let subtaskCount = 0;
    plan.epics.forEach(epic => {
      taskCount += epic.tasks ? epic.tasks.length : 0;
      epic.tasks.forEach(task => {
        subtaskCount += task.subtasks ? task.subtasks.length : 0;
      });
    });

    const isBalanced = Math.abs(plan.total_estimated_hours - plan.total_capacity_hours) <= 8;

    function formatModelBadge(engine) {
      if (!engine) return '⚡ Rule-Based';
      if (engine.includes('gemini')) {
        const m = engine.replace('gemini: ', '').replace('gemini_fallback: ', '');
        return `✨ Gemini (${m})`;
      }
      if (engine.includes('ollama')) {
        const m = engine.replace('ollama: ', '');
        return `🦙 Ollama (${m})`;
      }
      if (engine.includes('rule_based')) {
        return `⚡ Rule-Based Engine`;
      }
      return `🤖 ${engine}`;
    }

    // Render Summary Bar
    planSummaryBar.innerHTML = `
      <div class="plan-stats">
        <div class="stat-item">
          <span class="stat-label">Project</span>
          <span class="stat-val">${escapeHtml(plan.project_name)}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Est. Hours</span>
          <span class="stat-val ${isBalanced ? 'highlight' : ''}">${plan.total_estimated_hours}h / ${plan.total_capacity_hours}h</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Epics</span>
          <span class="stat-val">${plan.epics.length}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Tasks</span>
          <span class="stat-val">${taskCount}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Subtasks</span>
          <span class="stat-val">${subtaskCount}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Engine</span>
          <span class="stat-val" style="color: #38BDF8; font-size: 0.85rem; font-weight: 600;">
            ${formatModelBadge(plan.generated_by)}
          </span>
        </div>
      </div>
      <div class="plan-actions">
        <button class="btn-secondary" id="btn-copy-markdown" title="Copy markdown summary">
          📋 Copy Markdown
        </button>
        <button class="btn-secondary" id="btn-download-json" title="Download JSON plan">
          💾 Download JSON
        </button>
        ${isDryRun ? `
          <button class="btn-secondary" id="btn-provision-live" style="background: rgba(16, 185, 129, 0.15); border-color: rgba(16, 185, 129, 0.4); color: #34D399;">
            🚀 Create in Jira
          </button>
        ` : ''}
      </div>
    `;

    // Render Epics
    plan.epics.forEach((epic, epicIdx) => {
      const epicCard = document.createElement('div');
      const isOpen = openIndices ? openIndices.includes(epicIdx) : true;
      epicCard.className = `epic-card ${isOpen ? 'open' : ''}`;

      const jiraUrl = getJiraLink(epic.jira_key);
      const keyHtml = jiraUrl 
        ? `<a href="${jiraUrl}" target="_blank" class="epic-tag" style="text-decoration: underline;" title="Open in Jira">${epic.jira_key} ↗</a>`
        : `<span class="epic-tag">${epic.jira_key}</span>`;

      let tasksHtml = '';
      epic.tasks.forEach((task, taskIdx) => {
        const taskJiraUrl = getJiraLink(task.jira_key);
        const taskKeyHtml = taskJiraUrl
          ? `<a href="${taskJiraUrl}" target="_blank" class="task-key" style="text-decoration: underline;">${task.jira_key} ↗</a>`
          : `<span class="task-key">${task.jira_key}</span>`;

        let subtasksHtml = '';
        task.subtasks.forEach((sub, subIdx) => {
          subtasksHtml += `
            <div class="subtask-row">
              <div class="subtask-left">
                <span class="subtask-dot"></span>
                <div class="subtask-info">
                  <span class="subtask-title">${escapeHtml(sub.title)}</span>
                  <div class="subtask-desc">${escapeHtml(sub.description)}</div>
                </div>
              </div>
              <div class="subtask-right">
                <span class="subtask-hours">${sub.estimated_hours}h</span>
                <button type="button" class="btn-edit-subtask" title="Edit subtask details & hours" data-type="subtask" data-epic-idx="${epicIdx}" data-task-idx="${taskIdx}" data-sub-idx="${subIdx}">✏️</button>
              </div>
            </div>
          `;
        });

        tasksHtml += `
          <div class="task-item">
            <div class="task-header">
              <span class="task-title">${taskKeyHtml} ${escapeHtml(task.title)}</span>
              <div class="task-header-right">
                <span class="task-hours">${task.estimated_hours} Hours</span>
                <button type="button" class="btn-edit-item" title="Edit task details & hours" data-type="task" data-epic-idx="${epicIdx}" data-task-idx="${taskIdx}">✏️ Edit</button>
              </div>
            </div>
            <div class="task-description">${escapeHtml(task.description)}</div>
            <div class="subtask-grid">
              ${subtasksHtml}
            </div>
          </div>
        `;
      });

      epicCard.innerHTML = `
        <div class="epic-header">
          <div class="epic-title-wrap">
            ${keyHtml}
            <span class="epic-title">[Epic] ${escapeHtml(epic.title)}</span>
          </div>
          <div style="display: flex; align-items: center; gap: 10px;">
            <span class="epic-hours">${epic.estimated_hours}h</span>
            <button type="button" class="btn-edit-item" title="Edit epic details & hours" data-type="epic" data-epic-idx="${epicIdx}">✏️ Edit</button>
            <span class="chevron-icon">▼</span>
          </div>
        </div>
        <div class="epic-body">
          <div class="epic-description">${escapeHtml(epic.description)}</div>
          <div class="task-list">
            ${tasksHtml}
          </div>
        </div>
      `;

      // Accordion toggle
      const headerEl = epicCard.querySelector('.epic-header');
      headerEl.addEventListener('click', (e) => {
        if (e.target.tagName.toLowerCase() === 'a' || e.target.closest('.btn-edit-item, .btn-edit-subtask')) return;
        epicCard.classList.toggle('open');
      });

      epicsContainer.appendChild(epicCard);
    });

    // Wire Summary Buttons
    document.getElementById('btn-copy-markdown').addEventListener('click', () => copyMarkdownPlan(plan));
    document.getElementById('btn-download-json').addEventListener('click', () => downloadJsonPlan(plan));

    const btnLive = document.getElementById('btn-provision-live');
    if (btnLive) {
      btnLive.addEventListener('click', () => provisionToJiraLive(plan));
    }

    planResults.style.display = 'block';
  }

  // Event Delegation for Edit Buttons
  epicsContainer.addEventListener('click', (e) => {
    const editBtn = e.target.closest('.btn-edit-item, .btn-edit-subtask');
    if (!editBtn) return;
    e.stopPropagation();

    const type = editBtn.dataset.type;
    const epicIdx = parseInt(editBtn.dataset.epicIdx, 10);
    const taskIdx = editBtn.dataset.taskIdx !== undefined ? parseInt(editBtn.dataset.taskIdx, 10) : undefined;
    const subIdx = editBtn.dataset.subIdx !== undefined ? parseInt(editBtn.dataset.subIdx, 10) : undefined;

    openEditModal(type, epicIdx, taskIdx, subIdx);
  });

  // 6. Work Item Editing (Tasks, Subtasks, Epics)
  function openEditModal(type, epicIdx, taskIdx, subIdx) {
    const plan = state.currentPlan;
    if (!plan) return;

    let item = null;
    let fallbackKey = null;

    if (type === 'epic') {
      item = plan.epics[epicIdx];
    } else if (type === 'task') {
      item = plan.epics[epicIdx].tasks[taskIdx];
      fallbackKey = plan.epics[epicIdx].jira_key;
    } else if (type === 'subtask') {
      item = plan.epics[epicIdx].tasks[taskIdx].subtasks[subIdx];
      fallbackKey = plan.epics[epicIdx].tasks[taskIdx].jira_key;
    }

    if (!item) return;

    activeEditTarget = { type, epicIdx, taskIdx, subIdx, item };

    const typeLabel = type.charAt(0).toUpperCase() + type.slice(1);
    editModalHeading.textContent = `Edit ${typeLabel}`;
    editItemBadge.textContent = typeLabel;

    const jiraKey = item.jira_key || fallbackKey || 'Preview Item';
    editKeyBadge.textContent = jiraKey;

    editTitleInput.value = item.title || '';
    editHoursInput.value = item.estimated_hours || 1;
    editDescInput.value = item.description || '';

    const isLive = item.jira_key && !item.jira_key.startsWith('DRY-');
    if (isLive) {
      editSyncText.textContent = `Live Jira issue ${item.jira_key} will be synchronized automatically.`;
    } else {
      editSyncText.textContent = `Changes will update the plan and balance capacity & hours.`;
    }

    editTicketModal.classList.add('active');
    setTimeout(() => editTitleInput.focus(), 60);
  }

  function closeEditModal() {
    editTicketModal.classList.remove('active');
    activeEditTarget = null;
    btnSaveEdit.disabled = false;
    btnSaveEditText.textContent = '💾 Save & Sync';
  }

  async function saveEditItem() {
    if (!activeEditTarget || !state.currentPlan) return;

    const newTitle = editTitleInput.value.trim();
    const newHours = parseInt(editHoursInput.value, 10);
    const newDesc = editDescInput.value.trim();

    if (!newTitle) {
      alert('Please enter a title / summary for this work item.');
      editTitleInput.focus();
      return;
    }

    if (isNaN(newHours) || newHours < 1) {
      alert('Please enter a valid number of estimated hours (minimum 1).');
      editHoursInput.focus();
      return;
    }

    const { type, epicIdx, taskIdx, subIdx, item } = activeEditTarget;
    const jiraKey = item.jira_key || null;
    const isLive = jiraKey && !jiraKey.startsWith('DRY-');

    btnSaveEdit.disabled = true;
    btnSaveEditText.textContent = isLive ? 'Syncing to Jira...' : 'Saving...';

    try {
      if (isLive) {
        const headers = { 'Content-Type': 'application/json' };
        if (state.apiKey) headers['X-API-Key'] = state.apiKey;

        const res = await fetch('/project/ticket/update', {
          method: 'POST',
          headers: headers,
          body: JSON.stringify({
            jira_key: jiraKey,
            title: newTitle,
            description: newDesc,
            estimated_hours: newHours,
          }),
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.detail || `Jira update error (HTTP ${res.status})`);
        }
      }

      // Update in-memory state
      item.title = newTitle;
      item.description = newDesc;
      item.estimated_hours = newHours;

      // Recalculate plan hour totals
      const plan = state.currentPlan;
      if (type === 'subtask') {
        const task = plan.epics[epicIdx].tasks[taskIdx];
        task.estimated_hours = task.subtasks.reduce((sum, s) => sum + (s.estimated_hours || 0), 0);
        const epic = plan.epics[epicIdx];
        epic.estimated_hours = epic.tasks.reduce((sum, t) => sum + (t.estimated_hours || 0), 0);
      } else if (type === 'task') {
        const epic = plan.epics[epicIdx];
        epic.estimated_hours = epic.tasks.reduce((sum, t) => sum + (t.estimated_hours || 0), 0);
      }
      plan.total_estimated_hours = plan.epics.reduce((sum, e) => sum + (e.estimated_hours || 0), 0);

      // Preserve open cards across re-render
      const openIndices = [];
      document.querySelectorAll('.epic-card').forEach((card, idx) => {
        if (card.classList.contains('open')) openIndices.push(idx);
      });

      renderPlanHierarchy(plan, plan.epics.some(e => e.jira_key && e.jira_key.startsWith('DRY-')), openIndices);

      closeEditModal();
      showToast(isLive ? `✅ Synced to Jira: ${jiraKey}` : `✅ Saved changes: ${newTitle}`);
    } catch (err) {
      alert(`Update failed:\n${err.message}`);
    } finally {
      btnSaveEdit.disabled = false;
      btnSaveEditText.textContent = '💾 Save & Sync';
    }
  }

  function showToast(message, isError = false) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${isError ? 'toast-error' : ''}`;
    toast.innerHTML = `<span>${escapeHtml(message)}</span>`;

    container.appendChild(toast);

    setTimeout(() => {
      toast.style.animation = 'toastSlideOut 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards';
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  }


  // 6. Direct Live Provisioning to Jira
  async function provisionToJiraLive(plan) {
    if (!confirm('Are you sure you want to create these Epics, Tasks, and Subtasks directly in your connected Jira Cloud board?')) {
      return;
    }

    const btn = document.getElementById('btn-provision-live');
    btn.disabled = true;
    btn.textContent = 'Creating in Jira...';

    try {
      const headers = { 'Content-Type': 'application/json' };
      if (state.apiKey) headers['X-API-Key'] = state.apiKey;

      const res = await fetch('/project/provision', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
          plan: plan,
          project_key: jiraKeyInput.value.trim().toUpperCase() || 'KAN',
          dry_run: false,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Jira provision error: HTTP ${res.status}`);
      }

      const livePlan = await res.json();
      state.currentPlan = livePlan;
      alert('Success! All Epics, Tasks, and Subtasks have been created in Jira.');
      renderPlanHierarchy(livePlan, false);
    } catch (err) {
      alert(`Jira Live Creation Failed:\n${err.message}`);
      btn.disabled = false;
      btn.textContent = '🚀 Create in Jira';
    }
  }

  // Helper: Jira Browse Link
  function getJiraLink(key) {
    if (!key || key.startsWith('DRY-') || !state.jiraBaseUrl) return null;
    const base = state.jiraBaseUrl.replace(/\/+$/, '');
    return `${base}/browse/${key}`;
  }

  // Helper: Copy Markdown
  function copyMarkdownPlan(plan) {
    let md = `# Project Plan: ${plan.project_name}\n`;
    md += `**Capacity:** ${plan.developers} Devs × ${plan.weeks_duration} Weeks = ${plan.total_capacity_hours}h | **Estimated:** ${plan.total_estimated_hours}h\n\n`;

    plan.epics.forEach(epic => {
      md += `## [Epic] ${epic.title} (${epic.jira_key}) - ${epic.estimated_hours}h\n`;
      md += `${epic.description}\n\n`;
      epic.tasks.forEach(task => {
        md += `### Task: ${task.title} (${task.jira_key}) - ${task.estimated_hours}h\n`;
        md += `${task.description}\n`;
        task.subtasks.forEach(sub => {
          md += `- [ ] **${sub.title}** (${sub.estimated_hours}h): ${sub.description}\n`;
        });
        md += `\n`;
      });
    });

    navigator.clipboard.writeText(md).then(() => {
      alert('Markdown summary copied to clipboard!');
    }).catch(() => {
      prompt('Copy your plan markdown:', md);
    });
  }

  // Helper: Download JSON
  function downloadJsonPlan(plan) {
    const dataStr = 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(plan, null, 2));
    const dlAnchor = document.createElement('a');
    dlAnchor.setAttribute('href', dataStr);
    dlAnchor.setAttribute('download', `${plan.project_name.toLowerCase().replace(/[^a-z0-9]/g, '_')}_plan.json`);
    dlAnchor.click();
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // Modal Listeners
  btnOpenModal.addEventListener('click', () => {
    modalOverlay.classList.add('active');
  });

  btnCloseModal.addEventListener('click', () => {
    modalOverlay.classList.remove('active');
  });

  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) modalOverlay.classList.remove('active');
  });

  btnSaveApiKey.addEventListener('click', () => {
    state.apiKey = apiKeyInput.value.trim();
    localStorage.setItem('teampulse_admin_key', state.apiKey);
    modalOverlay.classList.remove('active');
    alert('API Key saved.');
  });

  // Edit Ticket Modal Listeners
  btnCloseEditModal.addEventListener('click', closeEditModal);
  btnCancelEdit.addEventListener('click', closeEditModal);
  btnSaveEdit.addEventListener('click', saveEditItem);

  editTicketModal.addEventListener('click', (e) => {
    if (e.target === editTicketModal) closeEditModal();
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && editTicketModal.classList.contains('active')) {
      closeEditModal();
    }
  });

  // Sliders Listeners
  devsSlider.addEventListener('input', updateCapacityDisplay);
  weeksSlider.addEventListener('input', updateCapacityDisplay);
  btnGenerate.addEventListener('click', handleGeneratePlan);

  // Initialize
  loadConfig();
  updateCapacityDisplay();
})();
