/* Cross-project hydraulic comparison workspace.
 *
 * The server sends a compact, already-normalized snapshot through the
 * `hype_comparison` custom message. This module owns only presentation: it never
 * opens a project, derives hydraulic metrics, or mutates a source bundle. All
 * interaction returns through one event input:
 *
 *   comparison_event = { type, ...fields, nonce }
 *
 * Required payload fields:
 *   view, members, primary_ids, selected_metric_ids, metric_specs, warnings,
 *   axis_scale
 *
 * Optional presentation fields:
 *   visible, collection_name/title, dirty, selected_member_id, sort_order,
 *   source_note
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var COLORS = ["#2f4b7c", "#0f766e", "#b45309", "#7c3aed", "#be123c",
                "#0369a1", "#4d7c0f", "#9f1239", "#4338ca", "#64748b"];
  var MAX_METRIC_PANELS = 6;
  var state = {
    payload: null,
    selectedMember: null,
    axisScale: null,
    sortOrder: null,
    eventsBound: false
  };

  function post(type, extra) {
    if (!(window.Shiny && window.Shiny.setInputValue)) return;
    var message = { type: type, nonce: Date.now() };
    if (extra) Object.keys(extra).forEach(function (key) { message[key] = extra[key]; });
    window.Shiny.setInputValue("comparison_event", message, { priority: "event" });
  }

  function node(tag, className, textValue) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (textValue !== undefined && textValue !== null) el.textContent = String(textValue);
    return el;
  }

  function svgNode(tag, attrs) {
    var el = document.createElementNS(SVG_NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (key) {
      if (attrs[key] !== undefined && attrs[key] !== null) el.setAttribute(key, attrs[key]);
    });
    return el;
  }

  function append(parent) {
    for (var i = 1; i < arguments.length; i++) {
      if (arguments[i]) parent.appendChild(arguments[i]);
    }
    return parent;
  }

  function button(label, action, className, title) {
    var el = node("button", className || "", label);
    el.type = "button";
    el.setAttribute("data-compare-action", action);
    if (title) el.title = title;
    return el;
  }

  function finite(value) {
    return typeof value === "number" && isFinite(value) ? value : null;
  }

  function firstDefined() {
    for (var i = 0; i < arguments.length; i++) {
      if (arguments[i] !== undefined) return arguments[i];
    }
    return undefined;
  }

  /* The persistence layer deliberately uses descriptive contract field names. Accept that
   * model-native UI payload as well as the compact renderer payload documented above, keeping
   * the conversion at the presentation boundary instead of weakening either contract. */
  function normalizePayload(raw) {
    raw = raw || {};
    var collection = raw.collection || {};
    var viewSettings = collection.view_settings || {};
    var normalized = {};
    Object.keys(raw).forEach(function (key) { normalized[key] = raw[key]; });
    normalized.collection_name = firstDefined(raw.collection_name, raw.title, collection.name);
    normalized.view = firstDefined(raw.view, viewSettings.view, "overview");
    normalized.primary_ids = firstDefined(raw.primary_ids, raw.primary_metric_ids, []);
    // The Metric tab is a LIST of aligned panels; a legacy single metric_id folds in.
    var metricIds = firstDefined(raw.selected_metric_ids, viewSettings.metric_ids);
    if (metricIds === undefined) {
      var single = firstDefined(raw.selected_metric_id, viewSettings.metric_id);
      metricIds = single ? [single] : [];
    }
    normalized.selected_metric_ids = (metricIds || []).map(String);
    normalized.metric_specs = firstDefined(raw.metric_specs, raw.metrics, []);
    normalized.warnings = firstDefined(raw.warnings, raw.findings, []);
    normalized.axis_scale = firstDefined(raw.axis_scale, viewSettings.scale, "auto");
    normalized.sort_order = firstDefined(raw.sort_order, viewSettings.order, "added");
    normalized.members = (raw.members || []).map(function (member) {
      var copy = {};
      Object.keys(member).forEach(function (key) { copy[key] = member[key]; });
      copy.id = firstDefined(member.id, member.member_id);
      copy.path = firstDefined(member.path, member.source_path);
      copy.valid = firstDefined(member.valid, member.readiness !== "invalid");
      if (member.readiness === "invalid") copy.status = "invalid";
      else if (member.readiness === "warning" &&
               (statusKey(copy.status) === "ready" || statusKey(copy.status) === "ok")) {
        copy.status = "warning";
      }
      copy.metrics = {};
      var observations = firstDefined(member.metrics, member.observations, {});
      Object.keys(observations || {}).forEach(function (metricId) {
        var observation = observations[metricId] || {};
        var metric = {};
        Object.keys(observation).forEach(function (key) { metric[key] = observation[key]; });
        metric.base = firstDefined(observation.base, observation.baseline);
        metric.lo = firstDefined(observation.lo, observation.low);
        metric.hi = firstDefined(observation.hi, observation.high);
        metric.complete = firstDefined(observation.complete, observation.completeness === "complete");
        metric.value_count = firstDefined(observation.value_count, observation.finite_case_count, 0);
        metric.completed_case_count = firstDefined(observation.completed_case_count,
                                                   observation.completed_scenario_count, 0);
        metric.configured_case_count = firstDefined(observation.configured_case_count,
                                                    observation.configured_scenario_count, 0);
        copy.metrics[metricId] = metric;
      });
      return copy;
    });
    return normalized;
  }

  function metricFor(member, metricId) {
    var metrics = member && member.metrics;
    return metrics && metricId ? metrics[metricId] || null : null;
  }

  function metricSpec(payload, metricId) {
    var specs = payload.metric_specs || [];
    for (var i = 0; i < specs.length; i++) if (specs[i].id === metricId) return specs[i];
    return { id: metricId || "", label: metricId || "Metric", dimension: "Other", unit: "" };
  }

  function displayUnit(spec, observation) {
    return (observation && observation.unit) || (spec && spec.unit) || "";
  }

  function formatNumber(value) {
    value = finite(value);
    if (value === null) return "n/a";
    var abs = Math.abs(value);
    if (abs !== 0 && (abs >= 1000000 || abs < 0.001)) return value.toExponential(3);
    try {
      return new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 }).format(value);
    } catch (e) {
      return String(Math.round(value * 10000) / 10000);
    }
  }

  function valueWithUnit(value, unit) {
    var text = formatNumber(value);
    return text === "n/a" || !unit ? text : text + " " + unit;
  }

  function shortLabel(value, limit) {
    var text = String(value || "Unnamed site");
    return text.length > limit ? text.slice(0, Math.max(1, limit - 1)) + "\u2026" : text;
  }

  function statusKey(status) {
    return String(status || "ready").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  }

  function statusLabel(status) {
    var text = String(status || "Ready").replace(/[_-]+/g, " ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function memberCanPlot(member) {
    if (!member || member.included === false || member.plot_eligible === false || member.valid === false) {
      return false;
    }
    var status = statusKey(member.status);
    return status !== "invalid" && status !== "error" && status !== "unsupported" && status !== "stale";
  }

  function membersForPlot(payload) {
    return (payload.members || []).filter(memberCanPlot);
  }

  function sensitivity(observation) {
    if (!observation) return null;
    var low = finite(observation.lo);
    var high = finite(observation.hi);
    var count = finite(observation.value_count);
    if (count === null) count = finite(observation.completed_case_count);
    if (low === null || high === null || count === null || count < 2) return null;
    if (low > high) { var swap = low; low = high; high = swap; }
    return { low: low, high: high, count: count, complete: observation.complete !== false };
  }

  function rangeText(observation, unit) {
    var range = sensitivity(observation);
    if (!range) return "No sensitivity range available";
    if (range.low === range.high) return "Unchanged across " + range.count + " cases";
    return "Sensitivity range " + valueWithUnit(range.low, unit) + " to " +
      valueWithUnit(range.high, unit) + (range.complete ? "" : " (partial design)");
  }

  function coverageFor(member) {
    if (member.alternatives_coverage) return String(member.alternatives_coverage);
    if (member.alternatives && typeof member.alternatives === "object") {
      var altCompleted = finite(member.alternatives.completed) || 0;
      var altConfigured = finite(member.alternatives.configured) || 0;
      return altConfigured ? altCompleted + "/" + altConfigured + " alternatives" : "Baseline only";
    }
    var metrics = member.metrics || {};
    var keys = Object.keys(metrics);
    var completed = 0, configured = 0;
    for (var i = 0; i < keys.length; i++) {
      var observation = metrics[keys[i]] || {};
      var c = finite(observation.completed_case_count);
      var n = finite(observation.configured_case_count);
      if (c !== null) completed = Math.max(completed, c);
      if (n !== null) configured = Math.max(configured, n);
    }
    if (!configured) return "Baseline only";
    return completed + "/" + configured + " alternatives";
  }

  function observationTooltip(member, spec, observation) {
    var unit = displayUnit(spec, observation);
    var lines = [member.label || "Unnamed site", (spec.label || spec.id) + ": " +
      valueWithUnit(observation && observation.base, unit), rangeText(observation, unit)];
    if (observation) {
      var completed = finite(observation.completed_case_count);
      var configured = finite(observation.configured_case_count);
      if (configured !== null && configured > 0) {
        lines.push(String(completed === null ? 0 : completed) + " of " + configured +
                   " alternatives completed");
      }
    }
    return lines.join("\n");
  }

  function autoScale(values) {
    var valid = values.filter(function (value) { return finite(value) !== null; });
    if (!valid.length || valid.some(function (value) { return value <= 0; })) return "linear";
    var low = Math.min.apply(null, valid), high = Math.max.apply(null, valid);
    return low > 0 && high / low >= 10 ? "log" : "linear";
  }

  function resolveScale(requested, values) {
    requested = requested || "auto";
    if (requested === "auto") return autoScale(values);
    if (requested === "log" && values.length &&
        values.every(function (value) { return finite(value) !== null && value > 0; })) return "log";
    return "linear";
  }

  function makeScale(rawValues, left, right, requested) {
    var values = rawValues.filter(function (value) { return finite(value) !== null; });
    var mode = resolveScale(requested, values);
    if (!values.length) return null;
    var transformed = values.map(function (value) { return mode === "log" ? Math.log(value) / Math.LN10 : value; });
    var low = Math.min.apply(null, transformed), high = Math.max.apply(null, transformed);
    if (low === high) {
      var pad = mode === "log" ? 0.5 : (Math.abs(low) * 0.12 || 1);
      low -= pad;
      high += pad;
    } else {
      var margin = (high - low) * 0.06;
      low -= margin;
      high += margin;
    }
    return {
      mode: mode,
      min: low,
      max: high,
      map: function (value) {
        var transformedValue = mode === "log" ? Math.log(value) / Math.LN10 : value;
        return left + (transformedValue - low) / (high - low) * (right - left);
      },
      ticks: function (count) {
        var ticks = [];
        for (var i = 0; i < count; i++) {
          var transformedValue = low + (high - low) * i / (count - 1);
          ticks.push({ value: mode === "log" ? Math.pow(10, transformedValue) : transformedValue,
                       position: left + (right - left) * i / (count - 1) });
        }
        return ticks;
      }
    };
  }

  function collectMetricValues(members, metricId) {
    var values = [];
    members.forEach(function (member) {
      var observation = metricFor(member, metricId);
      var base = finite(observation && observation.base);
      var range = sensitivity(observation);
      if (base !== null) values.push(base);
      if (range) values.push(range.low, range.high);
    });
    return values;
  }

  function addSvgTitle(parent, textValue) {
    var title = svgNode("title");
    title.textContent = textValue;
    parent.appendChild(title);
  }

  function axisTicks(svg, scale, top, bottom, labelY) {
    scale.ticks(5).forEach(function (tick) {
      append(svg,
        svgNode("line", { x1: tick.position, y1: top, x2: tick.position, y2: bottom,
                          "class": "hype-compare__grid" }),
        svgNode("line", { x1: tick.position, y1: bottom, x2: tick.position, y2: bottom + 5,
                          "class": "hype-compare__axis" }));
      var label = svgNode("text", { x: tick.position, y: labelY,
                                    "class": "hype-compare__tick", "text-anchor": "middle" });
      label.textContent = formatNumber(tick.value);
      svg.appendChild(label);
    });
  }

  function horizontalPlot(payload, metricId, members, compact, removable) {
    var spec = metricSpec(payload, metricId);
    var card = node("section", "hype-compare__plot-card");
    var head = node("div", "hype-compare__plot-head");
    append(head, node("h3", "hype-compare__plot-title", spec.label || metricId));
    var sampleObservation = members.length ? metricFor(members[0], metricId) : null;
    append(head, node("span", "hype-compare__unit", displayUnit(spec, sampleObservation) || "Unitless"));
    if (removable) {
      var remove = button("×", "metric_remove", "hype-compare__panel-remove",
                          "Remove this panel");
      remove.setAttribute("data-metric-id", metricId);
      remove.setAttribute("aria-label", "Remove " + (spec.label || metricId) + " panel");
      head.appendChild(remove);
    }
    card.appendChild(head);

    var values = collectMetricValues(members, metricId);
    var requestedScale = state.axisScale || payload.axis_scale || "auto";
    var width = compact ? 480 : 900;
    var left = compact ? 132 : 182;
    var right = width - 24;
    var top = 18, rowHeight = compact ? 34 : 38, bottomPad = 45;
    var height = top + Math.max(1, members.length) * rowHeight + bottomPad;
    var scale = makeScale(values, left, right, requestedScale);
    if (!scale) {
      card.appendChild(emptyState("No values are available for this metric.", true));
      return card;
    }

    var meta = node("div", "hype-compare__plot-meta", (scale.mode === "log" ? "Log" : "Linear") + " scale");
    head.appendChild(meta);
    var svg = svgNode("svg", { viewBox: "0 0 " + width + " " + height,
                               role: "img", "aria-label": spec.label + " by site" });
    axisTicks(svg, scale, top - 4, height - bottomPad, height - 13);

    members.forEach(function (member, index) {
      var y = top + index * rowHeight + rowHeight / 2;
      var observation = metricFor(member, metricId);
      var base = finite(observation && observation.base);
      var color = COLORS[index % COLORS.length];
      var selected = state.selectedMember === member.id;
      var label = svgNode("text", { x: left - 12, y: y + 4,
                                    "class": "hype-compare__site-label",
                                    "text-anchor": "end" });
      label.textContent = shortLabel(member.label, compact ? 19 : 28);
      addSvgTitle(label, member.label || "Unnamed site");
      svg.appendChild(label);
      append(svg, svgNode("line", { x1: left, y1: y + rowHeight / 2,
                                    x2: right, y2: y + rowHeight / 2,
                                    "class": "hype-compare__row-rule" }));
      if (base === null || (scale.mode === "log" && base <= 0)) {
        var missing = svgNode("text", { x: left + 7, y: y + 4,
                                       "class": "hype-compare__missing" });
        missing.textContent = "No value";
        svg.appendChild(missing);
        return;
      }
      var mark = svgNode("g", { "class": "hype-compare__mark" + (selected ? " is-selected" : ""),
                                "data-member-id": member.id,
                                tabindex: "0", role: "button" });
      mark.setAttribute("data-hype-tooltip", observationTooltip(member, spec, observation));
      addSvgTitle(mark, observationTooltip(member, spec, observation));
      var range = sensitivity(observation);
      if (range && (scale.mode !== "log" || (range.low > 0 && range.high > 0))) {
        var xLow = scale.map(range.low), xHigh = scale.map(range.high);
        append(mark,
          svgNode("line", { x1: xLow, y1: y, x2: xHigh, y2: y,
                            stroke: color, "class": "hype-compare__range" +
                              (range.complete ? "" : " is-partial") }),
          svgNode("line", { x1: xLow, y1: y - 5, x2: xLow, y2: y + 5,
                            stroke: color, "class": "hype-compare__cap" +
                              (range.complete ? "" : " is-partial") }),
          svgNode("line", { x1: xHigh, y1: y - 5, x2: xHigh, y2: y + 5,
                            stroke: color, "class": "hype-compare__cap" +
                              (range.complete ? "" : " is-partial") }));
      }
      if (selected) append(mark, svgNode("circle", { cx: scale.map(base), cy: y, r: 8,
                                                      "class": "hype-compare__dot-halo" }));
      append(mark, svgNode("circle", { cx: scale.map(base), cy: y, r: compact ? 4.5 : 5,
                                       fill: color, "class": "hype-compare__dot" }));
      svg.appendChild(mark);
    });

    var axisLabel = svgNode("text", { x: (left + right) / 2, y: height - 1,
                                      "class": "hype-compare__axis-label", "text-anchor": "middle" });
    axisLabel.textContent = displayUnit(spec, sampleObservation) || "Value";
    svg.appendChild(axisLabel);
    card.appendChild(svg);
    return card;
  }

  function legend() {
    var el = node("div", "hype-compare__legend");
    var point = node("span", "hype-compare__legend-item");
    append(point, node("i", "hype-compare__legend-dot"), document.createTextNode("Baseline"));
    var full = node("span", "hype-compare__legend-item");
    append(full, node("i", "hype-compare__legend-line"), document.createTextNode("Range across hydraulic alternatives"));
    var partial = node("span", "hype-compare__legend-item");
    append(partial, node("i", "hype-compare__legend-line is-partial"), document.createTextNode("Partial alternative design"));
    append(el, point, full, partial);
    return el;
  }

  function valueCell(member, spec) {
    var observation = metricFor(member, spec.id);
    var td = node("td", "hype-compare__number");
    var unit = displayUnit(spec, observation);
    var base = finite(observation && observation.base);
    append(td, node("strong", "", valueWithUnit(base, unit)));
    var range = sensitivity(observation);
    if (range) {
      td.appendChild(node("small", range.complete ? "" : "is-partial",
        range.low === range.high ? "Unchanged across " + range.count + " cases" :
        valueWithUnit(range.low, unit) + " to " + valueWithUnit(range.high, unit)));
    } else {
      td.appendChild(node("small", "", "Baseline only"));
    }
    return td;
  }

  function summaryTable(payload, members, metricIds) {
    var wrap = node("div", "hype-compare__table-wrap");
    var table = node("table", "hype-compare__summary");
    var thead = node("thead"), headRow = node("tr");
    headRow.appendChild(node("th", "", "Site"));
    metricIds.forEach(function (id) { headRow.appendChild(node("th", "", metricSpec(payload, id).label)); });
    thead.appendChild(headRow);
    var tbody = node("tbody");
    members.forEach(function (member) {
      var row = node("tr");
      row.setAttribute("data-member-id", member.id);
      var site = node("td", "hype-compare__table-site", member.label || "Unnamed site");
      site.title = member.label || "Unnamed site";
      row.appendChild(site);
      metricIds.forEach(function (id) { row.appendChild(valueCell(member, metricSpec(payload, id))); });
      tbody.appendChild(row);
    });
    append(table, thead, tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function emptyState(message, compact) {
    var el = node("div", "hype-compare__empty" + (compact ? " is-compact" : ""));
    append(el, node("span", "hype-compare__empty-icon", "\u223F"),
      node("h2", "", compact ? "Metric unavailable" : "No projects to compare"),
      node("p", "", message));
    if (!compact) el.appendChild(button("Add projects", "add_projects", "hype-compare__button is-primary"));
    return el;
  }

  function warningBlock(warnings) {
    if (!warnings || !warnings.length) return null;
    var wrap = node("section", "hype-compare__warnings");
    wrap.setAttribute("aria-label", "Comparison warnings");
    append(wrap, node("span", "hype-compare__warning-icon", "!"));
    var copy = node("div");
    append(copy, node("strong", "", warnings.length === 1 ? "Review this comparison" :
      "Review these comparison notes"));
    var list = node("ul");
    warnings.forEach(function (warning) {
      var text = typeof warning === "string" ? warning : warning.message || warning.label || "Compatibility warning";
      list.appendChild(node("li", "", text));
    });
    append(copy, list);
    append(wrap, copy);
    return wrap;
  }

  function renderOverview(payload, members) {
    var section = node("div", "hype-compare__view");
    var title = node("div", "hype-compare__view-head");
    append(title, node("div", "", ""));
    title.firstChild.appendChild(node("h2", "", "Hydraulic profile"));
    title.firstChild.appendChild(node("p", "", "Baseline results with sensitivity ranges from completed hydraulic alternatives."));
    title.appendChild(legend());
    section.appendChild(title);
    var ids = (payload.primary_ids || []).slice(0, 3);
    if (!members.length || !ids.length) {
      section.appendChild(emptyState("Add two or more projects with completed hydraulic results to begin."));
      return section;
    }
    var plots = node("div", "hype-compare__overview-plots");
    ids.forEach(function (id) { plots.appendChild(horizontalPlot(payload, id, members, true)); });
    section.appendChild(plots);
    section.appendChild(node("h3", "hype-compare__section-title", "Baseline summary"));
    section.appendChild(summaryTable(payload, members, ids));
    return section;
  }

  function metricPicker(payload, selectedIds) {
    var field = node("div", "hype-compare__field hype-compare__metric-add");
    field.appendChild(node("span", "", "Metric"));
    var row = node("div", "hype-compare__metric-add-row");
    var select = node("select", "hype-compare__select");
    select.setAttribute("data-compare-control", "metric-picker");
    var groups = {};
    (payload.metric_specs || []).forEach(function (spec) {
      var dimension = spec.dimension || "Other";
      if (!groups[dimension]) {
        groups[dimension] = document.createElement("optgroup");
        groups[dimension].label = dimension;
        select.appendChild(groups[dimension]);
      }
      var option = document.createElement("option");
      option.value = spec.id;
      option.textContent = spec.label + (spec.unit ? " (" + spec.unit + ")" : "");
      option.disabled = selectedIds.indexOf(spec.id) !== -1;
      groups[dimension].appendChild(option);
    });
    for (var i = 0; i < select.options.length; i++) {
      if (!select.options[i].disabled) { select.selectedIndex = i; break; }
    }
    row.appendChild(select);
    var add = button("Add panel", "metric_add", "hype-compare__button is-add");
    if (selectedIds.length >= MAX_METRIC_PANELS) {
      add.disabled = true;
      add.title = "Up to " + MAX_METRIC_PANELS + " metric panels can be shown at once.";
    }
    row.appendChild(add);
    field.appendChild(row);
    return field;
  }

  function segmented(labelText, name, options, active) {
    var field = node("div", "hype-compare__field");
    field.appendChild(node("span", "", labelText));
    var group = node("div", "hype-compare__segmented");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", labelText);
    options.forEach(function (option) {
      var b = button(option.label, name, option.value === active ? "is-active" : "");
      b.setAttribute("data-value", option.value);
      b.setAttribute("aria-pressed", option.value === active ? "true" : "false");
      group.appendChild(b);
    });
    field.appendChild(group);
    return field;
  }

  function renderMetric(payload, members) {
    var section = node("div", "hype-compare__view");
    var ids = payload.selected_metric_ids || [];
    var controls = node("div", "hype-compare__metric-controls");
    controls.appendChild(metricPicker(payload, ids));
    controls.appendChild(segmented("Order", "sort_order", [
      { value: "added", label: "Added" }, { value: "ascending", label: "Low to high" },
      { value: "descending", label: "High to low" }
    ], state.sortOrder || payload.sort_order || "added"));
    controls.appendChild(segmented("Scale", "axis_scale", [
      { value: "auto", label: "Auto" }, { value: "linear", label: "Linear" },
      { value: "log", label: "Log" }
    ], state.axisScale || payload.axis_scale || "auto"));
    section.appendChild(controls);
    if (!members.length) {
      section.appendChild(emptyState("No included project has a metric available for plotting."));
      return section;
    }
    if (!ids.length) {
      section.appendChild(emptyState("Add a metric above to compare it across sites.", true));
      return section;
    }
    // ONE site order shared by every panel (sorted by the first panel's baselines):
    // aligned rows are the point of stacking panels.
    var sorted = members.slice();
    var sortOrder = state.sortOrder || payload.sort_order;
    var keyId = ids[0];
    if (sortOrder === "value" || sortOrder === "ascending" || sortOrder === "descending") {
      sorted.sort(function (a, b) {
        var av = finite(metricFor(a, keyId) && metricFor(a, keyId).base);
        var bv = finite(metricFor(b, keyId) && metricFor(b, keyId).base);
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        return (sortOrder === "descending" ? -1 : 1) * (av - bv);
      });
    }
    section.appendChild(legend());
    var stack = node("div", "hype-compare__metric-stack");
    ids.forEach(function (id) {
      stack.appendChild(horizontalPlot(payload, id, sorted, false, true));
    });
    section.appendChild(stack);
    section.appendChild(summaryTable(payload, sorted, ids));
    return section;
  }

  function relationshipPlot(payload, members) {
    var ids = (payload.primary_ids || []).slice(0, 3);
    if (ids.length < 2) return emptyState("The primary connectivity and residence-time metrics are unavailable.", true);
    var xSpec = metricSpec(payload, ids[0]), ySpec = metricSpec(payload, ids[1]);
    var sizeSpec = ids[2] ? metricSpec(payload, ids[2]) : null;
    var points = [];
    members.forEach(function (member, index) {
      var xo = metricFor(member, ids[0]), yo = metricFor(member, ids[1]);
      var so = ids[2] ? metricFor(member, ids[2]) : null;
      var x = finite(xo && xo.base), y = finite(yo && yo.base), size = finite(so && so.base);
      if (x !== null && y !== null) points.push({ member: member, index: index, x: x, y: y, size: size,
                                                   xo: xo, yo: yo, so: so });
    });
    if (!points.length) return emptyState("No included project has both relationship metrics available.", true);
    var width = 900, height = 485, left = 92, right = 868, top = 28, bottom = 405;
    var requested = state.axisScale || payload.axis_scale || "auto";
    var xScale = makeScale(points.map(function (p) { return p.x; }), left, right, requested);
    var yScale = makeScale(points.map(function (p) { return p.y; }), bottom, top, requested);
    var sizes = points.map(function (p) { return p.size; }).filter(function (v) { return v !== null && v >= 0; });
    var sizeMin = sizes.length ? Math.min.apply(null, sizes) : 0;
    var sizeMax = sizes.length ? Math.max.apply(null, sizes) : 0;
    function radius(value) {
      if (value === null || value < 0 || sizeMax === sizeMin) return 10;
      return 6 + Math.sqrt((value - sizeMin) / (sizeMax - sizeMin)) * 12;
    }

    var card = node("section", "hype-compare__plot-card hype-compare__relationship-card");
    var head = node("div", "hype-compare__plot-head");
    append(head, node("h3", "hype-compare__plot-title", "Baseline relationships"),
      node("span", "hype-compare__plot-meta", "X: " + (xScale.mode === "log" ? "log" : "linear") +
        "  \u00b7  Y: " + (yScale.mode === "log" ? "log" : "linear")));
    card.appendChild(head);
    var svg = svgNode("svg", { viewBox: "0 0 " + width + " " + height,
                               role: "img", "aria-label": xSpec.label + " versus " + ySpec.label });
    xScale.ticks(6).forEach(function (tick) {
      append(svg, svgNode("line", { x1: tick.position, y1: top, x2: tick.position, y2: bottom,
                                    "class": "hype-compare__grid" }));
      var text = svgNode("text", { x: tick.position, y: bottom + 22,
                                   "class": "hype-compare__tick", "text-anchor": "middle" });
      text.textContent = formatNumber(tick.value); svg.appendChild(text);
    });
    yScale.ticks(6).forEach(function (tick) {
      append(svg, svgNode("line", { x1: left, y1: tick.position, x2: right, y2: tick.position,
                                    "class": "hype-compare__grid" }));
      var text = svgNode("text", { x: left - 12, y: tick.position + 4,
                                   "class": "hype-compare__tick", "text-anchor": "end" });
      text.textContent = formatNumber(tick.value); svg.appendChild(text);
    });
    append(svg,
      svgNode("line", { x1: left, y1: bottom, x2: right, y2: bottom, "class": "hype-compare__axis" }),
      svgNode("line", { x1: left, y1: top, x2: left, y2: bottom, "class": "hype-compare__axis" }));
    points.forEach(function (point) {
      var x = xScale.map(point.x), y = yScale.map(point.y), r = radius(point.size);
      var unitX = displayUnit(xSpec, point.xo), unitY = displayUnit(ySpec, point.yo);
      var tooltip = [point.member.label || "Unnamed site",
        xSpec.label + ": " + valueWithUnit(point.x, unitX),
        ySpec.label + ": " + valueWithUnit(point.y, unitY)];
      if (sizeSpec && point.size !== null) tooltip.push(sizeSpec.label + ": " +
        valueWithUnit(point.size, displayUnit(sizeSpec, point.so)));
      var group = svgNode("g", { "class": "hype-compare__bubble" +
        (state.selectedMember === point.member.id ? " is-selected" : ""),
        "data-member-id": point.member.id, tabindex: "0", role: "button" });
      group.setAttribute("data-hype-tooltip", tooltip.join("\n"));
      addSvgTitle(group, tooltip.join("\n"));
      var labelOnLeft = x > left + (right - left) * 0.72;
      append(group,
        svgNode("circle", { cx: x, cy: y, r: r, fill: COLORS[point.index % COLORS.length] }),
        svgNode("text", { x: labelOnLeft ? x - r - 5 : x + r + 5, y: y + 4,
                          "text-anchor": labelOnLeft ? "end" : "start",
                          "class": "hype-compare__bubble-label" }));
      group.lastChild.textContent = shortLabel(point.member.label, 22);
      svg.appendChild(group);
    });
    var xLabel = svgNode("text", { x: (left + right) / 2, y: height - 14,
                                   "class": "hype-compare__axis-label", "text-anchor": "middle" });
    xLabel.textContent = xSpec.label + (xSpec.unit ? " (" + xSpec.unit + ")" : "");
    var yLabel = svgNode("text", { x: 20, y: (top + bottom) / 2,
                                   "class": "hype-compare__axis-label", "text-anchor": "middle",
                                   transform: "rotate(-90 20 " + ((top + bottom) / 2) + ")" });
    yLabel.textContent = ySpec.label + (ySpec.unit ? " (" + ySpec.unit + ")" : "");
    append(svg, xLabel, yLabel);
    card.appendChild(svg);
    var note = "Each point is one site's baseline.";
    if (sizeSpec) note += " Bubble area represents " + sizeSpec.label.toLowerCase() + ".";
    card.appendChild(node("p", "hype-compare__chart-note", note +
      " Sensitivity ranges are shown in Overview and Metric views."));
    return card;
  }

  function renderRelationships(payload, members) {
    var section = node("div", "hype-compare__view");
    var title = node("div", "hype-compare__view-head");
    var copy = node("div");
    append(copy, node("h2", "", "Relationships"),
      node("p", "", "Compare site baselines across frequency, duration, and extent."));
    append(title, copy, segmented("Scale", "axis_scale", [
      { value: "auto", label: "Auto" }, { value: "linear", label: "Linear" },
      { value: "log", label: "Log" }
    ], state.axisScale || payload.axis_scale || "auto"));
    append(section, title, relationshipPlot(payload, members));
    return section;
  }

  function railMember(member) {
    var row = node("div", "hype-compare__member status-" + statusKey(member.status) +
      (state.selectedMember === member.id ? " is-selected" : ""));
    row.setAttribute("data-member-id", member.id);
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    if (member.path || member.source_path) row.title = member.path || member.source_path;
    var checkWrap = node("label", "hype-compare__include");
    checkWrap.title = "Include this site in plots";
    var check = document.createElement("input");
    check.type = "checkbox";
    check.checked = member.included !== false;
    check.setAttribute("data-member-include", member.id);
    check.setAttribute("aria-label", "Include " + (member.label || "site") + " in plots");
    checkWrap.appendChild(check);
    var copy = node("div", "hype-compare__member-copy");
    var nameLine = node("div", "hype-compare__member-name");
    append(nameLine, node("span", "hype-compare__status-dot"),
      node("strong", "", member.label || "Unnamed site"));
    var status = node("span", "hype-compare__member-status", statusLabel(member.status));
    var meta = node("div", "hype-compare__member-meta");
    append(meta, status, document.createTextNode(" \u00b7 " + coverageFor(member)));
    var captured = member.captured_at || member.run_date;
    if (captured) meta.appendChild(document.createTextNode(" \u00b7 " + String(captured)));
    append(copy, nameLine, meta);
    var remove = button("\u00d7", "remove_member", "hype-compare__member-remove", "Remove from comparison");
    remove.setAttribute("data-member-id", member.id);
    remove.setAttribute("aria-label", "Remove " + (member.label || "site"));
    append(row, checkWrap, copy, remove);
    return row;
  }

  function renderRail(payload) {
    var members = payload.members || [];
    var rail = node("aside", "hype-compare__rail");
    var head = node("div", "hype-compare__rail-head");
    append(head, node("div", "hype-compare__eyebrow", "Included sites"),
      node("span", "hype-compare__count", String(members.length)));
    rail.appendChild(head);
    var list = node("div", "hype-compare__member-list");
    if (members.length) members.forEach(function (member) { list.appendChild(railMember(member)); });
    else list.appendChild(node("p", "hype-compare__rail-empty", "No projects added yet."));
    rail.appendChild(list);
    var selected = null;
    members.forEach(function (member) {
      if (String(member.id) === String(state.selectedMember)) selected = member;
    });
    if (selected) rail.appendChild(memberEditor(selected));
    var foot = node("div", "hype-compare__rail-foot");
    foot.appendChild(button("+ Add projects", "add_projects", "hype-compare__rail-add"));
    rail.appendChild(foot);
    return rail;
  }

  function memberEditor(member) {
    var editor = node("div", "hype-compare__member-editor");
    editor.setAttribute("data-editor-member-id", member.id);
    var label = node("label", "", "Display alias");
    var input = node("input", "hype-compare__alias-input");
    input.type = "text";
    input.value = member.alias || "";
    input.placeholder = member.label || "Site name";
    input.maxLength = 120;
    input.setAttribute("data-member-alias", member.id);
    input.setAttribute("aria-label", "Display alias for " + (member.label || "site"));
    label.appendChild(input);
    var actions = node("div", "hype-compare__editor-actions");
    var apply = button("Apply", "member_alias", "hype-compare__editor-button");
    apply.setAttribute("data-member-id", member.id);
    actions.appendChild(apply);
    var status = statusKey(member.status);
    if (status === "missing" || status === "moved") {
      var relink = button("Relink\u2026", "relink_member", "hype-compare__editor-button is-relink");
      relink.setAttribute("data-member-id", member.id);
      actions.appendChild(relink);
    }
    append(editor, node("div", "hype-compare__editor-title", "Selected site"), label, actions);
    return editor;
  }

  function actionButton(label, action, primary) {
    return button(label, action, "hype-compare__button" + (primary ? " is-primary" : ""));
  }

  function renderTop(payload) {
    var top = node("header", "hype-compare__topbar");
    var identity = node("div", "hype-compare__identity");
    var back = button("\u2190", "back", "hype-compare__back", "Back to project");
    back.setAttribute("aria-label", "Back to project");
    var title = node("div");
    append(title, node("div", "hype-compare__eyebrow", "Cross-project hydraulic comparison"));
    var titleLine = node("div", "hype-compare__title-line");
    append(titleLine, node("h1", "", payload.collection_name || payload.title || "Untitled comparison"));
    if (payload.dirty) titleLine.appendChild(node("span", "hype-compare__dirty", "Unsaved"));
    title.appendChild(titleLine);
    append(identity, back, title);
    var actions = node("div", "hype-compare__actions");
    append(actions, actionButton("Add projects", "add_projects"), actionButton("Refresh", "refresh"),
      actionButton("Save", "save", true), actionButton("Save as\u2026", "save_as"),
      actionButton("Export\u2026", "export"));
    append(top, identity, actions);
    return top;
  }

  function renderTabs(activeView) {
    var tabs = node("nav", "hype-compare__tabs");
    tabs.setAttribute("aria-label", "Comparison views");
    [{ id: "overview", label: "Overview" }, { id: "metric", label: "Metric" },
     { id: "relationships", label: "Relationships" }].forEach(function (item) {
      var tab = button(item.label, "view", item.id === activeView ? "is-active" : "");
      tab.setAttribute("data-view", item.id);
      tab.setAttribute("aria-current", item.id === activeView ? "page" : "false");
      tabs.appendChild(tab);
    });
    return tabs;
  }

  function hostElement() {
    var host = document.getElementById("hype-comparison");
    if (!host) {
      host = node("div", "hype-compare");
      host.id = "hype-comparison";
      var shell = document.querySelector(".hype-shell") || document.body;
      shell.appendChild(host);
    }
    if (!host.classList.contains("hype-compare")) host.classList.add("hype-compare");
    return host;
  }

  function render(payload) {
    payload = normalizePayload(payload);
    state.payload = payload;
    if (payload.selected_member_id !== undefined) state.selectedMember = payload.selected_member_id;
    if (payload.axis_scale) state.axisScale = payload.axis_scale;
    if (payload.sort_order) state.sortOrder = payload.sort_order;
    var host = hostElement();
    if (payload.visible === false) {
      host.classList.remove("is-visible");
      host.setAttribute("aria-hidden", "true");
      host.setAttribute("hidden", "");
      return;
    }
    host.removeAttribute("hidden");
    host.classList.add("is-visible");
    host.setAttribute("aria-hidden", "false");
    host.textContent = "";
    var shell = node("div", "hype-compare__shell");
    var view = payload.view || "overview";
    append(shell, renderTop(payload), renderTabs(view));
    var body = node("div", "hype-compare__body");
    var main = node("main", "hype-compare__main");
    var warning = warningBlock(payload.warnings || []);
    if (warning) main.appendChild(warning);
    var members = membersForPlot(payload);
    if (view === "metric") main.appendChild(renderMetric(payload, members));
    else if (view === "relationships") main.appendChild(renderRelationships(payload, members));
    else main.appendChild(renderOverview(payload, members));
    append(body, renderRail(payload), main);
    shell.appendChild(body);
    host.appendChild(shell);
    host.appendChild(node("div", "hype-compare__tooltip"));
    bindEvents(host);
  }

  function updateLocal(action, value) {
    if (!state.payload) return;
    if (action === "view") state.payload.view = value;
    else if (action === "axis_scale") { state.axisScale = value; state.payload.axis_scale = value; }
    else if (action === "sort_order") { state.sortOrder = value; state.payload.sort_order = value; }
    else if (action === "metric_add") {
      var ids = state.payload.selected_metric_ids || [];
      if (ids.indexOf(value) === -1 && ids.length < MAX_METRIC_PANELS) ids.push(value);
      state.payload.selected_metric_ids = ids;
    } else if (action === "metric_remove") {
      state.payload.selected_metric_ids = (state.payload.selected_metric_ids || [])
        .filter(function (id) { return id !== value; });
    }
    render(state.payload);
  }

  function actionClick(event) {
    var target = event.target.closest ? event.target.closest("[data-compare-action]") : null;
    if (!target) return;
    var action = target.getAttribute("data-compare-action");
    if (action === "view") {
      var view = target.getAttribute("data-view");
      updateLocal("view", view); post("view", { view: view });
    } else if (action === "axis_scale" || action === "sort_order") {
      var value = target.getAttribute("data-value");
      updateLocal(action, value);
      var extra = {}; extra[action] = value; post(action, extra);
    } else if (action === "metric_add") {
      var picker = target.closest ? target.closest(".hype-compare__metric-add") : null;
      var select = picker && picker.querySelector("select");
      var metricId = select && select.value;
      if (!metricId) return;
      updateLocal("metric_add", metricId);
      post("metric_add", { metric_id: metricId });
    } else if (action === "metric_remove") {
      event.stopPropagation();
      var removeId = target.getAttribute("data-metric-id");
      updateLocal("metric_remove", removeId);
      post("metric_remove", { metric_id: removeId });
    } else if (action === "remove_member") {
      event.stopPropagation();
      post("remove_member", { id: target.getAttribute("data-member-id") });
    } else if (action === "member_alias") {
      event.stopPropagation();
      var memberId = target.getAttribute("data-member-id");
      var editor = target.closest("[data-editor-member-id]");
      var alias = editor && editor.querySelector("[data-member-alias]");
      post("member_alias", { id: memberId, alias: alias ? alias.value.trim() : "" });
    } else if (action === "relink_member") {
      event.stopPropagation();
      post("relink_member", { id: target.getAttribute("data-member-id") });
    } else {
      post(action);
    }
  }

  function changeControl(event) {
    var include = event.target.getAttribute && event.target.getAttribute("data-member-include");
    if (include) {
      if (state.payload) {
        (state.payload.members || []).forEach(function (member) {
          if (String(member.id) === String(include)) member.included = !!event.target.checked;
        });
        render(state.payload);
      }
      post("member_include", { id: include, included: !!event.target.checked });
      return;
    }
    // The metric picker select is passive: the Add panel button beside it commits.
  }

  function selectMember(event) {
    if (event.target.closest && event.target.closest("input,button,select,label")) return;
    var row = event.target.closest ? event.target.closest("[data-member-id]") : null;
    if (!row) return;
    var id = row.getAttribute("data-member-id");
    if (!id) return;
    state.selectedMember = id;
    if (state.payload) render(state.payload);
    post("member_select", { id: id });
  }

  function keyboardMember(event) {
    if (event.key === "Enter" && event.target.getAttribute && event.target.getAttribute("data-member-alias")) {
      event.preventDefault();
      post("member_alias", { id: event.target.getAttribute("data-member-alias"),
                             alias: event.target.value.trim() });
      return;
    }
    if (event.key !== "Enter" && event.key !== " ") return;
    var row = event.target.closest ? event.target.closest(".hype-compare__member") : null;
    if (!row || event.target !== row) return;
    event.preventDefault();
    state.selectedMember = row.getAttribute("data-member-id");
    if (state.payload) render(state.payload);
    post("member_select", { id: state.selectedMember });
  }

  function tooltipTarget(event) {
    return event.target.closest ? event.target.closest("[data-hype-tooltip]") : null;
  }

  function positionTooltip(host, event) {
    var tooltip = host.querySelector(".hype-compare__tooltip");
    if (!tooltip || !tooltip.classList.contains("is-visible")) return;
    var x = event.clientX !== undefined ? event.clientX + 14 : window.innerWidth / 2;
    var y = event.clientY !== undefined ? event.clientY + 14 : window.innerHeight / 2;
    var box = tooltip.getBoundingClientRect();
    tooltip.style.left = Math.max(8, Math.min(x, window.innerWidth - box.width - 8)) + "px";
    tooltip.style.top = Math.max(8, Math.min(y, window.innerHeight - box.height - 8)) + "px";
  }

  function showTooltip(host, event) {
    var target = tooltipTarget(event);
    if (!target) return;
    var tooltip = host.querySelector(".hype-compare__tooltip");
    if (!tooltip) return;
    tooltip.textContent = target.getAttribute("data-hype-tooltip");
    tooltip.classList.add("is-visible");
    positionTooltip(host, event);
  }

  function hideTooltip(host, event) {
    var target = tooltipTarget(event);
    if (!target) return;
    if (event.relatedTarget && target.contains(event.relatedTarget)) return;
    var tooltip = host.querySelector(".hype-compare__tooltip");
    if (tooltip) tooltip.classList.remove("is-visible");
  }

  function bindEvents(host) {
    if (host.getAttribute("data-events-bound") === "1") return;
    host.setAttribute("data-events-bound", "1");
    host.addEventListener("click", actionClick);
    host.addEventListener("click", selectMember);
    host.addEventListener("change", changeControl);
    host.addEventListener("keydown", keyboardMember);
    host.addEventListener("pointerover", function (event) { showTooltip(host, event); });
    host.addEventListener("pointermove", function (event) { positionTooltip(host, event); });
    host.addEventListener("pointerout", function (event) { hideTooltip(host, event); });
    host.addEventListener("focusin", function (event) { showTooltip(host, event); });
    host.addEventListener("focusout", function (event) { hideTooltip(host, event); });
  }

  function register() {
    if (!(window.Shiny && window.Shiny.addCustomMessageHandler)) return false;
    try { window.Shiny.addCustomMessageHandler("hype_comparison", render); }
    catch (e) { return true; }
    return true;
  }

  if (!register()) document.addEventListener("shiny:connected", register);
  window.__hypeComparison = { render: render };
})();
