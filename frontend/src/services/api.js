const API_BASE = "/api/v1";
const AUTH_KEY = "livesync:auth";

export function getStoredAuth() {
  try {
    return JSON.parse(localStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

export function storeAuth(auth) {
  localStorage.setItem(AUTH_KEY, JSON.stringify(auth));
}

export function clearAuth() {
  localStorage.removeItem(AUTH_KEY);
}

export function getClientId(surface) {
  const key = `gitwalk:client:${surface}`;
  let clientId = localStorage.getItem(key);
  if (!clientId) {
    clientId = `${surface}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 12)}`;
    localStorage.setItem(key, clientId);
  }
  return clientId;
}

// Stable per-machine-profile id, deliberately stored in browser localStorage
// (not Office.context.document.settings, which is file-embedded and would
// travel with a copied/emailed workbook) so device trust/blocking is bound
// to the physical machine, not the file.
const DEVICE_KEY = "gitwalk:device-id";
export function getDeviceId() {
  try {
    let deviceId = localStorage.getItem(DEVICE_KEY);
    if (!deviceId) {
      deviceId = `dev_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 14)}`;
      localStorage.setItem(DEVICE_KEY, deviceId);
    }
    return deviceId;
  } catch {
    return null;
  }
}

async function parseError(response) {
  const body = await response.json().catch(() => ({}));
  const detail = body.detail || body.message || `HTTP ${response.status}`;
  return { body, detail };
}

async function apiFetch(path, options = {}) {
  const auth = getStoredAuth();
  const headers = new Headers(options.headers || {});
  if (auth?.token) headers.set("Authorization", `Bearer ${auth.token}`);
  const deviceId = getDeviceId();
  if (deviceId) headers.set("X-Device-Id", deviceId);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    if (response.status === 401) {
      clearAuth();
      window.dispatchEvent(new Event("gitwalk:auth-expired"));
    }
    const parsed = await parseError(response);
    const message = typeof parsed.detail === "object"
      ? parsed.detail.message || JSON.stringify(parsed.detail)
      : parsed.detail;
    const error = new Error(message);
    error.status = response.status;
    error.detail = parsed.detail;
    error.code = typeof parsed.detail === "object" ? parsed.detail.code : null;
    throw error;
  }
  return response;
}

export async function requestLoginCode(email) {
  const response = await fetch(`${API_BASE}/auth/request-code`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!response.ok) {
    const parsed = await parseError(response);
    throw new Error(typeof parsed.detail === "object" ? parsed.detail.message : parsed.detail);
  }
  return response.json();
}

export async function verifyLoginCode(email, code, machineId = null) {
  const response = await fetch(`${API_BASE}/auth/verify-code`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(machineId ? { email, code, machine_id: machineId } : { email, code }),
  });
  if (!response.ok) {
    const parsed = await parseError(response);
    throw new Error(typeof parsed.detail === "object" ? parsed.detail.message : parsed.detail);
  }
  const auth = await response.json();
  storeAuth(auth);
  return auth;
}

export async function authenticateWorkbook(identity) {
  const response = await fetch(`${API_BASE}/auth/workbook`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(identity),
  });
  if (!response.ok) {
    const parsed = await parseError(response);
    throw new Error(typeof parsed.detail === "object" ? parsed.detail.message : parsed.detail);
  }
  const auth = await response.json();
  storeAuth(auth);
  return auth;
}

export async function verifyWorkbookAccess(payload) {
  const response = await fetch(`${API_BASE}/auth/workbook-verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const parsed = await parseError(response);
    const err = new Error(typeof parsed.detail === "object" ? parsed.detail.message : parsed.detail);
    err.status = response.status;
    err.detail = parsed.detail;
    throw err;
  }
  const result = await response.json();
  if (result.token) {
    storeAuth({ token: result.token, user: result.user });
  }
  return result;
}

export async function sanitizeLocalWorkbook(payload) {
  try {
    const response = await fetch(`${API_BASE}/workbooks/sanitize-local`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await response.json();
  } catch (err) {
    console.warn("sanitizeLocalWorkbook error:", err);
    return null;
  }
}

export async function setUserPassword(userIdOrEmail, password) {
  const response = await fetch(`${API_BASE}/auth/set-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id_or_email: userIdOrEmail, password }),
  });
  if (!response.ok) {
    const parsed = await parseError(response);
    throw new Error(typeof parsed.detail === "object" ? parsed.detail.message : parsed.detail);
  }
  return response.json();
}

export async function getProfile() {
  return (await apiFetch("/auth/me")).json();
}

export async function logout() {
  try {
    await apiFetch("/auth/logout", { method: "POST" });
  } finally {
    clearAuth();
  }
}

