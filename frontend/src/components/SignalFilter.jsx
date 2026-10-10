import React from "react";

/**
 * SignalFilter — Git Walk's reusable, non-dropdown filtering control.
 *
 * One shared timeline ("the rail") that any number of independent panels
 * ("receivers") can tune into — either together, or apart.
 *
 * The core idea, radio-style: there's one rail of beats (time, or whatever
 * axis you feed it). Each receiver gets its own colored tuner pin on that
 * rail. By default every pin is LINKED — click any beat and every receiver
 * jumps to it together, shown as overlapping pins and synced-glow receiver
 * chips (this covers "I want the same filter everywhere," the common case).
 * Click the link toggle to split the pins apart: now each receiver chip
 * can be armed (click it) and independently tuned to its own beat (click a
 * beat) — e.g. Open repositories pinned to Sept while My work stays on
 * Aug, at the same time, on the same rail. The rail is always one shared
 * frame of reference; only whether the pins move together is optional.
 *
 * Generic by design — no assumptions about repositories or working
 * copies — so the exact same component can drive independent-or-synced
 * filtering for any other set of panels in the product.
 *
 * Props:
 *   tagline               — short header line
 *   buckets                — [{ key, label, sublabel, count, intensity(0-1) }]
 *   receivers               — [{ id, label, count, color, selectedBucketKey }]
 *   linked                  — bool: are all receivers forced to the same beat?
 *   onToggleLinked()
 *   activeReceiverId        — which receiver is "armed" to receive the next
 *                              beat click (only meaningful when !linked)
 *   onArmReceiver(id)
 *   onSelectBucket(receiverId, key) — key is "" to clear that receiver
 *   activeDomainLabel       — external, shared domain filter label (if any)
 *   onClear()                — clears every axis for every receiver
 */
export default function SignalFilter({
  tagline = "Scan the timeline",
  buckets = [],
  receivers = [],
  linked = true,
  onToggleLinked,
  activeReceiverId = "",
  onArmReceiver,
  onSelectBucket,
  activeDomainLabel = "",
  onClear,
}) {
  const anyTuned = receivers.some((receiver) => receiver.selectedBucketKey) || Boolean(activeDomainLabel);

  const handleBeatClick = (key) => {
    if (linked) {
      const allOnKey = receivers.every((receiver) => receiver.selectedBucketKey === key);
      receivers.forEach((receiver) => onSelectBucket?.(receiver.id, allOnKey ? "" : key));
      return;
    }
    const armed = activeReceiverId || receivers[0]?.id;
    if (!armed) return;
    const current = receivers.find((receiver) => receiver.id === armed)?.selectedBucketKey;
    onSelectBucket?.(armed, current === key ? "" : key);
  };

  return (
    <section className="panel signal-filter">
      <div className="panel-header">
        <div>
          <p className="eyebrow">SIGNAL FILTER</p>
          <h2>{tagline}</h2>
        </div>
        {anyTuned ? (
          <button type="button" className="signal-clear" onClick={onClear}>Clear</button>
        ) : null}
      </div>

      <div className="signal-rail-wrap">
        <div className="signal-beats" role="group" aria-label="Filter by time">
          {buckets.map((bucket) => {
            const pins = receivers.filter((receiver) => receiver.selectedBucketKey === bucket.key);
            return (
              <button
                type="button" key={bucket.key}
                className={`signal-beat ${pins.length ? "active" : ""}`}
                style={{ "--intensity": bucket.intensity ?? 0 }}
                onClick={() => handleBeatClick(bucket.key)}
                title={`${bucket.count} touched ${bucket.label}${bucket.sublabel ? ` ${bucket.sublabel}` : ""}`}
              >
                <i />
                <span>{bucket.label}</span>
                {bucket.sublabel ? <small>{bucket.sublabel}</small> : null}
                {pins.length ? (
                  <em className="signal-pins">
                    {pins.map((pin) => <b key={pin.id} style={{ "--pin-color": pin.color }} />)}
                  </em>
                ) : null}
              </button>
            );
          })}
          {!buckets.length ? <div className="empty-state compact">Nothing to plot yet.</div> : null}
        </div>
      </div>

      <div className="signal-receivers">
        <button type="button" className={`signal-link-toggle ${linked ? "linked" : ""}`} onClick={onToggleLinked} title={linked ? "Split — tune each panel independently" : "Link — tune every panel together"}>
          <i />{linked ? "Linked" : "Independent"}
        </button>
        {activeDomainLabel ? <span className="signal-domain-chip">{activeDomainLabel}</span> : null}
        <div className="signal-receiver-chips">
          {receivers.map((receiver) => (
            <button
              type="button" key={receiver.id}
              className={`signal-receiver-chip ${receiver.selectedBucketKey ? "live" : ""} ${!linked && activeReceiverId === receiver.id ? "armed" : ""}`}
              style={{ "--pin-color": receiver.color }}
              disabled={linked}
              onClick={() => onArmReceiver?.(receiver.id)}
              title={linked ? "Unlink to tune this panel on its own" : `Tune ${receiver.label} independently`}
            >
              <i />{receiver.label}
              {receiver.selectedBucketKey ? <small>{buckets.find((bucket) => bucket.key === receiver.selectedBucketKey)?.label}</small> : null}
              <b>{receiver.count}</b>
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}
