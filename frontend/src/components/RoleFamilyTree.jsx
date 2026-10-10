import React, { useMemo } from "react";

/**
 * RBAC access map — a real three-level family tree: Organization (root) ->
 * Scope (organization-wide, or one branch per repository scope) -> Role ->
 * individual user leaves, connected with genuine parent/child lines rather
 * than a flat grid of disconnected cards. Built from the same
 * `overview.assignments` `security_overview()` already returns
 * (access_control/service.py), which now also excludes repository-scoped
 * assignments whose repository has been deleted, so this tree — and the
 * "Filter by repository" list built from it — only ever shows active
 * repositories.
 *
 * Each level hangs from a shared rail above it (a plain CSS org-chart
 * technique: one border-top rail plus one vertical stem per child), so the
 * tree stays genuinely connected however many scopes/roles/users exist,
 * scrolling horizontally only at the level that actually needs it — and
 * the `scopeFilter` prop collapses the top level to a single branch so
 * filtering to one repository shows just that repository's own tree.
 */
export default function RoleFamilyTree({ assignments, organizationName, onRevoke, busy, scopeFilter }) {
  const tree = useMemo(() => {
    const scopes = new Map();
    (assignments || []).forEach((item) => {
      const scopeKey = item.scope_type === "ORGANIZATION" ? "ORGANIZATION" : item.scope_id;
      if (scopeFilter && scopeFilter !== "ALL" && scopeKey !== scopeFilter) return;
      if (!scopes.has(scopeKey)) {
        scopes.set(scopeKey, {
          key: scopeKey,
          label: item.scope_type === "ORGANIZATION" ? "Organization-wide" : (item.scope_name || `Repository ${item.scope_id.slice(0, 10)}`),
          scopeType: item.scope_type,
          roles: new Map(),
        });
      }
      const scope = scopes.get(scopeKey);
      if (!scope.roles.has(item.role_key)) scope.roles.set(item.role_key, []);
      scope.roles.get(item.role_key).push(item);
    });
    return [...scopes.values()].sort((a, b) => (a.scopeType === "ORGANIZATION" ? -1 : 1) - (b.scopeType === "ORGANIZATION" ? -1 : 1));
  }, [assignments, scopeFilter]);

  const totalAssignments = tree.reduce(
    (sum, scope) => sum + [...scope.roles.values()].reduce((inner, members) => inner + members.length, 0),
    0
  );

  if (!tree.length) return <div className="empty-state compact">No role assignments {scopeFilter && scopeFilter !== "ALL" ? "for this repository" : "yet"}.</div>;

  return (
    <div className="role-tree-wrap">
      <div className="fam-tree">
        <div className="fam-tree-root">
          <strong>{organizationName || "Organization"}</strong>
          <small>{totalAssignments} assignment{totalAssignments === 1 ? "" : "s"}</small>
        </div>
        <div className="fam-tree-level">
          {tree.map((scope) => {
            const scopeCount = [...scope.roles.values()].reduce((total, members) => total + members.length, 0);
            return (
              <div key={scope.key} className="fam-tree-branch">
                <div className="fam-tree-node fam-tree-scope">
                  <strong>{scope.label}</strong>
                  <small>{scope.scopeType === "ORGANIZATION" ? "organization" : "repository"} &middot; {scopeCount}</small>
                </div>
                <div className="fam-tree-level fam-tree-level-roles">
                  {[...scope.roles.entries()].map(([roleKey, members]) => (
                    <div key={roleKey} className="fam-tree-branch">
                      <div className="fam-tree-node fam-tree-role"><b>{roleKey.replaceAll("_", " ")}</b></div>
                      <div className="fam-tree-level fam-tree-level-users">
                        {members.map((member) => (
                          <div key={member.assignment_id} className="fam-tree-branch">
                            <div className="fam-tree-node fam-tree-user">
                              <i className="user-avatar">{(member.email || member.user_id).slice(0, 2).toUpperCase()}</i>
                              <span>{member.email || member.user_id}</span>
                              {onRevoke ? <button onClick={() => onRevoke(member.assignment_id)} disabled={busy} title="Revoke">&times;</button> : null}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