export async function getSecurityAdminOverview(organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/overview${query}`)).json();
}

export async function addOrganizationMember(email, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/members${query}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }),
  })).json();
}

export async function createSecurityGroup(payload, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/groups${query}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function addSecurityGroupMember(groupId, userId, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/groups/${groupId}/members${query}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: userId }),
  })).json();
}

export async function createRoleAssignment(payload, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/role-assignments${query}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function revokeRoleAssignment(assignmentId, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  await apiFetch(`/admin/role-assignments/${assignmentId}${query}`, { method: "DELETE" });
}

export async function createAccessPolicy(payload, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/policies${query}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function updateOrganizationUserStatus(userId, status, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/users/${userId}/status${query}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }),
  })).json();
}

export async function createServiceAccount(payload, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/service-accounts${query}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function getNotifications(unreadOnly = false) {
  return (await apiFetch(`/notifications?unread_only=${unreadOnly ? "true" : "false"}`)).json();
}

export async function markNotificationRead(notificationId) {
  return (await apiFetch(`/notifications/${notificationId}/read`, { method: "POST" })).json();
}

export async function markAllNotificationsRead() {
  return (await apiFetch("/notifications/read-all", { method: "POST" })).json();
}

export async function ingestEucFromBranch(tableId, branchId) {
  return (await apiFetch(`/euc/repositories/${tableId}/branches/${branchId}/ingest`, { method: "POST" })).json();
}

export async function getRepositoryCommitGraph(tableId, limit = 300) {
  return (await apiFetch(`/repositories/${tableId}/commit-graph?limit=${limit}`)).json();
}

export async function getBranchProtection(tableId) {
  return (await apiFetch(`/repositories/${tableId}/branch-protection`)).json();
}

export async function updateBranchProtection(tableId, payload) {
  return (await apiFetch(`/repositories/${tableId}/branch-protection`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function getNotificationPreferences() {
  return (await apiFetch("/notifications/preferences")).json();
}

export async function updateNotificationPreference(type, enabled) {
  return (await apiFetch("/notifications/preferences", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type, enabled }),
  })).json();
}

export async function getOrganizationDevices(organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/devices${query}`)).json();
}

export async function setOrganizationDeviceTrust(fingerprintId, trustStatus, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  return (await apiFetch(`/admin/devices/${fingerprintId}/trust${query}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ trust_status: trustStatus }),
  })).json();
}

export async function revokeOrganizationSession(sessionId, organizationId = "") {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : "";
  await apiFetch(`/admin/sessions/${sessionId}/revoke${query}`, { method: "POST" });
}

export async function uploadAndProvision(file, metadata = {}) {
  const formData = new FormData();
  formData.append("file", file);
  Object.entries(metadata).forEach(([key, value]) => {
    if (value != null && String(value).trim() !== "") formData.append(key, value);
  });
  const response = await apiFetch("/upload-and-provision", {
    method: "POST",
    body: formData,
  });
  // The initial working copy is saved straight to the local gitwalk folder
  // server-side (see backend's upload_and_provision) -- no browser
  // Save-As download is triggered for it anymore.
  const result = await response.json();
  return {
    tableId: result.table_id || "UNKNOWN",
    branchTableId: result.branch_table_id || "",
    repositoryId: result.repository_id || "",
    branchId: result.branch_id || "",
    workingCopyId: result.working_copy_id || "",
    rowCount: String(result.row_count ?? "0"),
    colCount: String(result.column_count ?? "0"),
    macrosRegistered: Boolean(result.macros_registered),
    macroCount: result.macro_count || 0,
    macroRunnableCount: result.macro_runnable_count || 0,
    localPath: result.local_path || "",
  };
}

export async function realtimeSync(payload) {
  return (
    await apiFetch("/realtime-sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  ).json();
}

export async function bulkSync(payload) {
  return (
    await apiFetch("/bulk-sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  ).json();
}

export async function getWorkbookSnapshot(tableId, version = null) {
  const query = version == null ? "" : `?version=${encodeURIComponent(version)}`;
  return (await apiFetch(`/datasets/${tableId}/snapshot${query}`)).json();
}

export async function commitWorkbook(payload) {
  return (
    await apiFetch("/workbook-commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  ).json();
}

export async function getRepositoryDevices(tableId) {
  return (await apiFetch(`/repositories/${tableId}/devices`)).json();
}

export async function trustRepositoryDevice(tableId, fingerprintId) {
  return (
    await apiFetch(`/repositories/${tableId}/devices/${fingerprintId}/trust`, { method: "POST" })
  ).json();
}

export async function blockRepositoryDevice(tableId, fingerprintId) {
  return (
    await apiFetch(`/repositories/${tableId}/devices/${fingerprintId}/block`, { method: "POST" })
  ).json();
}

export async function getRepositoryActivity(tableId) {
  return (await apiFetch(`/repositories/${tableId}/activity`)).json();
}

export async function getRepositoryActivityMatrix(tableId, days = 14) {
  return (await apiFetch(`/repositories/${tableId}/activity/matrix?days=${days}`)).json();
}

export async function getRepositoryExpertise(tableId, days = 90) {
  return (await apiFetch(`/repositories/${tableId}/expertise?days=${days}`)).json();
}

export async function getMergeRequestReviewerSuggestion(mergeRequestId, useAi = true) {
  return (await apiFetch(`/merge-requests/${mergeRequestId}/suggest-reviewer?use_ai=${useAi}`)).json();
}

export async function getRepositoryOnboardingRamp(tableId) {
  return (await apiFetch(`/repositories/${tableId}/onboarding-ramp`)).json();
}

export async function getRepositoryAnomalies(tableId, days = 30) {
  return (await apiFetch(`/repositories/${tableId}/anomalies?days=${days}`)).json();
}

export async function getRepositoryTeamHealthDigest(tableId, useAi = true) {
  return (await apiFetch(`/repositories/${tableId}/team-health-digest?use_ai=${useAi}`)).json();
}

export async function getRepositoryMemberProfile(tableId, userId) {
  return (await apiFetch(`/repositories/${tableId}/members/${userId}/profile`)).json();
}

export async function runCommitAIReview(commitId) {
  return (
    await apiFetch(`/commits/${commitId}/ai-review`, { method: "POST" })
  ).json();
}

export async function startCommitAIReview(commitId) {
  return (
    await apiFetch(`/commits/${commitId}/ai-review/start`, { method: "POST" })
  ).json();
}

export async function getCommitAIReview(commitId) {
  return (await apiFetch(`/commits/${commitId}/ai-review`)).json();
}

export async function getBranchState(branchId, commitId = "") {
  const query = commitId ? `?commit_id=${encodeURIComponent(commitId)}` : "";
  return (await apiFetch(`/branches/${branchId}/state${query}`)).json();
}

export async function getBranchCommits(branchId, limit = 100) {
  return (await apiFetch(`/branches/${branchId}/commits?limit=${limit}`)).json();
}

export async function getBranchMetrics(branchId) {
  return (await apiFetch(`/branches/${branchId}/metrics`)).json();
}

export async function getCommitDetail(commitId) {
  return (await apiFetch(`/commits/${commitId}`)).json();
}

export async function getStableCellHistory(branchId, sheetId, rowId, columnId, limit = 100) {
  return (
    await apiFetch(
      `/branches/${branchId}/cells/${sheetId}/${rowId}/${columnId}/history?limit=${limit}`
    )
  ).json();
}

export async function getBranchDivergence(branchId) {
  return (await apiFetch(`/branches/${branchId}/divergence`)).json();
}

export async function syncBranchWithMain(branchId) {
  return (await apiFetch(`/branches/${branchId}/sync`, { method: "POST" })).json();
}

export async function getMergeRequests(tableId) {
  return (await apiFetch(`/repositories/${tableId}/merge-requests`)).json();
}

export async function createMergeRequest(payload) {
  return (
    await apiFetch("/merge-requests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  ).json();
}

export async function getMergeRequest(mergeRequestId) {
  return (await apiFetch(`/merge-requests/${mergeRequestId}`)).json();
}

export async function resolveMergeConflict(mergeRequestId, conflictId, payload) {
  return (
    await apiFetch(`/merge-requests/${mergeRequestId}/conflicts/${conflictId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  ).json();
}

export async function reviewMergeRequest(mergeRequestId, decision, comment = "") {
  return (
    await apiFetch(`/merge-requests/${mergeRequestId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, comment }),
    })
  ).json();
}

export async function mergeMergeRequest(mergeRequestId) {
  return (await apiFetch(`/merge-requests/${mergeRequestId}/merge`, { method: "POST" })).json();
}

export async function requestReviewer(mergeRequestId, reviewerUserId) {
  return (
    await apiFetch(`/merge-requests/${mergeRequestId}/reviewer-requests`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewer_user_id: reviewerUserId }),
    })
  ).json();
}

export async function listReviewerRequests(mergeRequestId) {
  return (await apiFetch(`/merge-requests/${mergeRequestId}/reviewer-requests`)).json();
}

export async function getReviewerRequest(requestId) {
  return (await apiFetch(`/reviewer-requests/${requestId}`)).json();
}

export async function respondToReviewerRequest(requestId, decision, comment = "") {
  return (
    await apiFetch(`/reviewer-requests/${requestId}/respond`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, comment }),
    })
  ).json();
}

export async function useReviewerAiFallback(requestId) {
  return (await apiFetch(`/reviewer-requests/${requestId}/ai-fallback`, { method: "POST" })).json();
}

export async function runMergeConflictAIAnalysis(mergeRequestId) {
  return (
    await apiFetch(`/merge-requests/${mergeRequestId}/ai-analyze`, { method: "POST" })
  ).json();
}

export async function startMergeConflictAIAnalysis(mergeRequestId) {
  return (
    await apiFetch(`/merge-requests/${mergeRequestId}/ai-analyze/start`, { method: "POST" })
  ).json();
}

export async function getMergeConflictAISuggestions(mergeRequestId) {
  return (await apiFetch(`/merge-requests/${mergeRequestId}/ai-suggestions`)).json();
}

export async function prepareAIConflictApply(mergeRequestId, conflictId) {
  return (
    await apiFetch(
      `/merge-requests/${mergeRequestId}/conflicts/${conflictId}/ai-apply`,
      { method: "POST" }
    )
  ).json();
}

export async function proposeFindingRemediation(eucId, findingId) {
  return (
    await apiFetch(`/euc/${eucId}/findings/${findingId}/remediation`, { method: "POST" })
  ).json();
}

export async function startFindingRemediation(eucId, findingId) {
  return (
    await apiFetch(`/euc/${eucId}/findings/${findingId}/remediation/start`, { method: "POST" })
  ).json();
}

export async function getFindingRemediations(eucId, findingId) {
  return (await apiFetch(`/euc/${eucId}/findings/${findingId}/remediation`)).json();
}

export async function prepareFindingRemediationApply(eucId, findingId) {
  return (
    await apiFetch(`/euc/${eucId}/findings/${findingId}/remediation/apply`, { method: "POST" })
  ).json();
}

export async function runMergeRequestAIAssessment(mergeRequestId) {
  return (
    await apiFetch(`/merge-requests/${mergeRequestId}/ai-assessment`, { method: "POST" })
  ).json();
}

export async function startMergeRequestAIAssessment(mergeRequestId) {
  return (
    await apiFetch(`/merge-requests/${mergeRequestId}/ai-assessment/start`, { method: "POST" })
  ).json();
}

export async function getMergeRequestAIAssessment(mergeRequestId) {
  return (await apiFetch(`/merge-requests/${mergeRequestId}/ai-assessment`)).json();
}

export async function revertSemanticCommit(commitId) {
  return (await apiFetch(`/commits/${commitId}/revert`, { method: "POST" })).json();
}

export async function getCellValue(payload) {
  const params = new URLSearchParams({
    table_id: payload.table_id,
    row_id: String(payload.row_id),
    column_name: payload.column_name,
  });
  return (await apiFetch(`/cell-value?${params}`)).json();
}

export async function getDatasets() {
  return (await apiFetch("/datasets")).json();
}

export async function getCategories() {
  return (await apiFetch("/categories")).json();
}

export async function createCategory(name, description = "", parentCategoryId = "CAT_HOME") {
  return (
    await apiFetch("/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        description,
        parent_category_id: parentCategoryId,
      }),
    })
  ).json();
}

export async function getRepository(tableId) {
  return (await apiFetch(`/repositories/${tableId}`)).json();
}

export async function checkRepositoryName(name) {
  return (await apiFetch(`/repositories/name-availability?name=${encodeURIComponent(name)}`)).json();
}

export async function getRepositoryBranches(tableId) {
  return (await apiFetch(`/repositories/${tableId}/branches`)).json();
}

export async function getMyWorkingCopies() {
  return (await apiFetch("/working-copies/mine")).json();
}

export async function moveRepositoryToCategory(tableId, categoryId) {
  return (
    await apiFetch(`/repositories/${tableId}/category`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category_id: categoryId }),
    })
  ).json();
}

export async function getCheckoutOptions(tableId) {
  return (await apiFetch(`/repositories/${tableId}/checkout-options`)).json();
}

export async function getEucStorageSetting() {
  return (await apiFetch("/settings/euc-storage")).json();
}

export async function saveEucStorageSetting(localDownloadDir) {
  return (
    await apiFetch("/settings/euc-storage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ local_download_dir: localDownloadDir }),
    })
  ).json();
}

export async function workOnWorkbook(tableId, mode = "continue", branchId = null, localDownloadDir = null) {
  const payload = { mode, branch_id: branchId };
  if (localDownloadDir) {
    payload.local_download_dir = localDownloadDir;
  }
  const response = await apiFetch(`/repositories/${tableId}/work-on-workbook`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  // Saved straight to the local gitwalk folder server-side -- no browser
  // Save-As download dialog anymore, just the JSON metadata below.
  const result = await response.json();
  return {
    repositoryId: result.repository_id,
    branchId: result.branch_id,
    workingCopyId: result.working_copy_id,
    localSavedPath: result.local_path,
  };
}

async function downloadResponse(response, fallbackName) {
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const filenameMatch = disposition.match(/filename="?(.+?)"?$/);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filenameMatch ? filenameMatch[1] : fallbackName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export async function downloadRepositoryBranch(tableId, branchId) {
  const response = await apiFetch(`/repositories/${tableId}/branches/${branchId}/download`);
  await downloadResponse(response, `gitwalk_${branchId}.xlsx`);
}

export async function getRepositorySheetData(tableId, branchId, sheetId, limit = 100, offset = 0) {
  return (
    await apiFetch(
      `/repositories/${tableId}/branches/${branchId}/sheets/${sheetId}/records?limit=${limit}&offset=${offset}`
    )
  ).json();
}

export async function deleteRepository(tableId) {
  return (await apiFetch(`/repositories/${tableId}`, { method: "DELETE" })).json();
}

export async function deleteBranch(branchId) {
  return (await apiFetch(`/branches/${branchId}`, { method: "DELETE" })).json();
}

export async function getDatasetData(tableId, limit = 100, offset = 0) {
  return (await apiFetch(`/datasets/${tableId}/data?limit=${limit}&offset=${offset}`)).json();
}

export async function getDatasetHistory(tableId, limit = 100) {
  return (await apiFetch(`/datasets/${tableId}/history?limit=${limit}`)).json();
}

export async function getDatasetMembers(tableId) {
  return (await apiFetch(`/datasets/${tableId}/members`)).json();
}

export async function addDatasetMember(tableId, email, role) {
  return (
    await apiFetch(`/datasets/${tableId}/members`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, role }),
    })
  ).json();
}

export async function revokeDatasetMember(tableId, email) {
  const query = new URLSearchParams({ email });
  return (await apiFetch(`/datasets/${tableId}/members?${query}`, { method: "DELETE" })).json();
}

export async function getWorkspaceState(tableId, sinceRevision = 0, waitSeconds = 0) {
  const query = new URLSearchParams({
    since_revision: String(sinceRevision),
    wait_seconds: String(waitSeconds),
  });
  return (await apiFetch(`/datasets/${tableId}/workspace?${query}`)).json();
}

export async function heartbeatPresence(tableId, clientId, surface, activity = "viewing", status = "ONLINE") {
  return (
    await apiFetch(`/datasets/${tableId}/presence`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId, surface, activity, status }),
    })
  ).json();
}

export async function getDatasetPresence(tableId) {
  return (await apiFetch(`/datasets/${tableId}/presence`)).json();
}

export async function leaveDatasetPresence(tableId, clientId) {
  const query = new URLSearchParams({ client_id: clientId });
  return (await apiFetch(`/datasets/${tableId}/presence?${query}`, { method: "DELETE" })).json();
}

export async function getKpis(tableId = "") {
  const query = tableId ? `?table_id=${encodeURIComponent(tableId)}` : "";
  return (await apiFetch(`/analytics/kpis${query}`)).json();
}

export async function rollbackChangeSet(tableId, batchId) {
  return (
    await apiFetch("/datasets/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ table_id: tableId, batch_id: batchId }),
    })
  ).json();
}

export async function downloadDataset(tableId, format = "xlsx") {
  const response = await apiFetch(`/datasets/${tableId}/export?format=${format}`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${tableId}.${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export async function getAIModels() {
  return (await apiFetch("/ai/models")).json();
}

export async function saveAIConfig(apiKey, model) {
  return (
    await apiFetch("/ai/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey, model }),
    })
  ).json();
}

export async function clearAIConfig() {
  return (await apiFetch("/ai/config", { method: "DELETE" })).json();
}

export async function generateAIInsight(payload) {
  return (
    await apiFetch("/ai/insights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
  ).json();
}

export async function getAIPlatformAdministration() {
  return (await apiFetch("/ai-platform/administration")).json();
}

export async function updateAIPlatformSettings(payload) {
  return (await apiFetch("/ai-platform/administration/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function updateAIModelPolicy(feature, payload) {
  return (await apiFetch(`/ai-platform/administration/model-policies/${encodeURIComponent(feature)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function getAIPlatformUsage() {
  return (await apiFetch("/ai-platform/usage")).json();
}

export async function getAIConversations() {
  return (await apiFetch("/ai-platform/conversations")).json();
}

export async function getAIConversation(conversationId) {
  return (await apiFetch(`/ai-platform/conversations/${conversationId}`)).json();
}

export async function sendAIPlatformMessage(payload) {
  return (await apiFetch("/ai-platform/chat", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function scanAIControls(repositoryId = null) {
  return (await apiFetch("/ai-platform/controls/scan", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repository_id: repositoryId }),
  })).json();
}

export async function getAIInsights(status = "OPEN") {
  return (await apiFetch(`/ai-platform/insights?status=${encodeURIComponent(status)}`)).json();
}

export async function explainAIInsight(insightId) {
  return (await apiFetch(`/ai-platform/insights/${insightId}/explain`, { method: "POST" })).json();
}

export async function getSignalSources() {
  return (await apiFetch("/ai-platform/signals/sources")).json();
}

export async function getSignalDefinitions() {
  return (await apiFetch("/ai-platform/signals")).json();
}

export async function createSignalDefinition(payload) {
  return (await apiFetch("/ai-platform/signals", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function updateSignalDefinition(signalId, payload) {
  return (await apiFetch(`/ai-platform/signals/${signalId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function deleteSignalDefinition(signalId) {
  await apiFetch(`/ai-platform/signals/${signalId}`, { method: "DELETE" });
}

export async function getSignalRuns(signalId, limit = 30) {
  return (await apiFetch(`/ai-platform/signals/${signalId}/runs?limit=${limit}`)).json();
}

export async function scanSignals(repositoryId = null, signalId = null) {
  return (await apiFetch("/ai-platform/signals/scan", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repository_id: repositoryId, signal_id: signalId }),
  })).json();
}

export async function runAIAgent(payload) {
  return (await apiFetch("/ai-platform/agents/run", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function startAIAgent(payload) {
  return (await apiFetch("/ai-platform/agents/run/start", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function getAIAgentRuns() {
  return (await apiFetch("/ai-platform/agents/runs")).json();
}

export async function confirmAIAction(actionId) {
  return (await apiFetch(`/ai-platform/actions/${actionId}/confirm`, { method: "POST" })).json();
}

export async function runAIEvaluation() {
  return (await apiFetch("/ai-platform/evaluations/run", { method: "POST" })).json();
}

export async function getAIEvaluations() {
  return (await apiFetch("/ai-platform/evaluations")).json();
}

export async function getAgentLedgerLeaderboard(days = 30) {
  return (await apiFetch(`/ai-platform/ledger/agents?days=${days}`)).json();
}

export async function getAgentLedgerTrend(days = 30) {
  return (await apiFetch(`/ai-platform/ledger/trend?days=${days}`)).json();
}

export async function getAgentLedgerModels(days = 30) {
  return (await apiFetch(`/ai-platform/ledger/models?days=${days}`)).json();
}

export async function getAgentLedgerRuns(filters = {}) {
  const query = new URLSearchParams();
  if (filters.agentKey) query.set("agent_key", filters.agentKey);
  if (filters.status) query.set("status", filters.status);
  query.set("limit", String(filters.limit || 50));
  query.set("cursor", String(filters.cursor || 0));
  return (await apiFetch(`/ai-platform/ledger/runs?${query}`)).json();
}

export async function getAgentLedgerRunReceipt(agentRunId) {
  return (await apiFetch(`/ai-platform/ledger/runs/${agentRunId}`)).json();
}

export async function getAgentRunProgress(agentRunId) {
  return (await apiFetch(`/ai-platform/agent-runs/${agentRunId}`)).json();
}

export async function startFormulaExplanation(tableId, payload) {
  return (
    await apiFetch(`/repositories/${tableId}/formula-explanation`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    })
  ).json();
}

export async function getFormulaExplanation(tableId, params) {
  const query = new URLSearchParams(params);
  return (await apiFetch(`/repositories/${tableId}/formula-explanation?${query}`)).json();
}

export async function startDataInsight(tableId, branchId, question) {
  return (
    await apiFetch(`/repositories/${tableId}/insights/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: branchId, question }),
    })
  ).json();
}

export async function getDataInsightResult(tableId, insightResultId) {
  return (await apiFetch(`/repositories/${tableId}/insights/results/${insightResultId}`)).json();
}

export async function startDiscovery(tableId, topic) {
  return (
    await apiFetch(`/repositories/${tableId}/discovery`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ topic }),
    })
  ).json();
}

export async function startRbacAnomalyScan(tableId) {
  return (await apiFetch(`/repositories/${tableId}/rbac-anomalies/scan`, { method: "POST" })).json();
}

export async function getRbacAnomalies(tableId, status) {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return (await apiFetch(`/repositories/${tableId}/rbac-anomalies${query}`)).json();
}

export async function decideRbacAnomaly(tableId, findingId, decision, reason) {
  return (
    await apiFetch(`/repositories/${tableId}/rbac-anomalies/${findingId}/decision`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, reason }),
    })
  ).json();
}

export async function startPortfolioBriefing() {
  return (await apiFetch("/euc/portfolio/briefing", { method: "POST" })).json();
}

export async function getPortfolioBriefing() {
  return (await apiFetch("/euc/portfolio/briefing")).json();
}

export async function getAgentLedgerBudget() {
  return (await apiFetch("/ai-platform/ledger/budget")).json();
}

export async function getPersonalActivity(range = "30d") {
  return (await apiFetch(`/ai-platform/personal-activity?range=${range}`)).json();
}

export async function getAuditEvents(tableId = "", options = {}) {
  const query = new URLSearchParams();
  if (tableId) query.set("table_id", tableId);
  if (options.eventType) query.set("event_type", options.eventType);
  if (options.status) query.set("status", options.status);
  if (options.since) query.set("since", options.since);
  if (options.until) query.set("until", options.until);
  query.set("limit", String(options.limit || 300));
  return (await apiFetch(`/audit/events?${query}`)).json();
}

export async function getOperationalMetrics(hours = 24) {
  return (await apiFetch(`/observability/metrics?hours=${hours}`)).json();
}

export async function getOperationalMetricsTrend(hours = 24 * 14) {
  return (await apiFetch(`/observability/metrics/trend?hours=${hours}`)).json();
}

export async function getSecurityPosture() {
  return (await apiFetch("/security/posture")).json();
}

export async function getRepositoryInsights(tableId) {
  return (await apiFetch(`/repositories/${tableId}/insights`)).json();
}

export async function getRepositoryStorage(tableId) {
  return (await apiFetch(`/repositories/${tableId}/storage`)).json();
}

export async function migrateRepositoryStorage(tableId) {
  return (
    await apiFetch(`/repositories/${tableId}/storage/migrate`, { method: "POST" })
  ).json();
}

export async function runStorageGC(tableId, dryRun = true) {
  const query = new URLSearchParams({ table_id: tableId, dry_run: String(dryRun) });
  return (await apiFetch(`/storage/gc?${query}`, { method: "POST" })).json();
}

export async function getPortfolioRiskOverview() {
  return (await apiFetch("/euc/portfolio/risk-overview")).json();
}

export async function getPortfolioAttestationOverview() {
  return (await apiFetch("/euc/portfolio/attestation-overview")).json();
}

export async function getEucAttestation(eucId) {
  return (await apiFetch(`/euc/${eucId}/attestation`)).json();
}

export async function submitEucAttestation(eucId, statement) {
  return (
    await apiFetch(`/euc/${eucId}/attestation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ statement }),
    })
  ).json();
}

export async function getEucAssets(repositoryId = "", search = "", since = "", until = "") {
  const query = new URLSearchParams();
  if (repositoryId) query.set("repository_id", repositoryId);
  if (search) query.set("search", search);
  if (since) query.set("since", since);
  if (until) query.set("until", until);
  return (await apiFetch(`/euc?${query}`)).json();
}

export async function uploadEuc(file, repositoryId) {
  const form = new FormData();
  form.append("file", file);
  form.append("repository_id", repositoryId);
  return (await apiFetch("/euc", { method: "POST", body: form })).json();
}

export async function analyzeEuc(eucId) {
  return (await apiFetch(`/euc/${eucId}/analysis`, { method: "POST" })).json();
}

export async function getEucInventory(eucId, include = "overview,sheets,formulas,objects,dependencies") {
  const query = new URLSearchParams({ include, limit: "250", cursor: "0" });
  return (await apiFetch(`/euc/${eucId}/inventory?${query}`)).json();
}

export async function exportEucInventory(eucId) {
  const response = await apiFetch(`/euc/${eucId}/export`);
  await downloadResponse(response, `${eucId}_inventory.json`);
}

export async function buildEucDependencyGraph(eucId) {
  return (await apiFetch(`/euc/${eucId}/dependency/analysis`, { method: "POST" })).json();
}

export async function getEucDependencyOverview(eucId) {
  return (await apiFetch(`/euc/${eucId}/dependency/overview`)).json();
}

export async function getEucDependencySheets(eucId) {
  return (await apiFetch(`/euc/${eucId}/dependency/sheets`)).json();
}

export async function getEucDependencyHotspots(eucId, limit = 30) {
  return (await apiFetch(`/euc/${eucId}/dependency/hotspots?limit=${limit}`)).json();
}

export async function getEucDependencyCycles(eucId) {
  return (await apiFetch(`/euc/${eucId}/dependency/cycles`)).json();
}

export async function getEucBrokenDependencies(eucId, limit = 200) {
  return (await apiFetch(`/euc/${eucId}/dependency/broken?limit=${limit}`)).json();
}

export async function searchEucDependencyNodes(eucId, query, limit = 50) {
  const params = new URLSearchParams({ query, limit: String(limit) });
  return (await apiFetch(`/euc/${eucId}/dependency/search?${params}`)).json();
}

export async function getEucCellLineage(eucId, sheetId, cellAddress, direction = "both", depth = 8) {
  const params = new URLSearchParams({ sheet_id: sheetId, cell_address: cellAddress, depth: String(depth) });
  const endpoint = direction === "downstream" ? "downstream" : "upstream";
  if (direction === "both") {
    const node = await searchEucDependencyNodes(eucId, cellAddress, 50);
    const exact = (node.nodes || []).find((item) => item.sheet_id === sheetId && item.cell_address === cellAddress.toUpperCase());
    if (!exact) throw new Error("That cell is not represented in the current dependency graph.");
    return (await apiFetch(`/euc/${eucId}/lineage/${encodeURIComponent(exact.node_id)}?direction=both&depth=${depth}`)).json();
  }
  return (await apiFetch(`/euc/${eucId}/dependency/${endpoint}?${params}`)).json();
}

export async function getEucDependencyImpact(eucId, sheetId, cellAddress, maxDepth = 12) {
  return (await apiFetch(`/euc/${eucId}/impact`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sheet_id: sheetId, cell_address: cellAddress, max_depth: maxDepth }),
  })).json();
}

export async function buildEucIntelligence(eucId, profile = "DEFAULT") {
  return (await apiFetch(`/euc/${eucId}/intelligence/analysis`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile }),
  })).json();
}

export async function getEucIntelligence(eucId) {
  return (await apiFetch(`/euc/${eucId}/intelligence`)).json();
}

export async function getEucComplexity(eucId) {
  return (await apiFetch(`/euc/${eucId}/complexity`)).json();
}

export async function getEucRiskExplanation(eucId) {
  return (await apiFetch(`/euc/${eucId}/risk/explain`)).json();
}

export async function getEucControls(eucId) {
  return (await apiFetch(`/euc/${eucId}/controls`)).json();
}

export async function getEucFindings(eucId, filters = {}) {
  const query = new URLSearchParams({ limit: String(filters.limit || 300) });
  ["severity", "category", "sheet_id", "status", "rule_id"].forEach((key) => {
    if (filters[key]) query.set(key, filters[key]);
  });
  return (await apiFetch(`/euc/${eucId}/findings?${query}`)).json();
}

export async function getEucFinding(eucId, findingId) {
  return (await apiFetch(`/euc/${eucId}/findings/${findingId}`)).json();
}

export async function updateEucFindingStatus(eucId, findingId, status, reason, expiresAt = null) {
  return (await apiFetch(`/euc/${eucId}/findings/${findingId}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, reason, expires_at: expiresAt || null }),
  })).json();
}

export async function runEucBranchComparison(tableId, branchId) {
  return (await apiFetch(`/euc/repositories/${tableId}/branches/${branchId}/ai-comparison`, {
    method: "POST",
  })).json();
}

export async function startEucBranchComparison(tableId, branchId) {
  return (await apiFetch(`/euc/repositories/${tableId}/branches/${branchId}/ai-comparison/start`, {
    method: "POST",
  })).json();
}

export async function getEucBranchComparison(tableId, branchId) {
  return (await apiFetch(`/euc/repositories/${tableId}/branches/${branchId}/ai-comparison`)).json();
}

export async function analyzeEucMigration(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration/analyze`, { method: "POST" })).json();
}

export async function getEucMigration(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration`)).json();
}

export async function getEucMigrationReadiness(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration/readiness`)).json();
}

export async function getEucMigrationBlockers(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration/blockers`)).json();
}

export async function getEucMigrationComponents(eucId, filters = {}) {
  const query = new URLSearchParams();
  if (filters.mode) query.set("mode", filters.mode);
  if (filters.sourceType) query.set("source_type", filters.sourceType);
  if (filters.wave !== undefined && filters.wave !== "") query.set("wave", filters.wave);
  return (await apiFetch(`/euc/${eucId}/migration/components?${query}`)).json();
}

export async function getEucMigrationWaves(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration/waves`)).json();
}

export async function getEucMigrationTarget(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration/target`)).json();
}

export async function getEucMigrationControls(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration/controls`)).json();
}

export async function getEucMigrationValidation(eucId) {
  return (await apiFetch(`/euc/${eucId}/migration/validation`)).json();
}

export async function overrideEucMigrationComponent(eucId, unitId, manualMode, reason) {
  return (await apiFetch(`/euc/${eucId}/migration/components/${unitId}/override`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ manual_mode: manualMode, reason }),
  })).json();
}

export async function buildEucApplicationModel(eucId, targetProfile = "WEB_POSTGRES_FASTAPI_REACT") {
  return (await apiFetch(`/euc/${eucId}/application-model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_profile: targetProfile }),
  })).json();
}

export async function getEucApplicationModel(eucId) {
  return (await apiFetch(`/euc/${eucId}/application-model`)).json();
}

export async function getEucApplicationManifest(eucId) {
  return (await apiFetch(`/euc/${eucId}/application-model/manifest`)).json();
}

export async function getEucApplicationComponents(eucId, componentType = "") {
  const query = new URLSearchParams();
  if (componentType) query.set("component_type", componentType);
  return (await apiFetch(`/euc/${eucId}/application-model/components?${query}`)).json();
}

export async function getEucApplicationLineage(eucId, query = "") {
  const params = new URLSearchParams();
  if (query) params.set("query", query);
  return (await apiFetch(`/euc/${eucId}/application-model/lineage?${params}`)).json();
}

export async function reviewEucApplicationComponent(eucId, componentId, decision, reason) {
  return (await apiFetch(`/euc/${eucId}/application-model/components/${componentId}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, reason }),
  })).json();
}

export async function bulkReviewEucApplicationComponents(eucId, decision, reason, componentType = null, minConfidence = 0) {
  return (await apiFetch(`/euc/${eucId}/application-model/components/bulk-review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, reason, component_type: componentType, min_confidence: minConfidence }),
  })).json();
}

export async function approveEucApplicationModel(eucId) {
  return (await apiFetch(`/euc/${eucId}/application-model/approve`, { method: "POST" })).json();
}

export async function generateEucApplication(eucId, mode = "MODEL_ONLY", targetProfile = "WEB_POSTGRES_FASTAPI_REACT") {
  return (await apiFetch(`/euc/${eucId}/application-model/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, target_profile: targetProfile }),
  })).json();
}

export async function getEucApplicationGeneration(eucId) {
  return (await apiFetch(`/euc/${eucId}/application-model/generation`)).json();
}

export async function downloadEucApplicationGeneration(eucId, generationRunId) {
  const response = await apiFetch(`/euc/${eucId}/application-model/generation/${generationRunId}/download`);
  await downloadResponse(response, `${generationRunId.toLowerCase()}.zip`);
}

export async function getEucApplicationGenerationFiles(eucId, generationRunId) {
  return (await apiFetch(`/euc/${eucId}/application-model/generation/${generationRunId}/files`)).json();
}

export async function getEucApplicationGenerationFile(eucId, generationRunId, path) {
  return (await apiFetch(`/euc/${eucId}/application-model/generation/${generationRunId}/files/${path}`)).json();
}

export async function getWorkbookBlame(branchId, sheetId = "", limit = 5000) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (sheetId) query.set("sheet_id", sheetId);
  return (await apiFetch(`/branches/${branchId}/blame?${query}`)).json();
}

export async function getWorkbookChangeActivity(branchId, options = {}) {
  const query = new URLSearchParams({
    sort: options.sort || "desc",
    limit: String(options.limit || 1000),
  });
  if (options.sheetId) query.set("sheet_id", options.sheetId);
  if (options.operation) query.set("operation", options.operation);
  return (await apiFetch(`/branches/${branchId}/change-activity?${query}`)).json();
}

export async function getRowHistory(branchId, sheetId, rowId) {
  return (await apiFetch(`/branches/${branchId}/rows/${sheetId}/${rowId}/history`)).json();
}

export async function getCellTraceability(branchId, sheetId, rowId, columnId) {
  return (
    await apiFetch(
      `/branches/${branchId}/cells/${sheetId}/${rowId}/${columnId}/traceability`
    )
  ).json();
}

export async function getFabricConnectorTypes() {
  return (await apiFetch("/information-fabric/connector-types")).json();
}

export async function getFabricConnections() {
  return (await apiFetch("/information-fabric/connections")).json();
}

export async function createFabricConnection(payload) {
  return (await apiFetch("/information-fabric/connections", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function uploadFabricSource(connectionId, file) {
  const body = new FormData(); body.append("file", file);
  return (await apiFetch(`/information-fabric/connections/${connectionId}/source-file`, { method: "POST", body })).json();
}

export async function testFabricConnection(connectionId) {
  return (await apiFetch(`/information-fabric/connections/${connectionId}/test`, { method: "POST" })).json();
}

export async function discoverFabricConnection(connectionId) {
  return (await apiFetch(`/information-fabric/connections/${connectionId}/discover`, { method: "POST" })).json();
}

export async function runFabricConnection(connectionId, payload = {}) {
  return (await apiFetch(`/information-fabric/connections/${connectionId}/runs`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })).json();
}

export async function getFabricRuns(connectionId = "") {
  const query = connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "";
  return (await apiFetch(`/information-fabric/runs${query}`)).json();
}

export async function getFabricDeadLetters() {
  return (await apiFetch("/information-fabric/dead-letters")).json();
}

export async function replayFabricDeadLetter(deadLetterId) {
  return (await apiFetch(`/information-fabric/dead-letters/${deadLetterId}/replay`, { method: "POST" })).json();
}

export async function getFabricCanonicalObjects(objectType = "") {
  const query = objectType ? `?object_type=${encodeURIComponent(objectType)}` : "";
  return (await apiFetch(`/information-fabric/canonical${query}`)).json();
}

export async function getFabricThread(nodeId, depth = 3) {
  return (await apiFetch(`/information-fabric/thread/${nodeId}?depth=${depth}`)).json();
}

export async function getFabricThreadTimeline(nodeId) {
  return (await apiFetch(`/information-fabric/thread/${nodeId}/timeline`)).json();
}

export async function searchFabric(query = "", itemType = "") {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  if (itemType) params.set("item_type", itemType);
  return (await apiFetch(`/information-fabric/search?${params}`)).json();
}

export async function registerMacroSource(tableId, file) {
  const form = new FormData();
  form.append("file", file);
  return (await apiFetch(`/repositories/${tableId}/macros/source`, { method: "POST", body: form })).json();
}

export async function extractRepositoryMacros(tableId) {
  return (await apiFetch(`/repositories/${tableId}/macros/extract`, { method: "POST" })).json();
}

export async function getRepositoryMacros(tableId) {
  return (await apiFetch(`/repositories/${tableId}/macros`)).json();
}

export async function getMacroSource(tableId, macroId) {
  return (await apiFetch(`/repositories/${tableId}/macros/${macroId}/source`)).json();
}

export async function prepareMacroRun(tableId, macroId, branchId) {
  return (await apiFetch(`/repositories/${tableId}/macros/${macroId}/runs`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ branch_id: branchId }),
  })).json();
}

export async function getMacroRun(tableId, runId) {
  return (await apiFetch(`/repositories/${tableId}/macros/runs/${runId}`)).json();
}

export async function confirmMacroRun(tableId, runId, commitMessage) {
  return (await apiFetch(`/repositories/${tableId}/macros/runs/${runId}/confirm`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ commit_message: commitMessage || null }),
  })).json();
}

export async function startMacroExplanation(tableId, macroId) {
  return (await apiFetch(`/repositories/${tableId}/macros/${macroId}/explain`, { method: "POST" })).json();
}

export async function getMacroExplanation(tableId, macroId) {
  return (await apiFetch(`/repositories/${tableId}/macros/${macroId}/explanation`)).json();
}

export async function saveMacroRecipe(tableId, macroId, name, description) {
  return (await apiFetch(`/repositories/${tableId}/macros/${macroId}/recipes`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, description: description || null }),
  })).json();
}

export async function getRepositoryRecipes(tableId) {
  return (await apiFetch(`/repositories/${tableId}/macros/recipes`)).json();
}

export async function runMacroRecipe(tableId, recipeId, branchId) {
  return (await apiFetch(`/repositories/${tableId}/macros/recipes/${recipeId}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ branch_id: branchId }),
  })).json();
}

export async function deleteMacroRecipe(tableId, recipeId) {
  return (await apiFetch(`/repositories/${tableId}/macros/recipes/${recipeId}`, { method: "DELETE" })).json();
}

export async function getFabricOperations() {
  return (await apiFetch("/information-fabric/operations")).json();
}
